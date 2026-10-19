import asyncio
from datetime import time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.timezone import now_local
from app.db import models
from app.services import bookings, sheets_sync


class FakeBackend:
    def __init__(self):
        self.schedules: list[list[list]] = []
        self.history: list[list[list]] = []
        self.events: list[list[list]] = []
        self.fail = False
        self.attempts = 0

    def replace_schedule(self, rows):
        self.attempts += 1
        if self.fail:
            raise ConnectionError("google is down")
        self.schedules.append(rows)

    def append_history(self, rows):
        if self.fail:
            raise ConnectionError("google is down")
        self.history.append(rows)

    def replace_events(self, rows):
        if self.fail:
            raise ConnectionError("google is down")
        self.events.append(rows)


@pytest.fixture(autouse=True)
def _reset_state():
    sheets_sync._state.schedule_digest = None
    sheets_sync._state.events_digest = None
    sheets_sync._state.wakeup = None  # an asyncio.Event belongs to the loop of the test that created it
    yield
    sheets_sync._state.schedule_digest = None
    sheets_sync._state.wakeup = None


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

    assert first == {"schedule_rewritten": True, "history_rows": 1, "events_rewritten": True}
    assert second == {"schedule_rewritten": False, "history_rows": 0, "events_rewritten": False}
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

    assert result == {"schedule_rewritten": True, "history_rows": 1, "events_rewritten": False}
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
    assert recovered == {"schedule_rewritten": True, "history_rows": 1, "events_rewritten": True}


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


async def test_a_long_backlog_goes_out_in_batches_in_order(session):
    total = sheets_sync.HISTORY_BATCH * 2 + 7
    session.add_all(
        models.ScheduleChange(
            band_name="Rockers",
            action="created",
            after={"date": "2026-01-05", "start": "18:00", "end": "19:00", "purpose": "band", "song_title": None, "status": "confirmed"},
        )
        for _ in range(total)
    )
    await session.commit()
    backend = FakeBackend()

    summaries = [await sheets_sync.sync_once(session, backend) for _ in range(4)]

    assert [len(batch) for batch in backend.history] == [sheets_sync.HISTORY_BATCH, sheets_sync.HISTORY_BATCH, 7]
    assert [summary["history_rows"] for summary in summaries] == [sheets_sync.HISTORY_BATCH, sheets_sync.HISTORY_BATCH, 7, 0]
    ids = [row[-1] for batch in backend.history for row in batch]
    assert ids == sorted(ids) and len(set(ids)) == total  # nothing skipped, nothing sent twice


def _fast_worker(monkeypatch, poll=0.01) -> list[dict]:
    """Shrink the worker's waits; returns the summaries of the rounds that sent history.

    After such a round the worker parks, idle, so that the test cancels it between two rounds and not in the middle
    of a query on the shared in-memory connection.
    """
    monkeypatch.setattr(sheets_sync, "POLL_SECONDS", poll)
    monkeypatch.setattr(sheets_sync, "DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(sheets_sync, "RETRY_SECONDS", 0.01)
    finished: list[dict] = []
    real = sheets_sync.sync_once

    async def counting(session, backend):
        summary = await real(session, backend)
        if summary["history_rows"]:
            finished.append(summary)
            await asyncio.Event().wait()
        return summary

    monkeypatch.setattr(sheets_sync, "sync_once", counting)
    return finished


async def _until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the sync worker"
        await asyncio.sleep(0.01)


async def test_the_worker_keeps_retrying_through_an_outage_and_catches_up(session, make_user, make_band, monkeypatch):
    await _book(session, make_user, make_band)
    finished = _fast_worker(monkeypatch)
    backend = FakeBackend()
    backend.fail = True
    task = asyncio.create_task(sheets_sync.sync_worker(async_sessionmaker(session.bind, expire_on_commit=False), backend))
    try:
        await _until(lambda: backend.attempts >= 3)  # it failed, waited and tried again, without dying
        assert not task.done() and backend.history == []
        backend.fail = False
        await _until(lambda: any(summary["history_rows"] == 1 for summary in finished))
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    change = (await session.execute(select(models.ScheduleChange))).scalar_one()
    assert change.sheets_synced_at is not None


async def test_a_change_request_wakes_the_worker_before_the_next_poll(session, make_user, make_band, monkeypatch):
    monkeypatch.setattr(settings, "google_sheets_id", "abc")
    monkeypatch.setattr(settings, "google_service_account_file", "secrets/key.json")
    finished = _fast_worker(monkeypatch, poll=60)
    backend = FakeBackend()
    task = asyncio.create_task(sheets_sync.sync_worker(async_sessionmaker(session.bind, expire_on_commit=False), backend))
    try:
        await asyncio.sleep(0.05)  # the worker is now waiting for a wake-up
        await _book(session, make_user, make_band)
        sheets_sync.request_sync()
        await _until(lambda: any(summary["history_rows"] == 1 for summary in finished))
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- the concerts sheet ----------------------------------------------------------------------------------------------


async def _concert(session, admin, title="Autumn", days=10, **extra):
    from app.services import events

    day = (now_local() + timedelta(days=days)).date()
    return await events.create_event(session, admin.telegram_id, title=title, event_date=day, submission_deadline=None, **extra)


async def _apply(session, event, make_user, make_band, band_name="Rockers", song="Song"):
    from app.services import events

    owner = await make_user(last_name="Петров", first_name="Иван", telegram_username="petrov")
    band = await make_band(owner, band_name)
    member_id = (await session.execute(select(models.BandMember.id).where(models.BandMember.band_id == band.id))).scalar_one()
    return await events.submit_application(session, event.id, owner.telegram_id, band.id, song, [member_id])


async def test_the_concerts_sheet_has_a_row_per_member_and_one_for_an_empty_concert(session, make_user, make_band):
    admin = await make_user(role="admin")
    full = await _concert(session, admin, "Autumn", days=10, location="Hall")
    empty = await _concert(session, admin, "Winter", days=20)
    application = await _apply(session, full, make_user, make_band)
    backend = FakeBackend()

    summary = await sheets_sync.sync_once(session, backend)

    header, first, second = backend.events[0]
    assert summary["events_rewritten"] is True and header == sheets_sync.EVENTS_HEADER
    day = (now_local() + timedelta(days=10)).date().isoformat()
    assert first[:5] == ["Autumn", day, "Hall", "Rockers", "Song"]
    assert first[5:10] == ["Петров", "Иван", "SE-2401", first[8], "@petrov"] and first[10:] == [full.id, application.id]
    assert second[0] == "Winter" and second[3:10] == [""] * 7 and second[10:] == [empty.id, ""]


async def test_the_concerts_sheet_is_rewritten_only_when_something_changed(session, make_user, make_band):
    from app.services import events

    admin = await make_user(role="admin")
    event = await _concert(session, admin)
    backend = FakeBackend()
    await sheets_sync.sync_once(session, backend)
    assert (await sheets_sync.sync_once(session, backend))["events_rewritten"] is False

    application = await _apply(session, event, make_user, make_band)
    assert (await sheets_sync.sync_once(session, backend))["events_rewritten"] is True
    owner_id = (await session.execute(select(models.User.telegram_id).where(models.User.telegram_username == "petrov"))).scalar_one()
    await events.update_application(session, application.id, owner_id, song_title="Other song")
    assert (await sheets_sync.sync_once(session, backend))["events_rewritten"] is True
    await events.delete_application(session, owner_id, application.id)
    await sheets_sync.sync_once(session, backend)

    assert len(backend.events) == 4 and len(backend.events[-1]) == 2  # header + the concert without applications


async def test_long_past_concerts_leave_the_sheet(session, make_user):
    admin = await make_user(role="admin")
    await _concert(session, admin, "Ancient", days=-(sheets_sync.EVENTS_PAST_DAYS + 5))
    await _concert(session, admin, "Recent", days=-5)
    backend = FakeBackend()

    await sheets_sync.sync_once(session, backend)

    assert [row[0] for row in backend.events[0][1:]] == ["Recent"]


async def test_a_failing_concerts_sheet_does_not_hold_the_history_back(session, make_user, make_band):
    await _book(session, make_user, make_band)
    backend = FakeBackend()
    original = backend.replace_events

    def broken(rows):
        raise ConnectionError("sheet is gone")

    backend.replace_events = broken
    with pytest.raises(ConnectionError):
        await sheets_sync.sync_once(session, backend)
    assert len(backend.history) == 1  # the history went out first and is marked as synced

    backend.replace_events = original
    recovered = await sheets_sync.sync_once(session, backend)
    assert recovered == {"schedule_rewritten": False, "history_rows": 0, "events_rewritten": True}


@pytest.mark.parametrize("change", ["create", "update", "submit", "update_application", "delete_application"])
async def test_every_concert_change_asks_for_a_sync(session, make_user, make_band, monkeypatch, change):
    from app.services import events

    admin = await make_user(role="admin")
    event = await _concert(session, admin)
    application = await _apply(session, event, make_user, make_band)
    owner_id = (await session.execute(select(models.User.telegram_id).where(models.User.telegram_username == "petrov"))).scalar_one()
    calls = []
    monkeypatch.setattr(sheets_sync, "request_sync", lambda: calls.append(change))

    if change == "create":
        await _concert(session, admin, "Another")
    elif change == "update":
        await events.update_event(session, event.id, admin.telegram_id, title="Renamed")
    elif change == "submit":
        member_id = (await session.execute(select(models.BandMember.id).where(models.BandMember.band_id == application.band_id))).scalar_one()
        await events.submit_application(session, event.id, owner_id, application.band_id, "Again", [member_id])
    elif change == "update_application":
        await events.update_application(session, application.id, owner_id, song_title="Other")
    else:
        await events.delete_application(session, owner_id, application.id)

    assert calls == [change]
