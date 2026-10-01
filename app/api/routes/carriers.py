"""Carrier verification against FMCSA."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import phrasing
from app.api.deps import get_container, session_for
from app.api.schemas import CarrierVerifyRequest, CarrierVerifyResponse
from app.container import Container
from app.events import EventType
from app.fmcsa import IneligibilityReason

router = APIRouter(tags=["carriers"])

#: What we say for each way FMCSA can refuse. Anything unmapped falls back to
#: the generic line rather than leaking the reason code to the carrier.
_SAY_BY_REASON = {
    IneligibilityReason.NOT_FOUND: phrasing.CARRIER_NOT_FOUND,
    IneligibilityReason.NOT_ALLOWED_TO_OPERATE: phrasing.CARRIER_NOT_ALLOWED,
    IneligibilityReason.AUTHORITY_INACTIVE: phrasing.CARRIER_INACTIVE,
    IneligibilityReason.OUT_OF_SERVICE: phrasing.CARRIER_OUT_OF_SERVICE,
    IneligibilityReason.INSURANCE_NOT_ON_FILE: phrasing.CARRIER_INSURANCE,
}


@router.post("/carriers/verify", response_model=CarrierVerifyResponse)
def verify_carrier(
    payload: CarrierVerifyRequest,
    container: Container = Depends(get_container),
) -> CarrierVerifyResponse:
    """Look the MC up and decide whether this carrier can be booked.

    Not gated by OTP: this is the step that happens *before* there is anything
    to verify, and it is also what tells us whether there is a contact to send a
    code to.
    """
    session = session_for(container, payload.run_id)
    check = container.fmcsa.check(payload.mc_number)

    # Note: this deliberately does not touch `session.mc_number`. That field is
    # what binds a verified OTP to an MC, and only OtpService.send may set it.
    session.fmcsa_eligible = check.eligible

    container.sink.emit(
        payload.run_id,
        EventType.FMCSA_CHECKED,
        mc_number=check.mc_number,
        eligible=check.eligible,
        reason=check.reason.value if check.reason else None,
        authority_age_days=check.authority_age_days,
        legal_name=check.legal_name,
        dot_number=check.dot_number,
    )

    if check.eligible:
        say = phrasing.CARRIER_ELIGIBLE
    else:
        say = _SAY_BY_REASON.get(check.reason, phrasing.CARRIER_UNVERIFIED)

    # The reason stays on this side of the wire: the agent gets a code
    # (`fmcsa_rejected:...`) and a sentence, never the detail behind either.
    return CarrierVerifyResponse(
        eligible=check.eligible,
        result_code=check.result_code(),
        legal_name=check.legal_name,
        say=say,
    )
