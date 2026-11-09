"""GspreadBackend against a stub Google client: the stub's methods are bound to the real gspread signatures,
so a call the library would reject (wrong keyword, missing argument) fails here too."""
import inspect
import itertools

import gspread
import pytest
from google.oauth2 import service_account

from app.core.config import settings
from app.services import sheets_sync


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
        self.rows[: len(kwargs["values"])] = kwargs["values"]  # every caller writes from A1

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
                rule = request["addConditionalFormatRule"]["rule"]
                self.rules.setdefault(rule["ranges"][0]["sheetId"], []).append(rule)

    def fetch_sheet_metadata(self, params=None):
        self._metadata(params)
        return {
            "sheets": [
                {"properties": {"sheetId": sheet.id, "title": title}, "conditionalFormats": list(self.rules.get(sheet.id, []))}
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
