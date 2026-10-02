"""OTP send and verify.

The service sends the code itself (see `app/mail.py`), so `/otp/send` returns
only an agent-facing view: a `masked_destination` and a `say`. The plaintext
code never leaves this process and never appears in a response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api import phrasing
from app.api.deps import get_container, session_for
from app.api.errors import ApiError
from app.api.schema_sample import SCHEMA_SAMPLE_RUN_ID, sample_send_otp, sample_verify_otp
from app.api.schemas import (
    OtpSendRequest,
    OtpSendResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from app.container import Container
from app.otp import DispatchStatus, OtpFailure, spoken_destination
from app.results import ResultCode

router = APIRouter(tags=["otp"])

_SAY_BY_STATUS = {
    DispatchStatus.ROUTED_ONBOARDING: phrasing.OTP_ROUTED_ONBOARDING,
    DispatchStatus.RESEND_LIMIT_REACHED: phrasing.OTP_RESEND_LIMIT,
    DispatchStatus.ALREADY_VERIFIED: phrasing.OTP_ALREADY_VERIFIED,
    DispatchStatus.LOCKED: phrasing.OTP_LOCKED,
    DispatchStatus.SEND_FAILED: phrasing.OTP_SEND_FAILED,
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
    """Send a code to the contact on file, or route the call to onboarding.

    The MC comes from the session, not the request: `verify_carrier` recorded
    it. Without a prior verification there is nothing to send to, and it is
    refused rather than sent to whatever MC the agent names.
    """
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_send_otp()

    session = session_for(container, payload.run_id)
    if session.mc_number is None:
        return OtpSendResponse(
            status=DispatchStatus.LOCKED.value,
            masked_destination=None,
            result_code=ResultCode.OTP_FAILED.value,
            say=phrasing.OTP_NOT_VERIFIED,
        )

    dispatch = container.otp.send(payload.run_id, session.mc_number)

    if dispatch.status is DispatchStatus.SENT:
        if dispatch.reviewer:
            say = phrasing.OTP_SENT_REVIEWER
        else:
            say = phrasing.OTP_SENT.format(
                spoken=spoken_destination(dispatch.masked_destination)
            )
    else:
        say = _SAY_BY_STATUS.get(dispatch.status, phrasing.OTP_LOCKED)

    return OtpSendResponse(
        status=dispatch.status.value,
        masked_destination=dispatch.masked_destination,
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
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_verify_otp()

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
