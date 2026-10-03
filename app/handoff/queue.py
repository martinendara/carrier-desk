"""The mocked senior-rep queue.

`/handoff` has to put something somewhere a human will look. For the POC that
is a list in memory, which is honest about being a placeholder — and the
`handoff_created` event is the durable record, so nothing is lost when the
process restarts.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

#: How long a ticket stays in the queue. The queue is a placeholder and nothing
#: in the service ever reads it back — `handoff_created` is the durable record a
#: rep actually works from — so the only thing retention decides is how long the
#: in-memory copy keeps growing. A day is long enough to look at a demo the next
#: morning and short enough that the list cannot grow without bound.
DEFAULT_RETENTION_SECONDS = 24 * 3600


class HandoffReason(str, Enum):
    """Why a rep is being asked to take over."""

    #: Booked, and confirmed.
    BOOKED = "booked"
    #: Booked, but we could not confirm the reference ourselves.
    BOOKING_PENDING = "booking_pending"
    #: Round three ran out.
    NEGOTIATION_FAILED = "negotiation_failed"
    #: No contact on file, so there was no OTP to send.
    ONBOARDING = "onboarding"
    #: FMCSA refused, or could not be reached.
    FMCSA_REJECTED = "fmcsa_rejected"
    #: The carrier asked for a person.
    REQUESTED = "requested"


@dataclass(frozen=True)
class HandoffTicket:
    """One item in the queue."""

    ticket_id: str
    run_id: str
    reason: HandoffReason
    created_at: datetime
    #: Context for the rep. Internal: it may contain the ceiling.
    summary: dict[str, Any] = field(default_factory=dict, repr=False)


class HandoffQueue:
    """An in-memory senior-rep queue. Not shared between workers.

    Bounded by age. Each ticket holds the call's negotiations, and those hold
    ceilings, so an unbounded list here is an unbounded amount of the one number
    the service is built to keep quiet — sitting in memory for no reason, since
    nothing reads it.
    """

    def __init__(
        self,
        *,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tickets: list[HandoffTicket] = []
        self._retention = timedelta(seconds=retention_seconds)
        # Injected so a test can age a ticket instead of sleeping for a day.
        self._now = clock or (lambda: datetime.now(timezone.utc))
        # Writes come from the threadpool, one thread per in-flight request.
        self._lock = threading.Lock()

    def create(
        self,
        run_id: str,
        reason: HandoffReason,
        summary: dict[str, Any] | None = None,
    ) -> HandoffTicket:
        ticket = HandoffTicket(
            ticket_id=f"rep_{secrets.token_hex(6)}",
            run_id=run_id,
            reason=reason,
            created_at=self._now(),
            summary=summary or {},
        )
        with self._lock:
            self._tickets.append(ticket)
            self._prune(ticket.created_at)
        return ticket

    def pending(self) -> list[HandoffTicket]:
        """Everything waiting. Used by the demo and by the tests."""
        with self._lock:
            now = self._now()
            self._tickets = [t for t in self._tickets if not self._is_stale(t, now)]
            return list(self._tickets)

    def prune(self) -> int:
        """Drop tickets past their retention. Returns how many went.

        The queue is never read by the service, so the sweep has to be driven
        from `create` — there is no reader to notice a stale entry.
        """
        with self._lock:
            return self._prune(self._now())

    def _prune(self, now: datetime) -> int:
        before = len(self._tickets)
        self._tickets = [t for t in self._tickets if not self._is_stale(t, now)]
        return before - len(self._tickets)

    def _is_stale(self, ticket: HandoffTicket, now: datetime) -> bool:
        return now - ticket.created_at > self._retention

    def __len__(self) -> int:
        with self._lock:
            return len(self._tickets)
