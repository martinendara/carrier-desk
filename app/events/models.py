"""What the service records about its own decisions.

The service logs **what it decided**; the platform records the conversation.
Keeping the two apart is what makes it safe for an event to contain the buy
ceiling: nothing in here is ever handed back to the agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """The closed set of events. Adding one means adding it to CLAUDE.md §9."""

    FMCSA_CHECKED = "fmcsa_checked"
    OTP_SENT = "otp_sent"
    OTP_VERIFIED = "otp_verified"
    OTP_FAILED = "otp_failed"
    #: Recorded even when the result count is zero: "we searched and found
    #: nothing" is the fact a rep needs, and it is the one most easily lost.
    SEARCH_PERFORMED = "search_performed"
    LOAD_PITCHED = "load_pitched"
    NEGOTIATION_OPENED = "negotiation_opened"
    NEGOTIATION_ROUND = "negotiation_round"
    NEGOTIATION_CLOSED = "negotiation_closed"
    BOOKING_ATTEMPTED = "booking_attempted"
    BOOKING_RESULT = "booking_result"
    HANDOFF_CREATED = "handoff_created"
    LEAD_CREATED = "lead_created"


class Event(BaseModel):
    """One thing that happened, tied to the call that caused it."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    type: EventType
    #: UTC, always. Never rendered for the agent.
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_jsonl(self) -> str:
        """One line of JSON, ready to append to a file."""
        return self.model_dump_json()
