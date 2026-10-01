"""Shared fixtures for the phase 2 unit tests.

Time is injected everywhere: a test that depends on the wall clock is a test
that fails at 23:59.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.events import InMemoryEventSink
from app.sessions import RunSessionStore

FIXTURES = Path(__file__).parent / "fixtures"

#: Fixed "now" for every phase 2 test, so urgency and authority ages are stable.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

#: A 32-byte secret, the shape the OTP service requires.
OTP_SECRET = b"test-secret-that-is-long-enough!!"


class FakeClock:
    """A monotonic clock the tests drive by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def load_fmcsa_fixture(name: str) -> dict:
    """Load one of the synthetic FMCSA payloads. See tests/fixtures/README.md."""
    return json.loads((FIXTURES / f"fmcsa_{name}.json").read_text(encoding="utf-8"))


class TickClock:
    """A datetime clock the tests advance by hand."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def events() -> InMemoryEventSink:
    return InMemoryEventSink()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tick() -> TickClock:
    return TickClock(NOW)


@pytest.fixture
def store(clock: FakeClock) -> RunSessionStore:
    return RunSessionStore(ttl_seconds=3600, clock=clock)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def later() -> datetime:
    """A "now" far enough ahead that nothing is urgent."""
    return NOW + timedelta(days=30)
