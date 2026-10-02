"""Booking and the senior-rep handoff."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.api import phrasing
from app.api.deps import get_container, get_deadline, require_verified, session_for
from app.api.schema_sample import SCHEMA_SAMPLE_RUN_ID, sample_book_load, sample_handoff
from app.api.schemas import (
    BookingRequest,
    BookingResponse,
    HandoffRequest,
    HandoffResponse,
)
from app.container import Container
from app.deadline import Deadline
from app.events import EventType
from app.handoff import HandoffReason
from app.results import ResultCode
from app.tms.models import BookingStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["bookings"])


@router.post("/bookings", response_model=BookingResponse)
def create_booking(
    payload: BookingRequest,
    container: Container = Depends(get_container),
    deadline: Deadline = Depends(get_deadline),
) -> BookingResponse:
    """Book the agreed load at the agreed rate.

    The rate comes out of the negotiation session, never from the request, so
    there is nothing for a caller to substitute. An ambiguous outcome is
    `booking_pending` and never `booked` — the client already refuses to guess,
    and this is where that promise is kept in the response the platform sees.
    """
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_book_load()

    # Serialised so two concurrent requests for the same run_id never both
    # book: the second waits here, then returns the first one's stored answer.
    with container.booking_lock:
        session = require_verified(session_for(container, payload.run_id))

        if session.booking_status is not None:
            return _booking_response(session.booking_status, session.booking_ref)

        negotiation = session.agreed_negotiation
        if negotiation is None or negotiation.agreed_rate is None:
            # Nothing to book. Not an error the carrier caused, so it is phrased
            # as a next step rather than a failure.
            from app.api.errors import ApiError

            raise ApiError(409, "no_agreed_rate", phrasing.NO_NEGOTIATION)

        mc_number = session.mc_number
        container.sink.emit(
            payload.run_id,
            EventType.BOOKING_ATTEMPTED,
            load_id=negotiation.load_id,
            mc_number=mc_number,
            agreed_rate=negotiation.agreed_rate,
            ceiling=negotiation.ceiling,
        )

        result = container.tms.book_load(
            negotiation.load_id, mc_number, negotiation.agreed_rate, deadline=deadline
        )

        confirmed = result.status is BookingStatus.BOOKED and result.booking_ref is not None
        session.booking_status = (
            result.status.value if confirmed else BookingStatus.BOOKED_UNCONFIRMED.value
        )
        session.booking_ref = result.booking_ref

        container.sink.emit(
            payload.run_id,
            EventType.BOOKING_RESULT,
            load_id=result.load_id,
            status=result.status.value,
            booking_ref=result.booking_ref,
            ambiguous=result.ambiguous,
        )

        return _booking_response(session.booking_status, session.booking_ref)


@router.post("/handoff", response_model=HandoffResponse)
def create_handoff(
    payload: HandoffRequest,
    container: Container = Depends(get_container),
) -> HandoffResponse:
    """Put this call in the senior-rep queue.

    Deliberately **not** gated by OTP: the handoff is the escape hatch, and a
    carrier with no contact on file has no way to become verified. Gating it
    would turn "route this to a human" into a dead end.
    """
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_handoff()

    session = session_for(container, payload.run_id)

    reason = _parse_reason(payload.reason)
    ticket = container.handoffs.create(
        payload.run_id,
        reason,
        summary={
            "mc_number": session.mc_number,
            "fmcsa_eligible": session.fmcsa_eligible,
            "otp_verified": session.otp_verified,
            "shown_load_ids": list(session.shown_load_ids),
            "negotiations": [
                {
                    "load_id": negotiation.load_id,
                    "outcome": negotiation.outcome.value,
                    "agreed_rate": negotiation.agreed_rate,
                    "rounds": negotiation.round_number,
                    "ceiling": negotiation.ceiling,
                }
                for negotiation in session.negotiations
            ],
        },
    )

    container.sink.emit(
        payload.run_id,
        EventType.HANDOFF_CREATED,
        ticket_id=ticket.ticket_id,
        reason=reason.value,
    )

    return HandoffResponse(ticket_id=ticket.ticket_id, say=phrasing.HANDOFF)


def _booking_response(status: str, booking_ref: str | None) -> BookingResponse:
    """The response for a booking outcome, reconstructed for a retry."""
    if status == BookingStatus.BOOKED.value:
        return BookingResponse(
            status=status,
            booking_ref=booking_ref,
            result_code=ResultCode.BOOKED.value,
            say=phrasing.BOOKED,
        )
    return BookingResponse(
        status=BookingStatus.BOOKED_UNCONFIRMED.value,
        booking_ref=booking_ref,
        result_code=ResultCode.BOOKING_PENDING.value,
        say=phrasing.BOOKING_PENDING,
    )


def _parse_reason(raw: str | None) -> HandoffReason:
    """Map a caller-supplied reason onto the enum, defaulting to `requested`.

    Never an error: a rep being asked to look at a call is the right outcome
    whatever string arrived.
    """
    if not raw:
        return HandoffReason.REQUESTED
    try:
        return HandoffReason(str(raw).strip().lower())
    except ValueError:
        logger.info("unknown handoff reason %r; recording it as 'requested'", raw)
        return HandoffReason.REQUESTED
