"""The sliding-window limiter, on its own."""

from __future__ import annotations

import pytest
from conftest import FakeClock

from app.rate_limit import MAX_KEYS, SlidingWindowLimiter


def make_limiter(clock: FakeClock, limit: int = 5, window: float = 3600.0):
    return SlidingWindowLimiter(limit=limit, window_seconds=window, clock=clock)


def test_attempts_up_to_the_limit_are_allowed(clock):
    limiter = make_limiter(clock, limit=5)

    for _ in range(5):
        assert limiter.hit("mc:123456").allowed


def test_the_attempt_after_the_limit_is_blocked(clock):
    limiter = make_limiter(clock, limit=5)
    for _ in range(5):
        limiter.hit("mc:123456")

    decision = limiter.hit("mc:123456")

    assert not decision.allowed
    assert decision.remaining == 0


def test_remaining_counts_down(clock):
    limiter = make_limiter(clock, limit=3)

    assert limiter.hit("k").remaining == 2
    assert limiter.hit("k").remaining == 1
    assert limiter.hit("k").remaining == 0


def test_the_window_slides(clock):
    limiter = make_limiter(clock, limit=2, window=60)
    limiter.hit("k")
    limiter.hit("k")
    assert not limiter.hit("k").allowed

    clock.advance(61)

    assert limiter.hit("k").allowed


def test_keys_are_independent(clock):
    limiter = make_limiter(clock, limit=1)
    assert limiter.hit("mc:111").allowed

    assert not limiter.hit("mc:111").allowed
    assert limiter.hit("mc:222").allowed


def test_a_blocked_attempt_does_not_extend_the_window(clock):
    """Hammering the endpoint must not keep pushing the caller's own ban out."""
    limiter = make_limiter(clock, limit=1, window=60)
    limiter.hit("k")

    for _ in range(20):
        clock.advance(2)
        limiter.hit("k")  # all blocked

    # 60 s after the single allowed hit, the window has still closed.
    clock.advance(20)
    assert limiter.hit("k").allowed


def test_retry_after_is_about_the_oldest_attempt(clock):
    limiter = make_limiter(clock, limit=1, window=100)
    limiter.hit("k")
    clock.advance(30)

    decision = limiter.hit("k")

    assert not decision.allowed
    assert 69 <= decision.retry_after_seconds <= 71
    assert decision.retry_after == 70


def test_retry_after_rounds_up_for_the_header(clock):
    limiter = make_limiter(clock, limit=1, window=100)
    limiter.hit("k")
    clock.advance(99.5)

    assert limiter.hit("k").retry_after == 1


def test_peek_does_not_consume(clock):
    limiter = make_limiter(clock, limit=3)

    assert limiter.peek("k") == 3
    limiter.hit("k")
    assert limiter.peek("k") == 2
    assert limiter.peek("k") == 2


def test_peek_on_an_unknown_key_is_the_full_limit(clock):
    assert make_limiter(clock, limit=5).peek("never-seen") == 5


def test_an_expired_key_comes_back_to_full(clock):
    limiter = make_limiter(clock, limit=2, window=60)
    limiter.hit("k")
    clock.advance(61)

    assert limiter.peek("k") == 2


def test_reset_forgets_a_key(clock):
    limiter = make_limiter(clock, limit=1)
    limiter.hit("k")
    limiter.reset("k")

    assert limiter.hit("k").allowed


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (5, 0), (5, -1)])
def test_nonsense_configuration_is_refused(limit, window):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=limit, window_seconds=window)


def test_the_tracked_keys_are_bounded(clock):
    """A caller looping over fake MCs must not grow this without limit."""
    limiter = make_limiter(clock, limit=1, window=60)

    for index in range(MAX_KEYS + 500):
        limiter.hit(f"mc:{index}")
        if index % 1000 == 0:
            clock.advance(120)  # let the earlier keys go stale

    assert len(limiter) <= MAX_KEYS
