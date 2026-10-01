"""Result codes: a closed set, so a typo cannot silently never match."""

from __future__ import annotations

import pytest

from app.results import SIMPLE_CODES, ResultCode, fmcsa_rejected, is_valid_result_code


def test_the_closed_set_is_the_documented_one():
    assert SIMPLE_CODES == {
        "booked",
        "booking_pending",
        "negotiation_failed",
        "no_match",
        "otp_failed",
        "routed_onboarding",
    }


@pytest.mark.parametrize("code", sorted(SIMPLE_CODES))
def test_every_member_validates(code):
    assert is_valid_result_code(code)


@pytest.mark.parametrize("code", sorted(SIMPLE_CODES))
def test_members_serialise_as_plain_strings(code):
    assert str(ResultCode(code)) == code


def test_a_parameterised_rejection_is_valid():
    assert is_valid_result_code("fmcsa_rejected:out_of_service")


@pytest.mark.parametrize(
    "code",
    ["booked_twice", "", "BOOKED", "fmcsa_rejected", "fmcsa_rejected:", "book"],
)
def test_near_misses_are_rejected(code):
    assert not is_valid_result_code(code)


def test_the_rejection_code_carries_its_reason():
    assert fmcsa_rejected("out_of_service") == "fmcsa_rejected:out_of_service"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Out Of Service", "fmcsa_rejected:outofservice"),
        ("weird|value", "fmcsa_rejected:weirdvalue"),
        ("", "fmcsa_rejected:unknown"),
        ("   ", "fmcsa_rejected:unknown"),
        ("café", "fmcsa_rejected:caf"),
    ],
)
def test_the_reason_is_sanitised(raw, expected):
    """It can end up in a string an agent reads out loud."""
    assert fmcsa_rejected(raw) == expected
    assert is_valid_result_code(fmcsa_rejected(raw))
