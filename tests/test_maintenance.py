"""The two structures that used to grow without bound.

Sessions and handoff tickets are the only state the service keeps that a caller
can add to and nothing ever removes. Both are bounded now, by age, on the write
path — no background thread to start, stop, monitor or leak.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import FakeClock

from app.handoff import HandoffQueue, HandoffReason
from app.sessions import RunSessionStore
from app.sessions.store import SWEEP_THRESHOLD

TTL = 3600.0


# --- run sessions --------------------------------------------------------


def test_expired_sessions_are_swept_once_the_store_grows():
    """Lazy expiry alone never removes an entry nobody reads again."""
    clock = FakeClock()
    store = RunSessionStore(ttl_seconds=TTL, clock=clock, sweep_threshold=10)

    for index in range(9):
        store.get_or_create(f"run-{index}")
    clock.advance(TTL + 1)  # every one of them is now expired
    store.get_or_create("run-fresh")  # the tenth write triggers the sweep

    assert len(store) == 1, "the expired sessions should be gone"
    assert store.get("run-fresh") is not None


def test_the_sweep_keeps_sessions_that_are_still_alive():
    clock = FakeClock()
    store = RunSessionStore(ttl_seconds=TTL, clock=clock, sweep_threshold=3)

    store.get_or_create("old")
    clock.advance(TTL + 1)
    store.get_or_create("new-a")
    store.get_or_create("new-b")  # crosses the threshold and sweeps

    assert store.get("old") is None
    assert store.get("new-a") is not None
    assert store.get("new-b") is not None


def test_a_sweep_during_a_write_does_not_deadlock():
    """The sweep runs while `get_or_create` holds a non-reentrant lock.

    Calling the public `purge_expired` from there would hang the call that
    triggered the sweep, which is the one thing worse than growing.
    """
    clock = FakeClock()
    store = RunSessionStore(ttl_seconds=TTL, clock=clock, sweep_threshold=1)

    store.get_or_create("first")
    clock.advance(TTL + 1)
    store.get_or_create("second")  # sweeps "first" while holding the lock

    assert store.get("first") is None
    assert len(store) == 1


def test_the_default_threshold_is_the_documented_one():
    assert SWEEP_THRESHOLD == 500


def test_purging_by_hand_still_works():
    clock = FakeClock()
    store = RunSessionStore(ttl_seconds=TTL, clock=clock)

    store.get_or_create("run-1")
    clock.advance(TTL + 1)

    assert store.purge_expired() == 1
    assert len(store) == 0


# --- handoff tickets -----------------------------------------------------


class TicketClock:
    """A wall clock the tests advance by hand, for the queue's `created_at`."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def test_a_ticket_past_its_retention_is_dropped_on_the_next_write():
    clock = TicketClock()
    queue = HandoffQueue(retention_seconds=60, clock=clock)
    queue.create("run-old", HandoffReason.BOOKED)

    clock.advance(seconds=61)
    queue.create("run-new", HandoffReason.BOOKED)

    assert [t.run_id for t in queue.pending()] == ["run-new"]


def test_a_fresh_ticket_stays():
    queue = HandoffQueue(retention_seconds=86400)
    queue.create("run-1", HandoffReason.BOOKED)

    assert len(queue) == 1


def test_prune_can_be_driven_by_hand():
    clock = TicketClock()
    queue = HandoffQueue(retention_seconds=60, clock=clock)
    queue.create("run-1", HandoffReason.REQUESTED)

    clock.advance(minutes=5)
    assert queue.prune() == 1
    assert queue.pending() == []


def test_pending_does_not_hand_out_its_own_list():
    """A caller mutating what it got back must not reach into the queue."""
    queue = HandoffQueue()
    queue.create("run-1", HandoffReason.BOOKED)

    handed_out = queue.pending()
    handed_out.clear()

    assert len(queue) == 1
