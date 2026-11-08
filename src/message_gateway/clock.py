"""Time handling: one configured timezone, business hours and the sheet date
formats.

The timezone comes from GATEWAY_TIMEZONE (default UTC). It is resolved lazily
and cached, so the environment can be loaded (e.g. from `.env`) before the
first call. Call `local_tz.cache_clear()` after changing the variable.

Business hours (the window in which the reminder engines run) come from
GATEWAY_BUSINESS_HOURS_START and GATEWAY_BUSINESS_HOURS_END, whole hours in
local time, default 07 to 20 (start inclusive, end exclusive).

Every "now" in the package goes through `now_local()`. Tests pin it by
setting the private `_clock` hook (see tests/conftest.py).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

# Canonical timestamp format for every TS column in the sheet.
TS_FMT = "%d/%m/%Y %H:%M:%S"
# Canonical date format for due dates.
DATE_FMT = "%d/%m/%Y"

DEFAULT_BUSINESS_HOURS = (7, 20)

# Test hook: when set, now_local() returns this callable's result.
_clock: Callable[[], datetime] | None = None


@lru_cache(maxsize=1)
def local_tz() -> ZoneInfo:
    return ZoneInfo(timezone_name())


def timezone_name() -> str:
    return (os.environ.get("GATEWAY_TIMEZONE") or "").strip() or "UTC"


def now_local() -> datetime:
    if _clock is not None:
        return _clock().astimezone(local_tz())
    return datetime.now(local_tz())


def _hour_setting(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    value = int(raw)
    if not 0 <= value <= 24:
        raise ValueError(f"{name} must be an hour between 0 and 24, got {raw!r}")
    return value


def business_hours() -> tuple[int, int]:
    """(start_hour, end_hour) in local time; start inclusive, end exclusive."""
    start = _hour_setting("GATEWAY_BUSINESS_HOURS_START", DEFAULT_BUSINESS_HOURS[0])
    end = _hour_setting("GATEWAY_BUSINESS_HOURS_END", DEFAULT_BUSINESS_HOURS[1])
    if start >= end:
        raise ValueError(f"business hours start ({start}) must be before end ({end})")
    return start, end


def is_business_hours(dt: datetime | None = None) -> bool:
    dt = dt or now_local()
    start, end = business_hours()
    return start <= dt.hour < end


def same_local_date(ts_str: str, day: datetime) -> bool:
    """True iff `ts_str` (TS_FMT, may be empty) falls on the same local date as `day`."""
    if not ts_str:
        return False
    parsed = parse_ts(ts_str)
    if not parsed:
        return False
    return parsed.date() == day.date()


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
