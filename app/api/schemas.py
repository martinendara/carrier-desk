"""Request and response bodies.

Two conventions hold across the whole surface:

* **Every request carries `run_id`**, injected by the platform as a fixed
  parameter. No request accepts an `otp_session_id`, a `negotiation_id` or a
  round number, and `extra="forbid"` makes sending one a 422 instead of a
  silently ignored field.
* **Responses are flat and short.** The agent reads them aloud, so a nested
  three-level structure is a liability, and any field that would make a sentence
  longer than a breath does not belong.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The run_id the platform's "Generate" button sends to capture a tool's schema.
SCHEMA_SAMPLE_RUN_ID = "schema-sample"

#: Anything the agent might be asked to say. Kept out of the way at the bottom.
RUN_ID = Field(
    ...,
    min_length=1,
    max_length=128,
    description="Fixed parameter injected by the platform into every tool call.",
)


class Request(BaseModel):
    """Base for every request body."""

    # `forbid` is the guard that keeps an id the agent should never carry from
    # being quietly accepted and ignored.
    model_config = ConfigDict(extra="forbid")


class RunRequest(Request):
    run_id: str = RUN_ID

    @field_validator("run_id")
    @classmethod
    def _strip_run_id(cls, value: str) -> str:
        """Trim surrounding spaces: the platform's Generate button sends
        `"schema-sample   "`, and a real run_id must not carry stray whitespace."""
        return value.strip()


# --- carriers -----------------------------------------------------------


class CarrierVerifyRequest(RunRequest):
    mc_number: str | None = Field(default=None, description="MC number, digits only, 1-8 characters.")

    @model_validator(mode="after")
    def _require_mc_number(self) -> "CarrierVerifyRequest":
        if self.run_id != SCHEMA_SAMPLE_RUN_ID and self.mc_number is None:
            raise ValueError("mc_number is required")
        return self


class CarrierVerifyResponse(BaseModel):
    """What the agent may be told about a carrier.

    Deliberately four fields, and the instruction was "eligible, legal_name and
    say". `result_code` stays because CLAUDE.md §8 makes the closed set of
    result codes the contract the platform routes on, and the reason is in it
    as a suffix (`fmcsa_rejected:insurance_not_on_file`).

    Nothing from the FMCSA record beyond the name: no DOT number, no address,
    no insurance detail, no tax id. The agent cannot read out what it was never
    handed, and a field that is not here cannot end up in a transcript.
    """

    eligible: bool
    result_code: str | None = None
    legal_name: str | None = None
    say: str


# --- OTP ----------------------------------------------------------------


class OtpSendRequest(RunRequest):
    """Ask for a code for the carrier this call already verified.

    There is deliberately no `mc_number` here: the MC is whatever
    `verify_carrier` recorded for this `run_id`, so a caller cannot verify one
    carrier and then request a code for another.
    """


class OtpSendResponse(BaseModel):
    status: str = Field(..., description="sent | routed_onboarding | resend_limit_reached | already_verified | locked | send_failed")
    masked_destination: str | None = Field(
        default=None,
        description="AGENT-FACING. Safe to speak: 'm***@gmail.com'.",
    )
    result_code: str | None = None
    say: str


class OtpVerifyRequest(RunRequest):
    code: str | None = Field(default=None, description="The six digits the carrier read back.")

    @model_validator(mode="after")
    def _require_code(self) -> "OtpVerifyRequest":
        if self.run_id != SCHEMA_SAMPLE_RUN_ID and self.code is None:
            raise ValueError("code is required")
        return self


class OtpVerifyResponse(BaseModel):
    verified: bool
    attempts_left: int
    result_code: str | None = None
    say: str


# --- loads --------------------------------------------------------------


class LoadSearchRequest(RunRequest):
    orig_city: str | None = None
    orig_state: str | None = None
    orig_zip: str | None = None
    dest_city: str | None = None
    dest_state: str | None = None
    dest_zip: str | None = None
    equipment: str | None = Field(default=None, description="Short code, e.g. DRY_VAN.")
    pickup_date: str | None = Field(
        default=None,
        description="YYYYMMDD or YYYYMMDDHHMMSS. The server-side field name is unconfirmed.",
    )


class LoadSearchResponse(BaseModel):
    #: Never contains MAX_BUY: the loads are serialised with `public_dict()`.
    loads: list[dict[str, Any]]
    count: int
    result_code: str | None = None
    say: str


class LoadDetailResponse(BaseModel):
    load: dict[str, Any]
    say: str


# --- negotiation --------------------------------------------------------


class NegotiationOpenRequest(RunRequest):
    load_id: str | None = Field(default=None, description="A load that was shown during this call.")

    @model_validator(mode="after")
    def _require_load_id(self) -> "NegotiationOpenRequest":
        if self.run_id != SCHEMA_SAMPLE_RUN_ID and self.load_id is None:
            raise ValueError("load_id is required")
        return self


class NegotiationOpenResponse(BaseModel):
    load_id: str
    offer: int = Field(..., description="The opening offer, in dollars.")
    say: str


class NegotiationCounterRequest(RunRequest):
    carrier_ask: int | None = Field(default=None, description="What the carrier asked for, in dollars.")

    @model_validator(mode="after")
    def _require_carrier_ask(self) -> "NegotiationCounterRequest":
        if self.run_id != SCHEMA_SAMPLE_RUN_ID and self.carrier_ask is None:
            raise ValueError("carrier_ask is required")
        return self


class NegotiationCounterResponse(BaseModel):
    action: str = Field(..., description="accept | counter | close | clarify")
    amount: int | None = Field(
        ...,
        description="The agreed rate, the counter, or the last offer; null for clarify.",
    )
    result_code: str | None = Field(
        default=None,
        description="negotiation_failed when the ladder closes without a deal; null otherwise.",
    )
    say: str


# --- booking and handoff ------------------------------------------------


class BookingRequest(RunRequest):
    pass


class BookingResponse(BaseModel):
    status: str = Field(..., description="booked | booking_pending")
    booking_ref: str | None = None
    result_code: str
    say: str


class HandoffRequest(RunRequest):
    reason: str | None = Field(
        default=None, description="Optional: why the call is being routed to a rep."
    )


class HandoffResponse(BaseModel):
    ticket_id: str
    say: str


class ErrorResponse(BaseModel):
    """Every failure looks like this. No internal detail, ever."""

    error: str
    say: str
