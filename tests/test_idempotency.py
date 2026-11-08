"""Message-ID idempotency and the Sheets 429 backoff."""

from __future__ import annotations

import httplib2
import pytest
from googleapiclient.errors import HttpError

from message_gateway.config import ConfigError, Settings
from message_gateway.records import is_msg_id_processed, log_activity
from message_gateway.store.sheets import GoogleSheetsStore, call_with_backoff


def test_fresh_store_has_nothing_processed(store):
    assert not is_msg_id_processed(store, "ABC123")
    assert not is_msg_id_processed(store, "")


@pytest.mark.parametrize(
    ("action", "related", "details"),
    [
        ("Message dispatch started", "Alice / msg_id:ABC123", ""),
        ("email message processed", "Alice / msg_id:ABC123", ""),
        ("ERROR", "msg_id:ABC123", "boom"),
        ("ERROR", "(agent)", "context msg_id:ABC123"),
    ],
)
def test_markers_that_count_as_processed(store, action, related, details):
    log_activity(store, action, related, details)
    assert is_msg_id_processed(store, "ABC123")
    assert not is_msg_id_processed(store, "OTHER")


def test_unrelated_actions_do_not_count(store):
    log_activity(store, "Task assigned", "msg_id:ABC123", "")
    assert not is_msg_id_processed(store, "ABC123")


def test_rows_outside_the_lookback_window_are_ignored(store):
    store.append(
        "Activity_Log!A:D",
        ["01/01/2020 00:00:00", "Message dispatch started", "old / msg_id:ANCIENT", ""],
    )
    assert not is_msg_id_processed(store, "ANCIENT", lookback_hours=24)


def test_scan_stops_at_the_first_row_older_than_the_window(store):
    # A matching row *before* an old row is never reached: the log is chronological.
    log_activity(store, "Message dispatch started", "msg_id:EARLY", "")
    store.append("Activity_Log!A:D", ["01/01/2020 00:00:00", "Task assigned", "x", ""])
    assert not is_msg_id_processed(store, "EARLY")


# ─── 429 backoff ───────────────────────────────────────────────────────────


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


def test_backoff_retries_429_with_doubling_delays():
    attempts, sleeps = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 4:
            raise http_error(429)
        return "ok"

    assert call_with_backoff(flaky, base_delay=1.0, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0, 4.0]


def test_backoff_gives_up_after_max_retries():
    sleeps = []

    def always_429():
        raise http_error(429)

    with pytest.raises(HttpError):
        call_with_backoff(always_429, max_retries=3, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]


def test_non_rate_limit_errors_are_not_retried():
    sleeps = []

    def forbidden():
        raise http_error(403)

    with pytest.raises(HttpError):
        call_with_backoff(forbidden, sleep=sleeps.append)
    assert sleeps == []


class FakeValues:
    def __init__(self, failures: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.failures = failures

    def _req(self, name, kwargs, result):
        values = self

        class Req:
            def execute(self_inner):
                values.calls.append((name, kwargs))
                if values.failures:
                    values.failures -= 1
                    raise http_error(429)
                return result

        return Req()

    def get(self, **kw):
        return self._req("get", kw, {"values": [["a", "b"]]})

    def append(self, **kw):
        return self._req("append", kw, {})

    def update(self, **kw):
        return self._req("update", kw, {})

    def batchUpdate(self, **kw):  # noqa: N802 (Google API method name)
        return self._req("batchUpdate", kw, {})


class FakeSpreadsheets:
    def __init__(self, values: FakeValues):
        self._values = values

    def values(self):
        return self._values


def test_sheets_store_calls_the_api_with_the_configured_sheet_id():
    values = FakeValues(failures=1)
    sleeps = []
    s = GoogleSheetsStore("SHEET-ID", service=FakeSpreadsheets(values), sleep=sleeps.append)

    assert s.read("Tasks!A2:U") == [["a", "b"]]
    s.append("Tasks!A:U", ["x"])
    s.update("Tasks!K2", [["done"]])
    s.batch_update([("Tasks!K2", [["a"]]), ("Tasks!L2", [["b"]])])
    s.batch_update([])  # no call

    names = [n for n, _ in values.calls]
    assert names == ["get", "get", "append", "update", "batchUpdate"]  # first get hit a 429
    assert sleeps == [1.0]
    assert all(kw["spreadsheetId"] == "SHEET-ID" for _, kw in values.calls)
    append_kw = values.calls[2][1]
    assert append_kw["valueInputOption"] == "USER_ENTERED"
    assert append_kw["insertDataOption"] == "INSERT_ROWS"
    assert append_kw["body"] == {"values": [["x"]]}
    batch = values.calls[4][1]["body"]
    assert batch["data"] == [
        {"range": "Tasks!K2", "values": [["a"]]},
        {"range": "Tasks!L2", "values": [["b"]]},
    ]


def test_sheets_store_requires_a_sheet_id():
    with pytest.raises(ValueError):
        GoogleSheetsStore("", service=object())
    with pytest.raises(ConfigError, match="GATEWAY_SHEET_ID"):
        GoogleSheetsStore.from_env(Settings())
