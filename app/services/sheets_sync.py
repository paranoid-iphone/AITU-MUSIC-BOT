"""One-way backup of the schedule to Google Sheets (bot -> sheet).

If the server goes down, the sheet still holds the current and next week's schedule plus the full change history.

- Sheet "Расписание": rewritten whenever the schedule changes (current + next week, confirmed bookings).
- Sheet "История": every schedule change is appended exactly once (marked with `sheets_synced_at`).

Writes are debounced (a burst of changes becomes one write) and retried every minute if Google was unreachable.
Without GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_FILE the whole feature is off.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.timezone import now_local
from app.db.models import Booking, BookingStatus, ScheduleChange
from app.services.bookings import _band_info, _booking_load_options, _booking_owner_label
from app.services.schedule_history import ACTION_TITLES, describe_slot, person_name
from app.services.weeks import week_start_of

logger = logging.getLogger(__name__)

SCHEDULE_SHEET = "Расписание"
HISTORY_SHEET = "История"
SCHEDULE_HEADER = [
    "Дата", "День", "Начало", "Конец", "Номер", "Направление", "Ответственный", "Telegram ответственного",
    "Цель", "Песня / название", "Источник", "ID брони",
]
HISTORY_HEADER = ["Время", "Кто", "Действие", "Номер", "Было", "Стало", "Причина", "ID брони", "ID записи"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
PURPOSES = {"concert": "к концерту", "self": "для себя", "personal": "личная", "staff": "стафф / ДСВР"}
SOURCES = {"manual": "вручную", "auto": "авто-распределение", "dsvr": "ДСВР", "staff": "админ"}
SCHEDULE_WEEKS = 2
HISTORY_BATCH = 500
DEBOUNCE_SECONDS = 5
RETRY_SECONDS = 30
POLL_SECONDS = 60
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsBackend(Protocol):
    def replace_schedule(self, rows: list[list]) -> None: ...

    def append_history(self, rows: list[list]) -> None: ...


def is_enabled() -> bool:
    return bool(settings.google_sheets_id and settings.google_service_account_file)


def build_schedule_rows(bookings: list[Booking]) -> list[list]:
    rows: list[list] = [SCHEDULE_HEADER]
    for booking in sorted(bookings, key=lambda item: (item.booking_date, item.start_time, item.id)):
        info = _band_info(booking)
        username = booking.band.responsible.telegram_username if booking.band is not None and booking.band.responsible else None
        rows.append(
            [
                booking.booking_date.isoformat(),
                WEEKDAYS[booking.booking_date.weekday()],
                booking.start_time.strftime("%H:%M"),
                booking.end_time.strftime("%H:%M"),
                _booking_owner_label(booking),
                info["group_title"] or "",
                info["responsible_name"] or "",
                f"@{username}" if username else "",
                PURPOSES.get(booking.purpose, booking.purpose),
                booking.song_title or "",
                SOURCES.get(booking.source, booking.source),
                booking.id,
            ]
        )
    return rows


def build_history_rows(changes: list[ScheduleChange]) -> list[list]:
    zone = ZoneInfo(settings.timezone)
    rows = []
    for change in changes:
        stamp = change.created_at.replace(tzinfo=timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            [
                stamp,
                person_name(change.actor),
                ACTION_TITLES.get(change.action, change.action),
                change.band_name or "",
                describe_slot(change.before) if change.before else "",
                describe_slot(change.after) if change.after else "",
                change.reason or "",
                change.booking_id or "",
                change.id,
            ]
        )
    return rows


class GspreadBackend:
    """Real Google Sheets writer. All methods are blocking: call them through asyncio.to_thread."""

    def __init__(self) -> None:
        self._spreadsheet = None

    def _open(self):
        if self._spreadsheet is None:
            import gspread
            from google.oauth2.service_account import Credentials

            credentials = Credentials.from_service_account_file(settings.google_service_account_file, scopes=SCOPES)
            self._spreadsheet = gspread.authorize(credentials).open_by_key(settings.google_sheets_id)
        return self._spreadsheet

    def _worksheet(self, title: str, columns: int):
        import gspread

        spreadsheet = self._open()
        try:
            return spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(title, rows=1000, cols=columns)

    def replace_schedule(self, rows: list[list]) -> None:
        try:
            worksheet = self._worksheet(SCHEDULE_SHEET, len(SCHEDULE_HEADER))
            worksheet.update(values=rows, range_name="A1")
            worksheet.batch_clear([f"A{len(rows) + 1}:Z5000"])  # drop leftovers of a longer previous version
        except Exception:
            self._spreadsheet = None  # reconnect next time
            raise

    def append_history(self, rows: list[list]) -> None:
        try:
            worksheet = self._worksheet(HISTORY_SHEET, len(HISTORY_HEADER))
            if not worksheet.get_values("A1:A1"):
                worksheet.update(values=[HISTORY_HEADER], range_name="A1")
            worksheet.append_rows(rows, value_input_option="RAW")
        except Exception:
            self._spreadsheet = None
            raise


class _State:
    schedule_digest: str | None = None
    wakeup: asyncio.Event | None = None


_state = _State()


def _digest(rows: list[list]) -> str:
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


async def sync_once(session: AsyncSession, backend: SheetsBackend) -> dict:
    """Push what changed. Raises if Google is unreachable; unsynced history stays unsynced and is retried."""
    first_day = week_start_of(now_local().date())
    last_day = first_day + timedelta(days=7 * SCHEDULE_WEEKS - 1)
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= first_day,
            Booking.booking_date <= last_day,
        )
        .options(*_booking_load_options())
    )
    schedule_rows = build_schedule_rows(list(result.scalars().unique()))
    summary = {"schedule_rewritten": False, "history_rows": 0}
    digest = _digest(schedule_rows)
    if digest != _state.schedule_digest:
        await asyncio.to_thread(backend.replace_schedule, schedule_rows)
        _state.schedule_digest = digest
        summary["schedule_rewritten"] = True

    pending = list(
        (
            await session.execute(
                select(ScheduleChange)
                .where(ScheduleChange.sheets_synced_at.is_(None))
                .options(selectinload(ScheduleChange.actor))
                .order_by(ScheduleChange.id)
                .limit(HISTORY_BATCH)
            )
        ).scalars()
    )
    if pending:
        await asyncio.to_thread(backend.append_history, build_history_rows(pending))
        now = datetime.utcnow()
        for change in pending:
            change.sheets_synced_at = now
        await session.commit()
        summary["history_rows"] = len(pending)
    return summary


def _wakeup() -> asyncio.Event:
    if _state.wakeup is None:
        _state.wakeup = asyncio.Event()
    return _state.wakeup


def request_sync() -> None:
    """Called after every committed schedule change; wakes the background worker."""
    if is_enabled():
        _wakeup().set()


async def sync_worker(session_factory: async_sessionmaker[AsyncSession], backend: SheetsBackend | None = None) -> None:
    backend = backend or GspreadBackend()
    logger.info("Google Sheets sync started")
    while True:
        try:
            try:
                await asyncio.wait_for(_wakeup().wait(), timeout=POLL_SECONDS)
                await asyncio.sleep(DEBOUNCE_SECONDS)  # let a burst of changes finish
            except asyncio.TimeoutError:
                pass  # periodic safety net: also retries after a failed attempt
            _wakeup().clear()
            async with session_factory() as session:
                summary = await sync_once(session, backend)
            if summary["schedule_rewritten"] or summary["history_rows"]:
                logger.info("Google Sheets synced: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Google Sheets sync failed; will retry")
            await asyncio.sleep(RETRY_SECONDS)
