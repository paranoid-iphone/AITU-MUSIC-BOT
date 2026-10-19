"""Put the bookings of the Google Sheets backup back into the database.

    python -m app.tools.restore_from_sheets [--csv FILE] [--apply]

Reads the "Расписание" sheet (or a CSV export of it) and adds the future confirmed bookings the database does not have.
Users and bands come from the database (restore a pg_dump first); a row whose band or author is unknown is reported and
skipped. The tool only adds: a booking that differs from its row, or one that is missing from the sheet, is reported
and left to an admin. Without --apply nothing is written.
"""
import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_local
from app.db.models import Band, Booking, BookingStatus, User
from app.services import bookings as booking_service
from app.services import outbox, sheets_sync
from app.services.schedule_history import record_change

RESTORE_REASON = "восстановлено из Google Sheets"
RESTORE = "restore"
EXISTS = "exists"
CONFLICT = "conflict"
SKIP = "skip"

PURPOSE_KEYS = {label: key for key, label in sheets_sync.PURPOSES.items()}
SOURCE_KEYS = {label: key for key, label in sheets_sync.SOURCES.items()}
REQUIRED_COLUMNS = ["Дата", "Начало", "Конец", "Цель", "Источник", "ID брони", "ID номера", "ID автора"]
SONG_COLUMN = "Песня / название"


@dataclass
class SheetRow:
    line: int
    booking_id: int | None
    band_id: int | None
    author_id: int | None
    booking_date: date
    start: time
    end: time
    purpose: str
    song_title: str | None
    source: str


@dataclass
class RowResult:
    row: SheetRow
    kind: str
    message: str = ""
    author_id: int | None = None  # who the restored booking is attributed to


def _parse_time(value: str) -> time:
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), pattern).time()
        except ValueError:
            continue
    raise ValueError(f"время «{value}»")


def _parse_id(value: str, column: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{column} «{value}» не число") from None


def _label_to_key(value: str, keys: dict[str, str], known: dict[str, str], column: str) -> str:
    value = value.strip()
    if value in keys:
        return keys[value]
    if value in known:  # the raw key, e.g. from a hand-edited sheet
        return value
    raise ValueError(f"{column} «{value}» неизвестна")


def parse_rows(values: list[list[str]]) -> tuple[list[SheetRow], list[str]]:
    """Turn the sheet into rows. A broken row is reported in `errors` and left out; a missing column is fatal."""
    if not values:
        raise ValueError("Лист «Расписание» пуст")
    header = [cell.strip() for cell in values[0]]
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValueError("В листе нет столбцов: " + ", ".join(missing))
    index = {name: header.index(name) for name in REQUIRED_COLUMNS}
    song_index = header.index(SONG_COLUMN) if SONG_COLUMN in header else None

    rows: list[SheetRow] = []
    errors: list[str] = []
    seen_ids: set[int] = set()
    for offset, raw in enumerate(values[1:], start=2):
        cells = [str(cell) for cell in raw] + [""] * (len(header) - len(raw))
        if not any(cell.strip() for cell in cells):
            continue
        try:
            booking_date = date.fromisoformat(cells[index["Дата"]].strip())
            start = _parse_time(cells[index["Начало"]])
            end = _parse_time(cells[index["Конец"]])
            if end <= start:
                raise ValueError("конец не позже начала")
            purpose = _label_to_key(cells[index["Цель"]], PURPOSE_KEYS, sheets_sync.PURPOSES, "цель")
            source = _label_to_key(cells[index["Источник"]], SOURCE_KEYS, sheets_sync.SOURCES, "источник")
            booking_id = _parse_id(cells[index["ID брони"]], "ID брони")
            band_id = _parse_id(cells[index["ID номера"]], "ID номера")
            author_id = _parse_id(cells[index["ID автора"]], "ID автора")
            if booking_id is not None and booking_id in seen_ids:
                raise ValueError(f"ID брони {booking_id} уже был выше")
        except ValueError as exc:
            errors.append(f"строка {offset}: {exc}")
            continue
        if booking_id is not None:
            seen_ids.add(booking_id)
        song = cells[song_index].strip() if song_index is not None else ""
        rows.append(SheetRow(offset, booking_id, band_id, author_id, booking_date, start, end, purpose, song or None, source))
    return rows, errors


def read_csv(path: str) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return [list(line) for line in csv.reader(handle)]


def _slot(booking_date: date, start: time, end: time) -> str:
    return f"{booking_date.strftime('%d.%m')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _differences(row: SheetRow, booking: Booking) -> list[str]:
    found = []
    if booking.status != BookingStatus.CONFIRMED.value:
        found.append(f"в БД статус «{booking.status}»")
    if (booking.booking_date, booking.start_time.replace(second=0), booking.end_time.replace(second=0)) != (
        row.booking_date,
        row.start,
        row.end,
    ):
        found.append(f"в БД {_slot(booking.booking_date, booking.start_time, booking.end_time)}, в таблице {_slot(row.booking_date, row.start, row.end)}")
    if booking.band_id != row.band_id:
        found.append(f"в БД номер {booking.band_id}, в таблице {row.band_id}")
    return found


async def plan_restore(session: AsyncSession, rows: list[SheetRow], now: datetime) -> list[RowResult]:
    """Decide what to do with every row. Reads only."""
    ids = [row.booking_id for row in rows if row.booking_id is not None]
    existing: dict[int, Booking] = {}
    if ids:
        existing = {item.id: item for item in (await session.execute(select(Booking).where(Booking.id.in_(ids)))).scalars()}
    bands = {item.id: item for item in (await session.execute(select(Band))).scalars()}
    user_ids = set((await session.execute(select(User.id))).scalars())

    occupied: dict[date, list[tuple[time, time, str]]] = {}
    if rows:
        confirmed = await session.execute(
            select(Booking).where(
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.booking_date >= min(row.booking_date for row in rows),
                Booking.booking_date <= max(row.booking_date for row in rows),
            )
        )
        for item in confirmed.scalars():
            occupied.setdefault(item.booking_date, []).append((item.start_time, item.end_time, f"бронью #{item.id}"))

    results: list[RowResult] = []
    for row in rows:
        current = existing.get(row.booking_id) if row.booking_id is not None else None
        if current is not None:
            differences = _differences(row, current)
            results.append(RowResult(row, CONFLICT, "; ".join(differences)) if differences else RowResult(row, EXISTS))
            continue
        if datetime.combine(row.booking_date, row.end) <= now:
            results.append(RowResult(row, SKIP, "бронь уже прошла"))
            continue
        band = None
        if row.band_id is not None:
            band = bands.get(row.band_id)
            if band is None:
                results.append(RowResult(row, SKIP, f"номера {row.band_id} нет в БД"))
                continue
        author_id, note = row.author_id, ""
        if author_id is None or author_id not in user_ids:
            if band is None:
                results.append(RowResult(row, SKIP, f"автора {row.author_id} нет в БД"))
                continue
            author_id = band.responsible_user_id or band.creator_id
            note = f"автор {row.author_id} не найден, поставлен ответственный номера"
        clash = next(
            (label for start, end, label in occupied.get(row.booking_date, []) if booking_service._overlaps(row.start, row.end, start, end)),
            None,
        )
        if clash is not None:
            results.append(RowResult(row, SKIP, f"слот занят {clash}"))
            continue
        occupied.setdefault(row.booking_date, []).append((row.start, row.end, f"строкой {row.line} таблицы"))
        results.append(RowResult(row, RESTORE, note, author_id))
    return results


async def apply_restore(session: AsyncSession, rows: list[SheetRow], now: datetime) -> list[RowResult]:
    """Add the missing bookings in one transaction; the plan is made again under the booking lock."""
    await booking_service._lock_bookings(session)
    results = await plan_restore(session, rows, now)
    todo = [item for item in results if item.kind == RESTORE]
    keyed = [item for item in todo if item.row.booking_id is not None]
    keyless = [item for item in todo if item.row.booking_id is None]
    for batch in (keyed, keyless):
        for item in batch:
            row = item.row
            booking = Booking(
                id=row.booking_id,
                band_id=row.band_id,
                created_by_id=item.author_id,
                booking_date=row.booking_date,
                start_time=row.start,
                end_time=row.end,
                purpose=row.purpose,
                song_title=row.song_title,
                source=row.source,
                status=BookingStatus.CONFIRMED.value,
            )
            session.add(booking)
            await session.flush()
            await record_change(session, booking, "restored", None, reason=RESTORE_REASON, notify=False)
        if batch is keyed and keyed and session.get_bind().dialect.name == "postgresql":
            # explicit ids do not move the sequence: without this the next ordinary booking would collide
            await session.execute(
                text("SELECT setval(pg_get_serial_sequence('bookings', 'id'), (SELECT MAX(id) FROM bookings))")
            )
    await outbox.commit_and_publish(session)
    return results


async def missing_from_sheet(session: AsyncSession, rows: list[SheetRow], now: datetime) -> list[Booking]:
    """Future confirmed bookings inside the sheet's date range that the sheet does not list."""
    if not rows:
        return []
    listed = {row.booking_id for row in rows if row.booking_id is not None}
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= now.date(),
            Booking.booking_date <= max(row.booking_date for row in rows),
        )
        .order_by(Booking.booking_date, Booking.start_time)
    )
    return [
        item
        for item in result.scalars()
        if item.id not in listed and datetime.combine(item.booking_date, item.end_time) > now
    ]


def format_report(results: list[RowResult], errors: list[str], extra: list[Booking]) -> str:
    titles = {
        RESTORE: "Будут восстановлены",
        CONFLICT: "Расхождения с БД (не трогаю, исправить в Mini App)",
        SKIP: "Пропущены",
    }
    lines: list[str] = []
    for kind, title in titles.items():
        group = [item for item in results if item.kind == kind]
        if not group:
            continue
        lines.append(f"{title}:")
        for item in group:
            row = item.row
            tail = f" — {item.message}" if item.message else ""
            lines.append(f"  строка {row.line}: {_slot(row.booking_date, row.start, row.end)} (бронь #{row.booking_id or 'новая'}){tail}")
    if errors:
        lines.append("Не удалось разобрать:")
        lines.extend(f"  {error}" for error in errors)
    if extra:
        lines.append("Есть в БД, но нет в таблице (не трогаю):")
        lines.extend(
            f"  бронь #{item.id}: {_slot(item.booking_date, item.start_time, item.end_time)}" for item in extra
        )
    counts = {kind: sum(1 for item in results if item.kind == kind) for kind in (RESTORE, EXISTS, CONFLICT, SKIP)}
    lines.append(
        f"Итого: восстановить {counts[RESTORE]}, уже есть {counts[EXISTS]}, "
        f"расхождений {counts[CONFLICT]}, пропущено {counts[SKIP]}, ошибок разбора {len(errors)}."
    )
    return "\n".join(lines)


async def run(values: list[list[str]], apply: bool) -> str:
    from app.db.session import AsyncSessionLocal, engine, prepare_database

    rows, errors = parse_rows(values)
    now = now_local()
    await prepare_database()
    try:
        async with AsyncSessionLocal() as session:
            if apply:
                results = await apply_restore(session, rows, now)
            else:
                results = await plan_restore(session, rows, now)
            extra = await missing_from_sheet(session, rows, now)
    finally:
        await engine.dispose()
    report = format_report(results, errors, extra)
    done = sum(1 for item in results if item.kind == RESTORE)
    return report + (f"\nЗаписано броней: {done}." if apply else "\nDry-run: ничего не записано. Для записи добавьте --apply.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore future bookings from the Google Sheets backup.")
    parser.add_argument("--csv", metavar="FILE", help="CSV export of the «Расписание» sheet instead of reading Google")
    parser.add_argument("--apply", action="store_true", help="write the bookings (default: only show what would be done)")
    args = parser.parse_args(argv)

    try:
        if args.csv:
            values = read_csv(args.csv)
        elif sheets_sync.is_enabled():
            values = sheets_sync.GspreadBackend().read_schedule()
        else:
            print("GOOGLE_SHEETS_ID и GOOGLE_SERVICE_ACCOUNT_FILE не заданы: укажите --csv FILE.", file=sys.stderr)
            return 2
        print(asyncio.run(run(values, args.apply)))
    except (OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Google unreachable, bad credentials, database down
        print(f"Не удалось выполнить: {exc!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
