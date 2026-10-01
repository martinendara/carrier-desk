"""Failures specific to negotiating.

These are distinct types rather than one `NegotiationError` because phase 3 maps
each to a different thing to say, and a caller needs to tell "there is nothing
to counter" from "you may not touch this load".
"""

from __future__ import annotations

from app.sessions.models import NegotiationOutcome


class NegotiationError(Exception):
    """Base class for negotiation failures."""


class NoActiveNegotiation(NegotiationError):
    """No negotiation has been opened for this call, so there is nothing to counter."""


class NegotiationAlreadyClosed(NegotiationError):
    """The negotiation already ended — accepted or closed out.

    This is what refuses a fourth round: the third one always concludes, so the
    next request finds nothing open.
    """

    def __init__(self, outcome: NegotiationOutcome) -> None:
        self.outcome = outcome
        super().__init__(f"negotiation already closed ({outcome.value})")


class CeilingUnavailable(NegotiationError):
    """No usable `MAX_BUY`, so there is no ceiling to negotiate against.

    Either the field was absent from `LOAD_GET`, or it is too small for the
    increment and margin to leave any room at all. Both mean a human should
    handle this load: without a ceiling we would be inventing a number.
    """
