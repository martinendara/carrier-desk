"""Search filter normalisation: the spoken form becomes the TMS identifier."""

from __future__ import annotations

import pytest

from app.api.search_normalization import (
    build_search_filters,
    is_anywhere,
    normalize_equipment,
    normalize_state,
    split_city_state,
)


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("dry van", "DRY_VAN"),
        ("van", "DRY_VAN"),
        ("Dry  Van", "DRY_VAN"),
        ("DRY_VAN", "DRY_VAN"),
        ("reefer", "REEFER"),
        ("refrigerated", "REEFER"),
        ("flatbed", "FLATBED"),
        ("flat bed", "FLATBED"),
        ("step deck", "STEP_DECK"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_equipment(spoken, expected):
    assert normalize_equipment(spoken) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Illinois", "IL"),
        ("illinois", "IL"),
        ("IL", "IL"),
        ("il", "IL"),
        ("New York", "NY"),
        ("new york", "NY"),
        ("", None),
        ("Chicago", None),
        (None, None),
    ],
)
def test_normalize_state(spoken, expected):
    assert normalize_state(spoken) == expected


def test_split_city_state():
    assert split_city_state("Chicago, Illinois") == ("Chicago", "Illinois")
    assert split_city_state("Chicago") == ("Chicago", None)
    assert split_city_state("") == (None, None)
    assert split_city_state(None) == (None, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("anywhere", True), ("Any", True), ("Chicago", False), ("", False)],
)
def test_is_anywhere(value, expected):
    assert is_anywhere(value) == expected


def test_build_filters_normalises_the_spoken_forms():
    filters = build_search_filters(
        equipment="dry van",
        orig_city="Chicago",
        orig_state="Illinois",
        dest_city="anywhere",
    )

    assert filters == {"EQTYPE": "DRY_VAN", "ORIG_CITY": "Chicago", "ORIG_STATE": "IL"}


def test_build_filters_splits_city_and_state_in_the_city_field():
    filters = build_search_filters(
        equipment="van",
        orig_city="Chicago, Illinois",
    )

    assert filters == {"EQTYPE": "DRY_VAN", "ORIG_CITY": "Chicago", "ORIG_STATE": "IL"}


def test_build_filters_empty_is_no_filters():
    assert build_search_filters() == {}
