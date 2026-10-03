"""Everything the server remembers about one call.

The call — not the endpoint — is the unit of state, and its key is the `run_id`
the platform injects into every tool. That is what lets the agent stay dumb:
it never carries an `otp_session_id`, a `negotiation_id` or a round number, so
it cannot drop one, replay one, or invent one. The server looks all of that up
from the `run_id` alone.

Grouping the state types here (rather than scattering them across the feature
packages) keeps the dependency arrow one-way: `otp`, `negotiation` and
`onboarding` import from `sessions`, and `sessions` imports from none of them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

#: A call may negotiate the load it was pitched on plus one alternative.
MAX_NEGOTIATIONS_PER_CALL = 2

#: Bound on the shown-loads list. A call shows at most a handful of loads; the
#: cap only exists so a caller looping over search cannot grow the session
#: without limit.
MAX_SHOWN_LOADS = 10


def utcnow() -> datetime:
    """The one clock this package reads. Injected everywhere else for tests."""
    return datetime.now(timezone.utc)


class OtpStatus(str, Enum):
    """Where an OTP challenge stands."""

    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"
    LOCKED = "locked"


class OtpChallenge(BaseModel):
    """A code that was sent, and what has happened to it since.

    Only the HMAC of the code is kept. The plaintext exists for the few
    milliseconds it takes to hand it to the delivery node, and is never stored
    and never returned to the agent.
    """

    code_hmac: str
    #: Full destination. Server-side only: it is what the platform's delivery
    #: node needs, and it is never in an agent-facing field.
    destination: str
    #: What the agent may say out loud: `m***@gmail.com`.
    masked_destination: str
    created_at: datetime
    expires_at: datetime
    max_attempts: int = 3
    attempts: int = 0
    resends: int = 0
    max_resends: int = 1
    locked: bool = False
    verified_at: datetime | None = None

    def status(self, now: datetime | None = None) -> OtpStatus:
        if self.verified_at is not None:
            return OtpStatus.VERIFIED
        if self.locked:
            return OtpStatus.LOCKED
        if self.is_expired(now):
            return OtpStatus.EXPIRED
        return OtpStatus.PENDING

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    @property
    def resends_left(self) -> int:
        return max(0, self.max_resends - self.resends)


class NegotiationOutcome(str, Enum):
    """How a negotiation ended."""

    OPEN = "open"
    ACCEPTED = "accepted"
    #: Round 3 ran out. No transfer; the gap is recorded for the rep.
    CLOSED = "closed"


class NegotiationSession(BaseModel):
    """One negotiation, with the ceiling and the round counter server-side.

    `ceiling` is `MAX_BUY`. It is `repr=False` so it stays out of casual logging,
    and :meth:`agent_view` is the only thing that may be serialised towards the
    agent — it has no ceiling, no gap and no step.
    """

    negotiation_id: str
    run_id: str
    load_id: str
    #: MAX_BUY. Never leaves the server. See CLAUDE.md principle 1.
    ceiling: int = Field(repr=False)
    #: The posted rate the opening offer was derived from.
    posted_rate: int
    opening_offer: int
    current_offer: int
    urgent: bool = False
    round_number: int = 0
    outcome: NegotiationOutcome = NegotiationOutcome.OPEN
    agreed_rate: int | None = None
    #: `request - ceiling` when the negotiation closed without a deal. Internal.
    gap: int | None = Field(default=None, repr=False)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # --- counter replay (idempotency) ------------------------------------
    #: The last counter this negotiation answered. A webhook retry of the same
    #: ask inside a short window replays the same decision instead of burning a
    #: round. All `repr=False`: none of this is agent-facing.
    last_counter_ask: int | None = Field(default=None, repr=False)
    last_counter_action: str | None = Field(default=None, repr=False)
    last_counter_amount: int | None = Field(default=None, repr=False)
    last_counter_at: datetime | None = Field(default=None, repr=False)

    @property
    def is_open(self) -> bool:
        return self.outcome is NegotiationOutcome.OPEN

    def agent_view(self, *, action: str, amount: int) -> dict[str, object]:
        """The whole of what the agent is allowed to know about a decision."""
        return {"action": action, "amount": amount}


class RunSession(BaseModel):
    """The state of one call, keyed by the platform's `run_id`."""

    run_id: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    #: The MC this call is about. **Only `OtpService.send` sets this**, and the
    #: routes that merely look a carrier up must not touch it: if a lookup could
    #: move it, an OTP verified for one MC would authorise another.
    mc_number: str | None = None
    fmcsa_eligible: bool | None = None
    #: What FMCSA said about the authority's age, kept so a later step can
    #: qualify the lead without a second lookup. **None for every carrier
    #: today**: the record carries no authority date (DECISIONS §48), which is
    #: why onboarding reads it as `Review`.
    authority_age_days: int | None = None

    otp: OtpChallenge | None = None

    #: Loads pitched to the carrier during this call, in the order shown.
    #: Only these may be negotiated.
    shown_load_ids: list[str] = Field(default_factory=list)

    negotiations: list[NegotiationSession] = Field(default_factory=list)

    #: The booking outcome for this call, stored once made. A retry (the
    #: platform cannot set zero retries) returns the same answer instead of a
    #: second LOAD_BOOK. `booking_status` is the response status ("booked" or
    #: "booked_unconfirmed"); None until the first booking completes.
    booking_status: str | None = None
    booking_ref: str | None = None

    # --- shown loads ----------------------------------------------------

    def record_shown_load(self, load_id: str) -> None:
        """Remember that this load was pitched, idempotently."""
        if load_id in self.shown_load_ids:
            return
        if len(self.shown_load_ids) >= MAX_SHOWN_LOADS:
            # Drop the oldest: the recent ones are the ones still being
            # discussed, and the cap exists only to bound growth.
            self.shown_load_ids.pop(0)
        self.shown_load_ids.append(load_id)
        self.touch()

    def was_shown(self, load_id: str) -> bool:
        return load_id in self.shown_load_ids

    # --- OTP ------------------------------------------------------------

    @property
    def otp_verified(self) -> bool:
        return self.otp is not None and self.otp.verified_at is not None

    # --- negotiations ---------------------------------------------------

    @property
    def active_negotiation(self) -> NegotiationSession | None:
        for negotiation in reversed(self.negotiations):
            if negotiation.is_open:
                return negotiation
        return None

    @property
    def agreed_negotiation(self) -> NegotiationSession | None:
        """The most recent negotiation that ended in an agreement.

        This is what booking reads its rate from. The rate never travels through
        the agent, so there is nothing for a caller to substitute.
        """
        for negotiation in reversed(self.negotiations):
            if negotiation.outcome is NegotiationOutcome.ACCEPTED:
                return negotiation
        return None

    @property
    def can_open_negotiation(self) -> bool:
        return len(self.negotiations) < MAX_NEGOTIATIONS_PER_CALL

    def add_negotiation(self, negotiation: NegotiationSession) -> None:
        self.negotiations.append(negotiation)
        self.touch()

    # --- bookkeeping ----------------------------------------------------

    def touch(self) -> None:
        self.updated_at = utcnow()

    def is_verified_for(self, mc_number: str) -> bool:
        """True when this call verified an OTP for exactly this MC."""
        return self.otp_verified and self.mc_number == str(mc_number).strip()
