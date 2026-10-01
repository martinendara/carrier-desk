"""A whole call, wired together, with the one assertion that matters most.

Phase 2 has no HTTP layer, so these tests drive the services the way the API
will: FMCSA, then OTP, then a search that records what was shown, then
negotiation. The point is the cross-cutting check at the end — after a full
call, the buy ceiling appears in no agent-facing value anywhere.

The search step is simulated here (recording the shown load and emitting the
event) because the search service belongs to phase 3.
"""

from __future__ import annotations

from fractions import Fraction

import httpx
import pytest
from conftest import OTP_SECRET, TickClock, load_fmcsa_fixture
from helpers import RECORDED_CEILING, make_load

from app.events import EventType, InMemoryEventSink
from app.fmcsa import FmcsaClient
from app.negotiation import NegotiationAction, NegotiationEngine
from app.onboarding import LeadTemperature, qualify
from app.otp import OtpService
from app.tms.models import LoadDetail

RUN = "call-2026-10-01-0001"
MC = "123456"

STEPS = (Fraction(2, 5), Fraction(3, 4))
URGENT_STEPS = (Fraction(3, 5), Fraction(9, 10))


def collect_numbers(value, found=None) -> list[int]:
    """Every integer anywhere in a nested structure, string digits included."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            collect_numbers(key, found)
            collect_numbers(item, found)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            collect_numbers(item, found)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        found.append(value)
    elif isinstance(value, str) and value.isdigit():
        found.append(int(value))
    return found


def fmcsa_for(fixture: str) -> FmcsaClient:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=load_fmcsa_fixture(fixture))
    )
    return FmcsaClient("https://fmcsa.test", "key", client=httpx.Client(transport=transport))


def engine_for(store, events, tick: TickClock) -> NegotiationEngine:
    return NegotiationEngine(
        store,
        steps=STEPS,
        urgent_steps=URGENT_STEPS,
        clock=tick,
        sink=events,
        id_factory=lambda: "neg_e2e",
    )


def test_a_full_call_never_puts_the_ceiling_in_front_of_the_agent(
    store, tick: TickClock, events: InMemoryEventSink
):
    """The ceiling is known to the server and to the event log, and nowhere else."""
    ceiling = RECORDED_CEILING
    agent_facing: list[object] = []

    # --- 1. Verify the carrier ------------------------------------------
    check = fmcsa_for("carrier_eligible").check(MC)
    events.emit(RUN, EventType.FMCSA_CHECKED, mc_number=MC, eligible=check.eligible)
    agent_facing.append({"eligible": check.eligible, "legal_name": check.legal_name})
    assert check.eligible

    # --- 2. Send and verify the OTP -------------------------------------
    otp = OtpService(store, secret=OTP_SECRET, clock=tick, sink=events)
    dispatch = otp.send(RUN, MC)
    agent_facing.append(dispatch.agent_view)
    assert dispatch.delivery_view is not None  # the platform gets the code

    verification = otp.verify(RUN, dispatch.code)
    agent_facing.append(verification.agent_view)
    assert verification.verified

    # --- 3. Search, and pitch the load ----------------------------------
    load: LoadDetail = make_load()
    session = store.get_or_create(RUN)
    session.record_shown_load(load.load_id)
    events.emit(RUN, EventType.SEARCH_PERFORMED, filters={"ORIG_STATE": "FL"}, results=1)
    events.emit(RUN, EventType.LOAD_PITCHED, load_id=load.load_id, rate=load.rate)
    agent_facing.append({"loads": [load.public_dict()]})

    # --- 4. Negotiate, three rounds -------------------------------------
    engine = engine_for(store, events, tick)
    offer = engine.open(RUN, load)
    agent_facing.append(offer.agent_view)
    assert offer.offer < ceiling

    for ask in (2400, 2500, 3000):
        decision = engine.counter(RUN, ask)
        agent_facing.append(decision.agent_view)

    assert decision.action is NegotiationAction.CLOSE

    # --- The check ------------------------------------------------------
    numbers = collect_numbers(agent_facing)
    assert ceiling not in numbers, f"the ceiling leaked into {agent_facing}"

    # And not smuggled through a key name either.
    text = str(agent_facing)
    for forbidden in ("max_buy", "MAX_BUY", "ceiling", "gap", "threshold"):
        assert forbidden not in text

    # The server, by contrast, knows all of it.
    assert events.of_type(EventType.NEGOTIATION_OPENED)[0].payload["ceiling"] == ceiling
    assert events.of_type(EventType.NEGOTIATION_CLOSED)[-1].payload["gap"] == 3000 - ceiling


def test_the_event_log_covers_every_step_of_the_call(store, tick, events):
    fmcsa_for("carrier_eligible").check(MC)
    otp = OtpService(store, secret=OTP_SECRET, clock=tick, sink=events)
    dispatch = otp.send(RUN, MC)
    otp.verify(RUN, dispatch.code)

    load = make_load()
    store.get_or_create(RUN).record_shown_load(load.load_id)
    engine = engine_for(store, events, tick)
    engine.open(RUN, load)
    engine.counter(RUN, 2400)

    assert [event.type for event in events.for_run(RUN)] == [
        EventType.OTP_SENT,
        EventType.OTP_VERIFIED,
        EventType.NEGOTIATION_OPENED,
        EventType.NEGOTIATION_ROUND,
    ]


def test_a_search_that_matches_nothing_is_still_recorded(store, tick, events):
    events.emit(RUN, EventType.SEARCH_PERFORMED, filters={"ORIG_STATE": "WY"}, results=0)

    assert events.of_type(EventType.SEARCH_PERFORMED)[0].payload["results"] == 0


def test_a_carrier_we_cannot_verify_never_gets_an_otp(store, tick, events):
    """FMCSA fails closed, and a rejected carrier never reaches the OTP step."""
    check = fmcsa_for("carrier_out_of_service").check(MC)
    events.emit(RUN, EventType.FMCSA_CHECKED, mc_number=MC, eligible=check.eligible)

    assert not check.eligible
    assert check.result_code() == "fmcsa_rejected:out_of_service"
    # Nothing downstream ran: no session was even started for this call.
    assert store.get(RUN) is None
    assert events.of_type(EventType.OTP_SENT) == []


def test_an_mc_with_no_contact_becomes_a_qualified_lead(store, tick, events):
    """The other way a call can end: no contact on file, so route the human."""
    unknown_mc = "999999"
    otp = OtpService(store, secret=OTP_SECRET, clock=tick, sink=events)
    dispatch = otp.send(RUN, unknown_mc)

    assert dispatch.status.value == "routed_onboarding"
    # No contact means FMCSA was never consulted, so the age is unknown — and
    # unknown routes to Review rather than to an optimistic default.
    lead = qualify(authority_age_days=None, matching_loads=0)
    events.emit(
        RUN,
        EventType.LEAD_CREATED,
        mc_number=unknown_mc,
        temperature=lead.temperature.value,
        reason=lead.reason,
    )

    assert lead.temperature is LeadTemperature.REVIEW
    assert events.of_type(EventType.LEAD_CREATED)[0].payload["temperature"] == "review"


def test_a_load_shown_in_another_call_cannot_be_negotiated(store, tick, events):
    """Two carriers on two calls must not be able to trade load ids."""
    engine = engine_for(store, events, tick)
    load = make_load()
    store.get_or_create("call-a").record_shown_load(load.load_id)

    from app.sessions import LoadNotShown

    with pytest.raises(LoadNotShown):
        engine.open("call-b", load)


def test_two_negotiations_are_allowed_and_a_third_is_not(store, tick, events):
    engine = engine_for(store, events, tick)
    session = store.get_or_create(RUN)
    loads = [make_load(load_id=f"LD{index}") for index in range(3)]
    for load in loads:
        session.record_shown_load(load.load_id)

    engine.open(RUN, loads[0])
    engine.open(RUN, loads[1])

    from app.sessions import NegotiationLimitReached

    with pytest.raises(NegotiationLimitReached):
        engine.open(RUN, loads[2])
