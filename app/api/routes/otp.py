"""OTP send and verify.

`/otp/send` returns two views in separate, documented fields, and this is the
one place in the API where that matters enough to spell out:

* ``masked_destination`` — agent-facing. Safe to say out loud.
* ``delivery`` — for the platform's send node only. Carries the plaintext code.

The platform keeps them apart by mapping `delivery` into a hidden Tool Call
Result field, which stays available to later nodes and never enters the agent's
conversation context. Nothing in the agent-facing fields repeats it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api import phrasing
from app.api.deps import get_container
from app.api.errors import ApiError
from app.api.schemas import (
    OtpDelivery,
    OtpSendRequest,
    OtpSendResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from app.container import Container
from app.otp import DispatchStatus, OtpFailure

router = APIRouter(tags=["otp"])

_SAY_BY_STATUS = {
    DispatchStatus.ROUTED_ONBOARDING: phrasing.OTP_ROUTED_ONBOARDING,
    DispatchStatus.RESEND_LIMIT_REACHED: phrasing.OTP_RESEND_LIMIT,
    DispatchStatus.ALREADY_VERIFIED: phrasing.OTP_ALREADY_VERIFIED,
    DispatchStatus.LOCKED: phrasing.OTP_LOCKED,
}


def _rate_limit_key(session, run_id: str) -> str:
    """Bucket attempts by MC, so a new `run_id` does not reset the count.

    Falling back to the run id when no MC is bound yet keeps an unstarted call
    from being free to probe.
    """
    mc_number = getattr(session, "mc_number", None)
    return f"mc:{mc_number}" if mc_number else f"run:{run_id}"


@router.post("/otp/send", response_model=OtpSendResponse)
def send_otp(
    payload: OtpSendRequest,
    container: Container = Depends(get_container),
) -> OtpSendResponse:
    """Send a code to the contact on file, or route the call to onboarding."""
    dispatch = container.otp.send(payload.run_id, payload.mc_number)

    delivery = dispatch.delivery_view
    if dispatch.status is DispatchStatus.SENT:
        say = phrasing.OTP_SENT.format(masked=dispatch.masked_destination)
    else:
        say = _SAY_BY_STATUS.get(dispatch.status, phrasing.OTP_LOCKED)

    return OtpSendResponse(
        status=dispatch.status.value,
        masked_destination=dispatch.masked_destination,
        delivery=OtpDelivery(**delivery) if delivery else None,
        result_code=dispatch.result_code,
        say=say,
    )


@router.post("/otp/verify", response_model=OtpVerifyResponse)
def verify_otp(
    payload: OtpVerifyRequest,
    container: Container = Depends(get_container),
) -> OtpVerifyResponse:
    """Check the code. Every attempt counts, valid-looking or not.

    Rate limited **per MC, across calls**. The per-call lock is three attempts;
    without this, opening a new call buys three more, and six digits is small
    enough that a patient caller would eventually find them.
    """
    session = container.store.get(payload.run_id)
    key = _rate_limit_key(session, payload.run_id)
    decision = container.otp_verify_limiter.hit(key)
    if not decision.allowed:
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "otp_rate_limited",
            phrasing.OTP_RATE_LIMITED,
            headers={"Retry-After": str(decision.retry_after)},
        )

    verification = container.otp.verify(payload.run_id, payload.code)

    if verification.verified:
        say = phrasing.OTP_VERIFIED
    elif verification.failure is OtpFailure.EXPIRED:
        say = phrasing.OTP_EXPIRED
    elif verification.failure is OtpFailure.LOCKED:
        say = phrasing.OTP_LOCKED
    elif verification.failure is OtpFailure.NO_CHALLENGE:
        say = phrasing.SESSION_NOT_FOUND
    else:
        say = phrasing.OTP_WRONG.format(attempts_left=verification.attempts_left)

    return OtpVerifyResponse(
        verified=verification.verified,
        attempts_left=verification.attempts_left,
        result_code=verification.result_code,
        say=say,
    )
