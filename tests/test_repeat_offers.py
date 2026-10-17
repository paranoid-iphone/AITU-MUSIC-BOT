"""Step 8: offer to repeat a rehearsal a week later once it has started, and book it on request."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db import models
from app.services import bookings, repeat_offers
from app.services.errors import ConflictError, ForbiddenError

NOW = datetime(2026, 10, 14, 18, 5)  # Wednesday; the rehearsal below started at 18:00
START = datetime(2026, 10, 14, 18, 0)


async def _booking(session, user, band_id, start=START, hours=1, purpose="self", song=None, source="manual", status="confirmed"):
    booking = models.Booking(
        band_id=band_id,
        created_by_id=user.id,
        booking_date=start.date(),
        start_time=start.time(),
        end_time=(start + timedelta(hours=hours)).time(),
        purpose=purpose,
        song_title=song,
        source=source,
        status=status,
    )
    session.add(booking)
    await session.commit()
    return booking


def _offers(published) -> list[dict]:
    return [event for event in published if event["type"] == "repeat_offer"]


@pytest.fixture
def now(freeze_now):
    freeze_now(NOW)


async def test_started_band_booking_is_offered_to_the_responsible_person(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    responsible = await make_user()
    band.responsible_user_id = responsible.id
    await session.commit()
    booking = await _booking(session, owner, band.id, song="Song")

    assert await repeat_offers.offer_repeats(session) == 1

    [offer] = _offers(published)
    assert offer["telegram_id"] == responsible.telegram_id
    assert offer["booking_id"] == booking.id
    assert "21.10" in offer["text"] and "21.10" in offer["button"]
    assert "18:00" in offer["button"]
    await session.refresh(booking)
    assert booking.repeat_offered_at == NOW


async def test_offer_is_sent_once(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    await _booking(session, owner, band.id)

    assert await repeat_offers.offer_repeats(session) == 1
    assert await repeat_offers.offer_repeats(session) == 0
    assert len(_offers(published)) == 1


async def test_personal_booking_is_offered_to_its_creator(session, make_user, published, now):
    user = await make_user()
    await _booking(session, user, None, purpose="personal")

    assert await repeat_offers.offer_repeats(session) == 1
    assert _offers(published)[0]["telegram_id"] == user.telegram_id


async def test_not_started_yet_is_skipped(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    await _booking(session, owner, band.id, start=NOW + timedelta(hours=1))

    assert await repeat_offers.offer_repeats(session) == 0
    assert _offers(published) == []


async def test_booking_older_than_lookback_is_skipped(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    await _booking(session, owner, band.id, start=NOW - timedelta(hours=5))

    assert await repeat_offers.offer_repeats(session) == 0


@pytest.mark.parametrize("purpose,source", [("staff", "staff"), ("staff", "dsvr")])
async def test_staff_and_dsvr_bookings_are_skipped(session, make_user, published, now, purpose, source):
    admin = await make_user(role="admin")
    await _booking(session, admin, None, purpose=purpose, source=source)

    assert await repeat_offers.offer_repeats(session) == 0


async def test_cancelled_booking_is_skipped(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    await _booking(session, owner, band.id, status="cancelled")

    assert await repeat_offers.offer_repeats(session) == 0


async def test_taken_slot_is_not_offered(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    other = await make_band(await make_user(), "Other")
    await _booking(session, owner, band.id)
    await _booking(session, owner, other.id, start=START + timedelta(days=7, minutes=30))  # overlaps 18:30-19:30

    assert await repeat_offers.offer_repeats(session) == 0
    assert _offers(published) == []


async def test_closed_day_is_not_offered(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    await _booking(session, owner, band.id)
    day = (await session.execute(select(models.AvailableDay).where(models.AvailableDay.day_of_week == START.weekday()))).scalar_one()
    day.is_enabled = False
    await session.commit()

    assert await repeat_offers.offer_repeats(session) == 0


async def test_band_future_cap_blocks_the_offer(session, make_user, make_band, published, now):
    owner = await make_user()
    band = await make_band(owner)
    session.add(models.ClubSetting(key="max_future_bookings_per_band", value="1"))
    await _booking(session, owner, band.id)
    await _booking(session, owner, band.id, start=NOW + timedelta(days=2, hours=1))  # one future booking already
    await session.commit()

    assert await repeat_offers.offer_repeats(session) == 0


async def test_recipient_with_notifications_off_gets_nothing(session, make_user, make_band, published, now):
    owner = await make_user(notifications_enabled=False)
    band = await make_band(owner)
    await _booking(session, owner, band.id)

    assert await repeat_offers.offer_repeats(session) == 0
    assert _offers(published) == []


async def test_repeat_booking_creates_the_same_slot_a_week_later(session, make_user, make_band, now):
    owner = await make_user()
    band = await make_band(owner)
    source = await _booking(session, owner, band.id, purpose="concert", song="Song")

    created = await repeat_offers.repeat_booking(session, owner.telegram_id, source.id)

    assert created.booking_date == date(2026, 10, 21)
    assert (created.start_time, created.end_time) == (time(18, 0), time(19, 0))
    assert (created.band_id, created.purpose, created.song_title) == (band.id, "concert", "Song")
    assert created.created_by_id == owner.id


async def test_repeat_booking_twice_conflicts(session, make_user, make_band, now):
    owner = await make_user()
    band = await make_band(owner)
    source = await _booking(session, owner, band.id)

    await repeat_offers.repeat_booking(session, owner.telegram_id, source.id)
    with pytest.raises(ConflictError):
        await repeat_offers.repeat_booking(session, owner.telegram_id, source.id)


async def test_repeat_booking_by_a_non_member_is_forbidden(session, make_user, make_band, now):
    owner = await make_user()
    band = await make_band(owner)
    stranger = await make_user()
    source = await _booking(session, owner, band.id)

    with pytest.raises(ForbiddenError):
        await repeat_offers.repeat_booking(session, stranger.telegram_id, source.id)


async def test_repeat_of_someone_elses_personal_booking_is_forbidden(session, make_user, now):
    owner = await make_user()
    stranger = await make_user()
    source = await _booking(session, owner, None, purpose="personal")

    with pytest.raises(ForbiddenError):
        await repeat_offers.repeat_booking(session, stranger.telegram_id, source.id)


async def test_repeat_of_a_cancelled_or_staff_booking_conflicts(session, make_user, make_band, now):
    owner = await make_user()
    band = await make_band(owner)
    cancelled = await _booking(session, owner, band.id, status="cancelled")
    staff = await _booking(session, owner, None, start=START + timedelta(hours=2), purpose="staff", source="staff")

    for booking in (cancelled, staff):
        with pytest.raises(ConflictError):
            await repeat_offers.repeat_booking(session, owner.telegram_id, booking.id)
