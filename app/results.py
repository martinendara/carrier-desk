"""The closed set of outcomes a call can end in.

Voice-agent workflows branch on these strings, so the set is closed on purpose:
a new outcome has to be added here and announced, rather than appearing as a
typo that silently never matches.

`fmcsa_rejected` is the one parameterised member, because the reason is what a
rep needs to see (`fmcsa_rejected:out_of_service`). It is built through
:func:`fmcsa_rejected` so the prefix cannot drift.
"""

from __future__ import annotations

from enum import Enum


class ResultCode(str, Enum):
    """How a call ended. `str` mixin so it serialises as a plain string."""

    BOOKED = "booked"
    #: The booking is uncertain: a rep confirms it (see DECISIONS.md, LOAD_BOOK).
    BOOKING_PENDING = "booking_pending"
    NEGOTIATION_FAILED = "negotiation_failed"
    NO_MATCH = "no_match"
    OTP_FAILED = "otp_failed"
    #: No contact on file, so the call goes to onboarding instead of an OTP.
    ROUTED_ONBOARDING = "routed_onboarding"

    def __str__(self) -> str:  # pragma: no cover - convenience for f-strings
        return self.value


#: Every prefix that may appear before a `:` in a result code.
PARAMETERISED_PREFIX = "fmcsa_rejected"


def fmcsa_rejected(reason: str) -> str:
    """Build `fmcsa_rejected:<reason>`.

    `reason` comes from the FMCSA client and is never caller-supplied, but it is
    sanitised anyway: it ends up in a string an agent may read out loud.
    """
    # `isascii()` as well as `isalnum()`: `"é".isalnum()` is True, and a reason
    # string is something an agent may read out loud.
    cleaned = "".join(
        char
        for char in str(reason).strip().lower()
        if char.isascii() and (char.isalnum() or char == "_")
    )
    return f"{PARAMETERISED_PREFIX}:{cleaned or 'unknown'}"


#: Result codes that carry no parameters.
SIMPLE_CODES = frozenset(code.value for code in ResultCode)


def is_valid_result_code(code: str) -> bool:
    """True if `code` is a member of the closed set."""
    text = str(code).strip()
    if text in SIMPLE_CODES:
        return True
    if text.startswith(f"{PARAMETERISED_PREFIX}:"):
        return len(text) > len(PARAMETERISED_PREFIX) + 1
    return False
