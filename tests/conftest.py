from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import FakeChannel, seeded_store
from message_gateway import clock
from message_gateway.clock import local_tz


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch: pytest.MonkeyPatch):
    """Every test runs in UTC, with default business hours and subject prefix,
    on the real clock unless it freezes time."""
    monkeypatch.setenv("GATEWAY_TIMEZONE", "UTC")
    for name in (
        "GATEWAY_SUBJECT_PREFIX",
        "GATEWAY_BUSINESS_HOURS_START",
        "GATEWAY_BUSINESS_HOURS_END",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(clock, "_clock", None)
    local_tz.cache_clear()
    yield
    local_tz.cache_clear()


class FrozenClock:
    """Pins `clock.now_local()` for every module; move it with `set`/`advance`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now: datetime | None = None
        monkeypatch.setattr(clock, "_clock", lambda: self.now)

    def set(self, *args: int) -> datetime:
        self.now = datetime(*args, tzinfo=local_tz())
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    return FrozenClock(monkeypatch)


@pytest.fixture
def store():
    return seeded_store()


@pytest.fixture
def channel():
    return FakeChannel()
