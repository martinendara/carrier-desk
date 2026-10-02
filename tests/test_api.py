"""HTTP API tests.

The app is built the same way production builds it — `create_app(settings)` —
with `TMS_MODE=fake` so every test drives the real client, the real socket code
and the real framing against a server in this process. Nothing here reaches the
live TMS.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager

import httpx
import pytest
from conftest import ELIGIBLE_OVERRIDES, NOW, fmcsa_payload
from fastapi.testclient import TestClient
from helpers import FakeMailer

from app.config import Settings
from app.fmcsa import FmcsaClient
from app.main import create_app
from app.tms.fake_server import BOOK_ALREADY, PARTIAL, TIMEOUT

API_KEY = "test-api-key"
SECRET = "test-secret-that-is-long-enough!!"
HEADERS = {"X-API-Key": API_KEY}

#: An MC that has a contact on file, and one that does not.
KNOWN_MC = "123456"
UNKNOWN_MC = "999999"


def fmcsa_returning(payload: dict) -> FmcsaClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    return FmcsaClient("https://fmcsa.test", "key", client=httpx.Client(transport=transport))


#: The recorded carrier with its authority in force and its insurance filed.
BOOKABLE = fmcsa_payload(**ELIGIBLE_OVERRIDES)


@contextmanager
def api_app(**overrides):
    """A running app, its TestClient, and its container."""
    options = {
        "tms_mode": "fake",
        "service_api_key": API_KEY,
        "otp_hmac_secret": SECRET,
        "events_jsonl_path": "memory",
        "request_budget_seconds": 5.0,
        # High by default so the limiter does not interfere with unrelated
        # tests; the tests that are about it set their own.
        "otp_verify_rate_limit_per_hour": 1000,
    }
    options.update(overrides)
    app = create_app(Settings(**options))
    container = app.state.container
    # FMCSA blocks Argentina, so the real client cannot be used here. Swapping
    # it is the only seam these tests need.
    container.fmcsa = fmcsa_returning(BOOKABLE)
    # The service sends email itself; tests use a fake that records the code.
    container.otp._mailer = FakeMailer()
    client = TestClient(app)
    try:
        yield client, container
    finally:
        container.close()


@pytest.fixture
def api():
    with api_app() as (client, container):
        yield client, container


# --- helpers ------------------------------------------------------------


def verify_carrier(client, run_id: str, mc: str = KNOWN_MC) -> dict:
    response = client.post(
        "/carriers/verify", json={"run_id": run_id, "mc_number": mc}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


def become_verified(client, run_id: str, mc: str = KNOWN_MC) -> str:
    """Run the carrier through OTP; return the code the fake mailer captured."""
    verify_carrier(client, run_id, mc)
    sent = client.post("/otp/send", json={"run_id": run_id}, headers=HEADERS)
    assert sent.status_code == 200, sent.text
    code = client.app.state.container.otp._mailer.last_code

    checked = client.post(
        "/otp/verify", json={"run_id": run_id, "code": code}, headers=HEADERS
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["verified"] is True
    return code


def search(client, run_id: str, **filters) -> dict:
    response = client.post(
        "/loads/search", json={"run_id": run_id, **filters}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- health and auth ----------------------------------------------------


def test_health_needs_no_auth(api):
    client, _ = api

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_says_nothing_about_configuration(api):
    client, _ = api

    assert "fake" not in json.dumps(client.get("/health").json())


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/carriers/verify", {"run_id": "r", "mc_number": "123456"}),
        ("post", "/otp/send", {"run_id": "r"}),
        ("post", "/otp/verify", {"run_id": "r", "code": "000000"}),
        ("post", "/loads/search", {"run_id": "r"}),
        ("get", "/loads/LD00896", None),
        ("post", "/negotiations", {"run_id": "r", "load_id": "LD00896"}),
        ("post", "/negotiations/counter", {"run_id": "r", "carrier_ask": 2000}),
        ("post", "/bookings", {"run_id": "r"}),
        ("post", "/handoff", {"run_id": "r"}),
    ],
)
def test_every_tool_requires_the_api_key(api, method, path, body):
    client, _ = api

    if method == "get":
        response = client.get(path, params={"run_id": "r"})
    else:
        response = client.post(path, json=body)

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_a_wrong_api_key_is_refused(api):
    client, _ = api

    response = client.post(
        "/carriers/verify",
        json={"run_id": "r", "mc_number": "123456"},
        headers={"X-API-Key": "not-the-key"},
    )

    assert response.status_code == 401


def test_an_unconfigured_key_refuses_everything():
    with api_app(service_api_key="") as (client, _):
        response = client.post(
            "/carriers/verify", json={"run_id": "r", "mc_number": "123456"}, headers=HEADERS
        )

    assert response.status_code == 503


# --- run_id is mandatory ------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/carriers/verify", "/otp/send", "/otp/verify", "/loads/search", "/bookings"]
)
def test_run_id_is_required(api, path):
    client, _ = api

    response = client.post(path, json={}, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_a_refused_body_is_logged_field_by_field_without_the_values(api, caplog):
    """The line that answers "the panel returns 422 and we cannot see why".

    The agent still gets a sentence, because a carrier is on the line. Whoever
    is reading the log gets the field and the kind of problem — which is what
    identifies a tool configured to send the wrong thing — and not the value,
    which is the caller's.
    """
    client, _ = api

    with caplog.at_level(logging.WARNING, logger="app.api.errors"):
        response = client.post(
            "/carriers/verify",
            json={"run_id": "run-1", "mc_number": 12345678},
            headers=HEADERS,
        )

    assert response.status_code == 422
    logged = caplog.text
    assert "mc_number" in logged
    assert "string_type" in logged
    assert "12345678" not in logged  # the value never travels


def test_a_missing_field_says_which_field_is_missing(api, caplog):
    client, _ = api

    with caplog.at_level(logging.WARNING, logger="app.api.errors"):
        client.post("/carriers/verify", json={"run_id": "run-1"}, headers=HEADERS)

    assert "mc_number" in caplog.text
    assert "required" in caplog.text


def test_an_agent_cannot_smuggle_an_id_field(api):
    """`extra="forbid"`: the ids the agent must never carry are rejected, not ignored."""
    client, _ = api

    response = client.post(
        "/negotiations/counter",
        json={"run_id": "r", "carrier_ask": 2000, "negotiation_id": "neg-1", "round": 2},
        headers=HEADERS,
    )

    assert response.status_code == 422


# --- gating -------------------------------------------------------------


#: Endpoints that must work before an OTP exists: verification itself, and the
#: handoff, which is the escape hatch for a carrier who can never verify.
UNVERIFIED_OK = {"/health", "/carriers/verify", "/otp/send", "/otp/verify", "/handoff"}

#: Minimal valid bodies, so a route reaches its gate instead of failing
#: validation first. A new route must be added here, which is what makes this
#: test notice it.
GATED_ROUTES = {
    ("POST", "/loads/search"): {"run_id": "gate"},
    ("POST", "/negotiations"): {"run_id": "gate", "load_id": "LD00896"},
    ("POST", "/negotiations/counter"): {"run_id": "gate", "carrier_ask": 2000},
    ("POST", "/bookings"): {"run_id": "gate"},
    ("GET", "/loads/{load_id}"): None,
}


def _is_builtin(path: str) -> bool:
    """FastAPI's own docs and schema routes, which are not ours to gate."""
    return path.startswith(("/docs", "/redoc", "/openapi"))


def _walk_routes(routes):
    """Every leaf route.

    FastAPI does not flatten an included router into the parent's `routes`; it
    keeps a wrapper around the original router. Recursing through both shapes is
    what makes this test see the endpoints that actually exist — otherwise it
    would pass by finding nothing, which is the worst way for a test like this
    to be wrong.
    """
    for route in routes:
        nested = getattr(route, "routes", None)
        if not nested:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None) if original is not None else None
        if nested:
            yield from _walk_routes(nested)
        else:
            yield route


def test_every_route_that_needs_otp_is_gated(api):
    """Walks the app's own route table.

    A route added later and not listed below fails this test rather than
    quietly shipping unauthenticated.
    """
    client, _ = api

    seen = set()
    for route in _walk_routes(client.app.routes):
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if not methods or path in UNVERIFIED_OK or _is_builtin(path):
            continue
        for method in methods:
            key = (method, path)
            seen.add(key)
            body = GATED_ROUTES.get(key)
            if method == "GET":
                response = client.get(
                    path.replace("{load_id}", "LD00896"),
                    params={"run_id": "gate-1"},
                    headers=HEADERS,
                )
            else:
                response = client.post(path, json=body, headers=HEADERS)
            assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
            assert response.json()["error"] == "not_verified"

    assert seen == set(GATED_ROUTES), f"route table changed: {seen ^ set(GATED_ROUTES)}"


def test_a_carrier_that_has_not_verified_cannot_search(api):
    client, _ = api

    response = client.post("/loads/search", json={"run_id": "r-gate"}, headers=HEADERS)

    assert response.status_code == 403
    assert "say" in response.json()


def test_the_gate_opens_once_the_code_is_verified(api):
    client, _ = api
    become_verified(client, "run-1")

    assert search(client, "run-1", equipment="DRY_VAN")["count"] > 0


def test_a_failed_otp_leaves_the_call_unverified(api):
    client, _ = api
    verify_carrier(client, "run-1")
    client.post("/otp/send", json={"run_id": "run-1"}, headers=HEADERS)

    client.post("/otp/verify", json={"run_id": "run-1", "code": "000000"}, headers=HEADERS)
    response = client.post("/loads/search", json={"run_id": "run-1"}, headers=HEADERS)

    assert response.status_code == 403


# --- carrier verification -----------------------------------------------


def test_an_eligible_carrier(api):
    client, _ = api

    body = verify_carrier(client, "run-1")

    assert body["eligible"] is True
    assert body["legal_name"] == "B MARRON LOGISTICS LLC"
    assert body["result_code"] is None
    assert body["say"]


def test_a_rejected_carrier_carries_the_reason_code_not_the_detail(api):
    client, container = api
    container.fmcsa = fmcsa_returning(
        fmcsa_payload(**ELIGIBLE_OVERRIDES, oosDate="20260115")
    )

    response = client.post(
        "/carriers/verify", json={"run_id": "run-1", "mc_number": KNOWN_MC}, headers=HEADERS
    )
    body = response.json()

    assert response.status_code == 200
    assert body["eligible"] is False
    assert body["result_code"] == "fmcsa_rejected:out_of_service"
    assert "out of service" in body["say"].lower()


def test_a_bad_mc_is_refused_before_any_lookup(api):
    client, _ = api

    response = client.post(
        "/carriers/verify", json={"run_id": "run-1", "mc_number": "MC-123"}, headers=HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_mc"


# --- OTP ----------------------------------------------------------------


def test_send_returns_the_agent_facing_view_only(api):
    client, container = api
    verify_carrier(client, "run-1")

    body = client.post(
        "/otp/send", json={"run_id": "run-1"}, headers=HEADERS
    ).json()

    assert body["status"] == "sent"
    assert body["masked_destination"].startswith("d***@")
    assert body["result_code"] is None
    assert "delivery" not in body
    # The code lives only in the mailer the service used, never in the response.
    assert len(container.otp._mailer.last_code) == 6


def test_the_sent_say_reads_naturally_in_voice(api):
    client, _ = api
    verify_carrier(client, "run-1")

    body = client.post("/otp/send", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert "the one starting with" in body["say"]
    assert "*" not in body["say"]


def test_send_without_a_prior_verify_is_locked(api):
    """The MC comes from verify_carrier; without it there is nothing to send to."""
    client, _ = api

    body = client.post("/otp/send", json={"run_id": "fresh-run"}, headers=HEADERS).json()

    assert body["status"] == "locked"
    assert "delivery" not in body
    assert body["result_code"] == "otp_failed"


def test_send_refuses_an_mc_number_in_the_body(api):
    """The agent cannot name a different MC than the one that was verified."""
    client, _ = api
    verify_carrier(client, "run-1")

    response = client.post(
        "/otp/send", json={"run_id": "run-1", "mc_number": "999999"}, headers=HEADERS
    )

    assert response.status_code == 422


def test_the_response_never_carries_the_code(api):
    client, container = api
    verify_carrier(client, "run-1")

    body = client.post(
        "/otp/send", json={"run_id": "run-1"}, headers=HEADERS
    ).json()

    assert container.otp._mailer.last_code not in json.dumps(body)


def test_the_full_address_is_never_in_the_spoken_line(api):
    client, container = api
    verify_carrier(client, "run-1")

    body = client.post(
        "/otp/send", json={"run_id": "run-1"}, headers=HEADERS
    ).json()

    assert container.otp._mailer.sent[0].to not in body["say"]
    assert container.otp._mailer.last_code not in body["say"]


def test_an_mc_with_no_contact_is_routed_to_onboarding(api):
    client, _ = api
    verify_carrier(client, "run-1", UNKNOWN_MC)

    body = client.post(
        "/otp/send", json={"run_id": "run-1"}, headers=HEADERS
    ).json()

    assert body["status"] == "routed_onboarding"
    assert "delivery" not in body
    assert body["result_code"] == "routed_onboarding"


def test_a_failed_email_is_send_failed(api):
    client, container = api
    verify_carrier(client, "run-1")
    container.otp._mailer = FakeMailer(fail=True)

    body = client.post(
        "/otp/send", json={"run_id": "run-1"}, headers=HEADERS
    ).json()

    assert body["status"] == "send_failed"
    assert body["result_code"] == "otp_failed"
    assert "colleague" in body["say"]


def test_a_wrong_code_reports_attempts_left(api):
    client, _ = api
    verify_carrier(client, "run-1")
    client.post("/otp/send", json={"run_id": "run-1"}, headers=HEADERS)

    body = client.post(
        "/otp/verify", json={"run_id": "run-1", "code": "000000"}, headers=HEADERS
    ).json()

    assert body["verified"] is False
    assert body["attempts_left"] == 2
    assert body["result_code"] == "otp_failed"


def test_three_wrong_codes_lock_the_call(api):
    client, _ = api
    verify_carrier(client, "run-1")
    client.post("/otp/send", json={"run_id": "run-1"}, headers=HEADERS)

    body = {}
    for _ in range(3):
        body = client.post(
            "/otp/verify", json={"run_id": "run-1", "code": "000000"}, headers=HEADERS
        ).json()

    assert body["verified"] is False
    assert body["attempts_left"] == 0


def test_verifying_without_a_challenge_does_not_leak(api):
    client, _ = api
    verify_carrier(client, "run-1")

    body = client.post(
        "/otp/verify", json={"run_id": "run-1", "code": "123456"}, headers=HEADERS
    ).json()

    assert body["verified"] is False


# --- loads --------------------------------------------------------------


def test_search_returns_at_most_three_loads(api):
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="DRY_VAN")

    assert body["count"] == 3
    assert len(body["loads"]) == 3


def test_no_load_response_ever_contains_max_buy(api):
    """The whole response, serialised, must not carry the ceiling."""
    client, _ = api
    become_verified(client, "run-1")

    search_body = search(client, "run-1", equipment="DRY_VAN")
    load_id = search_body["loads"][0]["load_id"]
    detail = client.get(
        f"/loads/{load_id}", params={"run_id": "run-1"}, headers=HEADERS
    ).json()

    for body in (search_body, detail):
        text = json.dumps(body)
        assert "max_buy" not in text.lower()
        assert "MAX_BUY" not in text


def test_no_load_response_exposes_the_posted_rate(api):
    """The price the agent quotes comes only from open_negotiation."""
    client, _ = api
    become_verified(client, "run-1")

    search_body = search(client, "run-1", equipment="DRY_VAN")
    load_id = search_body["loads"][0]["load_id"]
    detail = client.get(
        f"/loads/{load_id}", params={"run_id": "run-1"}, headers=HEADERS
    ).json()

    for load in search_body["loads"]:
        assert "rate" not in load
    assert "rate" not in detail["load"]


def test_search_with_no_matches_says_so(api):
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="FLATBED")

    assert body["count"] == 0
    assert body["loads"] == []
    assert body["result_code"] == "no_match"


def test_search_without_filters_is_a_speakable_error(api):
    client, _ = api
    become_verified(client, "run-1")

    response = client.post("/loads/search", json={"run_id": "run-1"}, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_empty_string_filters_are_treated_as_absent(api):
    """The platform sends "" for parameters the agent left blank."""
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="DRY_VAN", orig_city="", dest_state="")

    assert body["count"] > 0


def test_all_empty_string_filters_mean_no_filters(api):
    client, _ = api
    become_verified(client, "run-1")

    response = client.post(
        "/loads/search", json={"run_id": "run-1", "orig_city": "", "dest_state": ""},
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_load_detail_pitches_the_load(api):
    client, _ = api
    become_verified(client, "run-1")
    load_id = search(client, "run-1", equipment="DRY_VAN")["loads"][0]["load_id"]

    body = client.get(f"/loads/{load_id}", params={"run_id": "run-1"}, headers=HEADERS).json()

    assert body["load"]["load_id"] == load_id
    assert body["say"]


# --- negotiation --------------------------------------------------------


def _negotiable_load(client, run_id: str = "run-1") -> str:
    become_verified(client, run_id)
    return search(client, run_id, equipment="DRY_VAN")["loads"][0]["load_id"]


def test_opening_a_negotiation_returns_an_offer(api):
    client, _ = api
    load_id = _negotiable_load(client)

    body = client.post(
        "/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS
    ).json()

    assert body["load_id"] == load_id
    assert body["offer"] > 0
    assert "say" in body


def test_a_load_that_was_never_shown_cannot_be_negotiated(api):
    client, _ = api
    become_verified(client, "run-1")

    response = client.post(
        "/negotiations", json={"run_id": "run-1", "load_id": "LD00896"}, headers=HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"] == "load_not_shown"


def test_negotiating_costs_one_of_the_two_slots(api):
    client, _ = api
    become_verified(client, "run-1")
    loads = search(client, "run-1", equipment="DRY_VAN")["loads"]

    for load in loads[:2]:
        response = client.post(
            "/negotiations", json={"run_id": "run-1", "load_id": load["load_id"]}, headers=HEADERS
        )
        assert response.status_code == 200

    third = client.post(
        "/negotiations", json={"run_id": "run-1", "load_id": loads[2]["load_id"]}, headers=HEADERS
    )
    assert third.status_code == 409
    assert third.json()["error"] == "negotiation_limit"


def test_a_counter_returns_only_an_action_and_an_amount(api):
    client, _ = api
    load_id = _negotiable_load(client)
    client.post("/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS)

    body = client.post(
        "/negotiations/counter", json={"run_id": "run-1", "carrier_ask": 99999}, headers=HEADERS
    ).json()

    assert set(body) == {"action", "amount", "result_code", "say"}
    assert body["action"] in {"accept", "counter", "close"}
    assert body["result_code"] is None  # a counter, not a close, carries no code


def test_three_rounds_end_the_negotiation(api):
    client, _ = api
    load_id = _negotiable_load(client)
    client.post("/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS)

    actions = []
    for ask in (99999, 99998, 99997):
        body = client.post(
            "/negotiations/counter",
            json={"run_id": "run-1", "carrier_ask": ask},
            headers=HEADERS,
        ).json()
        actions.append(body["action"])

    assert actions == ["counter", "counter", "close"]


def test_a_close_carries_the_negotiation_failed_code(api):
    """The platform counts outcomes on one column; close must carry its code."""
    client, _ = api
    load_id = _negotiable_load(client)
    client.post("/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS)

    body = {}
    for ask in (99999, 99998, 99997):
        body = client.post(
            "/negotiations/counter",
            json={"run_id": "run-1", "carrier_ask": ask},
            headers=HEADERS,
        ).json()

    assert body["action"] == "close"
    assert body["result_code"] == "negotiation_failed"


def test_a_fourth_round_is_refused(api):
    client, _ = api
    load_id = _negotiable_load(client)
    client.post("/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS)
    for ask in (99999, 99998, 99997):
        client.post(
            "/negotiations/counter",
            json={"run_id": "run-1", "carrier_ask": ask},
            headers=HEADERS,
        )

    response = client.post(
        "/negotiations/counter", json={"run_id": "run-1", "carrier_ask": 2500}, headers=HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"] == "negotiation_closed"


def test_countering_without_an_offer_is_refused(api):
    client, _ = api
    become_verified(client, "run-1")

    response = client.post(
        "/negotiations/counter", json={"run_id": "run-1", "carrier_ask": 2500}, headers=HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"] == "no_negotiation"


def test_a_nonsense_ask_is_refused(api):
    client, _ = api
    load_id = _negotiable_load(client)
    client.post("/negotiations", json={"run_id": "run-1", "load_id": load_id}, headers=HEADERS)

    response = client.post(
        "/negotiations/counter", json={"run_id": "run-1", "carrier_ask": 0}, headers=HEADERS
    )

    assert response.status_code == 422


# --- booking ------------------------------------------------------------


def _agree_a_rate(client, run_id: str = "run-1", ask: int = 2500):
    load_id = _negotiable_load(client, run_id)
    client.post("/negotiations", json={"run_id": run_id, "load_id": load_id}, headers=HEADERS)
    decision = client.post(
        "/negotiations/counter", json={"run_id": run_id, "carrier_ask": ask}, headers=HEADERS
    ).json()
    assert decision["action"] == "accept"
    return load_id, decision["amount"]


def test_a_confirmed_booking_reports_booked(api):
    client, _ = api
    load_id, rate = _agree_a_rate(client)

    body = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert body["status"] == "booked"
    assert body["result_code"] == "booked"
    assert body["booking_ref"]


def test_the_rate_comes_from_the_session_not_the_request(api):
    """There is no rate field to send, and sending one is a 422."""
    client, container = api
    _agree_a_rate(client)

    response = client.post(
        "/bookings", json={"run_id": "run-1", "agreed_rate": 99999}, headers=HEADERS
    )

    assert response.status_code == 422


def test_an_ambiguous_booking_is_pending_never_booked(api):
    """The write lands, the response is lost: a rep confirms, we do not claim it."""
    client, container = api
    _agree_a_rate(client)
    container.fake_tms.script = [PARTIAL]
    container.fake_tms.booking_lands_despite_fault = True

    body = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert body["result_code"] == "booking_pending"
    assert body["status"] == "booked_unconfirmed"
    assert body["booking_ref"] is None


def test_an_already_booked_load_is_pending(api):
    client, container = api
    _agree_a_rate(client)
    container.fake_tms.book_result = BOOK_ALREADY

    body = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert body["result_code"] == "booking_pending"


def test_two_bookings_make_one_load_book(api):
    """The platform cannot set zero retries; a retry must not double-book."""
    client, container = api
    _agree_a_rate(client)

    first = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()
    second = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert second == first
    assert container.fake_tms.commands.count("LOAD_BOOK") == 1


def test_a_retry_after_booking_pending_returns_the_same_response(api):
    client, container = api
    _agree_a_rate(client)
    container.fake_tms.book_result = BOOK_ALREADY

    first = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()
    second = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS).json()

    assert first["result_code"] == "booking_pending"
    assert second == first
    assert container.fake_tms.commands.count("LOAD_BOOK") == 1


def test_booking_without_an_agreed_rate_is_refused(api):
    client, _ = api
    become_verified(client, "run-1")

    response = client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["error"] == "no_agreed_rate"


# --- handoff ------------------------------------------------------------


def test_a_handoff_works_without_verification(api):
    """The escape hatch must not require the thing it exists to work around."""
    client, container = api

    body = client.post(
        "/handoff", json={"run_id": "run-noverify", "reason": "onboarding"}, headers=HEADERS
    ).json()

    assert body["ticket_id"].startswith("rep_")
    assert len(container.handoffs) == 1


def test_an_unknown_reason_is_recorded_as_requested(api):
    client, container = api

    client.post(
        "/handoff", json={"run_id": "run-1", "reason": "something odd"}, headers=HEADERS
    )

    assert container.handoffs.pending()[0].reason.value == "requested"


def test_a_booked_reason_is_recognized(api):
    """A confirmed booking hands off as `booked`, not as a person request."""
    client, container = api

    client.post(
        "/handoff", json={"run_id": "run-1", "reason": "booked"}, headers=HEADERS
    )

    assert container.handoffs.pending()[0].reason.value == "booked"


# --- errors -------------------------------------------------------------


def test_a_tms_fault_becomes_a_speakable_error(api):
    client, container = api
    become_verified(client, "run-1")
    container.fake_tms.error_code = "SERVER_ERROR"
    container.fake_tms.error_command = "LOAD_QUERY"

    response = client.post(
        "/loads/search", json={"run_id": "run-1", "equipment": "DRY_VAN"}, headers=HEADERS
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "tms_unavailable"
    # Nothing internal in what the agent gets.
    text = json.dumps(body)
    for leak in ("SERVER_ERROR", "LOAD_QUERY", "AUTH", "127.0.0.1", "Traceback", "tms."):
        assert leak not in text


def test_an_unknown_run_id_is_treated_as_an_unverified_call(api):
    """A run_id we have never seen starts a fresh call, so it has verified nothing."""
    client, _ = api

    response = client.post(
        "/negotiations/counter", json={"run_id": "never-seen", "carrier_ask": 2000}, headers=HEADERS
    )

    assert response.status_code == 403
    assert response.json()["error"] == "not_verified"


def test_verifying_against_an_unknown_run_id_is_not_found(api):
    """Unlike the gated routes, verify does not start a call: there is no code to check."""
    client, _ = api

    response = client.post(
        "/otp/verify", json={"run_id": "never-seen", "code": "123456"}, headers=HEADERS
    )

    assert response.status_code == 404
    assert response.json()["error"] == "session_not_found"


def test_a_slow_tms_is_cut_off_by_the_budget():
    """A voice tool cannot hang; the budget ends it and says so."""
    with api_app(request_budget_seconds=0.4) as (client, container):
        become_verified(client, "run-1")
        container.fake_tms.mode = TIMEOUT
        container.fake_tms.hold_seconds = 3.0

        response = client.post(
            "/loads/search", json={"run_id": "run-1", "equipment": "DRY_VAN"}, headers=HEADERS
        )

    assert response.status_code == 504
    assert response.json()["error"] == "tms_timeout"


# --- events -------------------------------------------------------------


def test_a_full_call_is_recorded_in_the_event_log(api):
    from app.events import EventType

    client, container = api
    load_id, _ = _agree_a_rate(client)
    client.post("/bookings", json={"run_id": "run-1"}, headers=HEADERS)
    client.post("/handoff", json={"run_id": "run-1"}, headers=HEADERS)

    recorded = [event.type for event in container.sink.for_run("run-1")]

    assert recorded == [
        EventType.FMCSA_CHECKED,
        EventType.OTP_SENT,
        EventType.OTP_VERIFIED,
        EventType.SEARCH_PERFORMED,
        EventType.NEGOTIATION_OPENED,
        EventType.NEGOTIATION_CLOSED,
        EventType.BOOKING_ATTEMPTED,
        EventType.BOOKING_RESULT,
        EventType.HANDOFF_CREATED,
    ]


def test_the_event_log_holds_the_ceiling_the_agent_never_sees(api):
    from app.events import EventType

    client, container = api
    load_id, _ = _agree_a_rate(client)

    opened = container.sink.of_type(EventType.NEGOTIATION_OPENED)[0]

    assert opened.payload["ceiling"] > 0
    assert opened.payload["load_id"] == load_id


def test_a_search_that_finds_nothing_is_still_recorded(api):
    from app.events import EventType

    client, container = api
    become_verified(client, "run-1")
    search(client, "run-1", equipment="FLATBED")

    (event,) = container.sink.of_type(EventType.SEARCH_PERFORMED)

    assert event.payload["results"] == 0


# --- OTP rate limiting --------------------------------------------------


def _wrong_code_attempt(client, run_id: str, mc: str = KNOWN_MC):
    """One verify + send plus one deliberately wrong verify, for a fresh call."""
    verify_carrier(client, run_id, mc)
    client.post("/otp/send", json={"run_id": run_id}, headers=HEADERS)
    return client.post(
        "/otp/verify", json={"run_id": run_id, "code": "000000"}, headers=HEADERS
    )


def test_verify_is_rate_limited_per_mc_across_calls():
    """Five attempts an hour for one MC, no matter how many calls are started."""
    with api_app(otp_verify_rate_limit_per_hour=5) as (client, _):
        for index in range(5):
            response = _wrong_code_attempt(client, f"call-{index}")
            assert response.status_code == 200, f"attempt {index} was refused early"

        blocked = _wrong_code_attempt(client, "call-5")

    assert blocked.status_code == 429
    assert blocked.json()["error"] == "otp_rate_limited"


def test_a_rate_limited_response_says_when_to_come_back():
    with api_app(otp_verify_rate_limit_per_hour=1) as (client, _):
        _wrong_code_attempt(client, "call-0")

        blocked = _wrong_code_attempt(client, "call-1")

    assert blocked.status_code == 429
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= 3600


def test_the_limit_is_per_mc_not_global():
    with api_app(otp_verify_rate_limit_per_hour=1) as (client, _):
        assert _wrong_code_attempt(client, "call-a", "123456").status_code == 200
        assert _wrong_code_attempt(client, "call-b", "123456").status_code == 429
        # A different carrier is unaffected by the first one's attempts.
        assert _wrong_code_attempt(client, "call-c", "778899").status_code == 200


def test_the_rate_limited_line_is_speakable():
    with api_app(otp_verify_rate_limit_per_hour=1) as (client, _):
        _wrong_code_attempt(client, "call-0")

        body = _wrong_code_attempt(client, "call-1").json()

    assert body["say"]
    for leak in ("429", "rate", "limit", "Redis", "mc:", "call-"):
        assert leak.lower() not in body["say"].lower()


def test_a_correct_code_still_costs_an_attempt():
    """Otherwise the limit would only count failures, and guessing is not the
    only thing worth bounding."""
    with api_app(otp_verify_rate_limit_per_hour=2) as (client, _):
        become_verified(client, "call-0")
        become_verified(client, "call-1")

        blocked = _wrong_code_attempt(client, "call-2")

    assert blocked.status_code == 429


# --- the production guard -----------------------------------------------


def test_fake_tms_is_refused_in_production():
    """Shipping fixture loads to real carriers is not a bug you notice later."""
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="fake",
                service_api_key=API_KEY,
                otp_hmac_secret=SECRET,
                events_jsonl_path="memory",
            )
        )

    assert "TMS_MODE=fake" in str(caught.value)


@pytest.mark.parametrize("env_name", ["production", "prod", "PRODUCTION", " production "])
def test_the_guard_is_case_and_space_insensitive(env_name):
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError):
        create_app(
            Settings(
                env=env_name,
                tms_mode="fake",
                service_api_key=API_KEY,
                otp_hmac_secret=SECRET,
                events_jsonl_path="memory",
            )
        )


def test_fake_tms_is_fine_in_development():
    with api_app(env="development", tms_mode="fake") as (client, _):
        assert client.get("/health").json()["status"] == "ok"


def test_live_tms_is_fine_in_production():
    """The guard is about the fake, not about production."""
    settings = Settings(
        env="production",
        tms_mode="live",
        tms_host="tms.example.test",
        tms_port=1234,
        tms_token="not-a-real-token",
        service_api_key=API_KEY,
        otp_hmac_secret=SECRET,
        resend_api_key="re_test_key",
        events_jsonl_path="memory",
    )
    app = create_app(settings)
    try:
        assert app.state.container.settings.is_production
        assert app.state.container.fake_tms is None
    finally:
        app.state.container.close()


def test_live_tms_without_host_is_refused_in_production():
    """A missing TMS_HOST must name the variable, not crash as a bare ValueError
    that reads like a hung service."""
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="live",
                tms_host="",
                tms_port=1234,
                tms_token="not-a-real-token",
                service_api_key=API_KEY,
                otp_hmac_secret=SECRET,
                resend_api_key="re_test_key",
                events_jsonl_path="memory",
            )
        )

    assert "TMS_HOST" in str(caught.value)


def test_live_tms_without_host_is_refused_in_development_too():
    """The failing deploy is a bare container (`docker run -e PORT=10000`), which
    runs as development (no ENV set). Live mode needs the TMS address in every
    environment, so this check is not gated on production."""
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="development",
                tms_mode="live",
                tms_host="",
                tms_port=1234,
                tms_token="not-a-real-token",
                service_api_key=API_KEY,
                otp_hmac_secret=SECRET,
                events_jsonl_path="memory",
            )
        )

    assert "TMS_HOST" in str(caught.value)


@pytest.mark.parametrize(
    ("missing", "blank"),
    [
        ("tms_port", 0),
        ("tms_token", ""),
    ],
)
def test_live_tms_connectivity_is_named(missing, blank):
    from app.container import ConfigurationError

    options = {
        "env": "development",
        "tms_mode": "live",
        "tms_host": "tms.example.test",
        "tms_port": 1234,
        "tms_token": "not-a-real-token",
        "service_api_key": API_KEY,
        "otp_hmac_secret": SECRET,
        "events_jsonl_path": "memory",
    }
    options[missing] = blank

    with pytest.raises(ConfigurationError) as caught:
        create_app(Settings(**options))

    assert missing.upper() in str(caught.value)


def test_an_empty_api_key_is_refused_in_production():
    """Left to fail at request time it is a 503 that looks like an outage."""
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="live",
                tms_host="tms.example.test",
                tms_port=1234,
                tms_token="token",
                service_api_key="",
                otp_hmac_secret=SECRET,
                events_jsonl_path="memory",
            )
        )

    assert "SERVICE_API_KEY" in str(caught.value)


def test_an_empty_otp_secret_is_refused_in_production():
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="live",
                tms_host="tms.example.test",
                tms_port=1234,
                tms_token="token",
                service_api_key=API_KEY,
                otp_hmac_secret="",
                events_jsonl_path="memory",
            )
        )

    assert "OTP_HMAC_SECRET" in str(caught.value)


def test_an_empty_resend_key_is_refused_in_production():
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="live",
                tms_host="tms.example.test",
                tms_port=1234,
                tms_token="token",
                service_api_key=API_KEY,
                otp_hmac_secret=SECRET,
                resend_api_key="",
                events_jsonl_path="memory",
            )
        )

    assert "RESEND_API_KEY" in str(caught.value)


def test_the_tms_mode_guard_is_reported_first():
    """When several things are wrong, the most consequential one is named."""
    from app.container import ConfigurationError

    with pytest.raises(ConfigurationError) as caught:
        create_app(
            Settings(
                env="production",
                tms_mode="fake",
                service_api_key="",
                otp_hmac_secret="",
                events_jsonl_path="memory",
            )
        )

    assert "TMS_MODE=fake" in str(caught.value)


def test_empty_secrets_are_still_fine_in_development():
    """The guards are production-only; development stays forgiving."""
    with api_app(env="development", service_api_key="dev-key", otp_hmac_secret=SECRET) as (client, _):
        assert client.get("/health").json()["status"] == "ok"


# --- schema-sample -------------------------------------------------------


_SCHEMA_SAMPLE_CALLS = [
    ("/otp/send", {"run_id": "schema-sample"}, {"status", "masked_destination", "result_code", "say"}),
    ("/otp/verify", {"run_id": "schema-sample", "code": "000000"}, {"verified", "attempts_left", "result_code", "say"}),
    ("/loads/search", {"run_id": "schema-sample"}, {"loads", "count", "result_code", "say"}),
    ("/negotiations", {"run_id": "schema-sample", "load_id": "LD-SAMPLE"}, {"load_id", "offer", "say"}),
    ("/negotiations/counter", {"run_id": "schema-sample", "carrier_ask": 1600}, {"action", "amount", "result_code", "say"}),
    ("/bookings", {"run_id": "schema-sample"}, {"status", "booking_ref", "result_code", "say"}),
    ("/handoff", {"run_id": "schema-sample"}, {"ticket_id", "say"}),
]


@pytest.mark.parametrize(("path", "body", "keys"), _SCHEMA_SAMPLE_CALLS)
def test_schema_sample_returns_every_field(api, path, body, keys):
    """The Generate button captures a full schema, not an error field."""
    client, _ = api

    response = client.post(path, json=body, headers=HEADERS)

    assert response.status_code == 200
    assert keys <= set(response.json())


def test_schema_sample_send_otp_has_no_delivery(api):
    client, _ = api

    body = client.post("/otp/send", json={"run_id": "schema-sample"}, headers=HEADERS).json()

    assert "delivery" not in body


def test_schema_sample_matches_a_run_id_with_spaces(api):
    """The Generate button sends `run_id` with trailing spaces."""
    client, _ = api

    body = client.post(
        "/otp/send", json={"run_id": "schema-sample   "}, headers=HEADERS
    ).json()

    assert body["status"] == "sent"


@pytest.mark.parametrize(
    ("path", "keys"),
    [
        ("/carriers/verify", {"eligible", "result_code", "legal_name", "say"}),
        ("/otp/verify", {"verified", "attempts_left", "result_code", "say"}),
        ("/negotiations", {"load_id", "offer", "say"}),
        ("/negotiations/counter", {"action", "amount", "result_code", "say"}),
    ],
)
def test_schema_sample_works_with_an_incomplete_body(api, path, keys):
    """Generate omits required fields; schema-sample still returns the shape."""
    client, _ = api

    response = client.post(
        path, json={"run_id": "schema-sample   "}, headers=HEADERS
    )

    assert response.status_code == 200
    assert keys <= set(response.json())


@pytest.mark.parametrize(
    "path",
    ["/carriers/verify", "/otp/verify", "/negotiations", "/negotiations/counter"],
)
def test_a_real_request_still_requires_the_field(api, path):
    """Outside schema-sample, a missing required field is still a 422."""
    client, _ = api

    response = client.post(path, json={"run_id": "real-run"}, headers=HEADERS)

    assert response.status_code == 422


def test_schema_sample_search_returns_a_full_load(api):
    client, _ = api

    body = client.post("/loads/search", json={"run_id": "schema-sample"}, headers=HEADERS).json()

    load = body["loads"][0]
    assert load["load_id"] == "LD-SAMPLE"
    for field in ("delivery_dt", "weight", "commodity", "pieces", "dims", "notes"):
        assert field in load
    assert "rate" not in load
    assert "max_buy" not in load


def test_schema_sample_touches_nothing(api):
    """No session, no event, no TMS call — the handler never runs."""
    client, container = api

    for path, body, _ in _SCHEMA_SAMPLE_CALLS:
        client.post(path, json=body, headers=HEADERS)

    assert len(container.store) == 0
    assert container.sink.events == []
    assert container.fake_tms.requests == []


def test_schema_sample_still_requires_the_api_key(api):
    client, _ = api

    response = client.post("/otp/send", json={"run_id": "schema-sample"})

    assert response.status_code == 401


# --- search returns full detail -----------------------------------------


def test_search_returns_the_full_detail_for_each_load(api):
    """The pitch no longer needs a separate get_load round trip."""
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="DRY_VAN")

    load = body["loads"][0]
    for field in ("delivery_dt", "weight", "commodity", "pieces", "dims", "notes"):
        assert field in load
    assert "rate" not in load
    assert "max_buy" not in load


def test_a_search_with_equipment_and_origin_state_works(api):
    """The platform sends no zip or pickup date; two filters are enough."""
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="DRY_VAN", orig_state="FL")

    assert body["count"] == 1


def test_search_accepts_spoken_equipment_and_state(api):
    """The agent transcribes speech: 'dry van' and 'florida' must still match."""
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="dry van", orig_state="florida")

    assert body["count"] == 1
    assert body["loads"][0]["orig_state"] == "FL"


def test_search_splits_city_and_state_in_the_city_field(api):
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="van", orig_city="Fort Lauderdale, Florida")

    assert body["count"] == 1
    assert body["loads"][0]["load_id"] == "LD00896"


def test_search_treats_anywhere_as_no_destination(api):
    client, _ = api
    become_verified(client, "run-1")

    body = search(client, "run-1", equipment="DRY_VAN", dest_city="anywhere")

    assert body["count"] == 3
