from datetime import time, timedelta

import pytest
from sqlalchemy import select

from app.core.timezone import now_local
from app.db import models
from app.services import bands, bookings
from app.services.errors import ConflictError, ForbiddenError


def _day(days: int = 1):
    return (now_local() + timedelta(days=days)).date()


async def _book(session, user, band, day, start_h, end_h, song="Song"):
    return await bookings.create_booking(session, user.telegram_id, band.id, day, time(start_h), time(end_h), "self", song)


async def test_preview_lists_overlaps_without_changing_anything(session, make_user, make_band):
    owner = await make_user()
    dsvr = await make_user(role="dsvr", group_slug=None)
    band = await make_band(owner, "Rockers")
    day = _day()
    await _book(session, owner, band, day, 14, 15)
    await _book(session, owner, band, day, 16, 17)

    rows = await bookings.preview_priority_booking(session, dsvr.telegram_id, day, time(14, 30), time(15, 30))
    assert [r["band_name"] for r in rows] == ["Rockers"]
    assert len(await bookings.list_schedule(session)) == 2


async def test_priority_booking_cancels_overlaps_notifies_and_records(session, make_user, make_band, published):
    owner = await make_user()
    member = await make_user()
    dsvr = await make_user(role="dsvr", group_slug=None)
    band = await make_band(owner, "Rockers")
    await bands.join_band(session, member.telegram_id, band.invite_code, "бас")
    day = _day()
    await _book(session, member, band, day, 14, 15)  # created by member, responsible is owner
    await _book(session, owner, band, day, 15, 16)
    untouched = await _book(session, owner, band, day, 17, 18)
    published.clear()

    result = await bookings.create_priority_booking(session, dsvr.telegram_id, day, time(14, 30), time(15, 30), "Ректор")

    assert len(result["cancelled"]) == 2
    assert result["booking"].source == "dsvr"
    rows = await bookings.list_schedule(session)
    assert sorted((r["start_time"].hour, r["purpose"]) for r in rows) == [(14, "staff"), (17, "self")]
    await session.refresh(untouched)
    assert untouched.status == "confirmed"

    notified = {e["telegram_id"] for e in published if e["type"] == "notify"}
    assert notified == {owner.telegram_id, member.telegram_id}  # responsible + creator of the cancelled booking
    assert all("Ректор" in e["text"] for e in published if e["type"] == "notify")

    actions = [c.action for c in (await session.execute(select(models.ScheduleChange).order_by(models.ScheduleChange.id))).scalars()]
    assert actions.count("preempted") == 2 and actions[-1] == "created"
    cancelled = (await session.execute(select(models.Booking).where(models.Booking.status == "cancelled"))).scalars().all()
    assert {b.cancelled_by_id for b in cancelled} == {dsvr.id}
    assert all("ДСВР" in b.cancel_reason for b in cancelled)


async def test_personal_booking_creator_is_notified_when_preempted(session, make_user, published):
    student = await make_user()
    dsvr = await make_user(role="dsvr", group_slug=None)
    day = _day()
    await bookings.create_booking(session, student.telegram_id, None, day, time(12), time(13), "personal", "Solo")
    published.clear()

    await bookings.create_priority_booking(session, dsvr.telegram_id, day, time(12), time(13), "Мероприятие")

    assert [e["telegram_id"] for e in published if e["type"] == "notify"] == [student.telegram_id]


async def test_regular_and_group_admin_users_cannot_take_over_time(session, make_user):
    plain = await make_user()
    with pytest.raises(ForbiddenError):
        await bookings.create_priority_booking(session, plain.telegram_id, _day(), time(12), time(13), "x")
    with pytest.raises(ForbiddenError):
        await bookings.preview_priority_booking(session, plain.telegram_id, _day(), time(12), time(13))


async def test_main_admin_may_use_priority(session, make_user):
    admin = await make_user(role="admin")
    result = await bookings.create_priority_booking(session, admin.telegram_id, _day(), time(12), time(13), "Админ")
    assert result["cancelled"] == []


async def test_priority_rejects_past_and_out_of_hours(session, make_user):
    dsvr = await make_user(role="dsvr", group_slug=None)
    with pytest.raises(ConflictError):
        await bookings.create_priority_booking(session, dsvr.telegram_id, _day(-1), time(12), time(13), "past")
    with pytest.raises(ConflictError):
        await bookings.create_priority_booking(session, dsvr.telegram_id, _day(), time(3), time(4), "night")
    with pytest.raises(ConflictError):
        await bookings.create_priority_booking(session, dsvr.telegram_id, _day(), time(12), time(12, 20), "odd")
