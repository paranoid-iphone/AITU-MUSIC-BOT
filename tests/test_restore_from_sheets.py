import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.core.timezone import now_local
from app.db import models
from app.services import bookings, sheets_sync
from app.tools import restore_from_sheets as restore

HEADER = sheets_sync.SCHEDULE_HEADER


async def _sheet_values(session) -> list[list[str]]:
    """The schedule sheet as gspread returns it: every cell a string."""
    result = await session.execute(
        select(models.Booking)
        .where(models.Booking.status == models.BookingStatus.CONFIRMED.value)
        .options(*bookings._booking_load_options())
    )
    rows = sheets_sync.build_schedule_rows(list(result.scalars().unique()))
    return [[str(cell) for cell in row] for row in rows]


async def _three_bookings(session, make_user, make_band):
    """A band booking, a personal one and a staff one on three different days. Returns the ids."""
    leader = await make_user(last_name="Петров", first_name="Иван")
    admin = await make_user(role="admin")
    band = await make_band(leader, "Rockers")
    today = now_local().date()
    first = await bookings.create_booking(session, leader.telegram_id, band.id, today + timedelta(days=1), time(15), time(16), "concert", "Song")
    second = await bookings.create_booking(session, leader.telegram_id, None, today + timedelta(days=2), time(12), time(13), "personal", "Solo")
    third = await bookings.create_staff_booking(session, admin.telegram_id, today + timedelta(days=3), time(17), time(18), "Закрыто")
    return [first.id, second.id, third.id], band


async def _wipe_bookings(session):
    await session.execute(delete(models.ScheduleChange))
    await session.execute(delete(models.Booking))
    await session.commit()


async def _restore(session, values, apply=False):
    rows, errors = restore.parse_rows(values)
    now = now_local()
    results = await (restore.apply_restore if apply else restore.plan_restore)(session, rows, now)
    return results, errors


def _kinds(results):
    return [item.kind for item in results]


async def test_round_trip_restores_bookings_with_their_ids(session, make_user, make_band, published):
    ids, band = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    before = {
        item.id: (item.band_id, item.created_by_id, item.booking_date, item.start_time, item.end_time, item.purpose, item.song_title, item.source)
        for item in (await session.execute(select(models.Booking))).scalars()
    }
    await _wipe_bookings(session)
    published.clear()

    results, errors = await _restore(session, values)
    assert errors == []
    assert _kinds(results) == [restore.RESTORE] * 3
    assert (await session.execute(select(models.Booking))).first() is None  # dry-run wrote nothing

    results, _ = await _restore(session, values, apply=True)
    assert _kinds(results) == [restore.RESTORE] * 3
    after = {
        item.id: (item.band_id, item.created_by_id, item.booking_date, item.start_time, item.end_time, item.purpose, item.song_title, item.source)
        for item in (await session.execute(select(models.Booking))).scalars()
    }
    assert after == before and sorted(after) == sorted(ids)
    changes = (await session.execute(select(models.ScheduleChange))).scalars().all()
    assert {item.action for item in changes} == {"restored"} and len(changes) == 3
    assert all(item.reason == restore.RESTORE_REASON and item.actor_id is None for item in changes)
    assert {item["type"] for item in published} == {"schedule_changed"}  # nobody is notified


async def test_second_run_finds_everything_present(session, make_user, make_band):
    await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    results, _ = await _restore(session, values, apply=True)
    assert _kinds(results) == [restore.EXISTS] * 3
    assert (await session.execute(select(models.ScheduleChange).where(models.ScheduleChange.action == "restored"))).first() is None


async def test_second_apply_after_restore_adds_nothing(session, make_user, make_band):
    await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    await _restore(session, values, apply=True)
    results, _ = await _restore(session, values, apply=True)
    assert _kinds(results) == [restore.EXISTS] * 3
    assert len((await session.execute(select(models.Booking))).scalars().all()) == 3


async def test_differing_or_cancelled_bookings_are_reported_not_changed(session, make_user, make_band):
    ids, _ = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    moved = await session.get(models.Booking, ids[0])
    moved.start_time, moved.end_time = time(16), time(17)
    cancelled = await session.get(models.Booking, ids[1])
    cancelled.status = models.BookingStatus.CANCELLED.value
    await session.commit()

    results, _ = await _restore(session, values, apply=True)
    assert _kinds(results) == [restore.CONFLICT, restore.CONFLICT, restore.EXISTS]
    assert "в БД" in results[0].message and "статус" in results[1].message
    assert (await session.get(models.Booking, ids[0])).start_time == time(16)
    assert (await session.get(models.Booking, ids[1])).status == models.BookingStatus.CANCELLED.value


async def test_slot_taken_by_another_booking_is_skipped(session, make_user, make_band):
    ids, _ = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    other = await make_user()
    taken = values[1]
    session.add(
        models.Booking(
            id=500,
            created_by_id=other.id,
            booking_date=datetime.strptime(taken[0], "%Y-%m-%d").date(),
            start_time=time(15, 30),
            end_time=time(16, 30),
            purpose="personal",
        )
    )
    await session.commit()

    results, _ = await _restore(session, values, apply=True)
    assert _kinds(results) == [restore.SKIP, restore.RESTORE, restore.RESTORE]
    assert "слот занят" in results[0].message


async def test_two_sheet_rows_for_one_slot_restore_only_the_first(session, make_user, make_band):
    ids, _ = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    clone = list(values[1])
    clone[HEADER.index("ID брони")] = "999"
    clone[HEADER.index("Начало")] = "15:30"
    clone[HEADER.index("Конец")] = "16:30"

    results, _ = await _restore(session, values + [clone], apply=True)
    assert _kinds(results) == [restore.RESTORE] * 3 + [restore.SKIP]
    assert "таблицы" in results[3].message
    assert await session.get(models.Booking, 999) is None


async def test_past_booking_is_skipped(session, make_user, make_band):
    await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    past = list(values[1])
    past[0] = (now_local().date() - timedelta(days=2)).isoformat()
    results, _ = await _restore(session, [values[0], past])
    assert _kinds(results) == [restore.SKIP] and "прошла" in results[0].message


async def test_unknown_band_is_skipped(session, make_user, make_band):
    await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    row = list(values[1])
    row[HEADER.index("ID номера")] = "4242"
    results, _ = await _restore(session, [values[0], row])
    assert _kinds(results) == [restore.SKIP] and "4242" in results[0].message


async def test_unknown_author_falls_back_to_the_responsible_person_of_a_band(session, make_user, make_band):
    _, band = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    band_row = list(values[1])
    band_row[HEADER.index("ID автора")] = "99999"
    personal_row = list(values[2])
    personal_row[HEADER.index("ID автора")] = "99999"

    results, _ = await _restore(session, [values[0], band_row, personal_row], apply=True)
    assert _kinds(results) == [restore.RESTORE, restore.SKIP]
    assert "ответственный" in results[0].message and "автора" in results[1].message
    saved = await session.get(models.Booking, int(band_row[HEADER.index("ID брони")]))
    reloaded_band = await session.get(models.Band, band.id)
    assert saved.created_by_id == (reloaded_band.responsible_user_id or reloaded_band.creator_id)


async def test_row_without_a_booking_id_gets_a_new_one_and_is_not_duplicated(session, make_user, make_band):
    _, band = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    await _wipe_bookings(session)
    manual = list(values[1])
    manual[HEADER.index("ID брони")] = ""

    results, _ = await _restore(session, [values[0], manual], apply=True)
    assert _kinds(results) == [restore.RESTORE]
    assert len((await session.execute(select(models.Booking))).scalars().all()) == 1
    results, _ = await _restore(session, [values[0], manual], apply=True)
    assert _kinds(results) == [restore.SKIP] and "слот занят" in results[0].message


async def test_missing_from_sheet_lists_bookings_the_sheet_lacks(session, make_user, make_band):
    ids, _ = await _three_bookings(session, make_user, make_band)
    values = await _sheet_values(session)
    rows, _ = restore.parse_rows([values[0], values[3]])  # the sheet knows only the last booking
    extra = await restore.missing_from_sheet(session, rows, now_local())
    assert [item.id for item in extra] == ids[:2] and ids[2] not in [item.id for item in extra]
    report = restore.format_report([], [], extra)
    assert f"бронь #{ids[0]}" in report


def test_parse_rows_reads_columns_by_header_in_any_order():
    shuffled = list(reversed(HEADER))
    row = {"Дата": "2026-10-21", "Начало": "15:00", "Конец": "16:30", "Цель": "к концерту", "Источник": "вручную",
           "ID брони": "12", "ID номера": "3", "ID автора": "7", "Песня / название": "Song"}
    rows, errors = restore.parse_rows([shuffled, [row.get(name, "") for name in shuffled]])
    assert errors == []
    parsed = rows[0]
    assert (parsed.line, parsed.booking_id, parsed.band_id, parsed.author_id) == (2, 12, 3, 7)
    assert (parsed.start, parsed.end, parsed.purpose, parsed.source, parsed.song_title) == (time(15), time(16, 30), "concert", "manual", "Song")


def test_parse_rows_reports_broken_rows_with_their_line_and_keeps_the_good_ones():
    good = ["2026-10-21", "Ср", "15:00", "16:00", "Rockers", "", "", "", "для себя", "", "вручную", "1", "2", "3"]
    bad_time = list(good)
    bad_time[2] = "позже"
    bad_purpose = list(good)
    bad_purpose[8] = "праздник"
    backwards = list(good)
    backwards[2], backwards[3] = "16:00", "15:00"
    repeated = list(good)
    blank = [""] * len(HEADER)

    rows, errors = restore.parse_rows([HEADER, good, bad_time, bad_purpose, backwards, repeated, blank])
    assert len(rows) == 1 and rows[0].line == 2
    assert [error.split(":")[0] for error in errors] == ["строка 3", "строка 4", "строка 5", "строка 6"]


def test_parse_rows_needs_the_id_columns():
    old = [name for name in HEADER if name != "ID брони"]
    with pytest.raises(ValueError, match="ID брони"):
        restore.parse_rows([old])
    with pytest.raises(ValueError):
        restore.parse_rows([])


def test_csv_export_with_a_bom_is_read(tmp_path: Path):
    path = tmp_path / "schedule.csv"
    line = ["2026-10-21", "Ср", "15:00", "16:00", "Rockers", "", "", "", "для себя", "", "вручную", "1", "2", "3"]
    path.write_text("\n".join(",".join(row) for row in (HEADER, line)), encoding="utf-8-sig")
    rows, errors = restore.parse_rows(restore.read_csv(str(path)))
    assert errors == [] and rows[0].booking_id == 1


def test_main_without_a_source_explains_what_to_do(monkeypatch, capsys):
    monkeypatch.setattr(sheets_sync, "is_enabled", lambda: False)
    assert restore.main([]) == 2
    assert "--csv" in capsys.readouterr().err


def test_main_reports_a_bad_file_as_an_error(tmp_path, capsys):
    path = tmp_path / "broken.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    assert restore.main(["--csv", str(path)]) == 2
    assert "нет столбцов" in capsys.readouterr().err


def test_restored_action_has_a_title_and_a_translation_everywhere():
    from app.services.schedule_history import ACTION_TITLES

    assert "restored" in ACTION_TITLES
    for lang in ("ru", "en", "kz"):
        messages = json.loads((Path("app/locales") / f"{lang}.json").read_text(encoding="utf-8"))
        assert messages["webapp.admin.history.action.restored"]
