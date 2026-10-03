"""FMCSA client: parsing, eligibility, and failing closed.

`tests/fixtures/fmcsa_carrier_recorded.json` is a **real response** — the live
API answering MC 123456 on 2026-10-01, transcribed out of the service log of
the US deploy. Every other case here is that payload with named fields changed
(`fmcsa_payload`), so the distance between an eligible carrier and each way of
refusing one is one key, visible in the test that asserts it.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from conftest import ELIGIBLE_OVERRIDES, fmcsa_payload, load_fmcsa_fixture

from app.fmcsa import FmcsaClient, IneligibilityReason, McValidationError, validate_mc
from app.fmcsa.client import PAYLOAD_LOG_LIMIT


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
        **overrides,
    )


def failing_client(exc: Exception) -> FmcsaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return FmcsaClient(
        "https://mobile.fmcsa.dot.gov/qc/services", "test-key", client=http
    )


def recorded_carrier(**overrides) -> dict:
    """The recorded payload made bookable, with `overrides` applied on top.

    The two fields that stand between the real carrier and a booking are the
    authority and the insurance filing, so most tests start from the version
    where both are in order and change the one thing they are about.
    """
    return fmcsa_payload(**{**ELIGIBLE_OVERRIDES, **overrides})


def routing_client(routes: dict[str, object], calls: list[str]) -> FmcsaClient:
    """A client that answers per path, and records what was asked for.

    `routes` maps a fragment of the URL to a payload or to a whole response.
    The carrier lookup and the authority lookup are different endpoints, so a
    client that answers both the same way cannot tell one from the other.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url.split("?")[0])
        for fragment, reply in routes.items():
            if fragment in url:
                if isinstance(reply, httpx.Response):
                    return reply
                return httpx.Response(200, json=reply)
        return httpx.Response(404, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return FmcsaClient("https://mobile.fmcsa.dot.gov/qc/services", "test-key", client=http)


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
    check = client_for(recorded_carrier()).check("123456")

    assert check.eligible
    assert check.legal_name == "B MARRON LOGISTICS LLC"
    # An int in the response, a string on the way out: the docs describe a
    # string, the live API sends a number, and both are tolerated.
    assert check.dot_number == "3177404"
    assert check.reason is None
    assert check.result_code() is None


def test_the_authority_age_is_unknown_because_the_payload_has_no_date():
    """The last open `TODO: verify`. Unknown, not guessed, and not zero."""
    check = client_for(recorded_carrier()).check("123456")

    assert check.eligible
    assert check.authority_age_days is None


def test_the_request_carries_the_mc_and_the_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        # The recorded carrier has no active authority, so this is the only
        # request the check makes and `seen` cannot be overwritten.
        return httpx.Response(200, json=load_fmcsa_fixture("carrier_recorded"))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    FmcsaClient("https://mobile.fmcsa.dot.gov/qc/services", "secret-key", client=http).check(
        "123456"
    )

    assert "/carriers/docket-number/123456" in seen["url"]
    assert "webKey=secret-key" in seen["url"]


# --- rejections ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        # The carrier as FMCSA actually sent it: allowed to operate, an active
        # carrier record, and no operating authority of either kind.
        ({"commonAuthorityStatus": "I", "contractAuthorityStatus": "N"}, IneligibilityReason.AUTHORITY_INACTIVE),
        ({"allowedToOperate": "N"}, IneligibilityReason.NOT_ALLOWED_TO_OPERATE),
        ({"oosDate": "20260115"}, IneligibilityReason.OUT_OF_SERVICE),
        ({"bipdInsuranceOnFile": "0"}, IneligibilityReason.INSURANCE_NOT_ON_FILE),
    ],
)
def test_ineligible_carriers(fields, reason):
    check = client_for(fmcsa_payload(**{**ELIGIBLE_OVERRIDES, **fields})).check("123456")

    assert not check.eligible
    assert check.reason is reason
    assert check.result_code() == f"fmcsa_rejected:{reason.value}"


def test_contract_authority_on_its_own_is_enough():
    """A carrier may run on either kind. Common is not the only way in."""
    payload = fmcsa_payload(
        commonAuthorityStatus="I", contractAuthorityStatus="A", bipdInsuranceOnFile="1"
    )

    assert client_for(payload).check("123456").eligible


def test_the_recorded_carrier_is_refused_for_its_authority_not_its_insurance():
    """Both are true of MC 123456. The authority is the one a rep can act on.

    The order is deliberate: the authority is a statement about the carrier,
    the insurance filing is a document that lags it.
    """
    check = client_for(load_fmcsa_fixture("carrier_recorded")).check("123456")

    assert check.reason is IneligibilityReason.AUTHORITY_INACTIVE


def test_an_unknown_insurance_requirement_is_not_a_rejection():
    """"u" means FMCSA states no requirement, so there is nothing to hold to."""
    payload = recorded_carrier(bipdInsuranceRequired="u", bipdInsuranceOnFile="0")

    assert client_for(payload).check("123456").eligible


def test_status_code_alone_does_not_make_a_carrier_eligible():
    """`statusCode` is the carrier record, not the authority.

    The recorded response is the proof: an active carrier record (`"A"`) with
    both authority statuses inactive. Reading the first as the second would
    have booked a carrier that cannot legally haul.
    """
    payload = fmcsa_payload(
        statusCode="A",
        commonAuthorityStatus="I",
        contractAuthorityStatus="N",
        bipdInsuranceOnFile="1",
    )

    assert client_for(payload).check("123456").reason is IneligibilityReason.AUTHORITY_INACTIVE


def test_an_empty_result_is_not_found():
    check = client_for(load_fmcsa_fixture("carrier_not_found")).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.NOT_FOUND


def test_a_404_is_not_found_not_an_outage():
    """FMCSA not knowing the MC is an answer, and it routes differently."""
    check = client_for({}, status_code=404).check("123456")

    assert check.reason is IneligibilityReason.NOT_FOUND


def test_the_order_of_checks_follows_the_spec():
    """Everything is wrong at once; the first rule in the order is reported.

    A rep reads one reason and starts there, so which one comes first is a
    decision, not an accident: "not allowed to operate" is the most
    fundamental statement about a carrier, and the insurance filing the least.
    """
    payload = fmcsa_payload(
        allowedToOperate="N",
        commonAuthorityStatus="I",
        contractAuthorityStatus="N",
        oosDate="20260115",
        bipdInsuranceOnFile="0",
    )
    check = client_for(payload).check("123456")

    assert check.reason is IneligibilityReason.NOT_ALLOWED_TO_OPERATE


# --- failing closed -----------------------------------------------------


def test_a_payload_without_the_fields_we_need_fails_closed():
    """We would rather send a carrier to a human than guess that they are fine."""
    payload = {"content": {"carrier": {"legalName": "MYSTERY CARRIER"}}}

    check = client_for(payload).check("123456")

    assert not check.eligible
    assert check.reason is IneligibilityReason.INCOMPLETE_RESPONSE


def test_a_record_with_no_required_fields_at_all_is_not_eligible():
    """An empty carrier object is not a carrier that passes."""
    check = client_for({"content": {"carrier": {"legalName": "MYSTERY CARRIER"}}}).check("123456")

    assert check.reason is IneligibilityReason.INCOMPLETE_RESPONSE


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


# --- the authority record -----------------------------------------------
#
# The last open `TODO: verify`: the carrier payload has no authority date, so
# the age comes from this second endpoint — whose field names are as unverified
# as the carrier payload's were. Same way out: log the record and read it.


def test_the_authority_record_is_fetched_once_the_authority_is_active(caplog):
    calls: list[str] = []
    client = routing_client(
        {
            "docket-number": recorded_carrier(),
            "/authority": {"content": [{"carrierAuthority": {"originalActionDate": "2019-04-11"}}]},
        },
        calls,
    )

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        check = client.check("123456")
        client.join_recon()  # the authority fetch runs on a background thread

    assert check.eligible
    assert check.authority_active
    assert calls[-1].endswith("/carriers/3177404/authority")
    # Keys and values, so tomorrow's parsing is a transcription and not a guess.
    assert "originalActionDate" in caplog.text
    assert "2019-04-11" in caplog.text


def test_the_authority_record_is_not_fetched_when_the_authority_is_inactive():
    """No authority means no date worth a round trip, and none is spent."""
    calls: list[str] = []
    client = routing_client({"docket-number": load_fmcsa_fixture("carrier_recorded")}, calls)

    check = client.check("123456")

    assert check.reason is IneligibilityReason.AUTHORITY_INACTIVE
    assert not check.authority_active
    assert calls == ["https://mobile.fmcsa.dot.gov/qc/services/carriers/docket-number/123456"]


def test_the_authority_record_is_not_fetched_when_the_dot_number_is_missing():
    calls: list[str] = []
    payload = recorded_carrier()
    del payload["content"][0]["carrier"]["dotNumber"]
    client = routing_client({"docket-number": payload}, calls)

    check = client.check("123456")

    assert check.eligible
    assert check.dot_number is None
    assert len(calls) == 1


@pytest.mark.parametrize("reply", [httpx.Response(404), httpx.Response(500)])
def test_a_failed_authority_lookup_does_not_change_the_verdict(reply, caplog):
    """It is enrichment on a call the carrier is already on. It cannot decide."""
    calls: list[str] = []
    client = routing_client({"docket-number": recorded_carrier(), "/authority": reply}, calls)

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        check = client.check("123456")
        client.join_recon()  # the authority fetch runs on a background thread

    assert check.eligible
    assert check.authority_age_days is None
    assert str(reply.status_code) in caplog.text  # a wrong path shows up as a 404


def test_the_authority_log_never_carries_the_web_key(caplog):
    secret = "web-key-that-must-not-reach-a-log"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/authority" in str(request.url):
            return httpx.Response(200, json={"content": [{"note": secret}]})
        return httpx.Response(200, json=recorded_carrier())

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = FmcsaClient("https://mobile.fmcsa.dot.gov/qc/services", secret, client=http)

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        client.check("123456")
        client.join_recon()  # the authority fetch runs on a background thread

    assert secret not in caplog.text
    assert "***" in caplog.text
    # Nor the URL it was attached to.
    assert "webKey" not in caplog.text


# --- the wrapper around the carrier -------------------------------------
#
# The response we recorded was logged as the carrier object itself — the
# wrapper is what our own parser stripped on the way in, so it is the one thing
# that response did not settle. These pin the tolerance that remains.


def test_a_bare_carrier_object_without_a_wrapper_parses():
    payload = recorded_carrier()["content"][0]["carrier"]

    assert client_for(payload).check("123456").eligible


def test_a_content_object_wrapper_parses():
    payload = {"content": recorded_carrier()["content"][0]}

    assert client_for(payload).check("123456").legal_name == "B MARRON LOGISTICS LLC"


def test_boolean_flags_are_accepted_as_booleans():
    payload = recorded_carrier(allowedToOperate=True)

    assert client_for(payload).check("123456").eligible


def test_the_payload_is_kept_for_the_event_log():
    check = client_for(recorded_carrier()).check("123456")

    assert check.raw["phyState"] == "GA"
    # Nested containers are dropped, not dumped...
    assert "carrierOperation" not in check.raw
    # ...and the tax id is not kept at all.
    assert check.raw["ein"] == "***"


# --- what we log when the shape is not what we expected -----------------
#
# These logs are how the `TODO: verify` markers get closed: FMCSA answers from
# a US region only, so the first real payload is read out of a production log
# rather than off a developer's screen. That makes this text a deliverable, and
# it is pinned like one.


def _warnings(caplog) -> str:
    return caplog.text


def test_an_unrecognised_field_set_is_logged_with_keys_and_values(caplog):
    """Names tell us what we guessed wrong; values tell us what is right."""
    payload = {
        "content": {
            "carrier": {
                "legalName": "MYSTERY CARRIER",
                "dockets": [{"number": "MC123456", "statusCode": "A"}],
            }
        }
    }

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        client_for(payload).check("123456")

    logged = _warnings(caplog)
    assert "legalName" in logged
    assert "MYSTERY CARRIER" in logged
    # Nested values too: a `statusCode` that lives under `dockets` is exactly
    # the kind of thing this line exists to reveal.
    assert "MC123456" in logged


def test_the_top_level_keys_survive_a_truncation(caplog):
    """`top_level` is printed separately so a long body cannot hide the names."""
    payload = {
        "content": {
            "carrier": {
                "legalName": "BIG CO",
                "somethingWeNeverGuessed": "x" * 5000,
            }
        }
    }

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        client_for(payload).check("123456")

    logged = _warnings(caplog)
    assert "somethingWeNeverGuessed" in logged
    assert "chars]" in logged
    assert len(logged) < PAYLOAD_LOG_LIMIT + 1000


def test_a_response_with_no_carrier_object_is_logged(caplog):
    """200 with a shape we do not recognise is worth seeing, not just guessing."""
    payload = {"status": "ok", "detail": "nothing that looks like a carrier"}

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        check = client_for(payload).check("123456")

    assert check.reason is IneligibilityReason.NOT_FOUND
    assert "no carrier object" in _warnings(caplog)
    assert "nothing that looks like a carrier" in _warnings(caplog)


def test_the_log_line_never_carries_the_web_key(caplog):
    """The key is a query parameter, but a body that echoed it must not leak it."""
    secret = "web-key-that-must-not-reach-a-log"
    payload = {"content": {"carrier": {"legalName": "ECHO CO", "echo": secret}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = FmcsaClient("https://x.test", secret, client=http)

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        client.check("123456")

    assert secret not in _warnings(caplog)
    assert "***" in _warnings(caplog)


def test_a_recognised_carrier_payload_is_not_dumped_into_the_log(caplog):
    """Otherwise every healthy call in production writes a carrier record.

    The authority record is the exception, and deliberately so: it is the one
    whose field names are still unknown, so it is the one that gets printed.
    """
    calls: list[str] = []
    client = routing_client(
        {"docket-number": recorded_carrier(), "/authority": {"content": []}}, calls
    )

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        check = client.check("123456")

    assert check.eligible
    assert "B MARRON LOGISTICS LLC" not in _warnings(caplog)
    assert "did not carry the fields we need" not in _warnings(caplog)


def test_the_tax_id_never_reaches_the_log(caplog):
    """The one field in the record that is nobody's business here."""
    payload = {"content": {"carrier": {"legalName": "MYSTERY CARRIER", "ein": "12-3456789"}}}

    with caplog.at_level(logging.WARNING, logger="app.fmcsa.client"):
        client_for(payload).check("123456")

    assert "12-3456789" not in _warnings(caplog)
    assert '"ein": "***"' in _warnings(caplog)
