"""Failures that come from the state of a call, not from the caller's syntax."""

from __future__ import annotations


class SessionError(Exception):
    """Base class for run-session failures."""


class SessionNotFound(SessionError):
    """No session for this run_id, or it aged out of the TTL."""


class LoadNotShown(SessionError):
    """The load was not offered during this call.

    This is the guard that stops an agent — or anything pretending to be one —
    from negotiating a load the carrier was never pitched, which is how a caller
    picks a load out of thin air.
    """

    def __init__(self, load_id: str) -> None:
        self.load_id = load_id
        super().__init__(f"load {load_id} was not shown during this call")


class NegotiationLimitReached(SessionError):
    """The call already used its negotiation budget (original + one alternative)."""
