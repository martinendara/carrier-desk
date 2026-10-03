"""Run the approved QA plan end-to-end against a carrier-desk deployment.

This is the script behind `docs/qa-report.md`. It drives the real HTTP tool
surface (the same endpoints the voice agent calls) and asserts each scenario's
expected outcome. It never needs — and never sees — `MAX_BUY`: the ceiling is a
server-side value, and this script acts as the agent, whose whole job is to be
handed `{action, amount}` and nothing else.

Re-runnable by construction:

* every scenario is a fresh ``qa-<name>`` run_id, so runs never share state;
* the deployment runs ``TMS_MODE=fake`` seeded from ``tms_demo.txt`` (20 loads),
  and the fake server never removes a load from search — booking a load does not
  deplete the pool, and a re-run starts clean;
* ``qa-`` prefixes keep every event out of the live KPIs in Twin (``source='qa'``).

**Two modes.** With no arguments it runs the QA scenarios below. With ``--seed``
it instead creates the demo data the Twin Signals views need (unserved lanes,
failed negotiations, near-miss closes, bookings on several lanes, an onboarding
lead). Both write ``qa-`` run ids, so both stay out of the live metrics.

Usage (values are secrets and stay out of the repo):

    QA_BASE_URL=https://<host> QA_API_KEY=<service key> \
    QA_REVIEWER_MC=15735 QA_REVIEWER_OTP=123456 \
    .venv/bin/python scripts/qa_run.py            # the QA scenarios
    .venv/bin/python scripts/qa_run.py --seed     # demo data for the views

Exit code 0 iff every scenario passed.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

# --- configuration -------------------------------------------------------


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    reviewer_mc: str
    reviewer_otp: str


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"missing required env var {name}")
    return value


def load_config() -> Config:
    return Config(
        base_url=_require_env("QA_BASE_URL").rstrip("/"),
        api_key=_require_env("QA_API_KEY"),
        reviewer_mc=_require_env("QA_REVIEWER_MC"),
        reviewer_otp=_require_env("QA_REVIEWER_OTP"),
    )


# --- a thin HTTP client --------------------------------------------------


class Client:
    """A tiny wrapper over the eight tools, so each scenario reads like the flow."""

    def __init__(self, config: Config):
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={"X-API-Key": config.api_key},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict) -> httpx.Response:
        return self._client.post(path, json=body)

    def health(self) -> httpx.Response:
        return self._client.get("/health")

    def verify(self, run_id: str, mc: str) -> httpx.Response:
        return self._post("/carriers/verify", {"run_id": run_id, "mc_number": mc})

    def send_otp(self, run_id: str, *, mc_number: str | None = None) -> httpx.Response:
        body: dict = {"run_id": run_id}
        if mc_number is not None:  # the adversarial "send_otp with mc_number" case
            body["mc_number"] = mc_number
        return self._post("/otp/send", body)

    def verify_otp(self, run_id: str, code: str) -> httpx.Response:
        return self._post("/otp/verify", {"run_id": run_id, "code": code})

    def search(self, run_id: str, **filters: str) -> httpx.Response:
        return self._post("/loads/search", {"run_id": run_id, **filters})

    def open(self, run_id: str, load_id: str) -> httpx.Response:
        return self._post("/negotiations", {"run_id": run_id, "load_id": load_id})

    def counter(self, run_id: str, ask: int) -> httpx.Response:
        return self._post("/negotiations/counter", {"run_id": run_id, "carrier_ask": ask})

    def book(self, run_id: str, *, extra: dict | None = None) -> httpx.Response:
        body: dict = {"run_id": run_id}
        if extra:
            body.update(extra)
        return self._post("/bookings", body)

    def handoff(self, run_id: str, reason: str | None = None) -> httpx.Response:
        body: dict = {"run_id": run_id}
        if reason is not None:
            body["reason"] = reason
        return self._post("/handoff", body)


# --- scenario registry ---------------------------------------------------


@dataclass
class Result:
    name: str
    passed: bool
    note: str = ""


_SCENARIOS: dict[str, object] = {}
_SEED_STEPS: dict[str, object] = {}
RESULTS: list[Result] = []


def _register(registry: dict, name: str):
    """Register `name` in `registry` and wrap it to record a Result."""

    def decorator(fn):
        def run(client: Client, config: Config) -> None:
            try:
                note = fn(client, config)
            except AssertionError as exc:
                RESULTS.append(Result(name, False, str(exc)))
            except httpx.HTTPStatusError as exc:
                RESULTS.append(
                    Result(name, False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
                )
            except Exception as exc:  # noqa: BLE001 - a scenario is a whole call
                RESULTS.append(Result(name, False, f"{type(exc).__name__}: {exc}"))
            else:
                RESULTS.append(Result(name, True, note or ""))

        registry[name] = run
        return run

    return decorator


def scenario(name: str):
    """A QA scenario, asserting a documented outcome."""
    return _register(_SCENARIOS, name)


def seed_step(name: str):
    """A seed step, creating demo data. Assertions only guard the shape."""
    return _register(_SEED_STEPS, name)


# --- shared flow helpers -------------------------------------------------


def assert_status(response: httpx.Response, expected: int, context: str) -> httpx.Response:
    if response.status_code != expected:
        raise AssertionError(
            f"{context}: expected HTTP {expected}, got {response.status_code} "
            f"(body={response.text[:200]!r})"
        )
    return response


def assert_json(response: httpx.Response, expected: int, context: str) -> dict:
    assert_status(response, expected, context)
    return response.json()


def assert_booking_settled(book: dict, context: str) -> str:
    """A booking ended in one of its two settled states, and claimed nothing else.

    With `TMS_FAKE_FAULT_RATE` above zero — 0.10 on the deployment — a booking
    whose response is lost settles as `booking_pending`, because the recovery
    could not read the load's status back. That is a *correct* outcome of the
    same ladder, not a failure of it, so an end-to-end script cannot demand
    `booked` specifically: it does not control the fault rate.

    What it can demand is the invariant that holds at any fault rate, and it is
    the one that matters to a carrier:

      * the status is one of the two settled states, never anything else;
      * `booked` always carries a reference, and never comes from a response we
        did not read;
      * `booking_pending` never carries one.

    The strict "a confirmed booking reports booked, with a reference" case is
    pinned in pytest, where the fake's fault rate is a test parameter.
    """
    status = book.get("status")
    assert status in ("booked", "booked_unconfirmed"), f"{context}: unexpected {book}"
    if status == "booked":
        assert book.get("booking_ref"), f"{context}: booked without a reference: {book}"
        assert book.get("result_code") == "booked", f"{context}: {book}"
    else:
        assert book.get("booking_ref") is None, f"{context}: pending with a reference: {book}"
        assert book.get("result_code") == "booking_pending", f"{context}: {book}"
    return status


def verify_and_otp(client: Client, run_id: str, mc: str, otp: str) -> None:
    """verify → send → verify OTP. Raises if the gate does not open."""
    verify = assert_json(client.verify(run_id, mc), 200, "verify")
    assert verify.get("eligible") is True, f"expected eligible, got {verify}"

    send = assert_json(client.send_otp(run_id), 200, "send_otp")
    assert send.get("status") == "sent", f"expected sent, got {send}"

    code = assert_json(client.verify_otp(run_id, otp), 200, "verify_otp")
    assert code.get("verified") is True, f"expected verified, got {code}"


def verified_search(client: Client, run_id: str, mc: str, otp: str, **filters: str) -> list[dict]:
    """The standard prefix, then a search that must find at least one load."""
    verify_and_otp(client, run_id, mc, otp)
    search = assert_json(client.search(run_id, **filters), 200, "search")
    assert search.get("count", 0) >= 1, f"expected at least one load, got {search}"
    return search["loads"]


def open_first(client: Client, run_id: str, mc: str, otp: str, **filters: str) -> int:
    """Verified search, then open a negotiation on the first match. Returns the offer."""
    loads = verified_search(client, run_id, mc, otp, **filters)
    opened = assert_json(client.open(run_id, loads[0]["load_id"]), 200, "open")
    return opened["offer"]


#: Demo ceilings top out around $6.2k, so these are safely above every ceiling,
#: and distinct so a repeat of the same ask is never mistaken for a webhook retry.
HUGE = (999_999, 999_998, 999_997, 999_996)


def wrong_code(otp: str) -> str:
    """A six-digit code guaranteed to differ from the reviewer OTP."""
    return f"{(int(otp) + 1) % 1_000_000:06d}"


# --- standard: a booking at each depth of the ladder ---------------------


@scenario("standard-book-at-first-offer")
def standard_book_first(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-book-first", config.reviewer_mc, config.reviewer_otp, orig_state="IL", dest_state="MO")
    acc = assert_json(client.counter("qa-book-first", offer), 200, "counter")
    assert acc["action"] == "accept" and acc["amount"] == offer, f"expected accept at {offer}, got {acc}"
    book = assert_json(client.book("qa-book-first"), 200, "book")
    status = assert_booking_settled(book, "book at first offer")
    assert_json(client.handoff("qa-book-first", "booked"), 200, "handoff")
    return f"{status} at first offer ${offer}"


@scenario("standard-book-after-1-counter")
def standard_book_1_counter(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-book-1", config.reviewer_mc, config.reviewer_otp, orig_state="TX", dest_state="CO")
    c1 = assert_json(client.counter("qa-book-1", HUGE[0]), 200, "counter 1")
    assert c1["action"] == "counter", f"expected counter, got {c1}"
    acc = assert_json(client.counter("qa-book-1", c1["amount"]), 200, "counter 2")
    assert acc["action"] == "accept", f"expected accept, got {acc}"
    book = assert_json(client.book("qa-book-1"), 200, "book")
    status = assert_booking_settled(book, "book after 1 counter")
    assert_json(client.handoff("qa-book-1", "booked"), 200, "handoff")
    return f"offer ${offer} → counter ${c1['amount']} → {status} ${acc['amount']}"


@scenario("standard-book-after-2-counters")
def standard_book_2_counters(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-book-2", config.reviewer_mc, config.reviewer_otp, orig_state="GA", dest_state="FL")
    c1 = assert_json(client.counter("qa-book-2", HUGE[0]), 200, "counter 1")
    assert c1["action"] == "counter", f"expected counter, got {c1}"
    c2 = assert_json(client.counter("qa-book-2", HUGE[1]), 200, "counter 2")
    assert c2["action"] == "counter", f"expected counter, got {c2}"
    acc = assert_json(client.counter("qa-book-2", c2["amount"]), 200, "counter 3")
    assert acc["action"] == "accept", f"expected accept, got {acc}"
    book = assert_json(client.book("qa-book-2"), 200, "book")
    status = assert_booking_settled(book, "book after 2 counters")
    assert_json(client.handoff("qa-book-2", "booked"), 200, "handoff")
    return f"offer ${offer} → ${c1['amount']} → ${c2['amount']} → {status} ${acc['amount']}"


@scenario("standard-book-after-3-counters")
def standard_book_3_counters(client: Client, config: Config) -> str:
    """Round 3 accepts an ask one increment above the last counter (≤ ceiling)."""
    open_first(client, "qa-book-3", config.reviewer_mc, config.reviewer_otp, orig_state="CO", dest_state="MO")
    c1 = assert_json(client.counter("qa-book-3", HUGE[0]), 200, "counter 1")
    c2 = assert_json(client.counter("qa-book-3", HUGE[1]), 200, "counter 2")
    assert c1["action"] == "counter" and c2["action"] == "counter", f"got {c1} / {c2}"
    acc = assert_json(client.counter("qa-book-3", c2["amount"] + 25), 200, "counter 3")
    assert acc["action"] == "accept", f"expected accept on round 3, got {acc}"
    book = assert_json(client.book("qa-book-3"), 200, "book")
    status = assert_booking_settled(book, "book after 3 counters")
    assert_json(client.handoff("qa-book-3", "booked"), 200, "handoff")
    return f"round-3 accept at ${acc['amount']} → {status}"


@scenario("standard-urgent-pickup")
def standard_urgent(client: Client, config: Config) -> str:
    """A near-term pickup still negotiates to a booking on the urgent ladder."""
    offer = open_first(
        client, "qa-urgent", config.reviewer_mc, config.reviewer_otp,
        orig_state="IL", dest_state="TN",
    )
    c1 = assert_json(client.counter("qa-urgent", HUGE[0]), 200, "counter 1")
    assert c1["action"] == "counter", f"expected counter, got {c1}"
    acc = assert_json(client.counter("qa-urgent", c1["amount"]), 200, "counter 2")
    assert acc["action"] == "accept", f"expected accept, got {acc}"
    book = assert_json(client.book("qa-urgent"), 200, "book")
    status = assert_booking_settled(book, "urgent booking")
    assert_json(client.handoff("qa-urgent", "booked"), 200, "handoff")
    return f"urgent lane {status} at ${acc['amount']}"


# --- edges ---------------------------------------------------------------


@scenario("edge-fmcsa-inactive")
def edge_fmcsa_inactive(client: Client, config: Config) -> str:
    verify = assert_json(client.verify("qa-fmcsa", "123456"), 200, "verify")
    assert verify.get("eligible") is False, f"expected ineligible, got {verify}"
    assert str(verify.get("result_code", "")).startswith("fmcsa_rejected:"), f"got {verify}"
    return f"rejected: {verify['result_code']}"


@scenario("edge-otp-fails-three-times")
def edge_otp_fails(client: Client, config: Config) -> str:
    assert_json(client.verify("qa-otp-fail", config.reviewer_mc), 200, "verify")
    assert_json(client.send_otp("qa-otp-fail"), 200, "send_otp")
    bad = wrong_code(config.reviewer_otp)
    left = None
    for attempt in range(3):
        left = assert_json(client.verify_otp("qa-otp-fail", bad), 200, f"verify {attempt + 1}")
        assert left.get("verified") is False, f"attempt {attempt + 1} should fail, got {left}"
    assert left and left.get("attempts_left") == 0, f"expected locked (0 left), got {left}"
    return "locked after 3 wrong codes"


@scenario("edge-no-match")
def edge_no_match(client: Client, config: Config) -> str:
    verify_and_otp(client, "qa-nomatch", config.reviewer_mc, config.reviewer_otp)
    search = assert_json(client.search("qa-nomatch", orig_state="HI"), 200, "search")
    assert search.get("count") == 0, f"expected 0 loads, got {search}"
    assert search.get("result_code") == "no_match", f"expected no_match, got {search}"
    return "unserved lane returns no_match"


@scenario("edge-no-deal-after-three-rounds")
def edge_no_deal(client: Client, config: Config) -> str:
    open_first(client, "qa-nodeal", config.reviewer_mc, config.reviewer_otp, orig_state="TN", dest_state="AL")
    decision = None
    for i, ask in enumerate(HUGE[:3], start=1):
        decision = assert_json(client.counter("qa-nodeal", ask), 200, f"counter {i}")
    assert decision["action"] == "close", f"expected close, got {decision}"
    assert decision["result_code"] == "negotiation_failed", f"got {decision}"
    assert_json(client.handoff("qa-nodeal", "negotiation_failed"), 200, "handoff")
    return "closed with negotiation_failed"


@scenario("edge-clarify")
def edge_clarify(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-clarify", config.reviewer_mc, config.reviewer_otp, orig_state="UT", dest_state="NV")
    decision = assert_json(client.counter("qa-clarify", 1), 200, "counter")
    assert decision["action"] == "clarify" and decision["amount"] is None, f"got {decision}"
    return f"offer ${offer} read as a misheard 1 → clarify"


@scenario("edge-idempotent-counter")
def edge_idem_counter(client: Client, config: Config) -> str:
    open_first(client, "qa-idem-counter", config.reviewer_mc, config.reviewer_otp, orig_state="PA", dest_state="MA")
    first = assert_json(client.counter("qa-idem-counter", HUGE[0]), 200, "counter 1")
    second = assert_json(client.counter("qa-idem-counter", HUGE[0]), 200, "counter replay")
    assert first == second, f"replay diverged: {first} vs {second}"
    assert first["action"] == "counter", f"got {first}"
    return "repeat ask replays the round, not a new one"


@scenario("edge-idempotent-book")
def edge_idem_book(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-idem-book", config.reviewer_mc, config.reviewer_otp, orig_state="MI", dest_state="OH")
    assert_json(client.counter("qa-idem-book", offer), 200, "counter")
    first = assert_json(client.book("qa-idem-book"), 200, "book 1")
    second = assert_json(client.book("qa-idem-book"), 200, "book retry")
    assert_booking_settled(first, "idempotent book")
    assert first == second, f"the retry diverged: {first} vs {second}"
    return f"retry returns the same answer (ref {first['booking_ref']})"


# --- adversarial: server-side guards ------------------------------------


@scenario("adv-search-without-otp")
def adv_search_no_otp(client: Client, config: Config) -> str:
    assert_json(client.verify("qa-adv-search", config.reviewer_mc), 200, "verify")
    assert_status(client.search("qa-adv-search", orig_state="IL"), 403, "search without OTP")
    return "403"


@scenario("adv-negotiate-without-otp")
def adv_nego_no_otp(client: Client, config: Config) -> str:
    assert_json(client.verify("qa-adv-nego", config.reviewer_mc), 200, "verify")
    assert_status(client.open("qa-adv-nego", "LD10001"), 403, "open without OTP")
    return "403"


@scenario("adv-book-without-otp")
def adv_book_no_otp(client: Client, config: Config) -> str:
    assert_json(client.verify("qa-adv-book", config.reviewer_mc), 200, "verify")
    assert_status(client.book("qa-adv-book"), 403, "book without OTP")
    return "403"


@scenario("adv-send-before-verify")
def adv_send_before_verify(client: Client, config: Config) -> str:
    send = assert_json(client.send_otp("qa-adv-send"), 200, "send before verify")
    assert send.get("status") == "locked", f"expected locked, got {send}"
    assert send.get("result_code") == "otp_failed", f"got {send}"
    return "locked"


@scenario("adv-send-with-mc-number")
def adv_send_mc_number(client: Client, config: Config) -> str:
    assert_json(client.verify("qa-adv-mc", config.reviewer_mc), 200, "verify")
    assert_status(client.send_otp("qa-adv-mc", mc_number="15735"), 422, "send with mc_number")
    return "422 (mc_number rejected)"


@scenario("adv-ask-over-ceiling-never-accepted")
def adv_over_ceiling(client: Client, config: Config) -> str:
    open_first(client, "qa-adv-over", config.reviewer_mc, config.reviewer_otp, orig_state="FL")
    decisions = [
        assert_json(client.counter("qa-adv-over", ask), 200, f"ask {ask}") for ask in HUGE[:3]
    ]
    assert all(d["action"] != "accept" for d in decisions), f"an over-ceiling ask was accepted: {decisions}"
    assert decisions[-1]["action"] == "close", f"expected close, got {decisions[-1]}"
    return "three over-ceiling asks, never accepted"


@scenario("adv-negotiate-a-load-not-shown")
def adv_not_shown(client: Client, config: Config) -> str:
    verified_search(client, "qa-adv-notshown", config.reviewer_mc, config.reviewer_otp, orig_state="IL")
    assert_status(client.open("qa-adv-notshown", "LD00859"), 409, "open a not-shown load")
    return "409 (LD00859 was never pitched)"


@scenario("adv-fourth-round-refused")
def adv_fourth_round(client: Client, config: Config) -> str:
    open_first(client, "qa-adv-round4", config.reviewer_mc, config.reviewer_otp, orig_state="MN", dest_state="IL")
    for ask in HUGE[:3]:
        assert_json(client.counter("qa-adv-round4", ask), 200, f"ask {ask}")
    assert_status(client.counter("qa-adv-round4", HUGE[3]), 409, "fourth round")
    return "409"


@scenario("adv-book-with-agreed-rate")
def adv_book_agreed_rate(client: Client, config: Config) -> str:
    verified_search(client, "qa-adv-rate", config.reviewer_mc, config.reviewer_otp, orig_state="WA", dest_state="OR")
    assert_status(client.book("qa-adv-rate", extra={"agreed_rate": 123}), 422, "book with agreed_rate")
    return "422 (agreed_rate rejected)"


@scenario("adv-foreign-run-id-isolation")
def adv_foreign_run(client: Client, config: Config) -> str:
    # Run A pitches a load; run B (a different call) cannot negotiate it.
    shown = verified_search(
        client, "qa-adv-iso-a", config.reviewer_mc, config.reviewer_otp, orig_state="IL"
    )
    load_id = shown[0]["load_id"]
    open_first(client, "qa-adv-iso-b", config.reviewer_mc, config.reviewer_otp, orig_state="FL")
    assert_status(client.open("qa-adv-iso-b", load_id), 409, "cross-call negotiation")
    return f"409 (load {load_id} shown to run A only)"


# =========================================================================
# Seed mode — demo data for the Twin Signals views
#
# The views are only as good as what they have to read, and a fresh Twin has no
# unserved lanes, no repeat failures and no near-miss closes. Each step below
# drives the real HTTP surface exactly as a call would, so every row it creates
# is a row a real call could have created.
#
# Every run id is `qa-seed-<token>-<name>`. The token is a timestamp, so a
# re-run never collides with the previous one's session (a run id keeps its
# session for an hour and its negotiations would be spent). To clear a previous
# seed out of Twin: DELETE FROM call_events WHERE run_id LIKE 'qa-seed-%';
#
# **One MC only, and that is a limit of the environment, not of this script.**
# Every step needs a call that reaches the OTP gate, and only the reviewer MC
# can do that unattended: FMCSA is a live lookup (so the MC must come back
# eligible), the contact must be in `app/carriers/directory.py` (a code change
# plus a redeploy to add one), and the code must be readable by the script. The
# reviewer MC is the one MC that satisfies all three — a fixed code, no email.
# MC 000000 is used for the onboarding step on purpose: it is *not* in the
# directory, which is exactly what routes that call to onboarding.
# =========================================================================

#: MC 000000 has no contact on file and FMCSA does not know it, which is what
#: the onboarding path needs. Not a real carrier, and not a real lookup.
ONBOARDING_MC = "000000"

#: Optional environment override for `--token`.
TOKEN_ENV = "QA_SEED_TOKEN"


def seed_run_id(token: str, name: str) -> str:
    return f"qa-seed-{token}-{name}"


def _ceil25(value: int) -> int:
    return -(-value // 25) * 25


def fake_ceiling(rate: int) -> int:
    """The `MAX_BUY` the fake TMS invents for a load it only saw in `LOAD_QUERY`.

    Mirrors `app/tms/fake_server.py::_fake_max_buy` exactly. The seed needs it
    for one step only: a close whose gap is under 5% requires an ask a hair
    above the ceiling, and the ceiling is not observable from the outside — the
    agent is told neither it nor the gap, by design. So the operator computes it
    from the same fixture the deployment serves. This is the operator's
    knowledge, not the agent's: nothing here is ever sent to the service, and
    the ask that IS sent is just a number the carrier could have said.
    """
    return _ceil25(rate + max(300, rate // 5))


def load_fixture_rates(path: Path) -> tuple[dict[str, int], str | None]:
    """Read `LOAD_ID -> RATE` from a TMS fixture.

    Also returns the load whose `LOAD_GET` was recorded for real: its `MAX_BUY`
    is the fixture's own number rather than the fake's invented one, so the
    formula above does not apply to it and the seed must not use it for a
    near-miss close.
    """
    rates: dict[str, int] = {}
    recorded_detail: str | None = None
    if not path.exists():
        return rates, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip() == "END":
            continue
        fields = {}
        for token in line.split("|"):
            key, _, value = token.partition(":")
            if _:
                fields[key.strip().upper()] = value.strip()
        load_id = fields.get("LOAD_ID")
        if not load_id:
            continue
        if "MAX_BUY" in fields and recorded_detail is None:
            recorded_detail = load_id
        if "RATE" in fields and fields["RATE"].isdigit():
            rates[load_id] = int(fields["RATE"])
    return rates, recorded_detail


@dataclass(frozen=True)
class Lane:
    """A load in the demo fixture, plus the filters that isolate it.

    `counters` is how many rounds of the ladder to walk before agreeing, so a
    booking can land above the posted rate the way a real one does. Booking
    every lane at its opening offer would put `avg_delta_rpm` at zero in
    `v_rate_per_mile`, which is the number that view exists to show.
    """

    load_id: str
    filters: dict[str, str]
    counters: int = 0


#: Lanes the seed uses, each with filters that match exactly one fixture load —
#: so the step never has to guess which result it got back.
SEED_LANES = {
    # Two calls close on this one, for `v_signal_price_check` and its
    # `failed_negotiations >= 2`.
    "repeat_failure": Lane("LD10010", {"orig_state": "TN", "dest_state": "AL", "equipment": "DRY_VAN"}),
    # Near-miss closes for `v_signal_recoverable`. Ceilings 2100 / 1150 / 1200;
    # a $25 gap is 1.2–2.2% of each, comfortably inside the 5% band.
    "recoverable_a": Lane("LD10001", {"orig_state": "IL", "dest_state": "TN", "equipment": "DRY_VAN"}),
    "recoverable_b": Lane("LD10013", {"orig_state": "FL", "dest_state": "FL", "equipment": "DRY_VAN"}),
    "recoverable_c": Lane("LD10015", {"orig_state": "MI", "dest_state": "OH", "equipment": "FLATBED"}),
    # Bookings across lanes and equipment, for `v_rate_per_mile` and
    # `v_rep_queue`. Each walks a different depth of the ladder, so the agreed
    # rate per mile differs from the posted one by a different amount on each
    # lane instead of being identical on all three.
    "book_a": Lane("LD10004", {"orig_state": "GA", "dest_state": "FL", "equipment": "DRY_VAN"}, counters=0),
    "book_b": Lane("LD10006", {"orig_state": "CO", "dest_state": "MO", "equipment": "DRY_VAN"}, counters=1),
    "book_c": Lane("LD10012", {"orig_state": "WA", "dest_state": "OR", "equipment": "REEFER"}, counters=2),
}

#: Lanes the fixture does **not** serve, for `v_signal_unserved_demand`. Each is
#: the reverse of a lane the fixture does serve, so the emptiness is deliberate
#: and stays true as long as the fixture is the demo file.
UNSERVED_LANES = [
    ("ca_ny", {"equipment": "DRY_VAN", "orig_state": "CA", "dest_state": "NY"}),
    ("or_wa", {"equipment": "REEFER", "orig_state": "OR", "dest_state": "WA"}),
    ("nv_ut", {"equipment": "FLATBED", "orig_state": "NV", "dest_state": "UT"}),
    ("ma_pa", {"equipment": "DRY_VAN", "orig_state": "MA", "dest_state": "PA"}),
]

#: Above every demo ceiling, so the first two rounds always counter rather than
#: accept. Distinct per round so a repeat is never mistaken for a webhook retry.
SEED_HIGH_ASKS = (999_999, 999_998)

#: How far above the ceiling a near-miss ask sits. $25 is one increment: the
#: smallest real miss, and the one a rep is most likely to save.
NEAR_MISS_MARGIN = 25


def _seed_gate(client: Client, run_id: str, config: Config, mc: str) -> None:
    """verify → send → verify OTP. Raises if the gate does not open."""
    verify = assert_json(client.verify(run_id, mc), 200, "verify")
    assert verify.get("eligible") is True, f"MC {mc} is not eligible right now: {verify}"
    send = assert_json(client.send_otp(run_id), 200, "send_otp")
    assert send.get("status") == "sent", f"expected sent, got {send}"
    code = assert_json(client.verify_otp(run_id, config.reviewer_otp), 200, "verify_otp")
    assert code.get("verified") is True, f"expected verified, got {code}"


def _seed_open(client: Client, run_id: str, lane: Lane) -> int:
    """Search the lane, open the lane's load, return the opening offer.

    The load is located by id in the results rather than by position, so a
    fixture that grows a load cannot silently change which one is negotiated.
    """
    search = assert_json(client.search(run_id, **lane.filters), 200, "search")
    assert search.get("count", 0) >= 1, f"lane {lane.load_id} returned nothing: {search}"
    ids = [load["load_id"] for load in search["loads"]]
    assert lane.load_id in ids, f"{lane.load_id} not in {ids} for filters {lane.filters}"
    opened = assert_json(client.open(run_id, lane.load_id), 200, "open")
    return int(opened["offer"])


@seed_step("seed-unserved-lanes")
def seed_unserved(client: Client, config: Config) -> str:
    """Four lanes the fixture cannot serve, one call each."""
    token = _seed_token()
    for name, filters in UNSERVED_LANES:
        run_id = seed_run_id(token, f"unserved-{name}")
        _seed_gate(client, run_id, config, config.reviewer_mc)
        search = assert_json(client.search(run_id, **filters), 200, "search")
        assert search.get("count") == 0, f"expected an unserved lane, got {search}"
        assert search.get("result_code") == "no_match", f"expected no_match, got {search}"
    return f"{len(UNSERVED_LANES)} unserved lanes ({', '.join(n for n, _ in UNSERVED_LANES)})"


@seed_step("seed-repeat-failure")
def seed_repeat_failure(client: Client, config: Config) -> str:
    """The same load walked away from twice, from two separate calls."""
    token = _seed_token()
    lane = SEED_LANES["repeat_failure"]
    asks = []
    for suffix in ("a", "b"):
        run_id = seed_run_id(token, f"nodeal-{suffix}")
        _seed_gate(client, run_id, config, config.reviewer_mc)
        _seed_open(client, run_id, lane)
        for ask in SEED_HIGH_ASKS:
            decision = assert_json(client.counter(run_id, ask), 200, "counter")
            assert decision["action"] == "counter", f"expected counter, got {decision}"
        # Round 3: above the ceiling, so it closes and the gap is recorded.
        ask = _ceiling_for(lane) + 200
        decision = assert_json(client.counter(run_id, ask), 200, "counter 3")
        assert decision["action"] == "close", f"expected close, got {decision}"
        asks.append(ask)
    return f"{lane.load_id} closed twice (asks {asks})"


@seed_step("seed-near-miss-closes")
def seed_near_miss(client: Client, config: Config) -> str:
    """Three closes a hair above the ceiling — the recoverable pile."""
    token = _seed_token()
    made = []
    for name in ("recoverable_a", "recoverable_b", "recoverable_c"):
        lane = SEED_LANES[name]
        ceiling = _ceiling_for(lane)
        run_id = seed_run_id(token, name)
        _seed_gate(client, run_id, config, config.reviewer_mc)
        _seed_open(client, run_id, lane)
        for ask in SEED_HIGH_ASKS:
            assert_json(client.counter(run_id, ask), 200, "counter")
        decision = assert_json(
            client.counter(run_id, ceiling + NEAR_MISS_MARGIN), 200, "counter 3"
        )
        assert decision["action"] == "close", (
            f"{lane.load_id}: expected a close at ceiling+{NEAR_MISS_MARGIN}, got {decision}. "
            "The ceiling the seed computed from the fixture does not match the deploy's."
        )
        made.append(f"{lane.load_id}@+{NEAR_MISS_MARGIN}")
    return f"{len(made)} near-miss closes: {', '.join(made)}"


@seed_step("seed-bookings")
def seed_bookings(client: Client, config: Config) -> str:
    """A booking on each of three lanes, each at a different depth of the ladder.

    Depth 0 agrees at the opening offer (agreed == posted), 1 and 2 walk the
    ladder up first (agreed > posted). A carrier who simply accepts the first
    number and one who pushes twice look different in `v_rate_per_mile`, and a
    demo where every lane looks the same teaches nothing.
    """
    token = _seed_token()
    made = []
    for name in ("book_a", "book_b", "book_c"):
        lane = SEED_LANES[name]
        run_id = seed_run_id(token, name)
        _seed_gate(client, run_id, config, config.reviewer_mc)
        offer = _seed_open(client, run_id, lane)

        amount = offer
        for _ in range(lane.counters):
            decision = assert_json(client.counter(run_id, amount + 100_000), 200, "counter")
            assert decision["action"] == "counter", f"expected counter, got {decision}"
            amount = int(decision["amount"])
        accepted = assert_json(client.counter(run_id, amount), 200, "accept")
        assert accepted["action"] == "accept", f"expected accept, got {accepted}"

        booked = assert_json(client.book(run_id), 200, "book")
        status = assert_booking_settled(booked, f"booking {lane.load_id}")
        # Hand off with the reason the booking actually reached: a pending one
        # belongs in the rep queue under its own reason, and `v_rep_queue` shows
        # both. With TMS_FAKE_FAULT_RATE at 0.10 a lane lands pending now and
        # then, which is exactly the data that view wants.
        reason = "booked" if status == "booked" else "booking_pending"
        assert_json(client.handoff(run_id, reason), 200, "handoff")
        made.append(f"{lane.load_id}@${accepted['amount']}(+{lane.counters},{status})")
    return f"{len(made)} bookings: {', '.join(made)}"


@seed_step("seed-onboarding-lead")
def seed_onboarding(client: Client, config: Config) -> str:
    """A carrier with no contact on file, routed to onboarding.

    This one does not use the reviewer MC: the whole point is an MC the
    directory does not know, which is what makes `/otp/send` answer
    `routed_onboarding`. No OTP is issued and no code is asked for.
    """
    run_id = seed_run_id(_seed_token(), "onboarding")
    verify = assert_json(client.verify(run_id, ONBOARDING_MC), 200, "verify")
    assert verify.get("eligible") is False, f"expected an ineligible MC, got {verify}"

    send = assert_json(client.send_otp(run_id), 200, "send_otp")
    assert send.get("status") == "routed_onboarding", (
        f"expected routed_onboarding, got {send} — is MC {ONBOARDING_MC} in the carrier directory?"
    )
    assert send.get("masked_destination") is None, f"no code should have been sent: {send}"

    assert_json(client.handoff(run_id, "onboarding"), 200, "handoff")
    return f"MC {ONBOARDING_MC} routed to onboarding"


#: Filled in by `main()` before the seed steps run.
_RATES: dict[str, int] = {}
_RECORDED_DETAIL: str | None = None
_SEED_TOKEN_VALUE = ""


def _seed_token() -> str:
    return _SEED_TOKEN_VALUE


def _ceiling_for(lane: Lane) -> int:
    """The ceiling the fake TMS will report for `lane`.

    Refuses a load the fixture recorded a real `LOAD_GET` for: its MAX_BUY is
    the recorded number, not the invented one, so the formula would be wrong by
    exactly the amount this step cannot afford to be wrong by.
    """
    if lane.load_id == _RECORDED_DETAIL:
        raise AssertionError(
            f"{lane.load_id} has a recorded MAX_BUY; pick a synthesised load for a near-miss"
        )
    rate = _RATES.get(lane.load_id)
    if rate is None:
        raise AssertionError(
            f"no RATE for {lane.load_id} in the fixture; pass --fixtures pointing at the "
            "same file the deployment serves (TMS_FAKE_FIXTURES)"
        )
    return fake_ceiling(rate)


# --- driver --------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seed",
        action="store_true",
        help="create demo data for the Twin Signals views instead of running the QA scenarios",
    )
    parser.add_argument(
        "--fixtures",
        default="tests/fixtures/tms_demo.txt",
        help="the fixture file the deployment serves as TMS_FAKE_FIXTURES (seed mode only)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="run-id token for seed mode; defaults to a UTC timestamp. "
        f"Also read from ${TOKEN_ENV}.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _SEED_TOKEN_VALUE, _RATES, _RECORDED_DETAIL

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    client = Client(config)

    # Confirm the deploy is alive before driving calls against it.
    health = assert_json(client.health(), 200, "health")
    print(f"health: {health}")

    if args.seed:
        _SEED_TOKEN_VALUE = (
            args.token or os.environ.get(TOKEN_ENV, "").strip()
            or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        )
        path = Path(args.fixtures)
        _RATES, _RECORDED_DETAIL = load_fixture_rates(path)
        print(f"seed token: {_SEED_TOKEN_VALUE}")
        print(f"fixture: {path} — {len(_RATES)} loads with a rate")
        runner = _SEED_STEPS
    else:
        runner = _SCENARIOS

    for name, run in runner.items():
        run(client, config)

    client.close()

    print()
    failures = [r for r in RESULTS if not r.passed]
    for result in RESULTS:
        mark = "PASS" if result.passed else "FAIL"
        suffix = f"  — {result.note}" if result.note else ""
        print(f"{mark:4}  {result.name}{suffix}")

    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} scenarios passed")
    if failures:
        print("\nfailed:")
        for result in failures:
            print(f"  - {result.name}: {result.note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
