"""Run sessions: keying, TTL, and the rules about what may be negotiated."""

from __future__ import annotations

import pytest
from conftest import FakeClock

from app.sessions import (
    MAX_SHOWN_LOADS,
    LoadNotShown,
    RunSessionStore,
    SessionNotFound,
    validate_run_id,
)

RUN = "run-abc-123"


# --- run_id validation --------------------------------------------------


@pytest.mark.parametrize("run_id", ["run-1", "abc", "A" * 128, "run_with.dots-and-dashes"])
def test_valid_run_ids(run_id):
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize("run_id", ["", "   ", "A" * 129, "run\x00id", "run\nid", "café"])
def test_invalid_run_ids(run_id):
    with pytest.raises(ValueError):
        validate_run_id(run_id)


def test_a_run_id_is_stripped():
    assert validate_run_id("  run-1  ") == "run-1"


# --- the store ----------------------------------------------------------


def test_get_or_create_is_stable(store):
    first = store.get_or_create(RUN)
    second = store.get_or_create(RUN)

    assert first is second


def test_get_returns_none_for_an_unknown_run(store):
    assert store.get("never-seen") is None


def test_require_raises_for_an_unknown_run(store):
    with pytest.raises(SessionNotFound):
        store.require("never-seen")


def test_separate_runs_do_not_share_state(store):
    first = store.get_or_create("run-1")
    first.record_shown_load("LD1")

    assert store.get_or_create("run-2").shown_load_ids == []


def test_a_session_expires_after_the_ttl(clock: FakeClock):
    store = RunSessionStore(ttl_seconds=60, clock=clock)
    store.get_or_create(RUN)

    clock.advance(59)
    assert store.get(RUN) is not None

    clock.advance(2)
    assert store.get(RUN) is None


def test_reading_a_session_extends_its_ttl(clock: FakeClock):
    """A call that is still talking must not age out from under itself."""
    store = RunSessionStore(ttl_seconds=60, clock=clock)
    store.get_or_create(RUN)

    for _ in range(5):
        clock.advance(50)
        store.get_or_create(RUN)

    assert store.get(RUN) is not None


def test_purge_expired_removes_only_the_stale_ones(clock: FakeClock):
    store = RunSessionStore(ttl_seconds=60, clock=clock)
    store.get_or_create("old")
    clock.advance(30)
    store.get_or_create("new")
    clock.advance(40)  # "old" is at 70, "new" at 40

    assert store.purge_expired() == 1
    assert store.get("new") is not None


def test_drop_forgets_a_run(store):
    store.get_or_create(RUN)
    store.drop(RUN)

    assert store.get(RUN) is None


# --- shown loads --------------------------------------------------------


def test_recording_a_load_makes_it_negotiable(store):
    session = store.get_or_create(RUN)
    session.record_shown_load("LD1")

    assert session.was_shown("LD1")
    assert not session.was_shown("LD2")


def test_recording_the_same_load_twice_is_idempotent(store):
    session = store.get_or_create(RUN)
    session.record_shown_load("LD1")
    session.record_shown_load("LD1")

    assert session.shown_load_ids == ["LD1"]


def test_the_shown_list_is_capped(store):
    session = store.get_or_create(RUN)
    for index in range(MAX_SHOWN_LOADS + 3):
        session.record_shown_load(f"LD{index}")

    assert len(session.shown_load_ids) == MAX_SHOWN_LOADS
    assert "LD0" not in session.shown_load_ids  # the oldest went
    assert f"LD{MAX_SHOWN_LOADS + 2}" in session.shown_load_ids


def test_nothing_is_negotiable_before_a_search(store):
    session = store.get_or_create(RUN)

    assert not session.was_shown("LD1")


# --- negotiation budget -------------------------------------------------


def test_a_fresh_call_can_open_a_negotiation(store):
    assert store.get_or_create(RUN).can_open_negotiation


def test_the_budget_is_two(store):
    from app.sessions import MAX_NEGOTIATIONS_PER_CALL, NegotiationSession

    session = store.get_or_create(RUN)
    assert MAX_NEGOTIATIONS_PER_CALL == 2

    for index in range(MAX_NEGOTIATIONS_PER_CALL):
        session.add_negotiation(
            NegotiationSession(
                negotiation_id=f"neg{index}",
                run_id=RUN,
                load_id=f"LD{index}",
                ceiling=2000,
                posted_rate=1500,
                opening_offer=1500,
                current_offer=1500,
            )
        )

    assert not session.can_open_negotiation


def test_the_active_negotiation_is_the_open_one(store):
    from app.sessions import NegotiationOutcome, NegotiationSession

    session = store.get_or_create(RUN)
    negotiation = NegotiationSession(
        negotiation_id="neg1",
        run_id=RUN,
        load_id="LD1",
        ceiling=2000,
        posted_rate=1500,
        opening_offer=1500,
        current_offer=1500,
    )
    session.add_negotiation(negotiation)

    assert session.active_negotiation is negotiation

    negotiation.outcome = NegotiationOutcome.CLOSED
    assert session.active_negotiation is None


# --- OTP state ----------------------------------------------------------


def test_a_session_is_only_verified_for_its_own_mc(store):
    from datetime import datetime, timezone

    from app.sessions import OtpChallenge

    session = store.get_or_create(RUN)
    session.mc_number = "123456"
    session.otp = OtpChallenge(
        code_hmac="x",
        destination="a@b.co",
        masked_destination="a***@b.co",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
        verified_at=datetime.now(timezone.utc),
    )

    assert session.is_verified_for("123456")
    assert not session.is_verified_for("654321")


def test_a_session_with_no_otp_is_not_verified(store):
    assert not store.get_or_create(RUN).otp_verified


def test_load_not_shown_names_the_load():
    error = LoadNotShown("LD999")

    assert error.load_id == "LD999"
    assert "LD999" in str(error)
