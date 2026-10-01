"""Shared dependencies and the verification gate."""

from __future__ import annotations

from fastapi import Depends, Request

from app.container import Container
from app.deadline import Deadline
from app.sessions import NotVerified, RunSession


def get_container(request: Request) -> Container:
    """The service built at startup, held on `app.state`."""
    return request.app.state.container


def get_deadline(container: Container = Depends(get_container)) -> Deadline:
    """A fresh time budget for this request.

    Created per request rather than per TMS call, so a booking that makes three
    TMS calls still fits inside one budget — which is the whole point of having
    one.
    """
    return Deadline(container.settings.request_budget_seconds)


def session_for(container: Container, run_id: str) -> RunSession:
    """The call's session, started if this is its first tool."""
    return container.store.get_or_create(run_id)


def require_verified(session: RunSession) -> RunSession:
    """Refuse search, negotiation and booking without a verified OTP.

    Called explicitly by each gated handler rather than installed as a FastAPI
    dependency: every gated route takes a different request model, and a
    dependency would have to re-declare it and risk FastAPI treating the body as
    two parameters. The protection against a forgotten call is
    `tests/test_api.py::test_every_route_that_needs_otp_is_gated`, which walks
    the app's own route table.
    """
    if not session.otp_verified:
        raise NotVerified(f"run {session.run_id!r} has not verified an OTP")
    return session
