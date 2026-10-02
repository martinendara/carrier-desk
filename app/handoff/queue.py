"""The mocked senior-rep queue.

`/handoff` has to put something somewhere a human will look. For the POC that
is a list in memory, which is honest about being a placeholder — and the
`handoff_created` event is the durable record, so nothing is lost when the
process restarts.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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
    """An in-memory senior-rep queue. Not shared between workers."""

    def __init__(self) -> None:
        self._tickets: list[HandoffTicket] = []

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
            created_at=datetime.now(timezone.utc),
            summary=summary or {},
        )
        self._tickets.append(ticket)
        return ticket

    def pending(self) -> list[HandoffTicket]:
        """Everything waiting. Used by the demo and by the tests."""
        return list(self._tickets)

    def __len__(self) -> int:
        return len(self._tickets)
