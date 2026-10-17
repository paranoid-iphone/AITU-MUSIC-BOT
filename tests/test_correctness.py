from datetime import date, datetime, time, timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.db import models
from app.services import bands, bookings, events, users
from app.services.errors import ConflictError, NotFoundError

NOW = datetime(2026, 10, 16, 12, 0)
TOMORROW = date(2026, 10, 17)


@pytest.fixture(autouse=True)
def _clock(freeze_now):
    freeze_now(NOW)


async def _book(session, user, band, hour=14, song="Song"):
    return await bookings.create_booking(
        session, user.telegram_id, band.id, TOMORROW, time(hour), time(hour + 1), "self", song
    )


# --- M7: deleting a band --------------------------------------------------------------------------------------------


async def test_delete_band_cancels_future_bookings_and_drops_requests(session, make_user, make_band, published):
    owner = await make_user()
    band = await make_band(owner, "Rockers")
    future = await _book(session, owner, band)
    past = models.Booking(
        band_id=band.id,
        created_by_id=owner.id,
        booking_date=date(2026, 10, 10),
        start_time=time(10),
        end_time=time(11),
        purpose="self",
    )
    session.add(past)
    request = models.AvailabilityRequest(
        week_start=date(2026, 10, 19), band_id=band.id, submitted_by_id=owner.id, duration_minutes=60
    )
    request.windows.append(models.AvailabilityWindow(priority=1, day_of_week=2, start_time=time(10), end_time=time(12)))
    session.add(request)
    await session.commit()
    published.clear()

    await bands.delete_band(session, band.id, owner.telegram_id)

    await session.refresh(future)
    await session.refresh(past)
    assert future.status == "cancelled"
    assert future.cancel_reason == "Коллектив удалён"
    assert future.cancelled_by_id == owner.id
    assert past.status == "confirmed"  # history is kept
    assert past.band_id is None and future.band_id is None
    assert await session.get(models.Band, band.id) is None
    assert (await session.execute(select(func.count()).select_from(models.AvailabilityRequest))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(models.AvailabilityWindow))).scalar_one() == 0
    change = (
        await session.execute(select(models.ScheduleChange).where(models.ScheduleChange.action == "cancelled"))
    ).scalar_one()
    assert change.band_name == "Rockers"
    assert any(event["type"] == "schedule_changed" for event in published)
    # the freed slot can be booked again
    other = await make_user()
    other_band = await make_band(other, "Others")
    await _book(session, other, other_band)


# --- M10: limit getters must not commit -----------------------------------------------------------------------------


async def test_limit_getters_do_not_commit_when_setting_is_missing(session, monkeypatch):
    await session.execute(models.ClubSetting.__table__.delete())
    await session.commit()
    calls = []
    real_commit = session.commit

    async def spy():
        calls.append(1)
        await real_commit()

    monkeypatch.setattr(session, "commit", spy)

    assert await bookings.get_weekly_booking_limit_minutes(session) == 240
    assert await bookings.get_daily_booking_limit_minutes(session) == 0
    assert calls == []


# --- L7: past bookings are frozen -----------------------------------------------------------------------------------


async def test_past_booking_cannot_be_changed_or_cancelled(session, make_user, make_band, freeze_now):
    owner = await make_user()
    band = await make_band(owner)
    booking = await _book(session, owner, band, hour=14)

    freeze_now(datetime(2026, 10, 17, 15, 30))  # booking ended at 15:00
    with pytest.raises(ConflictError):
        await bookings.delete_booking(session, owner.telegram_id, booking.id)
    with pytest.raises(ConflictError):
        await bookings.update_booking(session, owner.telegram_id, booking.id, song_title="Changed")
    await session.refresh(booking)
    assert booking.status == "confirmed" and booking.song_title == "Song"


async def test_ongoing_booking_can_still_be_cancelled(session, make_user, make_band, freeze_now):
    owner = await make_user()
    band = await make_band(owner)
    booking = await _book(session, owner, band, hour=14)

    freeze_now(datetime(2026, 10, 17, 14, 30))
    await bookings.delete_booking(session, owner.telegram_id, booking.id)
    await session.refresh(booking)
    assert booking.status == "cancelled"


# --- M11: events ----------------------------------------------------------------------------------------------------


async def _event(session, admin, **data):
    payload = {"title": "Concert", "event_date": date(2026, 11, 1), "submission_deadline": None}
    payload.update(data)
    return await events.create_event(session, admin.telegram_id, **payload)


async def _owner_and_member(session, make_user, make_band, name="Rockers"):
    owner = await make_user()
    band = await make_band(owner, name)
    member_id = (await session.execute(select(models.BandMember.id).where(models.BandMember.band_id == band.id))).scalar_one()
    return owner, band, member_id


async def test_application_requires_existing_open_event_before_deadline(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _owner_and_member(session, make_user, make_band)

    with pytest.raises(NotFoundError):
        await events.submit_application(session, 9999, owner.telegram_id, band.id, "Song", [member_id])

    closed = await _event(session, admin, status="closed")
    with pytest.raises(ConflictError):
        await events.submit_application(session, closed.id, owner.telegram_id, band.id, "Song", [member_id])

    expired = await _event(session, admin, submission_deadline=date(2026, 10, 15))
    with pytest.raises(ConflictError):
        await events.submit_application(session, expired.id, owner.telegram_id, band.id, "Song", [member_id])

    on_deadline = await _event(session, admin, submission_deadline=date(2026, 10, 16))
    application = await events.submit_application(session, on_deadline.id, owner.telegram_id, band.id, "Song", [member_id])
    assert application.event_id == on_deadline.id


async def test_application_cannot_be_updated_after_deadline_but_can_be_withdrawn(
    session, make_user, make_band, freeze_now
):
    admin = await make_user(role="admin")
    owner, band, member_id = await _owner_and_member(session, make_user, make_band)
    event = await _event(session, admin, submission_deadline=date(2026, 10, 16))
    application = await events.submit_application(session, event.id, owner.telegram_id, band.id, "Song", [member_id])

    freeze_now(datetime(2026, 10, 17, 9, 0))
    with pytest.raises(ConflictError):
        await events.update_application(session, application.id, owner.telegram_id, song_title="Other")
    await events.delete_application(session, owner.telegram_id, application.id)


async def test_export_stores_formula_like_text_as_plain_strings(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _owner_and_member(session, make_user, make_band, name="=1+1")
    event = await _event(session, admin, title="+cmd")
    await events.submit_application(session, event.id, owner.telegram_id, band.id, '=HYPERLINK("http://x","y")', [member_id])

    data = await events.export_event_applications(session, admin.telegram_id, event.id)

    sheet = load_workbook(BytesIO(data)).active
    row = {cell.column_letter: cell for cell in sheet[2]}
    assert row["B"].value == "=1+1" and row["B"].data_type == "s"
    assert row["C"].value.startswith("=HYPERLINK") and row["C"].data_type == "s"
    assert row["A"].value == "+cmd"


# --- M4: username lookup --------------------------------------------------------------------------------------------


async def test_username_lookup_is_case_insensitive(session, make_user):
    user = await make_user(telegram_username="MixedCase")
    found = await users.get_user_by_identifier(session, "@mixedcase")
    assert found is not None and found.id == user.id
    assert await users.get_user_by_identifier(session, "nobody") is None


async def test_ambiguous_username_is_a_conflict_not_a_crash(session, make_user):
    await make_user(telegram_username="twin")
    await make_user(telegram_username="Twin")
    with pytest.raises(ConflictError):
        await users.get_user_by_identifier(session, "twin")


async def test_new_holder_of_a_username_takes_it_from_the_stale_one(session, make_user):
    stale = await make_user(telegram_username="handle")

    fresh = await users.upsert_telegram_user(session, 777001, "Handle", "New Person")

    await session.refresh(stale)
    assert stale.telegram_username is None
    found = await users.get_user_by_identifier(session, "handle")
    assert found is not None and found.id == fresh.id
