"""The money rules, as pure functions.

No state, no clock of its own, no session: everything here can be checked by
hand against CLAUDE.md §7. `engine.py` is the part that remembers a round.

All arithmetic is exact. The steps arrive as `Fraction` (see `app/config.py`),
so `0.40 × gap` is 2/5 of the gap and not the nearest binary float to it. This
is money: `floor25` next to a value that is a hair below a multiple of 25 would
drop a whole increment, and it would do it only for some gaps — the kind of bug
that survives a demo and shows up in a reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from typing import Sequence

DEFAULT_INCREMENT = 25
DEFAULT_CEILING_MARGIN = 25
URGENT_WINDOW = timedelta(hours=24)


def floor_to(value: int | Fraction, increment: int = DEFAULT_INCREMENT) -> int:
    """Round **down** to a multiple of `increment`."""
    if increment <= 0:
        raise ValueError("increment must be positive")
    return int(value // increment) * increment


def is_urgent(pickup_dt: datetime, now: datetime, window: timedelta = URGENT_WINDOW) -> bool:
    """True when the pickup is inside the urgency window.

    Both datetimes are UTC (DECISIONS.md §2). A pickup already in the past is
    urgent too — it is the most urgent thing there is.
    """
    return (pickup_dt - now) < window


def opening_offer(
    rate: int,
    ceiling: int,
    *,
    increment: int = DEFAULT_INCREMENT,
    ceiling_margin: int = DEFAULT_CEILING_MARGIN,
) -> int:
    """`O`, the first number the carrier hears.

    - `RATE < C` → `floor25(RATE)`: open at what the load was posted for.
    - otherwise → `floor25(0.90 × C)`: the posted rate is already at or above
      what we will pay, so open near the ceiling and keep the margin.

    The result is capped at `C − margin` in both branches. CLAUDE.md states the
    margin as a global rule ("never offer more than C − 25"); it only ever binds
    when the posted rate sits within one increment of the ceiling, which is
    pathological data — but capping is the safe reading, and it is one rule
    instead of two. See DECISIONS.md.
    """
    if rate < ceiling:
        raw: int | Fraction = rate
    else:
        # 0.90 × C in integers: floor(9C/10) is below 0.9C, and the gap to the
        # next multiple of 25 is at least 25, so the floor25 result is the same.
        raw = (9 * ceiling) // 10

    return min(floor_to(raw, increment), ceiling - ceiling_margin)


def concession_step(
    opening: int,
    ceiling: int,
    current_offer: int,
    round_number: int,
    *,
    urgent: bool = False,
    steps: Sequence[Fraction],
    urgent_steps: Sequence[Fraction],
    increment: int = DEFAULT_INCREMENT,
    ceiling_margin: int = DEFAULT_CEILING_MARGIN,
) -> int:
    """The number to counter with on this round.

    Each step is rounded down to the increment, capped at `C − margin`, and
    never below `current_offer + increment`. When the cap does not allow the
    increment, the step stays at the cap — that is the only case where the two
    rules conflict, and holding at the cap is the one that respects the ceiling.
    """
    ladder = urgent_steps if urgent else steps
    if not ladder:
        raise ValueError("at least one concession step is required")

    # A ladder shorter than the number of rounds repeats its last step.
    fraction = ladder[min(round_number, len(ladder)) - 1]

    gap = ceiling - opening
    candidate = floor_to(opening + fraction * gap, increment)

    cap = ceiling - ceiling_margin
    candidate = min(candidate, cap)
    candidate = max(candidate, min(current_offer + increment, cap))
    return candidate
