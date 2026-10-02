"""Opening a negotiation and answering carrier requests.

The round number never appears in a request or a response. The server counts
the rounds, so a caller cannot skip one, replay one, or restart the ladder by
losing count.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import phrasing
from app.api.deps import get_container, get_deadline, require_verified, session_for
from app.api.schema_sample import (
    SCHEMA_SAMPLE_RUN_ID,
    sample_counter_offer,
    sample_open_negotiation,
)
from app.api.schemas import (
    NegotiationCounterRequest,
    NegotiationCounterResponse,
    NegotiationOpenRequest,
    NegotiationOpenResponse,
)
from app.container import Container
from app.deadline import Deadline
from app.negotiation import NegotiationAction
from app.results import ResultCode

router = APIRouter(tags=["negotiations"])

_SAY = {
    NegotiationAction.ACCEPT: phrasing.OFFER_ACCEPT,
    NegotiationAction.COUNTER: phrasing.OFFER_COUNTER,
    NegotiationAction.CLOSE: phrasing.OFFER_CLOSE,
    NegotiationAction.CLARIFY: phrasing.ASK_CLARIFY,
}


@router.post("/negotiations", response_model=NegotiationOpenResponse)
def open_negotiation(
    payload: NegotiationOpenRequest,
    container: Container = Depends(get_container),
    deadline: Deadline = Depends(get_deadline),
) -> NegotiationOpenResponse:
    """Start negotiating a load that was shown during this call.

    The load is fetched here, server-side, so `MAX_BUY` exists only inside this
    process. The engine reads the ceiling off the fetched record; the caller
    never supplies a number.
    """
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_open_negotiation()

    require_verified(session_for(container, payload.run_id))

    load = container.tms.get_load(payload.load_id, deadline=deadline)
    offer = container.negotiation.open(payload.run_id, load)

    return NegotiationOpenResponse(
        load_id=offer.load_id,
        offer=offer.offer,
        say=phrasing.OFFER_OPEN.format(amount=phrasing.spoken_amount(offer.offer)),
    )


@router.post("/negotiations/counter", response_model=NegotiationCounterResponse)
def counter_offer(
    payload: NegotiationCounterRequest,
    container: Container = Depends(get_container),
) -> NegotiationCounterResponse:
    """One carrier request, one round.

    The response is `{action, amount}` and nothing else. No ceiling, no gap, no
    step — those are what the whole design exists to keep on this side.
    """
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_counter_offer()

    require_verified(session_for(container, payload.run_id))

    decision = container.negotiation.counter(payload.run_id, payload.carrier_ask)

    # Only a close is a terminal result; accept, counter and clarify leave the
    # call open.
    result_code = (
        ResultCode.NEGOTIATION_FAILED.value
        if decision.action is NegotiationAction.CLOSE
        else None
    )

    say = _SAY[decision.action]
    if decision.action in (NegotiationAction.ACCEPT, NegotiationAction.COUNTER):
        say = say.format(amount=phrasing.spoken_amount(decision.amount))

    return NegotiationCounterResponse(
        action=decision.action.value,
        amount=decision.amount,
        result_code=result_code,
        say=say,
    )
