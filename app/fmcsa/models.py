"""The authority picture we need, and the reasons we can refuse one.

Field names in the FMCSA payload are **TODO: verify**. FMCSA blocks requests
from Argentina (403 on the whole domain), so no real response has been recorded
yet and every key below is read defensively — see `app/fmcsa/client.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IneligibilityReason(str, Enum):
    """Why a carrier cannot be booked. Rendered as `fmcsa_rejected:<reason>`."""

    NOT_FOUND = "not_found"
    NOT_ALLOWED_TO_OPERATE = "not_allowed_to_operate"
    AUTHORITY_INACTIVE = "authority_inactive"
    OUT_OF_SERVICE = "out_of_service"
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
    #: Days since the authority was granted. None when the payload carried no
    #: date we recognised — which onboarding treats as "unknown" and sends to
    #: Review. TODO: verify the date field name.
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
