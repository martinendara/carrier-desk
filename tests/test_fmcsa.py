"""FMCSA client: parsing, eligibility, and failing closed.

The payloads come from `tests/fixtures/fmcsa_*.json`, which are **synthetic**
(see tests/fixtures/README.md): FMCSA returns 403 to the whole domain from
Argentina, so nothing here has been recorded from the live service. These tests
pin the behaviour down; they do not prove the field names are right.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from conftest import load_fmcsa_fixture

from app.fmcsa import FmcsaClient, IneligibilityReason, McValidationError, validate_mc

TODAY = date(2026, 10, 1)


def client_for(payload, *, status_code: int = 200, **overrides) -> FmcsaClient:
    """A client wired to a mock transport that always answers the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(status_code, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return FmcsaClient(
        "https://mobile.fmcsa.dot.gov/qc/services",
        "test-key",
        client=http,
        clock=lambda: TODAY,
        **overrides,
    )


def failing_client(exc: Exception) -> FmcsaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return FmcsaClient(
        "https://mobile.fmcsa.dot.gov/qc/services", "test-key", client=http, clock=lambda: TODAY
    )


# --- MC validation ------------------------------------------------------


@pytest.mark.parametrize("mc", ["1", "123456", "00000001"])
def test_valid_mc_numbers(mc):
    assert validate_mc(mc) == mc


@pytest.mark.parametrize("mc", ["", "abc", "12a456", "123456789", "123-456", "MC123", "1.5"])
def test_invalid_mc_numbers(mc):
    with pytest.raises(McValidationError):
        validate_mc(mc)


def test_an_invalid_mc_never_reaches_the_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = FmcsaClient("https://x.test", "k", client=http)

    with pytest.raises(McValidationError):
        client.check("MC-123")

    assert calls == []


# --- the happy path -----------------------------------------------------


def test_an_eligible_carrier():
    check = client_for(load_fmcsa_fixture("carrier_eligible")).check("123456")

    assert check.eligible
    assert check.legal_name == "RIDGELINE FREIGHT LLC"
    assert check.dot_number == "3456789"
    assert check.reason is None
    assert check.result_code() is None


def test_the_authority_age_is_computed_from_the_date():
    check = client_for(load_fmcsa_fixture("carrier_eligible")).check("123456")

    # addDate 2019-04-11, "today" 2026-10-01. TODO: verify the date key.
    assert check.authority_age_days == (TODAY - date(2019, 4, 11)).days


def test_the_request_carries_the_mc_and_the_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=load_fmcsa_fixture("carrier_eligible"))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    FmcsaClient("https://mobile.fmcsa.dot.gov/qc/services", "secret-key", client=http).check(
        "123456"
    )

    assert "/carriers/docket-number/123456" in seen["url"]
    assert "webKey=secret-key" in seen["url"]


# --- rejections ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("carrier_out_of_service", IneligibilityReason.OUT_OF_SERVICE),
        ("carrier_inactive", IneligibilityReason.AUTHORITY_INACTIVE),
        ("carrier_not_allowed", IneligibilityReason.NOT_ALLOWED_TO_OPERATE),
    ],
)
def test_ineligible_carriers(fixture, reason):
    check = client_for(load_fmcsa_fixture(fixture)).check("123456")

    assert not check.eligible
    assert check.reason is reason
    assert check.result_code() == f"fmcsa_rejected:{reason.value}"


def test_an_empty_result_is_not_found():
    check = client_for(load_fmcsa_fixture("carrier_not_found")).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.NOT_FOUND


def test_a_404_is_not_found_not_an_outage():
    """FMCSA not knowing the MC is an answer, and it routes differently."""
    check = client_for({}, status_code=404).check("123456")

    assert check.reason is IneligibilityReason.NOT_FOUND


def test_the_order_of_checks_follows_the_spec():
    """`not allowed` is reported before `inactive` when both are true."""
    payload = {
        "content": {
            "carrier": {
                "allowedToOperate": "N",
                "statusCode": "I",
                "outOfService": True,
                "legalName": "X",
            }
        }
    }
    check = client_for(payload).check("123456")

    assert check.reason is IneligibilityReason.NOT_ALLOWED_TO_OPERATE


# --- failing closed -----------------------------------------------------


def test_a_payload_without_the_fields_we_need_fails_closed():
    """We would rather send a carrier to a human than guess that they are fine."""
    payload = {"content": {"carrier": {"legalName": "MYSTERY CARRIER"}}}

    check = client_for(payload).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.INCOMPLETE_RESPONSE


def test_a_carrier_with_no_recognised_date_is_still_eligible():
    """A missing date costs the qualification, not the eligibility."""
    check = client_for(load_fmcsa_fixture("carrier_no_date")).check("123456")

    assert check.eligible
    assert check.authority_age_days is None


@pytest.mark.parametrize("code", [403, 500, 502, 429])
def test_an_error_status_fails_closed(code):
    check = client_for({}, status_code=code).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.VERIFICATION_UNAVAILABLE


def test_the_argentina_403_fails_closed():
    """FMCSA blocks the whole domain from here; the call must still behave."""
    check = client_for({}, status_code=403).check("123456")

    assert not check.eligible
    assert check.result_code() == "fmcsa_rejected:verification_unavailable"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("no route to host"),
    ],
)
def test_a_transport_failure_fails_closed(exc):
    check = failing_client(exc).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.VERIFICATION_UNAVAILABLE


def test_a_non_json_body_fails_closed():
    """A block page or a proxy error arrives as 200 text/html."""
    response = httpx.Response(200, text="<html>blocked</html>")
    check = client_for(response).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.VERIFICATION_UNAVAILABLE


# --- defensive parsing --------------------------------------------------


def test_alternate_key_spellings_and_date_formats_still_parse():
    """TODO: verify — these are guesses about how FMCSA might spell things."""
    check = client_for(load_fmcsa_fixture("carrier_alternate_keys")).check("123456")

    assert check.eligible
    assert check.legal_name == "COASTAL BEND CARRIERS"
    assert check.dot_number == "5554443"
    assert check.authority_age_days == (TODAY - date(2024, 3, 15)).days


def test_a_bare_carrier_object_without_a_wrapper_parses():
    payload = {
        "allowedToOperate": "Y",
        "legalName": "NO WRAPPER LLC",
        "statusCode": "A",
        "outOfService": False,
    }

    assert client_for(payload).check("123456").eligible


def test_a_list_content_wrapper_parses():
    payload = {
        "content": [
            {
                "carrier": {
                    "allowedToOperate": "Y",
                    "legalName": "LISTED LLC",
                    "statusCode": "A",
                    "outOfService": False,
                }
            }
        ]
    }

    assert client_for(payload).check("123456").legal_name == "LISTED LLC"


def test_boolean_flags_are_accepted_as_booleans():
    payload = {
        "content": {
            "carrier": {
                "allowedToOperate": True,
                "statusCode": "A",
                "outOfService": False,
                "legalName": "BOOL CO",
            }
        }
    }

    assert client_for(payload).check("123456").eligible


def test_the_payload_is_kept_for_the_event_log():
    check = client_for(load_fmcsa_fixture("carrier_eligible")).check("123456")

    assert check.raw["phyState"] == "GA"
    assert "carrier" not in check.raw  # nested objects are dropped, not dumped
