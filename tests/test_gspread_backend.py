"""GspreadBackend against a stub Google client: the stub's methods are bound to the real gspread signatures,
so a call the library would reject (wrong keyword, missing argument) fails here too."""
import inspect
import itertools
import re
from types import SimpleNamespace

import gspread
import pytest
from google.oauth2 import service_account

from app.core.config import settings
from app.services import sheets_sync
from app.services.bookings import booking_owner_label


def _checked(real_method, record):
    """Wrap a stub so every call is validated against the real method signature."""
    signature = inspect.signature(real_method)

    def call(*args, **kwargs):
        signature.bind(None, *args, **kwargs)  # None stands for `self`
        record.append((real_method.__name__, args, kwargs))
        return []

    return call


_sheet_ids = itertools.count(100)


class StubWorksheet:
    def __init__(self, calls, has_header=True, row_count=1000):
        self.id = next(_sheet_ids)
        self.calls = calls
        self.has_header = has_header
        self.row_count = row_count
        self.rows: list[list] = []  # cell values, top to bottom, as Google would keep them
        self._resize = _checked(gspread.Worksheet.resize, calls)
        self._update = _checked(gspread.Worksheet.update, calls)
        self.batch_clear = _checked(gspread.Worksheet.batch_clear, calls)
        self._insert_rows = _checked(gspread.Worksheet.insert_rows, calls)
        self._get_values = _checked(gspread.Worksheet.get_values, calls)
        self.get_all_values = _checked(gspread.Worksheet.get_all_values, calls)

    def update(self, *args, **kwargs):
        self._update(*args, **kwargs)
        self.has_header = True  # like a real sheet: once written, cell A1 is no longer empty
        cell = re.fullmatch(r"([A-Z])(\d+)", kwargs["range_name"])
        if cell and kwargs["range_name"] != "A1":  # one cell somewhere else: the status line
            row, column = int(cell.group(2)) - 1, ord(cell.group(1)) - ord("A")
            while len(self.rows) <= row:
                self.rows.append([])
            self.rows[row] += [""] * (column + 1 - len(self.rows[row]))
            self.rows[row][column] = kwargs["values"][0][0]
            return
        self.rows[: len(kwargs["values"])] = kwargs["values"]  # the others write from A1

    def insert_rows(self, values, **kwargs):
        self._insert_rows(values, **kwargs)
        position = kwargs.get("row", 1) - 1
        self.rows[position:position] = values

    def get_values(self, *args, **kwargs):
        self._get_values(*args, **kwargs)
        return [["Время"]] if self.has_header else [[]]  # gspread 6 pads an empty range to [[]], not []

    def resize(self, *args, **kwargs):
        self._resize(*args, **kwargs)
        self.row_count = kwargs.get("rows", self.row_count)


class StubSpreadsheet:
    def __init__(self, calls, existing=()):
        self.calls = calls
        self.sheets = {title: StubWorksheet(calls) for title in existing}
        self.requests: list[dict] = []  # every batchUpdate request, in order
        self.rules: dict[int, list] = {}  # conditional format rules per sheet id, as Google would keep them
        self.protections: dict[int, list] = {}  # protected ranges per sheet id
        self.layout_failure: Exception | None = None
        self._add = _checked(gspread.Spreadsheet.add_worksheet, calls)
        self._batch_update = _checked(gspread.Spreadsheet.batch_update, calls)
        self._metadata = _checked(gspread.Spreadsheet.fetch_sheet_metadata, calls)
        self.update_timezone = _checked(gspread.Spreadsheet.update_timezone, calls)

    def batch_update(self, body):
        self._batch_update(body)
        if self.layout_failure is not None:
            raise self.layout_failure
        for request in body["requests"]:
            self.requests.append(request)
            if "deleteConditionalFormatRule" in request:
                target = request["deleteConditionalFormatRule"]
                self.rules[target["sheetId"]].pop(target["index"])
            elif "addConditionalFormatRule" in request:
                added = request["addConditionalFormatRule"]
                self.rules.setdefault(added["rule"]["ranges"][0]["sheetId"], []).insert(added["index"], added["rule"])
            elif "addProtectedRange" in request:
                protected = request["addProtectedRange"]["protectedRange"]
                protection_id = next(_sheet_ids)
                self.protections.setdefault(protected["range"]["sheetId"], []).append({**protected, "protectedRangeId": protection_id})
            elif "deleteProtectedRange" in request:
                gone = request["deleteProtectedRange"]["protectedRangeId"]
                self.protections = {
                    sheet: [item for item in items if item["protectedRangeId"] != gone] for sheet, items in self.protections.items()
                }

    def fetch_sheet_metadata(self, params=None):
        self._metadata(params)
        return {
            "sheets": [
                {
                    "properties": {"sheetId": sheet.id, "title": title},
                    "conditionalFormats": list(self.rules.get(sheet.id, [])),
                    "protectedRanges": list(self.protections.get(sheet.id, [])),
                }
                for title, sheet in self.sheets.items()
            ]
        }

    def worksheet(self, title):
        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self._add(title, rows, cols)
        self.sheets[title] = StubWorksheet(self.calls, has_header=False)
        return self.sheets[title]


@pytest.fixture
def backend(monkeypatch):
    calls: list = []
    spreadsheet = StubSpreadsheet(calls)

    class StubClient:
        def open_by_key(self, key):
            calls.append(("open_by_key", (key,), {}))
            return spreadsheet

    monkeypatch.setattr(settings, "google_sheets_id", "sheet-id")
    monkeypatch.setattr(settings, "google_service_account_file", "secrets/key.json")
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", classmethod(lambda cls, path, scopes: object()))
    monkeypatch.setattr(gspread, "authorize", lambda credentials: StubClient())
    instance = sheets_sync.GspreadBackend()
    instance.calls = calls
    instance.spreadsheet = spreadsheet
    return instance


def test_schedule_sheet_is_created_written_and_trailing_rows_cleared(backend):
    rows = [sheets_sync.SCHEDULE_HEADER, ["2026-10-21", "Ср"]]
    backend.replace_schedule(rows)

    names = [name for name, _, _ in backend.calls]
    assert names[:4] == ["open_by_key", "add_worksheet", "update", "batch_clear"]
    update = next(call for call in backend.calls if call[0] == "update")
    assert update[2] == {"values": rows, "range_name": "A1", "value_input_option": "RAW"}
    clear = next(call for call in backend.calls if call[0] == "batch_clear")
    assert clear[1] == (["A3:Z5000"],)


def test_schedule_sheet_grows_when_the_rows_do_not_fit(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    worksheet = backend.spreadsheet.sheets[sheets_sync.SCHEDULE_SHEET]
    worksheet.row_count = 3
    rows = [sheets_sync.SCHEDULE_HEADER] + [["2026-10-21", "Ср"]] * 5
    backend.replace_schedule(rows)

    resize = next(call for call in backend.calls if call[0] == "resize")
    assert resize[2] == {"rows": 6}
    assert worksheet.row_count == 6


def test_history_header_is_written_once_and_stays_first(backend):
    backend.append_history([["2026-10-16 17:00:00", "Петров Иван"]])
    backend.append_history([["2026-10-16 17:05:00", "Петров Иван"]])

    updates = [call for call in backend.calls if call[0] == "update"]
    assert len(updates) == 1 and updates[0][2]["values"] == [sheets_sync.HISTORY_HEADER]
    assert backend.spreadsheet.sheets[sheets_sync.HISTORY_SHEET].rows[0] == sheets_sync.HISTORY_HEADER


def test_the_newest_history_entry_is_on_top_across_batches(backend):
    backend.append_history([["10:00", "первая"], ["10:01", "вторая"]])  # a batch goes oldest first
    backend.append_history([["10:02", "третья"]])

    assert backend.spreadsheet.sheets[sheets_sync.HISTORY_SHEET].rows == [
        sheets_sync.HISTORY_HEADER,
        ["10:02", "третья"],
        ["10:01", "вторая"],
        ["10:00", "первая"],
    ]


def test_new_history_rows_do_not_take_the_look_of_the_header(backend):
    backend.append_history([["10:00", "первая"]])

    insert = next(call for call in backend.calls if call[0] == "insert_rows")
    assert insert[2]["value_input_option"] == "RAW"
    assert not insert[2].get("inherit_from_before")


def _history_rules(backend) -> list[dict]:
    sheet = backend.spreadsheet.sheets[sheets_sync.HISTORY_SHEET]
    return backend.spreadsheet.rules.get(sheet.id, [])


def _rule_formula(rule) -> str:
    return rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]


GREEN, YELLOW, RED, BLUE = "green", "yellow", "red", "blue"


@pytest.mark.parametrize(
    "action, colour",
    [
        ("создал(а) бронь", GREEN),
        ("изменил(а) бронь", YELLOW),
        ("отменил(а) бронь", RED),
        ("вытеснил(а) бронь (ДСВР)", RED),
        ("авто-распределение", BLUE),
        ("восстановил(а) бронь из Sheets", BLUE),
    ],
)
def test_each_history_action_has_its_colour(backend, action, colour):
    backend.append_history([["10:00", "Петров Иван", action]])

    matching = [rule for rule in _history_rules(backend) if f'$C1="{action}"' in _rule_formula(rule)]
    assert len(matching) == 1
    background = matching[0]["booleanRule"]["format"]["backgroundColor"]
    dominant = {GREEN: background["green"] > background["blue"] and background["green"] > background["red"],
                YELLOW: background["red"] > background["blue"] and background["green"] > background["blue"],
                RED: background["red"] > background["green"] and background["red"] > background["blue"],
                BLUE: background["blue"] > background["red"] and background["blue"] >= background["green"]}
    assert dominant[colour]


def test_history_colours_cover_the_whole_sheet_from_the_header(backend):
    """A range starting under the header could slide down as rows are inserted at its top edge."""
    backend.append_history([["10:00", "Петров Иван"]])

    for rule in _history_rules(backend):
        assert rule["ranges"][0]["startRowIndex"] == 0
        assert "endRowIndex" not in rule["ranges"][0]


def test_the_history_colour_formulas_work_in_any_spreadsheet_locale(backend):
    backend.append_history([["10:00", "Петров Иван"]])

    formulas = [_rule_formula(rule) for rule in _history_rules(backend)]
    assert formulas and not any("," in formula or ";" in formula for formula in formulas)


def test_the_id_columns_of_the_history_are_hidden_from_people(backend):
    backend.append_history([["10:00", "Петров Иван"]])

    # H, I: "ID брони", "ID записи"
    assert _hidden_columns(backend) == [(7, 9)]


def test_the_history_header_is_frozen(backend):
    backend.append_history([["10:00", "Петров Иван"]])

    sheet = backend.spreadsheet.sheets[sheets_sync.HISTORY_SHEET]
    frozen = [
        request["updateSheetProperties"]["properties"]
        for request in backend.spreadsheet.requests
        if "updateSheetProperties" in request
    ]
    assert frozen == [{"sheetId": sheet.id, "gridProperties": {"frozenRowCount": 1}}]


def test_the_history_layout_is_applied_once_however_many_batches_go_out(backend):
    backend.append_history([["10:00", "Петров Иван"]])
    backend.append_history([["10:01", "Петров Иван"]])

    assert len(_hidden_columns(backend)) == 1


def test_a_layout_failure_does_not_lose_history_and_is_retried(backend):
    backend.spreadsheet.layout_failure = ConnectionError("batchUpdate refused")

    backend.append_history([["10:00", "первая"]])  # must not raise

    worksheet = backend.spreadsheet.sheets[sheets_sync.HISTORY_SHEET]
    assert worksheet.rows == [sheets_sync.HISTORY_HEADER, ["10:00", "первая"]]
    assert _hidden_columns(backend) == []

    backend.spreadsheet.layout_failure = None
    backend.append_history([["10:01", "вторая"]])
    assert _hidden_columns(backend) == [(7, 9)]


def test_restarting_the_backend_does_not_pile_up_history_colour_rules(backend):
    backend.append_history([["10:00", "Петров Иван"]])
    after_first_start = len(_history_rules(backend))
    assert after_first_start > 0

    restarted = sheets_sync.GspreadBackend()  # a new process finds the rules of the previous one
    restarted.append_history([["10:01", "Петров Иван"]])

    assert len(_history_rules(backend)) == after_first_start


def test_connection_is_dropped_after_a_failure_so_it_reconnects(backend, monkeypatch):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    worksheet = backend.spreadsheet.sheets[sheets_sync.SCHEDULE_SHEET]

    def boom(*args, **kwargs):
        raise ConnectionError("network")

    worksheet.update = boom
    with pytest.raises(ConnectionError):
        backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    assert backend._spreadsheet is None


def test_schedule_is_read_back_from_its_sheet(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    assert backend.read_schedule() == []
    assert backend.calls[-1][0] == "get_all_values"


def test_reading_drops_the_connection_after_a_failure(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])

    def boom():
        raise ConnectionError("network")

    backend.spreadsheet.sheets[sheets_sync.SCHEDULE_SHEET].get_all_values = boom
    with pytest.raises(ConnectionError):
        backend.read_schedule()
    assert backend._spreadsheet is None


def _hidden_columns(backend) -> list[tuple[int, int]]:
    found = []
    for request in backend.spreadsheet.requests:
        update = request.get("updateDimensionProperties")
        if update and update["properties"].get("hiddenByUser"):
            found.append((update["range"]["startIndex"], update["range"]["endIndex"]))
    return found


def test_the_id_columns_of_the_schedule_are_hidden_from_people(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])

    # L, M, N: "ID брони", "ID номера", "ID автора"
    assert _hidden_columns(backend) == [(11, 14)]


def test_the_layout_is_applied_once_however_many_times_the_schedule_is_written(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])

    assert len(_hidden_columns(backend)) == 1


def test_a_layout_failure_does_not_stop_the_backup_and_is_retried(backend):
    rows = [sheets_sync.SCHEDULE_HEADER, ["2026-10-21", "Ср"]]
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])  # opens the sheet
    backend._schedule_layout_done = False
    backend.spreadsheet.requests.clear()
    backend.spreadsheet.layout_failure = ConnectionError("batchUpdate refused")

    backend.replace_schedule(rows)  # must not raise

    update = [call for call in backend.calls if call[0] == "update"][-1]
    assert update[2]["values"] == rows
    assert _hidden_columns(backend) == []

    backend.spreadsheet.layout_failure = None
    backend.replace_schedule(rows)
    assert _hidden_columns(backend) == [(11, 14)]


def test_restarting_the_backend_does_not_pile_up_colour_rules(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    sheet = backend.spreadsheet.sheets[sheets_sync.SCHEDULE_SHEET]
    after_first_start = len(backend.spreadsheet.rules[sheet.id])
    assert after_first_start > 0

    restarted = sheets_sync.GspreadBackend()  # a new process finds the rules of the previous one
    restarted.replace_schedule([sheets_sync.SCHEDULE_HEADER])

    assert len(backend.spreadsheet.rules[sheet.id]) == after_first_start


def test_the_spreadsheet_uses_the_club_timezone(backend, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Asia/Almaty")
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])

    assert ("update_timezone", ("Asia/Almaty",), {}) in backend.calls


def test_the_day_stripe_formula_works_in_any_spreadsheet_locale(backend):
    """A Russian-locale spreadsheet rejected a comma between arguments with 400, and the separator depends on the locale."""
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    sheet = backend.spreadsheet.sheets[sheets_sync.SCHEDULE_SHEET]

    [rule] = backend.spreadsheet.rules[sheet.id]
    formula = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
    assert "," not in formula and ";" not in formula


WEEK_DAYS = 7
ROOM_08_21 = (480, 1260)


def _grid(backend, window=ROOM_08_21):
    backend.replace_grid(sheets_sync.build_grid_rows(*window))
    return backend.spreadsheet.sheets[sheets_sync.GRID_SHEET]


def _week_header_rows(grid) -> list[int]:
    """Zero-based indexes of the rows that open a week block."""
    return [index for index, row in enumerate(grid.rows) if row and row[0] == "Время"]


def _slots(grid, header_row: int) -> list[str]:
    labels = []
    for row in grid.rows[header_row + 1 :]:
        if not row or not row[0]:
            break
        labels.append(row[0])
    return labels


def test_the_grid_shows_two_weeks_of_seven_days_one_under_the_other(backend):
    grid = _grid(backend)

    headers = _week_header_rows(grid)
    assert len(headers) == 2
    assert headers[0] < headers[1]
    assert all(len(grid.rows[row]) == 1 + WEEK_DAYS for row in headers)


def test_the_grid_has_a_row_per_half_hour_of_the_room_hours(backend):
    grid = _grid(backend)

    first_week, second_week = _week_header_rows(grid)
    slots = _slots(grid, first_week)
    assert slots[:3] == ["'08:00", "'08:30", "'09:00"]  # the apostrophe keeps "08:00" text, as it is on the schedule sheet
    assert slots[-1] == "'20:30"
    assert len(slots) == 26
    assert _slots(grid, second_week) == slots


def test_the_grid_follows_changed_room_hours(backend):
    grid = _grid(backend, window=(600, 720))

    assert _slots(grid, _week_header_rows(grid)[0]) == ["'10:00", "'10:30", "'11:00", "'11:30"]


def test_the_weeks_of_the_grid_are_worked_out_by_the_sheet_itself(backend):
    """On Monday the grid has to move on even with the bot down, so no date comes from the bot."""
    grid = _grid(backend)

    first_week, second_week = _week_header_rows(grid)
    first_header = first_week + 1  # as a row number of the sheet
    assert grid.rows[first_week][1] == "=TODAY()-WEEKDAY(TODAY()-1)+1"  # Monday: WEEKDAY of the day before is 1 on a Monday
    assert grid.rows[first_week][2:] == [f"={column}{first_header}+1" for column in "BCDEFG"]
    assert grid.rows[second_week][1] == f"=B{first_header}+7"
    assert grid.rows[second_week][2:] == [f"={column}{second_week + 1}+1" for column in "BCDEFG"]


def test_a_grid_cell_shows_the_booking_that_covers_its_day_and_half_hour(backend):
    grid = _grid(backend)

    assert grid.rows[3][1] == (
        """=IFERROR(INDIRECT("'Расписание'!E"&SUMPRODUCT("""
        """(IFERROR('Расписание'!$A$2:$A$1000+0)=B$3)"""
        """*('Расписание'!$C$2:$C$1000<=$A4)"""
        """*('Расписание'!$D$2:$D$1000>$A4)"""
        """*ROW('Расписание'!$A$2:$A$1000))))"""
    )


def test_every_grid_cell_looks_at_its_own_day_and_half_hour(backend):
    grid = _grid(backend)

    second_week = _week_header_rows(grid)[1]
    last_row = second_week + 26  # a zero-based index: the header plus 26 half hours
    formula = grid.rows[last_row][7]
    assert f"=H${second_week + 1})" in formula
    assert f"<=$A{last_row + 1})" in formula
    assert f">$A{last_row + 1})" in formula


def test_the_grid_formulas_work_in_any_spreadsheet_locale(backend):
    """The separator between arguments depends on the spreadsheet locale, so no formula may have one."""
    grid = _grid(backend)

    formulas = [cell for row in grid.rows for cell in row if cell.startswith("=")]
    assert len(formulas) == 2 * (7 + 7 * 26)
    for formula in formulas:
        outside_strings = re.sub(r'"[^"]*"', "", formula)
        assert "," not in outside_strings and ";" not in outside_strings


def test_the_grid_is_entered_as_formulas(backend):
    _grid(backend)

    update = next(call for call in backend.calls if call[0] == "update")
    assert update[2]["value_input_option"] == "USER_ENTERED"


def _matches(condition: dict, value: str) -> bool:
    kind = condition["type"]
    expected = condition["values"][0]["userEnteredValue"]
    if kind == "TEXT_EQ":
        return value == expected
    if kind == "TEXT_STARTS_WITH":
        return value.startswith(expected)
    assert kind == "CUSTOM_FORMULA" and re.fullmatch(r'=[A-Z]+\d+<>""', expected), expected
    return value != ""


def _colour_of(backend, grid, value: str):
    """The colour Google would paint a slot holding this value: the first matching rule wins."""
    for rule in backend.spreadsheet.rules[grid.id]:
        if _matches(rule["booleanRule"]["condition"], value):
            return rule["booleanRule"]["format"]["backgroundColor"]
    return None


def _booking(**fields):
    return SimpleNamespace(**{"band": None, "purpose": "self", "created_by": SimpleNamespace(last_name="Ли", first_name="Ан"), **fields})


def test_slots_are_coloured_by_the_kind_of_booking(backend):
    grid = _grid(backend)

    band = _colour_of(backend, grid, booking_owner_label(_booking(band=SimpleNamespace(name="Rockers"))))
    personal = _colour_of(backend, grid, booking_owner_label(_booking(purpose="personal")))
    staff = _colour_of(backend, grid, booking_owner_label(_booking(purpose="staff")))

    assert None not in (band, personal, staff)
    assert len({str(band), str(personal), str(staff)}) == 3


def test_a_free_slot_stays_white(backend):
    grid = _grid(backend)

    assert _colour_of(backend, grid, "") is None


def test_the_colours_cover_exactly_the_slots_of_both_weeks(backend):
    grid = _grid(backend)

    first_week, second_week = _week_header_rows(grid)
    ranges = {
        (item["startRowIndex"], item["endRowIndex"], item["startColumnIndex"], item["endColumnIndex"])
        for rule in backend.spreadsheet.rules[grid.id]
        for item in rule["ranges"]
    }
    assert ranges == {(first_week + 1, first_week + 27, 1, 8), (second_week + 1, second_week + 27, 1, 8)}


def test_the_band_colour_looks_at_the_first_cell_of_its_range(backend):
    """A relative reference counts from the top left cell of the range, so another anchor would colour the wrong slots."""
    grid = _grid(backend)

    anchors = {}
    for rule in backend.spreadsheet.rules[grid.id]:
        condition = rule["booleanRule"]["condition"]
        if condition["type"] == "CUSTOM_FORMULA":
            anchors[rule["ranges"][0]["startRowIndex"]] = condition["values"][0]["userEnteredValue"]
    assert anchors == {row: f'=B{row + 1}<>""' for row in anchors}
    assert len(anchors) == 2


def test_nobody_edits_the_grid_by_accident(backend):
    grid = _grid(backend)

    [protection] = backend.spreadsheet.protections[grid.id]
    assert protection["range"] == {"sheetId": grid.id}
    assert protection["warningOnly"] is False


def test_the_warning_about_edits_opens_the_grid(backend):
    grid = _grid(backend)

    assert "не попадают в бота" in grid.rows[0][1]
    merge = next(request["mergeCells"] for request in backend.spreadsheet.requests if "mergeCells" in request)
    # Column A stays out of the merge: Google refuses to merge frozen and unfrozen columns.
    assert merge["range"] == {"sheetId": grid.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 8}


def test_the_grid_is_the_first_tab_with_the_times_frozen(backend):
    grid = _grid(backend)

    updates = [request["updateSheetProperties"] for request in backend.spreadsheet.requests if "updateSheetProperties" in request]
    assert any(item["properties"].get("index") == 0 and item["properties"]["sheetId"] == grid.id for item in updates)
    assert any(item["properties"].get("gridProperties") == {"frozenColumnCount": 1} for item in updates)


def test_the_days_of_the_grid_read_as_dates(backend):
    grid = _grid(backend)

    first_week, second_week = _week_header_rows(grid)
    formatted = {
        request["repeatCell"]["range"]["startRowIndex"]
        for request in backend.spreadsheet.requests
        if "repeatCell" in request
        and request["repeatCell"].get("cell", {}).get("userEnteredFormat", {}).get("numberFormat", {}).get("type") == "DATE"
    }
    assert formatted == {first_week, second_week}


def test_restarting_the_backend_does_not_pile_up_the_look_of_the_grid(backend):
    grid = _grid(backend)
    rules = len(backend.spreadsheet.rules[grid.id])

    restarted = sheets_sync.GspreadBackend()
    restarted.replace_grid(sheets_sync.build_grid_rows(*ROOM_08_21))

    assert len(backend.spreadsheet.rules[grid.id]) == rules
    assert len(backend.spreadsheet.protections[grid.id]) == 1


def test_new_room_hours_leave_no_old_look_behind(backend):
    """The second week moves when the room hours change, and its old header must not stay painted."""
    grid = _grid(backend, window=(600, 720))

    first_format = next(request["repeatCell"] for request in backend.spreadsheet.requests if "repeatCell" in request)
    assert first_format == {"range": {"sheetId": grid.id}, "cell": {}, "fields": "userEnteredFormat"}


def test_shorter_room_hours_leave_no_old_cells_behind(backend):
    grid = _grid(backend, window=(600, 720))

    assert all(len(row) == 8 for row in grid.rows)  # an empty row would leave the old cells of that row in place
    clears = [call for call in backend.calls if call[0] == "batch_clear"]
    assert clears == [("batch_clear", ([f"A{len(grid.rows) + 1}:Z5000"],), {})]


def test_the_grid_sheet_grows_when_the_rows_do_not_fit(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    backend.spreadsheet.sheets[sheets_sync.GRID_SHEET] = StubWorksheet(backend.calls, row_count=10)

    grid = _grid(backend)

    assert grid.row_count == len(grid.rows)


def test_a_grid_failure_drops_the_connection_so_it_reconnects(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    backend.spreadsheet.layout_failure = ConnectionError("google is down")

    with pytest.raises(ConnectionError):
        backend.replace_grid(sheets_sync.build_grid_rows(*ROOM_08_21))

    assert backend._spreadsheet is None


def test_the_status_line_changes_only_its_own_cell(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    grid = _grid(backend)
    before = [list(row) for row in grid.rows]

    backend.update_status("Обновлено ботом: 09.11 14:36")

    update = backend.calls[-1]
    assert update[0] == "update"
    assert update[2] == {"values": [["Обновлено ботом: 09.11 14:36"]], "range_name": "B2", "value_input_option": "RAW"}
    assert grid.rows[1][1] == "Обновлено ботом: 09.11 14:36"
    assert grid.rows[:1] == before[:1] and grid.rows[2:] == before[2:]
    assert grid.rows[1][2:] == before[1][2:] and grid.rows[1][0] == before[1][0]


def test_a_status_line_failure_drops_the_connection_so_it_reconnects(backend):
    backend.replace_schedule([sheets_sync.SCHEDULE_HEADER])
    grid = _grid(backend)

    def boom(*args, **kwargs):
        raise ConnectionError("network")

    grid.update = boom
    with pytest.raises(ConnectionError):
        backend.update_status("Обновлено ботом: 09.11 14:36")

    assert backend._spreadsheet is None
