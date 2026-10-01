"""Tests for the typed views over TMS records."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.tms.models import BookResult, BookingStatus, Load, LoadDetail
from app.tms.protocol import TmsMalformed, parse_record

from test_protocol import LIVE_LINE, MANUAL_LINE


def test_load_from_the_live_record():
    load = Load.from_record(parse_record(LIVE_LINE))

    assert load.load_id == "LD00896"
    assert load.orig_city == "Fort Lauderdale"
    assert load.orig_state == "FL"
    assert load.dest_city == "Pittsburgh"
    assert load.eqtype == "DRY_VAN"
    assert load.rate == 2073
    assert load.miles == 989
    assert load.status == "OPEN"
    assert load.pickup_dt == datetime(2026, 10, 6, 22, 18, tzinfo=timezone.utc)


def test_load_from_the_manual_record():
    load = Load.from_record(parse_record(MANUAL_LINE))

    assert load.load_id == "LD0000045821"
    assert load.rate == 2150
    assert load.miles == 1004


@pytest.mark.parametrize(
    "field",
    ["LOAD_ID", "ORIG_CITY", "ORIG_STATE", "DEST_CITY", "DEST_STATE", "PICKUP_DT", "EQTYPE", "RATE", "STATUS"],
)
def test_a_missing_required_field_is_malformed(field):
    """A load without a rate cannot be pitched; defaulting it would invent a number."""
    record = parse_record(LIVE_LINE)
    del record[field]

    with pytest.raises(TmsMalformed):
        Load.from_record(record)


def test_a_blank_required_field_is_malformed():
    record = parse_record(LIVE_LINE)
    record["RATE"] = "   "

    with pytest.raises(TmsMalformed):
        Load.from_record(record)


def test_an_unparseable_number_is_malformed():
    record = parse_record(LIVE_LINE)
    record["RATE"] = "N/A"

    with pytest.raises(TmsMalformed):
        Load.from_record(record)


def test_optional_fields_may_be_absent():
    record = parse_record(LIVE_LINE)
    del record["MILES"]
    del record["ORIG_ZIP"]

    load = Load.from_record(record)

    assert load.miles is None
    assert load.orig_zip is None


def test_zip_codes_stay_strings_so_leading_zeros_survive():
    record = parse_record(LIVE_LINE)
    record["ORIG_ZIP"] = "00123"

    assert Load.from_record(record).orig_zip == "00123"


def test_unknown_fields_are_kept():
    record = parse_record(LIVE_LINE)
    record["TRAILER_TAG"] = "T-991"

    load = Load.from_record(record)

    assert load.extra == {"TRAILER_TAG": "T-991"}
    assert load.public_dict()["TRAILER_TAG"] == "T-991"


def test_is_open_tracks_the_status_field():
    record = parse_record(LIVE_LINE)

    assert Load.from_record(record).is_open

    record["STATUS"] = "BOOKED"
    assert not Load.from_record(record).is_open


# --- The ceiling stays server-side --------------------------------------


def _detail() -> LoadDetail:
    record = parse_record(LIVE_LINE)
    record.update(
        {
            "DELIVERY_DT": "20261009101800",
            "WEIGHT": "24137",
            "COMMODITY": "Lumber",
            "PIECES": "30",
            "DIMS": "50ft x 8ft x 9ft",
            "NOTES": "No paperwork at pickup.",
            "MAX_BUY": "2528",
        }
    )
    return LoadDetail.from_record(record)


def test_detail_carries_max_buy_for_the_negotiation_engine():
    assert _detail().max_buy == 2528


def test_public_dict_never_exposes_max_buy():
    """CLAUDE.md principle 1: the agent cannot leak what it never receives."""
    public = _detail().public_dict()

    assert "max_buy" not in public
    assert "MAX_BUY" not in public
    assert 2528 not in public.values()


def test_public_dict_is_json_ready():
    public = _detail().public_dict()

    assert public["load_id"] == "LD00896"
    assert public["pickup_dt"] == "2026-10-06T22:18:00Z"
    assert public["delivery_dt"] == "2026-10-09T10:18:00Z"
    assert public["weight"] == 24137
    assert public["notes"] == "No paperwork at pickup."


def test_public_dict_does_not_expose_the_internal_extra_bucket():
    assert "extra" not in _detail().public_dict()


def test_detail_blank_notes_become_an_empty_string():
    record = parse_record(LIVE_LINE)
    record["NOTES"] = " " * 60

    assert LoadDetail.from_record(record).notes == ""


# --- Booking results ----------------------------------------------------


def test_a_confirmed_booking_needs_a_reference():
    result = BookResult(
        load_id="LD00896", status=BookingStatus.BOOKED, booking_ref="BR-1"
    )

    assert result.confirmed


def test_an_unconfirmed_booking_is_never_confirmed():
    result = BookResult(
        load_id="LD00896",
        status=BookingStatus.BOOKED_UNCONFIRMED,
        booking_ref=None,
        ambiguous=True,
    )

    assert not result.confirmed
    assert result.ambiguous
