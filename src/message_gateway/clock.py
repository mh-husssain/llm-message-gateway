"""Time handling: one configured timezone and the two sheet date formats.

The timezone comes from GATEWAY_TIMEZONE (default UTC). It is resolved lazily
and cached, so the environment can be loaded (e.g. from `.env`) before the
first call. Call `local_tz.cache_clear()` after changing the variable.
"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

# Canonical timestamp format for every TS column in the sheet.
TS_FMT = "%d/%m/%Y %H:%M:%S"
# Canonical date format for due dates.
DATE_FMT = "%d/%m/%Y"


@lru_cache(maxsize=1)
def local_tz() -> ZoneInfo:
    return ZoneInfo(timezone_name())


def timezone_name() -> str:
    return (os.environ.get("GATEWAY_TIMEZONE") or "").strip() or "UTC"


def now_local() -> datetime:
    return datetime.now(local_tz())


def fmt_ts(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, TS_FMT).replace(tzinfo=local_tz())
    except (ValueError, TypeError):
        return None


def iso_to_sheet_date(iso: str) -> str:
    """'2026-05-05' -> '05/05/2026'. Returns '' on parse failure or empty input."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime(DATE_FMT)
    except (ValueError, TypeError):
        return ""


def parse_deadline_iso(iso: str | None) -> datetime | None:
    """Parse a full ISO 8601 datetime emitted by the classifier for independent
    tasks (e.g. '2026-05-07T16:00:00+00:00'). Returns an aware datetime in the
    configured timezone, or None on failure.

    Accepts offset-suffixed forms ('...+02:00', '...Z') and naked forms
    ('2026-05-07T16:00:00'); a naked value is assumed to be local time.
    """
    if not iso:
        return None
    s = iso.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz())


def parse_sheet_date(s: str) -> datetime | None:
    """Parse a sheet date cell.

    The canonical format is dd/mm/yyyy, but older rows hold stringified
    datetimes (yyyy-mm-dd HH:MM:SS), so those are accepted too.
    """
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=local_tz())
        except ValueError:
            continue
    return None


def format_display_date(s: str) -> str:
    """Normalise a sheet date string to DD/MM/YYYY for reply text.

    Returns the input unchanged when it can't be parsed.
    """
    if not s:
        return s
    parsed = parse_sheet_date(s)
    return parsed.strftime(DATE_FMT) if parsed else s
