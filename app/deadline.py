"""A wall-clock budget for one HTTP request.

A voice tool cannot hang. The platform's webhook timeout is not documented, so
we set our own — shorter than any plausible platform limit — and spend it
deliberately: the TMS retry ladder is cut short when the remaining time is less
than the next backoff, rather than sleeping through a retry that cannot finish.

Monotonic on purpose: a wall-clock adjustment must not extend or collapse a
budget mid-request.
"""

from __future__ import annotations

import time
from typing import Callable

#: Enough for a carrier to hear a short sentence, and short enough that a stuck
#: tool does not become a stuck call.
DEFAULT_BUDGET_SECONDS = 8.0


class Deadline:
    """A budget that knows how much of it is left."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("a deadline needs a positive number of seconds")
        self._clock = clock
        self._started_at = clock()
        self._seconds = seconds

    @property
    def total_seconds(self) -> float:
        return self._seconds

    @property
    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._seconds - (self._clock() - self._started_at))

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def allows(self, seconds: float) -> bool:
        """Whether there is room for `seconds` more of work."""
        return self.remaining > seconds

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Deadline(total={self._seconds}s, remaining={self.remaining:.3f}s)"
