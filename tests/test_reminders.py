"""Rehearsal reminders: the evening before and shortly before the start, to every band member who wants them."""
from datetime import datetime, timedelta

import pytest

from app.core.timezone import utcnow
from app.db import models
from app.services import bands, bookings, reminders

TODAY = datetime(2026, 10, 14).date()  # Wednesday


async def _booking(session, user, band_id, start, hours=2, purpose="self", status="confirmed", created_at=None):
    booking = models.Booking(
        band_id=band_id,
        created_by_id=user.id,
        booking_date=start.date(),
        start_time=start.time(),
        end_time=(start + timedelta(hours=hours)).time(),
        purpose=purpose,
        status=status,
        created_at=created_at or datetime(2026, 10, 1, 9, 0),  # naive UTC, long before
    )
    session.add(booking)
    await session.commit()
    return booking


def _reminders(published) -> list[dict]:
    return [event for event in published if event["type"] == "rehearsal_reminder"]


@pytest.fixture
def clock(freeze_now):
    def set_to(hour, minute=0, day=TODAY):
        freeze_now(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))

    return set_to


@pytest.fixture
async def band_of_three(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner, "Echo")
    members = [owner]
    for role in ("bass", "drums"):
        member = await make_user()
        await bands.join_band(session, member.telegram_id, band.invite_code, role)
        members.append(member)
    return band, members


async def test_the_evening_before_every_member_is_reminded(session, band_of_three, published, clock):
    band, members = band_of_three
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 3)

    assert await reminders.send_reminders(session) == 1

    sent = _reminders(published)
    assert sorted(event["telegram_id"] for event in sent) == sorted(member.telegram_id for member in members)
    assert "Завтра в 18:00" in sent[0]["text"] and "«Echo»" in sent[0]["text"] and "до 20:00" in sent[0]["text"]
    assert sent[0]["button"]


async def test_nothing_is_sent_the_day_before_until_the_evening(session, band_of_three, published, clock):
    band, members = band_of_three
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(19, 55)

    assert await reminders.send_reminders(session) == 0
    assert _reminders(published) == []


async def test_two_hours_before_the_start_members_are_reminded_again(session, band_of_three, published, clock):
    band, members = band_of_three
    booking = await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 0)
    await reminders.send_reminders(session)
    published.clear()

    clock(15, 0, TODAY + timedelta(days=1))
    assert await reminders.send_reminders(session) == 0  # three hours before: too early
    clock(16, 2, TODAY + timedelta(days=1))
    assert await reminders.send_reminders(session) == 1

    sent = _reminders(published)
    assert len(sent) == len(members)
    assert "Сегодня в 18:00" in sent[0]["text"]
    await session.refresh(booking)
    assert booking.reminded_soon_at is not None


async def test_each_reminder_goes_out_once(session, band_of_three, published, clock):
    band, members = band_of_three
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 0)
    assert await reminders.send_reminders(session) == 1
    clock(21, 0)
    assert await reminders.send_reminders(session) == 0
    assert len(_reminders(published)) == len(members)


async def test_a_rehearsal_just_after_midnight_gets_one_reminder_not_two(session, band_of_three, published, clock):
    band, members = band_of_three
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 0, 30), hours=1)
    clock(23, 0)

    assert await reminders.send_reminders(session) == 1
    clock(23, 5)
    assert await reminders.send_reminders(session) == 0
    assert len(_reminders(published)) == len(members)
    assert "Сегодня в 00:30" in _reminders(published)[0]["text"]


async def test_a_booking_made_inside_the_last_two_hours_is_not_reminded(session, band_of_three, published, clock):
    band, members = band_of_three
    clock(17, 0)
    made_at = utcnow() - timedelta(minutes=5)  # just made: naive UTC, as the column stores it
    await _booking(session, members[0], band.id, datetime(2026, 10, 14, 18, 0), created_at=made_at)

    assert await reminders.send_reminders(session) == 0
    assert _reminders(published) == []


async def test_missed_reminders_are_not_sent_late(session, band_of_three, published, clock):
    band, members = band_of_three
    await _booking(session, members[0], band.id, datetime(2026, 10, 14, 18, 0))
    clock(18, 10)  # the rehearsal has started: the backend was down at the right moment

    assert await reminders.send_reminders(session) == 0


async def test_a_personal_rehearsal_reminds_its_author(session, make_user, published, clock):
    author = await make_user()
    await _booking(session, author, None, datetime(2026, 10, 15, 10, 0), purpose="personal")
    clock(20, 0)

    assert await reminders.send_reminders(session) == 1
    [event] = _reminders(published)
    assert event["telegram_id"] == author.telegram_id
    assert "личная репетиция" in event["text"]


async def test_staff_and_cancelled_bookings_are_not_rehearsals(session, band_of_three, make_user, published, clock):
    band, members = band_of_three
    staff = await make_user(role="dsvr")
    await _booking(session, staff, None, datetime(2026, 10, 15, 10, 0), purpose="staff")
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0), status="cancelled")
    clock(20, 0)

    assert await reminders.send_reminders(session) == 0


async def test_the_responsible_person_is_reminded_once_even_outside_the_band(session, band_of_three, make_user, published, clock):
    band, members = band_of_three
    outsider = await make_user()
    band.responsible_user_id = outsider.id
    await session.commit()
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 0)

    await reminders.send_reminders(session)
    ids = [event["telegram_id"] for event in _reminders(published)]
    assert sorted(ids) == sorted([*(member.telegram_id for member in members), outsider.telegram_id])


async def test_people_who_turned_reminders_or_all_notifications_off_are_skipped(session, band_of_three, published, clock):
    band, members = band_of_three
    members[1].rehearsal_reminders_enabled = False
    members[2].notifications_enabled = False
    await session.commit()
    await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 0)

    assert await reminders.send_reminders(session) == 1
    assert [event["telegram_id"] for event in _reminders(published)] == [members[0].telegram_id]
    published.clear()
    clock(21, 0)
    assert await reminders.send_reminders(session) == 0  # marked as reminded although some were skipped


async def test_moving_a_booking_reminds_about_the_new_time(session, band_of_three, published, clock):
    band, members = band_of_three
    booking = await _booking(session, members[0], band.id, datetime(2026, 10, 15, 18, 0))
    clock(20, 0)
    await reminders.send_reminders(session)
    published.clear()

    await bookings.update_booking(session, members[0].telegram_id, booking.id, start_time=datetime(2026, 1, 1, 19, 0).time(),
                                  end_time=datetime(2026, 1, 1, 21, 0).time())
    await session.refresh(booking)
    assert booking.reminded_evening_at is None and booking.reminded_soon_at is None

    clock(20, 10)
    assert await reminders.send_reminders(session) == 1
    assert "Завтра в 19:00" in _reminders(published)[0]["text"]


async def test_a_new_user_wants_reminders_by_default(session, make_user):
    user = await make_user()
    await session.refresh(user)
    assert user.rehearsal_reminders_enabled is True
