"""GspreadBackend against a stub Google client: the stub's methods are bound to the real gspread signatures,
so a call the library would reject (wrong keyword, missing argument) fails here too."""
import inspect

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


class StubWorksheet:
    def __init__(self, calls, has_header=True, row_count=1000):
        self.calls = calls
        self.has_header = has_header
        self.row_count = row_count
        self._resize = _checked(gspread.Worksheet.resize, calls)
        self._update = _checked(gspread.Worksheet.update, calls)
        self.batch_clear = _checked(gspread.Worksheet.batch_clear, calls)
        self.append_rows = _checked(gspread.Worksheet.append_rows, calls)
        self._get_values = _checked(gspread.Worksheet.get_values, calls)
        self.get_all_values = _checked(gspread.Worksheet.get_all_values, calls)

    def update(self, *args, **kwargs):
        self._update(*args, **kwargs)
        self.has_header = True  # like a real sheet: once written, cell A1 is no longer empty

    def get_values(self, *args, **kwargs):
        self._get_values(*args, **kwargs)
        return [["Время"]] if self.has_header else []

    def resize(self, *args, **kwargs):
        self._resize(*args, **kwargs)
        self.row_count = kwargs.get("rows", self.row_count)


class StubSpreadsheet:
    def __init__(self, calls, existing=()):
        self.calls = calls
        self.sheets = {title: StubWorksheet(calls) for title in existing}
        self._add = _checked(gspread.Spreadsheet.add_worksheet, calls)

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
    assert names == ["open_by_key", "add_worksheet", "update", "batch_clear"]
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


def test_history_header_is_written_once_then_rows_are_appended(backend):
    backend.append_history([["2026-10-16 17:00:00", "Петров Иван"]])
    backend.append_history([["2026-10-16 17:05:00", "Петров Иван"]])

    updates = [call for call in backend.calls if call[0] == "update"]
    assert len(updates) == 1 and updates[0][2]["values"] == [sheets_sync.HISTORY_HEADER]
    appends = [call for call in backend.calls if call[0] == "append_rows"]
    assert len(appends) == 2 and appends[0][2] == {"value_input_option": "RAW"}


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
