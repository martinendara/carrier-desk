"""Negotiation: the money arithmetic and the round machine.

The edge cases named in the brief are all here, plus the one that matters most:
no agent-facing response ever carries the ceiling.
"""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction

import pytest

from app.negotiation import (
    CeilingUnavailable,
    NegotiationAction,
    NegotiationAlreadyClosed,
    NegotiationEngine,
    NoActiveNegotiation,
    concession_step,
    floor_to,
    is_urgent,
    opening_offer,
)
from app.sessions import LoadNotShown, NegotiationLimitReached, RunSessionStore
from helpers import RECORDED_CEILING, RECORDED_LOAD_ID, RECORDED_RATE, make_load
from conftest import NOW, TickClock

NORMAL_STEPS = (Fraction(2, 5), Fraction(3, 4))
URGENT_STEPS = (Fraction(3, 5), Fraction(9, 10))


def make_engine(store, events=None, **overrides) -> NegotiationEngine:
    options = {
        "steps": NORMAL_STEPS,
        "urgent_steps": URGENT_STEPS,
        "max_rounds": 3,
        "increment": 25,
        "ceiling_margin": 25,
        "clock": lambda: NOW,
        "sink": events,
        "id_factory": lambda: "neg_test",
    }
    options.update(overrides)
    return NegotiationEngine(store, **options)


def opened(store, events=None, load=None, run_id="run-1", **overrides):
    """A session with one shown load and an open negotiation on it."""
    engine = make_engine(store, events, **overrides)
    load = load or make_load()
    session = store.get_or_create(run_id)
    session.record_shown_load(load.load_id)
    offer = engine.open(run_id, load)
    return engine, offer, run_id


def opened_timed(store, events=None, load=None, run_id="run-1", **overrides):
    """Like `opened`, but with a clock the test advances past the replay window."""
    clock = TickClock(NOW)
    engine, offer, rid = opened(store, events, load, run_id, clock=clock, **overrides)
    return engine, offer, rid, clock


# --- floor25 and the opening offer --------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2073, 2050), (2050, 2050), (2528, 2525), (25, 25), (24, 0), (0, 0)],
)
def test_floor_to_rounds_down(value, expected):
    assert floor_to(value) == expected


def test_opening_with_the_ceiling_above_the_posted_rate():
    """The recorded load: RATE 2073 under a ceiling of 2528."""
    assert opening_offer(RECORDED_RATE, RECORDED_CEILING) == 2050


def test_opening_with_the_ceiling_below_the_posted_rate():
    """RATE above MAX_BUY: open at 90% of the ceiling, not at the posted rate."""
    assert opening_offer(2600, 2528) == 2275


def test_opening_is_never_above_the_ceiling_margin():
    """A posted rate inside one increment of the ceiling still respects C - 25."""
    assert opening_offer(2600, 2528) <= 2528 - 25
    assert opening_offer(2526, 2528) == 2503


def test_opening_uses_integer_arithmetic_for_the_ninety_percent():
    """0.90 x C, floored, must not depend on how a float rounds."""
    for ceiling in range(50, 5000, 25):
        assert opening_offer(ceiling + 1000, ceiling) <= (9 * ceiling) // 10


# --- the concession ladder ----------------------------------------------


def test_round_one_and_two_steps():
    # O = 2050, C = 2528, gap = 478.
    # Round 1: 40% of 478 = 191.2 -> 2241.2 -> floor25 -> 2225.
    # Round 2: 75% of 478 = 358.5 -> 2408.5 -> floor25 -> 2400.
    assert concession_step(2050, 2528, 2050, 1, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS) == 2225
    assert concession_step(2050, 2528, 2225, 2, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS) == 2400


def test_urgent_uses_the_steeper_ladder():
    # 60% of 478 = 286.8 -> 2336.8 -> floor25 -> 2325.
    # 90% of 478 = 430.2 -> 2480.2 -> floor25 -> 2475.
    assert concession_step(
        2050, 2528, 2050, 1, urgent=True, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS
    ) == 2325
    assert concession_step(
        2050, 2528, 2325, 2, urgent=True, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS
    ) == 2475


def test_a_step_never_reaches_the_ceiling():
    for ceiling in range(100, 5000, 25):
        for opening in range(0, ceiling, 25):
            step = concession_step(
                opening, ceiling, opening, 1, steps=(Fraction(1, 1),), urgent_steps=URGENT_STEPS
            )
            assert step <= ceiling - 25


def test_a_narrow_margin_still_concedes_by_one_increment():
    """Where rounding alone would leave the offer flat, the increment floor holds.

    O = 2500, C = 2600, gap = 100. 40% = 40, so floor25(2540) = 2525 — but if the
    rounding had landed on the current offer the concession would vanish and the
    counter would repeat itself. The `current + 25` floor is what prevents that.
    """
    step = concession_step(2500, 2600, 2500, 1, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS)

    assert step == 2525
    assert step > 2500


def test_the_ceiling_cap_wins_over_the_increment_floor():
    """When the cap leaves no room for +25, the step stays at the cap."""
    # C - 25 = 2575 and the current offer is already there.
    step = concession_step(2500, 2600, 2575, 1, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS)

    assert step == 2575
    assert step <= 2600 - 25


def test_no_step_ever_exceeds_the_ceiling_minus_margin():
    for ceiling in range(100, 3000, 25):
        opening = ceiling // 2
        current = opening
        for round_number in (1, 2):
            step = concession_step(
                opening, ceiling, current, round_number, steps=NORMAL_STEPS, urgent_steps=URGENT_STEPS
            )
            assert step <= ceiling - 25
            assert step >= current or step == ceiling - 25
            current = step


# --- urgency -------------------------------------------------------------


def test_urgency_window_is_twenty_four_hours():
    assert is_urgent(NOW + timedelta(hours=23), NOW)
    assert not is_urgent(NOW + timedelta(hours=25), NOW)
    assert not is_urgent(NOW + timedelta(hours=24), NOW)


def test_a_pickup_in_the_past_is_urgent():
    assert is_urgent(NOW - timedelta(hours=3), NOW)


# --- opening a negotiation ----------------------------------------------


def test_open_returns_the_opening_offer_and_hides_everything_else(store):
    engine, offer, _ = opened(store)

    assert offer.agent_view == {"load_id": RECORDED_LOAD_ID, "offer": 2050}
    assert RECORDED_CEILING not in offer.agent_view.values()


def test_open_refuses_a_load_that_was_not_shown_in_this_call(store):
    """The core guard: you negotiate what you were pitched, and nothing else."""
    engine = make_engine(store)
    store.get_or_create("run-1")  # a session exists, but nothing has been shown

    with pytest.raises(LoadNotShown):
        engine.open("run-1", make_load(load_id="LD00001"))


def test_open_refuses_a_load_shown_in_a_different_call(store):
    engine = make_engine(store)
    store.get_or_create("run-1").record_shown_load(RECORDED_LOAD_ID)
    store.get_or_create("run-2")  # a different call showed nothing

    with pytest.raises(LoadNotShown):
        engine.open("run-2", make_load())


def test_open_allows_two_negotiations_and_refuses_the_third(store):
    engine = make_engine(store)
    session = store.get_or_create("run-1")
    first, second = make_load(load_id="LD1"), make_load(load_id="LD2")
    session.record_shown_load("LD1")
    session.record_shown_load("LD2")
    session.record_shown_load("LD3")

    engine.open("run-1", first)
    engine.open("run-1", second)

    with pytest.raises(NegotiationLimitReached):
        engine.open("run-1", make_load(load_id="LD3"))


def test_open_refuses_a_load_with_no_max_buy(store):
    """Without a ceiling we would be inventing the number we can pay."""
    engine = make_engine(store)
    load = make_load(max_buy=None)
    store.get_or_create("run-1").record_shown_load(load.load_id)

    with pytest.raises(CeilingUnavailable):
        engine.open("run-1", load)


def test_open_refuses_a_ceiling_with_no_room(store):
    engine = make_engine(store)
    load = make_load(rate=40, max_buy=40)
    store.get_or_create("run-1").record_shown_load(load.load_id)

    with pytest.raises(CeilingUnavailable):
        engine.open("run-1", load)


def test_open_is_urgent_when_the_pickup_is_close(store):
    engine = make_engine(store)
    load = make_load(pickup_dt=NOW + timedelta(hours=6))
    store.get_or_create("run-1").record_shown_load(load.load_id)

    assert engine.open("run-1", load).urgent


# --- rounds --------------------------------------------------------------


def test_a_request_at_the_current_offer_is_accepted(store):
    engine, offer, run_id = opened(store)

    decision = engine.counter(run_id, offer.offer)

    assert decision.action is NegotiationAction.ACCEPT
    assert decision.agent_view == {"action": "accept", "amount": 2050}


def test_a_request_below_the_opening_is_accepted_at_the_request(store):
    """The carrier asks for less than we offered. Take it, do not talk them up."""
    engine, _, run_id = opened(store)

    decision = engine.counter(run_id, 1900)

    assert decision.action is NegotiationAction.ACCEPT
    assert decision.agent_view == {"action": "accept", "amount": 1900}


def test_a_request_above_the_step_is_countered(store):
    engine, _, run_id = opened(store)

    decision = engine.counter(run_id, 2400)

    assert decision.action is NegotiationAction.COUNTER
    assert decision.agent_view == {"action": "counter", "amount": 2225}


def test_a_request_at_the_step_is_accepted_at_the_request(store):
    engine, _, run_id = opened(store)

    decision = engine.counter(run_id, 2150)

    assert decision.action is NegotiationAction.ACCEPT
    assert decision.amount == 2150


def test_three_rounds_without_agreement_close_the_call(store, events):
    engine, _, run_id, clock = opened_timed(store, events)

    assert engine.counter(run_id, 3000).action is NegotiationAction.COUNTER
    clock.advance(seconds=16)
    assert engine.counter(run_id, 3000).action is NegotiationAction.COUNTER
    clock.advance(seconds=16)
    final = engine.counter(run_id, 3000)

    assert final.action is NegotiationAction.CLOSE
    # No transfer happens on a close: the caller sees an action, not a fallback.
    assert final.agent_view["action"] == "close"


def test_the_final_round_accepts_anything_up_to_the_ceiling(store):
    engine, _, run_id, clock = opened_timed(store)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)

    decision = engine.counter(run_id, RECORDED_CEILING)

    assert decision.action is NegotiationAction.ACCEPT
    assert decision.amount == RECORDED_CEILING


def test_the_final_round_closes_above_the_ceiling(store):
    engine, _, run_id, clock = opened_timed(store)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)

    decision = engine.counter(run_id, RECORDED_CEILING + 1)

    assert decision.action is NegotiationAction.CLOSE


def test_a_fourth_round_is_refused(store):
    """The third round always concludes, so there is never a fourth."""
    engine, _, run_id, clock = opened_timed(store)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)  # closes

    with pytest.raises(NegotiationAlreadyClosed):
        engine.counter(run_id, 2500)


def test_a_round_after_an_acceptance_is_refused(store):
    engine, _, run_id = opened(store)
    engine.counter(run_id, 2000)  # accepted

    with pytest.raises(NegotiationAlreadyClosed):
        engine.counter(run_id, 2100)


def test_a_round_with_no_negotiation_is_refused(store):
    engine = make_engine(store)
    store.get_or_create("run-1")

    with pytest.raises(NoActiveNegotiation):
        engine.counter("run-1", 2000)


def test_a_counter_is_never_above_the_request(store):
    engine, _, run_id = opened(store)

    decision = engine.counter(run_id, 2300)

    assert decision.action is NegotiationAction.COUNTER
    assert decision.amount == 2225
    assert decision.amount < 2300


@pytest.mark.parametrize("ask", [0, -100, "not a number", "", None, True])
def test_a_nonsense_request_is_rejected(store, ask):
    engine, _, run_id = opened(store)

    with pytest.raises(ValueError):
        engine.counter(run_id, ask)


def test_the_urgent_ladder_reaches_higher_sooner(store):
    engine = make_engine(store)
    load = make_load(pickup_dt=NOW + timedelta(hours=4))
    store.get_or_create("run-1").record_shown_load(load.load_id)
    engine.open("run-1", load)

    urgent_counter = engine.counter("run-1", 5000)

    calm_store = RunSessionStore(ttl_seconds=3600, clock=lambda: 0.0)
    calm_engine = make_engine(calm_store)
    calm_load = make_load(pickup_dt=NOW + timedelta(days=10))
    calm_store.get_or_create("run-1").record_shown_load(calm_load.load_id)
    calm_engine.open("run-1", calm_load)

    calm_counter = calm_engine.counter("run-1", 5000)

    assert urgent_counter.amount > calm_counter.amount


# --- the ceiling never reaches the agent --------------------------------


def test_no_agent_facing_response_carries_the_ceiling(store):
    """Exhaustive over the asks a carrier could make on the recorded load.

    The one permitted exception is a carrier asking for exactly the ceiling in
    the final round, where the number the agent hears is the carrier's own ask
    coming back — not `C` being disclosed. That is asserted, not assumed.
    """
    seen_agent_values = []
    ceiling = RECORDED_CEILING

    for ask in range(1800, ceiling + 400, 25):
        fresh_store = RunSessionStore(ttl_seconds=3600, clock=lambda: 0.0)
        engine, offer, run_id = opened(fresh_store)
        seen_agent_values.extend(offer.agent_view.values())

        for _ in range(3):
            decision = engine.counter(run_id, ask)
            seen_agent_values.extend(decision.agent_view.values())
            if decision.action is not NegotiationAction.COUNTER:
                break

    leaked = [value for value in seen_agent_values if value == ceiling]
    assert not leaked, "the ceiling appeared in an agent-facing response"


def test_the_close_decision_reports_the_last_offer_not_the_ceiling(store):
    engine, _, run_id, clock = opened_timed(store)
    engine.counter(run_id, 5000)
    clock.advance(seconds=16)
    engine.counter(run_id, 5000)
    clock.advance(seconds=16)

    decision = engine.counter(run_id, 5000)

    assert decision.action is NegotiationAction.CLOSE
    assert decision.amount <= RECORDED_CEILING - 25


def test_the_agent_never_sees_the_gap_or_the_steps(store):
    engine, _, run_id = opened(store)

    for _ in range(3):
        decision = engine.counter(run_id, 3000)
        assert set(decision.agent_view) == {"action", "amount"}
        assert isinstance(decision.agent_view["amount"], int)


# --- the event log is allowed to know -----------------------------------


def test_the_opening_event_records_the_ceiling_for_a_rep(store, events):
    from app.events import EventType

    opened(store, events)

    (event,) = events.of_type(EventType.NEGOTIATION_OPENED)
    assert event.payload["ceiling"] == RECORDED_CEILING
    assert event.payload["opening_offer"] == 2050


def test_a_closed_call_records_the_gap_for_a_rep(store, events):
    from app.events import EventType

    engine, _, run_id, clock = opened_timed(store, events)
    engine.counter(run_id, 5000)
    clock.advance(seconds=16)
    engine.counter(run_id, 5000)
    clock.advance(seconds=16)
    engine.counter(run_id, 5000)

    closed = events.of_type(EventType.NEGOTIATION_CLOSED)[-1]
    assert closed.payload["outcome"] == "closed"
    assert closed.payload["gap"] == 5000 - RECORDED_CEILING


def test_every_round_is_recorded_with_its_number(store, events):
    from app.events import EventType

    engine, _, run_id, clock = opened_timed(store, events)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)

    rounds = events.of_type(EventType.NEGOTIATION_ROUND)
    assert [event.payload["round"] for event in rounds] == [1, 2]


# --- counter replay (idempotency) ---------------------------------------


def test_a_retry_of_the_same_ask_replays_the_round(store, events):
    """A webhook retry of the same ask inside the window is not a new round."""
    from app.events import EventType

    engine, _, run_id, clock = opened_timed(store, events)

    first = engine.counter(run_id, 3000)
    clock.advance(seconds=2)
    replay = engine.counter(run_id, 3000)

    assert replay == first
    assert replay.action is NegotiationAction.COUNTER
    # The round counter did not advance, and no second round event was written.
    assert store.require(run_id).negotiations[-1].round_number == 1
    assert len(events.of_type(EventType.NEGOTIATION_ROUND)) == 1


def test_a_retry_of_an_accept_replays_the_accept(store):
    """A retry of the accept must not read as a fourth-round refusal."""
    engine, offer, run_id, clock = opened_timed(store)

    first = engine.counter(run_id, offer.offer)  # accept at the opening offer
    clock.advance(seconds=2)
    replay = engine.counter(run_id, offer.offer)

    assert first.action is NegotiationAction.ACCEPT
    assert replay == first


def test_a_retry_of_a_close_replays_the_close(store):
    engine, _, run_id, clock = opened_timed(store)

    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    closed = engine.counter(run_id, 3000)  # closes on round 3
    clock.advance(seconds=2)

    replay = engine.counter(run_id, 3000)

    assert closed.action is NegotiationAction.CLOSE
    assert replay == closed


def test_the_same_ask_after_the_window_is_a_new_round(store):
    engine, _, run_id, clock = opened_timed(store)

    first = engine.counter(run_id, 3000)
    clock.advance(seconds=16)
    second = engine.counter(run_id, 3000)

    assert second.action is NegotiationAction.COUNTER
    assert second.round_number == 2
    assert second.amount != first.amount


def test_a_different_ask_within_the_window_is_a_new_round(store):
    engine, _, run_id, clock = opened_timed(store)

    engine.counter(run_id, 3000)
    clock.advance(seconds=1)
    second = engine.counter(run_id, 3100)

    assert second.round_number == 2


# --- clarify -------------------------------------------------------------


def test_an_ask_far_below_the_offer_is_clarified(store):
    """A misheard number (e.g. 'ten fifty' heard as 550) must not be accepted."""
    engine, offer, run_id = opened(store)

    decision = engine.counter(run_id, offer.offer * 3 // 4)  # 75% of the offer

    assert decision.action is NegotiationAction.CLARIFY
    assert decision.amount is None
    assert store.require(run_id).negotiations[-1].round_number == 0


def test_a_clarify_does_not_consume_a_round(store):
    """After a clarify, the next ask is still round 1."""
    engine, offer, run_id, clock = opened_timed(store)

    engine.counter(run_id, offer.offer * 3 // 4)  # clarify
    clock.advance(seconds=16)
    decision = engine.counter(run_id, offer.offer)  # a normal ask

    assert decision.action is NegotiationAction.ACCEPT
    assert decision.round_number == 1


def test_an_ask_at_the_threshold_is_not_clarified(store):
    """At 80% of the offer the normal accept path still runs."""
    engine, offer, run_id = opened(store)

    decision = engine.counter(run_id, offer.offer * 4 // 5)  # exactly 80%

    assert decision.action is not NegotiationAction.CLARIFY
    assert decision.action is NegotiationAction.ACCEPT


def test_a_retry_of_a_clarify_replays_the_clarify(store):
    engine, offer, run_id, clock = opened_timed(store)

    first = engine.counter(run_id, offer.offer * 3 // 4)
    clock.advance(seconds=2)
    replay = engine.counter(run_id, offer.offer * 3 // 4)

    assert first.action is NegotiationAction.CLARIFY
    assert replay == first
    assert store.require(run_id).negotiations[-1].round_number == 0
