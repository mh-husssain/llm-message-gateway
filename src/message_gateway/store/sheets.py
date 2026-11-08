"""Google Sheets `Store`, with exponential backoff on HTTP 429.

The spreadsheet ID comes from GATEWAY_SHEET_ID. Values are written with
valueInputOption=USER_ENTERED, which is why free-text cells pass through the
formula-injection guard (`text.sanitize_freetext`) before they get here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from googleapiclient.errors import HttpError

from ..config import Settings
from ..google_auth import SHEETS_SCOPES, service_account_credentials

log = logging.getLogger("message_gateway.store.sheets")

# The Sheets per-user write quota is 60/min; bursts back off and recover
# instead of failing.
SHEETS_MAX_RETRIES = 6
SHEETS_BASE_DELAY = 1.0

T = TypeVar("T")


def call_with_backoff(
    fn: Callable[[], T],
    *,
    what: str = "",
    max_retries: int = SHEETS_MAX_RETRIES,
    base_delay: float = SHEETS_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run fn(); on HTTP 429 sleep base_delay, 2x, 4x, ... and retry.

    Any other error, or a 429 on the last attempt, is re-raised.
    """
    delay = base_delay
    for attempt in range(max_retries):
        try:
            return fn()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status != 429 or attempt == max_retries - 1:
                raise
            log.warning(
                "Sheets 429 on %s; sleeping %.1fs (attempt %d/%d)",
                what or getattr(fn, "__name__", "call"),
                delay,
                attempt + 1,
                max_retries,
            )
            sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


class GoogleSheetsStore:
    def __init__(
        self,
        sheet_id: str,
        *,
        service: Any = None,
        credentials: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not sheet_id:
            raise ValueError("sheet_id is required")
        self.sheet_id = sheet_id
        if service is None:
            from googleapiclient.discovery import build

            service = build(
                "sheets", "v4", credentials=credentials, cache_discovery=False
            ).spreadsheets()
        self._svc = service
        self._sleep = sleep

    @classmethod
    def from_env(cls, settings: Settings, *, credentials: Any = None) -> GoogleSheetsStore:
        settings.require("sheet_id")
        if credentials is None:
            settings.require("google_service_account")
            credentials = service_account_credentials(
                settings.google_service_account, SHEETS_SCOPES
            )
        return cls(settings.sheet_id, credentials=credentials)

    def _call(self, fn: Callable[[], T], what: str) -> T:
        return call_with_backoff(fn, what=what, sleep=self._sleep)

    def read(self, range_: str) -> list[list[str]]:
        resp = self._call(
            lambda: self._svc.values().get(spreadsheetId=self.sheet_id, range=range_).execute(),
            what=f"read {range_}",
        )
        return resp.get("values", [])

    def append(self, range_: str, row: list[str]) -> None:
        self._call(
            lambda: (
                self._svc.values()
                .append(
                    spreadsheetId=self.sheet_id,
                    range=range_,
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [row]},
                )
                .execute()
            ),
            what=f"append {range_}",
        )

    def update(self, range_: str, values: list[list[str]]) -> None:
        self._call(
            lambda: (
                self._svc.values()
                .update(
                    spreadsheetId=self.sheet_id,
                    range=range_,
                    valueInputOption="USER_ENTERED",
                    body={"values": values},
                )
                .execute()
            ),
            what=f"update {range_}",
        )

    def batch_update(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        if not updates:
            return
        self._call(
            lambda: (
                self._svc.values()
                .batchUpdate(
                    spreadsheetId=self.sheet_id,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": [{"range": r, "values": v} for r, v in updates],
                    },
                )
                .execute()
            ),
            what="batch_update",
        )
