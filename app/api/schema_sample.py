"""Canned responses for the ``schema-sample`` run id.

The HappyRobot "Generate" button on a Tool Call Result fires a real request with
example values. There is no session for it, so a normal handler would refuse or
write an ``error`` field and the platform would capture an empty schema. When
``run_id`` is exactly ``"schema-sample"``, each tool returns a canned response
that carries every field of its real response, with obvious placeholder values,
so the platform can capture the shape.

Nothing here touches state: no session, no TMS, no FMCSA, no event. The check
sits at the top of each handler, before any of that runs, and ``X-API-Key`` is
still required because auth is a router-level dependency that runs first.
"""

from __future__ import annotations

from app.api.schemas import (
    SCHEMA_SAMPLE_RUN_ID,
    BookingResponse,
    CarrierVerifyResponse,
    HandoffResponse,
    LoadSearchResponse,
    NegotiationCounterResponse,
    NegotiationOpenResponse,
    OtpSendResponse,
    OtpVerifyResponse,
)

#: A load with every public field a real search returns. No `rate`, no
#: `max_buy` — the same shape the agent actually sees.
SAMPLE_LOAD: dict[str, object] = {
    "load_id": "LD-SAMPLE",
    "orig_city": "Chicago",
    "orig_state": "IL",
    "orig_zip": "60607",
    "dest_city": "Columbus",
    "dest_state": "OH",
    "dest_zip": "43215",
    "pickup_dt": "2026-10-06T12:00:00Z",
    "eqtype": "DRY_VAN",
    "miles": 300,
    "status": "OPEN",
    "delivery_dt": "2026-10-09T12:00:00Z",
    "weight": 24000,
    "commodity": "General freight",
    "pieces": 24,
    "dims": "48x40x48",
    "notes": "",
}


def sample_verify_carrier() -> CarrierVerifyResponse:
    return CarrierVerifyResponse(
        eligible=True,
        result_code=None,
        legal_name="SAMPLE CARRIER LLC",
        say="Thanks, I've found your carrier record.",
    )


def sample_send_otp() -> OtpSendResponse:
    return OtpSendResponse(
        status="sent",
        masked_destination="s***@example.com",
        result_code=None,
        say="I've sent a six digit code to the email we have on file, s***@example.com.",
    )


def sample_verify_otp() -> OtpVerifyResponse:
    return OtpVerifyResponse(
        verified=True,
        attempts_left=3,
        result_code=None,
        say="Thanks, that's confirmed.",
    )


def sample_search_loads() -> LoadSearchResponse:
    return LoadSearchResponse(
        loads=[SAMPLE_LOAD],
        count=1,
        result_code=None,
        say="I've got 1 load that could work.",
    )


def sample_open_negotiation() -> NegotiationOpenResponse:
    return NegotiationOpenResponse(
        load_id="LD-SAMPLE",
        offer=1500,
        say="I can offer 1500 dollars on that load.",
    )


def sample_counter_offer() -> NegotiationCounterResponse:
    return NegotiationCounterResponse(
        action="counter",
        amount=1600,
        result_code=None,
        say="I can go up to 1600 dollars.",
    )


def sample_book_load() -> BookingResponse:
    return BookingResponse(
        status="booked",
        booking_ref="BR-SAMPLE",
        result_code="booked",
        say="You're all set — that load is booked.",
    )


def sample_handoff() -> HandoffResponse:
    return HandoffResponse(
        ticket_id="TK-SAMPLE",
        say="I've asked a rep to pick this up. They'll be with you shortly.",
    )
