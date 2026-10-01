"""The authority picture we need, and the reasons we can refuse one.

The field names behind these values are recorded rather than guessed: they come
from the live response captured on 2026-10-01, which lives in
`tests/fixtures/fmcsa_carrier_recorded.json`. See `app/fmcsa/client.py` for
which key feeds which decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IneligibilityReason(str, Enum):
    """Why a carrier cannot be booked. Rendered as `fmcsa_rejected:<reason>`."""

    NOT_FOUND = "not_found"
    NOT_ALLOWED_TO_OPERATE = "not_allowed_to_operate"
    #: Neither common nor contract authority is in force. What a broker has to
    #: check, and what `statusCode` does not tell you: the recorded response
    #: carries `statusCode: "A"` with both authority statuses inactive.
    AUTHORITY_INACTIVE = "authority_inactive"
    OUT_OF_SERVICE = "out_of_service"
    #: FMCSA states the required liability cover and shows nothing filed. Its
    #: own reason, because it is a different phone call from an inactive
    #: authority — this one a rep can fix by asking for a certificate. See
    #: DECISIONS.md §46.
    INSURANCE_NOT_ON_FILE = "insurance_not_on_file"
    #: The response came back but did not carry the fields we need. Treated as
    #: a refusal rather than a pass: see DECISIONS.md on failing closed.
    INCOMPLETE_RESPONSE = "incomplete_response"
    #: FMCSA was unreachable, timed out, or blocked us (the Argentina case).
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


@dataclass(frozen=True)
class AuthorityCheck:
    """The answer the rest of the service branches on.

    `eligible` is the only field a caller should trust for a go/no-go; the rest
    is context for a rep and for the event log.
    """

    eligible: bool
    mc_number: str
    legal_name: str | None = None
    dot_number: str | None = None
    reason: IneligibilityReason | None = None
    #: Days since the authority was granted. **Always None today**: the carrier
    #: payload carries no authority date at all, so there is nothing to measure
    #: from. Onboarding reads unknown as Review, which is the safe direction.
    #: The last open `TODO: verify` — see PROGRESS.md.
    authority_age_days: int | None = None
    #: Fields we parsed but do not model, kept so a mis-guessed key is visible
    #: in the event log instead of silently absent.
    raw: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def rejected(self) -> bool:
        return not self.eligible

    def result_code(self) -> str | None:
        """`fmcsa_rejected:<reason>` when ineligible, else None."""
        if self.eligible:
            return None
        from app.results import fmcsa_rejected

        return fmcsa_rejected(self.reason.value if self.reason else "unknown")
