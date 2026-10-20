from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.timezone import now_local
from app.db import models
from app.services import bands, bookings, schedule_history
from app.services.errors import ConflictError, ForbiddenError


def _slot(hour: int = 14, days: int = 1):
    start = (now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return start.date(), start.time(), (start + timedelta(hours=1)).time()


async def _member_of(session, band, user):
    await bands.join_band(session, user.telegram_id, band.invite_code, "гитара")


async def test_create_records_history_and_notifies_responsible(session, make_user, make_band, published):
    owner = await make_user()
    member = await make_user()
    band = await make_band(owner, "Rockers")
    await _member_of(session, band, member)
    day, start, end = _slot()

    await bookings.create_booking(session, member.telegram_id, band.id, day, start, end, "self", "Song")

    changes = (await session.execute(select(models.ScheduleChange))).scalars().all()
    assert [c.action for c in changes] == ["created"]
    assert changes[0].band_name == "Rockers"
    notifications = [e for e in published if e["type"] == "notify"]
    assert [e["telegram_id"] for e in notifications] == [owner.telegram_id]
    assert "Rockers" in notifications[0]["text"]
    assert any(e["type"] == "schedule_changed" for e in published)


async def test_no_notification_when_responsible_acts(session, make_user, make_band, published):
    owner = await make_user()
    band = await make_band(owner)
    day, start, end = _slot()
    await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "Song")
    assert [e for e in published if e["type"] == "notify"] == []


async def test_update_records_before_and_after(session, make_user, make_band, published):
    owner = await make_user()
    admin = await make_user(role="admin")
    band = await make_band(owner)
    day, start, end = _slot(14)
    booking = await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "Song")
    published.clear()

    _, new_start, new_end = _slot(16)
    await bookings.update_booking(session, admin.telegram_id, booking.id, start_time=new_start, end_time=new_end)

    change = (await session.execute(select(models.ScheduleChange).where(models.ScheduleChange.action == "updated"))).scalar_one()
    assert change.before["start"] == "14:00"
    assert change.after["start"] == "16:00"
    text = next(e["text"] for e in published if e["type"] == "notify")
    assert "Было" in text and "14:00" in text and "16:00" in text


async def test_delete_soft_cancels_and_frees_the_slot(session, make_user, make_band, published):
    owner = await make_user()
    band = await make_band(owner)
    day, start, end = _slot()
    booking = await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "Song")

    await bookings.delete_booking(session, owner.telegram_id, booking.id)

    await session.refresh(booking)
    assert booking.status == "cancelled"
    assert booking.cancelled_by_id == owner.id
    assert await bookings.list_schedule(session) == []
    # The exact same slot can be booked again (no unique constraint on the slot any more).
    again = await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "Song 2")
    assert again.id != booking.id
    actions = [c.action for c in (await session.execute(select(models.ScheduleChange).order_by(models.ScheduleChange.id))).scalars()]
    assert actions == ["created", "cancelled", "created"]


async def test_cancelled_booking_cannot_be_edited_or_cancelled_twice(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    day, start, end = _slot()
    booking = await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "Song")
    await bookings.delete_booking(session, owner.telegram_id, booking.id)
    with pytest.raises(ConflictError):
        await bookings.update_booking(session, owner.telegram_id, booking.id, song_title="x")
    with pytest.raises(ConflictError):
        await bookings.delete_booking(session, owner.telegram_id, booking.id)


async def test_history_visibility(session, make_user, make_band, grant):
    main_admin = await make_user(role="admin")
    dance_admin = await make_user(group_slug="dance")
    dance_id = (await session.execute(select(models.Group.id).where(models.Group.slug == "dance"))).scalar_one()
    await grant(dance_admin, "Админ группы", "dance")

    dance_owner = await make_user(group_slug="dance")
    music_owner = await make_user(group_slug="music")
    stranger = await make_user(group_slug="music")
    dance_band = await make_band(dance_owner, "Dance", group_id=dance_id)
    music_band = await make_band(music_owner, "Music")
    for owner, band, hour in ((dance_owner, dance_band, 14), (music_owner, music_band, 16)):
        day, start, end = _slot(hour)
        await bookings.create_booking(session, owner.telegram_id, band.id, day, start, end, "self", "S")

    assert {c["band_name"] for c in await schedule_history.list_changes(session, main_admin.telegram_id)} == {"Dance", "Music"}
    assert {c["band_name"] for c in await schedule_history.list_changes(session, dance_admin.telegram_id)} == {"Dance"}
    assert {c["band_name"] for c in await schedule_history.list_changes(session, music_owner.telegram_id, band_id=music_band.id)} == {"Music"}
    with pytest.raises(ForbiddenError):
        await schedule_history.list_changes(session, stranger.telegram_id, band_id=music_band.id)
    with pytest.raises(ForbiddenError):
        await schedule_history.list_changes(session, music_owner.telegram_id)


async def test_list_changes_pages_by_before_id(session, make_user):
    admin = await make_user(role="admin", group_slug=None)
    session.add_all([models.ScheduleChange(band_name=f"Band{i}", action="created", after={}) for i in range(5)])
    await session.commit()

    first = await schedule_history.list_changes(session, admin.telegram_id, limit=2)
    rest = await schedule_history.list_changes(session, admin.telegram_id, limit=10, before_id=first[-1]["id"])
    assert [row["band_name"] for row in first] == ["Band4", "Band3"]
    assert [row["band_name"] for row in rest] == ["Band2", "Band1", "Band0"]
