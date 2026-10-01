"""Lead qualification: the rules, in the documented order."""

from __future__ import annotations

import pytest

from app.onboarding import (
    HOT_MATCH_THRESHOLD,
    MIN_AUTHORITY_AGE_DAYS,
    LeadTemperature,
    qualify,
)


@pytest.mark.parametrize(
    ("age", "matches", "expected"),
    [
        # The Review short-circuit.
        (None, 10, LeadTemperature.REVIEW),  # unknown authority age
        (89, 10, LeadTemperature.REVIEW),  # one day too new
        (0, 10, LeadTemperature.REVIEW),  # granted today
        (365, 0, LeadTemperature.REVIEW),  # nothing matched
        # The thresholds.
        (90, 0, LeadTemperature.REVIEW),  # exactly at the age, but no matches
        (90, 1, LeadTemperature.WARM),
        (90, 2, LeadTemperature.WARM),
        (90, 3, LeadTemperature.HOT),
        (90, 40, LeadTemperature.HOT),
    ],
)
def test_the_decision_table(age, matches, expected):
    assert qualify(authority_age_days=age, matching_loads=matches).temperature is expected


def test_a_new_authority_with_many_matches_is_still_reviewed():
    """Age is checked first, so a flood of matches cannot upgrade a new carrier."""
    result = qualify(authority_age_days=10, matching_loads=50)

    assert result.temperature is LeadTemperature.REVIEW
    assert result.reason == "authority_too_new"


def test_an_unknown_age_is_never_treated_as_old_enough():
    result = qualify(authority_age_days=None, matching_loads=50)

    assert result.temperature is LeadTemperature.REVIEW
    assert result.reason == "authority_age_unknown"


def test_no_matches_outranks_the_warm_band():
    """One or two matches is Warm, but zero is Review — not Warm."""
    result = qualify(authority_age_days=400, matching_loads=0)

    assert result.temperature is LeadTemperature.REVIEW
    assert result.reason == "no_matching_loads"


def test_the_boundaries_are_inclusive_as_documented():
    assert qualify(authority_age_days=MIN_AUTHORITY_AGE_DAYS, matching_loads=1).temperature is (
        LeadTemperature.WARM
    )
    assert qualify(authority_age_days=MIN_AUTHORITY_AGE_DAYS - 1, matching_loads=5).temperature is (
        LeadTemperature.REVIEW
    )
    assert qualify(
        authority_age_days=200, matching_loads=HOT_MATCH_THRESHOLD
    ).temperature is LeadTemperature.HOT


def test_a_negative_match_count_is_clamped():
    """Defensive: a bad count from upstream becomes Review, not a crash."""
    result = qualify(authority_age_days=400, matching_loads=-3)

    assert result.temperature is LeadTemperature.REVIEW
    assert result.matching_loads == 0


def test_the_verdict_carries_its_reason_for_the_event_log():
    result = qualify(authority_age_days=200, matching_loads=5)

    assert result.reason == "strong_match"
    assert result.authority_age_days == 200
    assert result.matching_loads == 5


def test_qualification_is_deterministic():
    """No LLM: the same inputs give the same answer, every time."""
    answers = {qualify(authority_age_days=120, matching_loads=2).temperature for _ in range(50)}

    assert answers == {LeadTemperature.WARM}
