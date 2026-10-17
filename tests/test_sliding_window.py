"""Step 4: the sliding 7-day window is the default model; planning UI is hidden without it; per-band booking cap."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.bot import keyboards
from app.core.config import Settings, settings
from app.core.timezone import now_local
from app.db import models
from app.services import admin, bands, bookings
from app.services.errors import ConflictError, ForbiddenError


def _slot(hour: int = 14, days: int = 1):
    start = (now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return start.date(), start.time(), (start + timedelta(hours=1)).time()


def _callbacks(markup) -> set[str]:
    return {button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data}


async def _set_max(session, value: int) -> None:
    session.add(models.ClubSetting(key="max_future_bookings_per_band", value=str(value)))
    await session.commit()


# ---------- default model ----------


def test_weekly_planning_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WEEKLY_PLANNING_ENABLED", raising=False)
    assert Settings(_env_file=None).weekly_planning_enabled is False


# ---------- bot keyboards ----------


def test_planning_buttons_hidden_when_planning_is_off(monkeypatch):
    monkeypatch.setattr(settings, "weekly_planning_enabled", False)
    assert "plan:start" not in _callbacks(keyboards.rehearsals_menu_keyboard())
    assert "plan:admin" not in _callbacks(keyboards.admin_schedule_keyboard())


def test_planning_buttons_shown_when_planning_is_on(monkeypatch):
    monkeypatch.setattr(settings, "weekly_planning_enabled", True)
    assert "plan:start" in _callbacks(keyboards.rehearsals_menu_keyboard())
    assert "plan:admin" in _callbacks(keyboards.admin_schedule_keyboard())


def test_admin_settings_has_max_bookings_button():
    assert "admin:max_future_bookings" in _callbacks(keyboards.admin_settings_keyboard())


# ---------- max future bookings per band ----------


async def test_max_future_bookings_blocks_the_next_booking(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    await _set_max(session, 2)
    for hour in (10, 12):
        day, start, end = _slot(hour)
        await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", None)

    day, start, end = _slot(16)
    with pytest.raises(ConflictError, match="future bookings"):
        await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", None)


async def test_cancelling_a_booking_frees_the_cap(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    await _set_max(session, 1)
    day, start, end = _slot(10)
    first = await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", None)
    day2, start2, end2 = _slot(12)
    with pytest.raises(ConflictError):
        await bookings.create_booking(session, owner.telegram_id, band.id, day2, start2, end2, "self", None)

    await bookings.delete_booking(session, owner.telegram_id, first.id)

    await bookings.create_booking(session, owner.telegram_id, band.id, day2, start2, end2, "self", None)


async def test_zero_means_no_cap(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    await _set_max(session, 0)
    for hour in (10, 12, 14):
        day, start, end = _slot(hour)
        await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", None)


async def test_cap_is_per_band_and_ignores_personal_bookings(session, make_user, make_band):
    owner = await make_user()
    other = await make_user()
    band = await make_band(owner, "A")
    other_band = await make_band(other, "B")
    await _set_max(session, 1)
    day, start, end = _slot(10)
    await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", None)

    day2, start2, end2 = _slot(12)
    await bookings.create_booking(session, other.telegram_id, other_band.id, day2, start2, end2, "self", None)
    day3, start3, end3 = _slot(14)
    await bookings.create_booking(session, owner.telegram_id, None, day3, start3, end3, "personal", None)


async def test_get_max_future_bookings_defaults_to_zero_without_committing(session):
    assert await bookings.get_max_future_bookings_per_band(session) == 0
    assert (await session.execute(select(models.ClubSetting).where(models.ClubSetting.key == "max_future_bookings_per_band"))).first() is None


# ---------- admin setting ----------


async def test_admin_can_read_and_update_the_cap(session, make_user):
    boss = await make_user(role="admin")
    assert (await admin.get_max_future_bookings(session, boss.telegram_id))["max_future_bookings_per_band"] == 0
    updated = await admin.update_max_future_bookings(session, boss.telegram_id, 3)
    assert updated["max_future_bookings_per_band"] == 3
    assert await bookings.get_max_future_bookings_per_band(session) == 3
    await admin.update_max_future_bookings(session, boss.telegram_id, 0)
    assert await bookings.get_max_future_bookings_per_band(session) == 0


async def test_regular_user_cannot_change_the_cap(session, make_user):
    user = await make_user()
    with pytest.raises(ForbiddenError):
        await admin.update_max_future_bookings(session, user.telegram_id, 2)


async def test_negative_cap_is_rejected(session, make_user):
    boss = await make_user(role="admin")
    with pytest.raises(ConflictError):
        await admin.update_max_future_bookings(session, boss.telegram_id, -1)


# ---------- notifications_enabled ----------


async def test_responsible_with_notifications_off_gets_no_message_but_history_is_kept(session, make_user, make_band, published):
    owner = await make_user(notifications_enabled=False)
    member = await make_user()
    band = await make_band(owner)
    await bands.join_band(session, member.telegram_id, band.invite_code, "гитара")
    published.clear()
    day, start, end = _slot()

    await bookings.create_booking(session, member.telegram_id, band.id, day, start, end, "self", None)

    assert [e for e in published if e["type"] == "notify"] == []
    assert any(e["type"] == "schedule_changed" for e in published)
    assert len((await session.execute(select(models.ScheduleChange))).scalars().all()) == 1


async def test_creator_with_notifications_off_is_not_told_about_cancellation(session, make_user, make_band, published):
    owner = await make_user()
    member = await make_user(notifications_enabled=False)
    band = await make_band(owner)
    await bands.join_band(session, member.telegram_id, band.invite_code, "гитара")
    day, start, end = _slot()
    booking = await bookings.create_booking(session, member.telegram_id, band.id, day, start, end, "self", None)
    published.clear()

    await bookings.delete_booking(session, owner.telegram_id, booking.id)

    assert [e for e in published if e["type"] == "notify"] == []
