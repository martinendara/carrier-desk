"""In-memory rate limiting.

The per-call OTP lock (three attempts) bounds guesses *within* one call. It does
nothing about a caller who opens a new call — a new `run_id` — and gets three
more. This is the limit that sits above that: N attempts per hour per MC, no
matter how many calls are started.

**Single instance, like everything else with state here.** A second worker would
keep its own counters and effectively double the limit. Redis behind this same
interface is the production path; see DECISIONS.md.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

#: Upper bound on tracked keys. Without it, a caller looping over fake MC
#: numbers grows this dictionary without limit — a slow way to run a service out
#: of memory.
MAX_KEYS = 10_000


@dataclass(frozen=True)
class RateLimitDecision:
    """Whether the attempt is allowed, and how long until it would be."""

    allowed: bool
    remaining: int
    #: Seconds until the oldest attempt falls out of the window. Zero when
    #: allowed.
    retry_after_seconds: float = 0.0

    @property
    def retry_after(self) -> int:
        """Whole seconds, rounded up, for a `Retry-After` header."""
        return int(self.retry_after_seconds + 0.999)


class SlidingWindowLimiter:
    """At most `limit` hits per `window_seconds` for any key."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> RateLimitDecision:
        """Consume one attempt for `key` and report whether it was allowed.

        A blocked attempt is **not** recorded, so a caller hammering the
        endpoint does not keep extending its own ban — the window closes on
        schedule whether or not anyone is knocking.
        """
        now = self._clock()
        with self._lock:
            self._evict(now)
            recent = self._hits.setdefault(key, deque())
            self._drop_old(recent, now)

            if len(recent) >= self._limit:
                retry_after = self._window - (now - recent[0])
                return RateLimitDecision(
                    allowed=False, remaining=0, retry_after_seconds=max(0.0, retry_after)
                )

            recent.append(now)
            return RateLimitDecision(allowed=True, remaining=self._limit - len(recent))

    def peek(self, key: str) -> int:
        """How many attempts `key` has left, without consuming one."""
        now = self._clock()
        with self._lock:
            recent = self._hits.get(key)
            if recent is None:
                return self._limit
            self._drop_old(recent, now)
            return max(0, self._limit - len(recent))

    def reset(self, key: str) -> None:
        """Forget a key. Used by tests; no endpoint exposes it."""
        with self._lock:
            self._hits.pop(key, None)

    def _drop_old(self, recent: deque[float], now: float) -> None:
        cutoff = now - self._window
        while recent and recent[0] <= cutoff:
            recent.popleft()

    def _evict(self, now: float) -> None:
        """Keep the dictionary bounded, dropping keys with nothing in the window."""
        if len(self._hits) < MAX_KEYS:
            return
        stale = [key for key, recent in self._hits.items() if not recent or recent[-1] <= now - self._window]
        for key in stale:
            del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)
