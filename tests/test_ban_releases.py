"""Banning or deleting a member frees what they hold: personal rehearsals, waiting entries, open move requests."""
from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db import models
from app.services import bookings, move_requests, users, waitlist

NOW = datetime(2026, 10, 14, 12, 0)  # Wednesday
DAY = date(2026, 10, 15)


@pytest.fixture
async def world(session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    admin = await make_user(role="admin", group_slug=None)
    victim = await make_user(last_name="Петров", first_name="Иван")
    other = await make_user(last_name="Сидоров", first_name="Пётр")
    other_band = await make_band(other, "Dancers")
    personal = await bookings.create_booking(session, victim.telegram_id, None, DAY, time(15), time(16), "personal", None)
    # Plain ids: a service commit expires the ORM objects, and the tests must not lazy-load them afterwards.
    return type(
        "World",
        (),
        {
            "admin": admin,
            "admin_id": admin.id,
            "admin_tg": admin.telegram_id,
            "victim_tg": victim.telegram_id,
            "victim_id": victim.id,
            "other_tg": other.telegram_id,
            "other_band_id": other_band.id,
            "personal_id": personal.id,
            "victim": victim,
            "other": other,
            "other_band": other_band,
            "personal": personal,
        },
    )


def _kinds(published, kind):
    return [event for event in published if event["type"] == kind]


async def _booking(session, booking_id):
    session.expire_all()
    return await session.get(models.Booking, booking_id)


@pytest.mark.parametrize("action", ["ban", "delete"])
async def test_future_personal_rehearsals_are_cancelled_and_offered_to_waiters(session, world, published, action):
    entry_id = (await waitlist.join(session, world.other_tg, world.other_band_id, DAY, time(15), time(16))).id
    published.clear()

    if action == "ban":
        await users.ban_user(session, world.admin_tg, str(world.victim_tg), "spam")
    else:
        await users.soft_delete_user(session, world.admin_tg, str(world.victim_tg), "gone")

    booking = await _booking(session, world.personal_id)
    assert booking.status == "cancelled" and booking.cancelled_by_id == world.admin_id
    [offer] = _kinds(published, "waitlist_offer")
    assert offer["telegram_id"] == world.other_tg and offer["entry_id"] == entry_id
    assert _kinds(published, "schedule_changed")
    assert not [event for event in _kinds(published, "notify") if event["telegram_id"] == world.victim_tg]
    change = (
        await session.execute(select(models.ScheduleChange).where(models.ScheduleChange.booking_id == booking.id))
    ).scalars().all()[-1]
    assert change.action == "cancelled" and change.actor_id == world.admin_id


async def test_past_personal_rehearsals_and_band_bookings_stay(session, world, make_band, freeze_now):
    band = await make_band(world.victim, "Rockers")
    band_booking_id = (
        await bookings.create_booking(session, world.victim_tg, band.id, DAY, time(17), time(18), "self", None)
    ).id
    freeze_now(datetime(2026, 10, 15, 16, 30))  # the personal rehearsal (15:00-16:00) is over

    await users.ban_user(session, world.admin_tg, str(world.victim_tg), None)

    assert (await _booking(session, world.personal_id)).status == "confirmed"
    assert (await _booking(session, band_booking_id)).status == "confirmed"
    member = await session.execute(select(models.BandMember).where(models.BandMember.user_id == world.victim_id))
    assert member.scalars().first() is not None


async def test_waiting_entries_and_open_move_requests_of_the_member_are_closed(session, world, make_band):
    victim_band = await make_band(world.victim, "Rockers")
    held = await bookings.create_booking(session, world.other_tg, world.other_band_id, DAY, time(17), time(18), "self", None)
    await waitlist.join(session, world.victim_tg, victim_band.id, DAY, time(17), time(18))
    request_id = (await move_requests.create_request(session, world.victim_tg, held.id, DAY, time(19), time(20), None)).id

    await users.soft_delete_user(session, world.admin_tg, str(world.victim_tg), None)

    assert (await session.execute(select(models.WaitlistEntry))).scalars().all() == []
    session.expire_all()
    closed = await session.get(models.MoveRequest, request_id)
    assert closed.status == "cancelled" and closed.resolved_by_id == world.admin_id
