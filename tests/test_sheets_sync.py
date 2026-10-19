from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.timezone import now_local
from app.db import models
from app.services import bookings, sheets_sync


class FakeBackend:
    def __init__(self):
        self.schedules: list[list[list]] = []
        self.history: list[list[list]] = []
        self.fail = False

    def replace_schedule(self, rows):
        if self.fail:
            raise ConnectionError("google is down")
        self.schedules.append(rows)

    def append_history(self, rows):
        if self.fail:
            raise ConnectionError("google is down")
        self.history.append(rows)


@pytest.fixture(autouse=True)
def _reset_state():
    sheets_sync._state.schedule_digest = None
    yield
    sheets_sync._state.schedule_digest = None


async def _book(session, make_user, make_band, hour=15, days=1):
    leader = await make_user(last_name="Петров", first_name="Иван", telegram_username="petrov")
    band = await make_band(leader, "Rockers")
    day = (now_local() + timedelta(days=days)).date()
    await bookings.create_booking(session, leader.telegram_id, band.id, day, time(hour), time(hour + 1), "concert", "Song")
    return leader, band, day


def test_is_enabled_needs_both_settings(monkeypatch):
    monkeypatch.setattr(settings, "google_sheets_id", "abc")
    monkeypatch.setattr(settings, "google_service_account_file", "")
    assert sheets_sync.is_enabled() is False
    monkeypatch.setattr(settings, "google_service_account_file", "secrets/key.json")
    assert sheets_sync.is_enabled() is True


async def test_schedule_rows_contain_responsible_and_labels(session, make_user, make_band):
    _, _, day = await _book(session, make_user, make_band)
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)

    header, row = backend.schedules[0]
    assert header == sheets_sync.SCHEDULE_HEADER
    assert row[0] == day.isoformat()
    assert row[2:5] == ["15:00", "16:00", "Rockers"]
    assert row[6:10] == ["Петров Иван", "@petrov", "к концерту", "Song"]
    assert row[10] == "вручную"


async def test_schedule_rows_carry_ids_for_restoring(session, make_user, make_band):
    leader, band, _ = await _book(session, make_user, make_band)
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)

    header, row = backend.schedules[0]
    booking = (await session.execute(select(models.Booking))).scalar_one()
    assert header[-3:] == ["ID брони", "ID номера", "ID автора"]
    assert row[-3:] == [booking.id, band.id, leader.id]


async def test_personal_booking_has_no_band_id_but_keeps_the_author(session, make_user):
    user = await make_user()
    day = (now_local() + timedelta(days=1)).date()
    session.add(
        models.Booking(created_by_id=user.id, booking_date=day, start_time=time(12), end_time=time(13), purpose="personal")
    )
    await session.commit()
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)

    row = backend.schedules[0][1]
    assert row[-2:] == ["", user.id]


async def _booking_on(session, make_user, make_band, day):
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    session.add(
        models.Booking(
            band_id=band.id, created_by_id=leader.id, booking_date=day, start_time=time(12), end_time=time(13), purpose="self"
        )
    )
    await session.commit()


async def test_schedule_sheet_reaches_thirty_days_ahead(session, make_user, make_band):
    today = now_local().date()
    await _booking_on(session, make_user, make_band, today + timedelta(days=sheets_sync.SCHEDULE_HORIZON_DAYS))
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)
    assert len(backend.schedules[-1]) == 2


async def test_schedule_sheet_stops_after_the_horizon(session, make_user, make_band):
    today = now_local().date()
    await _booking_on(session, make_user, make_band, today + timedelta(days=sheets_sync.SCHEDULE_HORIZON_DAYS + 1))
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)
    assert backend.schedules[-1] == [sheets_sync.SCHEDULE_HEADER]


async def test_history_is_appended_once_and_marked(session, make_user, make_band):
    await _book(session, make_user, make_band)
    backend = FakeBackend()

    first = await sheets_sync.sync_once(session, backend)
    second = await sheets_sync.sync_once(session, backend)

    assert first == {"schedule_rewritten": True, "history_rows": 1}
    assert second == {"schedule_rewritten": False, "history_rows": 0}
    assert len(backend.history) == 1 and len(backend.schedules) == 1
    stamp, who, action, band, before, after, reason, booking_id, change_id = backend.history[0][0]
    assert (who, action, band, before) == ("Петров Иван", "создал(а) бронь", "Rockers", "")
    assert "15:00–16:00" in after
    assert len(stamp) == 19
    change = (await session.execute(select(models.ScheduleChange))).scalar_one()
    assert change.sheets_synced_at is not None and change.id == change_id


async def test_changes_made_later_are_synced_incrementally(session, make_user, make_band):
    leader, band, day = await _book(session, make_user, make_band)
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)

    booking = (await session.execute(select(models.Booking))).scalar_one()
    await bookings.update_booking(session, leader.telegram_id, booking.id, song_title="Renamed")
    result = await sheets_sync.sync_once(session, backend)

    assert result == {"schedule_rewritten": True, "history_rows": 1}
    assert backend.schedules[-1][1][9] == "Renamed"
    assert backend.history[-1][0][2] == "изменил(а) бронь"


async def test_cancelled_bookings_leave_the_schedule_but_stay_in_history(session, make_user, make_band):
    leader, _, _ = await _book(session, make_user, make_band)
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)
    booking = (await session.execute(select(models.Booking))).scalar_one()
    await bookings.delete_booking(session, leader.telegram_id, booking.id)

    await sheets_sync.sync_once(session, backend)

    assert backend.schedules[-1] == [sheets_sync.SCHEDULE_HEADER]
    assert [row[0][2] for row in backend.history] == ["создал(а) бронь", "отменил(а) бронь"]


async def test_google_outage_keeps_history_unsynced_and_retries(session, make_user, make_band):
    await _book(session, make_user, make_band)
    backend = FakeBackend()
    backend.fail = True
    with pytest.raises(ConnectionError):
        await sheets_sync.sync_once(session, backend)
    change = (await session.execute(select(models.ScheduleChange))).scalar_one()
    assert change.sheets_synced_at is None

    backend.fail = False
    recovered = await sheets_sync.sync_once(session, backend)
    assert recovered == {"schedule_rewritten": True, "history_rows": 1}


async def test_old_weeks_are_not_in_the_schedule_sheet(session, make_user, make_band):
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    last_week = now_local().date() - timedelta(days=14)
    session.add(
        models.Booking(
            band_id=band.id, created_by_id=leader.id, booking_date=last_week, start_time=time(12), end_time=time(13), purpose="self"
        )
    )
    await session.commit()
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)
    assert backend.schedules[-1] == [sheets_sync.SCHEDULE_HEADER]


async def test_committing_a_schedule_change_requests_a_sync(session, make_user, make_band, monkeypatch):
    calls = []
    monkeypatch.setattr(sheets_sync, "request_sync", lambda: calls.append(1))
    await _book(session, make_user, make_band, hour=17)
    assert calls == [1]  # one schedule_changed per committed booking


async def test_request_sync_wakes_worker_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_sheets_id", "")
    sheets_sync._state.wakeup = None
    sheets_sync.request_sync()
    assert sheets_sync._state.wakeup is None
    monkeypatch.setattr(settings, "google_sheets_id", "abc")
    monkeypatch.setattr(settings, "google_service_account_file", "secrets/key.json")
    sheets_sync.request_sync()
    assert sheets_sync._state.wakeup is not None and sheets_sync._state.wakeup.is_set()
    sheets_sync._state.wakeup = None
