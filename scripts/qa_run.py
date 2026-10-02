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

Usage (values are secrets and stay out of the repo):

    QA_BASE_URL=https://<host> QA_API_KEY=<service key> \
    QA_REVIEWER_MC=15735 QA_REVIEWER_OTP=123456 \
    .venv/bin/python scripts/qa_run.py

Exit code 0 iff every scenario passed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

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
RESULTS: list[Result] = []


def scenario(name: str):
    """Register a scenario under `name` and wrap it to record a Result."""

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

        _SCENARIOS[name] = run
        return run

    return decorator


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
    assert book["status"] == "booked" and book["result_code"] == "booked", f"expected booked, got {book}"
    assert_json(client.handoff("qa-book-first", "booked"), 200, "handoff")
    return f"booked at first offer ${offer}"


@scenario("standard-book-after-1-counter")
def standard_book_1_counter(client: Client, config: Config) -> str:
    offer = open_first(client, "qa-book-1", config.reviewer_mc, config.reviewer_otp, orig_state="TX", dest_state="CO")
    c1 = assert_json(client.counter("qa-book-1", HUGE[0]), 200, "counter 1")
    assert c1["action"] == "counter", f"expected counter, got {c1}"
    acc = assert_json(client.counter("qa-book-1", c1["amount"]), 200, "counter 2")
    assert acc["action"] == "accept", f"expected accept, got {acc}"
    book = assert_json(client.book("qa-book-1"), 200, "book")
    assert book["status"] == "booked", f"expected booked, got {book}"
    assert_json(client.handoff("qa-book-1", "booked"), 200, "handoff")
    return f"offer ${offer} → counter ${c1['amount']} → booked ${acc['amount']}"


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
    assert book["status"] == "booked", f"expected booked, got {book}"
    assert_json(client.handoff("qa-book-2", "booked"), 200, "handoff")
    return f"offer ${offer} → ${c1['amount']} → ${c2['amount']} → booked ${acc['amount']}"


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
    assert book["status"] == "booked", f"expected booked, got {book}"
    assert_json(client.handoff("qa-book-3", "booked"), 200, "handoff")
    return f"round-3 accept at ${acc['amount']}"


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
    assert book["status"] == "booked", f"expected booked, got {book}"
    assert_json(client.handoff("qa-urgent", "booked"), 200, "handoff")
    return f"urgent lane booked at ${acc['amount']}"


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
    assert first["status"] == "booked", f"expected booked, got {first}"
    assert first["booking_ref"] == second["booking_ref"], f"refs diverge: {first} vs {second}"
    return f"retry returns the same booking_ref {first['booking_ref']}"


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


# --- driver --------------------------------------------------------------


def main() -> int:
    config = load_config()
    client = Client(config)

    # Item 4: confirm the deploy is alive before driving calls against it.
    health = assert_json(client.health(), 200, "health")
    print(f"health: {health}")

    for name, run in _SCENARIOS.items():
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
