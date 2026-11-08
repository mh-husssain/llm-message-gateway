from __future__ import annotations

import pytest

from fakes import FakeChannel, seeded_store
from message_gateway.clock import local_tz


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch: pytest.MonkeyPatch):
    """Every test runs in UTC with the default subject prefix."""
    monkeypatch.setenv("GATEWAY_TIMEZONE", "UTC")
    monkeypatch.delenv("GATEWAY_SUBJECT_PREFIX", raising=False)
    local_tz.cache_clear()
    yield
    local_tz.cache_clear()


@pytest.fixture
def store():
    return seeded_store()


@pytest.fixture
def channel():
    return FakeChannel()
