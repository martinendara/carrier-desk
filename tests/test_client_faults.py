"""Client behaviour against the fake TMS: every fault class, the retry policy,
and the ambiguous-booking path.

Every LOAD_BOOK in this file goes to the local fake server. The live server is
never contacted: each successful booking there permanently consumes a load.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fake_tms import (
    BOOK_ALREADY,
    BOOK_INVALID_RATE,
    DELAYED,
    MALFORMED,
    NORMAL,
    PARTIAL,
    TIMEOUT,
    FakeTms,
)

from app.tms.client import TmsClient
from app.tms.models import BookingStatus
from app.tms.protocol import (
    TmsError,
    TmsInvalidInput,
    TmsMalformed,
    TmsPartialResponse,
    TmsTimeout,
)

TOKEN = "test-token"

#: One load from the recorded fixtures, and its neighbours.
LOAD_ID = "LD00896"
GA_LOAD_ID = "LD00881"


def make_client(tms: FakeTms, **overrides) -> TmsClient:
    """A client tuned for fast, deterministic tests."""
    options = {
        "connect_timeout": 1.0,
        "read_deadline": 0.2,
        "max_attempts": 3,
        "jitter": 0.0,
        "sleep": lambda _seconds: None,
    }
    options.update(overrides)
    return TmsClient("127.0.0.1", tms.port, TOKEN, **options)


# --- Happy path ---------------------------------------------------------


def test_query_returns_the_recorded_loads():
    with FakeTms() as tms:
        loads = make_client(tms).query_loads(ORIG_STATE="GA")

    assert [load.load_id for load in loads] == [GA_LOAD_ID]


def test_query_applies_max_results():
    """Every fixture load is a DRY_VAN, so this filter matches all five."""
    with FakeTms() as tms:
        client = make_client(tms)
        everything = client.query_loads(EQTYPE="DRY_VAN")
        capped = client.query_loads(EQTYPE="DRY_VAN", max_results=2)

    assert len(everything) == 5
    assert len(capped) == 2


def test_query_sends_the_filters_it_was_given():
    with FakeTms() as tms:
        make_client(tms).query_loads(EQTYPE="DRY_VAN", max_results=5)

    assert tms.requests[0]["CMD"] == "LOAD_QUERY"
    assert tms.requests[0]["EQTYPE"] == "DRY_VAN"
    assert tms.requests[0]["MAX_RESULTS"] == "5"


def test_an_unknown_equipment_value_is_an_empty_result_not_an_error():
    with FakeTms() as tms:
        loads = make_client(tms).query_loads(EQTYPE="FLATBED")

    assert loads == []


def test_get_load_returns_the_detail_record():
    with FakeTms() as tms:
        load = make_client(tms).get_load(LOAD_ID)

    assert load.load_id == LOAD_ID
    assert load.max_buy == 2528


def test_the_token_never_lands_in_the_fake_servers_records():
    with FakeTms() as tms:
        make_client(tms).get_load(LOAD_ID)

    assert tms.requests[0]["AUTH"] == "<redacted>"
    assert all(TOKEN not in str(request) for request in tms.requests)


# --- Fault classes ------------------------------------------------------


def test_timeout_raises_after_the_retry_budget():
    with FakeTms(mode=TIMEOUT, hold_seconds=1.0) as tms:
        with pytest.raises(TmsTimeout):
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert len(tms.commands) == 3  # every attempt was made


def test_a_closed_connection_before_end_is_a_partial_response():
    with FakeTms(mode=PARTIAL) as tms:
        with pytest.raises(TmsPartialResponse):
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert len(tms.commands) == 3


def test_partial_records_are_never_returned_as_a_short_result():
    """A truncated search must not look like 'fewer loads available'."""
    with FakeTms(mode=PARTIAL) as tms:
        with pytest.raises(TmsPartialResponse) as caught:
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert "without END" in str(caught.value)


def test_a_framing_violation_is_malformed():
    with FakeTms(mode=MALFORMED) as tms:
        with pytest.raises(TmsMalformed):
            make_client(tms).query_loads(ORIG_STATE="GA")


def test_an_unterminated_oversized_line_is_malformed_not_a_timeout():
    """Without a byte cap this would sit until the deadline and look like a hang."""
    oversized = b"LOAD_ID:LD1|" + b"x" * 8000  # never terminated

    with FakeTms(mode=MALFORMED, raw_response=oversized) as tms:
        with pytest.raises(TmsMalformed):
            make_client(tms).query_loads(ORIG_STATE="GA")


def test_delayed_termination_does_not_burn_the_deadline():
    """The server holds the connection open after a complete response."""
    with FakeTms(mode=DELAYED, hold_seconds=2.0) as tms:
        client = make_client(tms, read_deadline=0.5)
        started = time.monotonic()
        loads = client.query_loads(ORIG_STATE="GA")
        elapsed = time.monotonic() - started

    assert [load.load_id for load in loads] == [GA_LOAD_ID]
    assert elapsed < 0.5


# --- Retry policy -------------------------------------------------------


@pytest.mark.parametrize("code", ["AUTH_FAILED", "MISSING_FIELD", "UNKNOWN_LOAD", "UNKNOWN_CMD"])
def test_rejections_are_never_retried(code):
    with FakeTms(error_code=code) as tms:
        with pytest.raises(TmsError) as caught:
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert caught.value.code == code
    assert len(tms.commands) == 1


def test_server_error_is_retried():
    with FakeTms(error_code="SERVER_ERROR") as tms:
        with pytest.raises(TmsError):
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert len(tms.commands) == 3


def test_an_undocumented_error_code_is_retried():
    """An unknown failure is more likely transient than a request we got wrong."""
    with FakeTms(error_code="SOMETHING_NEW") as tms:
        with pytest.raises(TmsError):
            make_client(tms).query_loads(ORIG_STATE="GA")

    assert len(tms.commands) == 3


def test_an_unknown_load_is_not_retried():
    with FakeTms() as tms:
        with pytest.raises(TmsError) as caught:
            make_client(tms).get_load("LD00000")

    assert caught.value.code == "UNKNOWN_LOAD"
    assert len(tms.commands) == 1


def test_a_recovered_attempt_returns_normally():
    """First attempt dies, second one works: the caller sees a plain result."""
    with FakeTms(script=[TIMEOUT, NORMAL], hold_seconds=0.5) as tms:
        loads = make_client(tms).query_loads(ORIG_STATE="GA")

    assert [load.load_id for load in loads] == [GA_LOAD_ID]
    assert len(tms.commands) == 2


# --- Input validation (nothing reaches the wire) ------------------------


def test_a_query_without_filters_never_reaches_the_wire():
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).query_loads()

    assert tms.commands == []


def test_an_unknown_filter_name_is_rejected_locally():
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).query_loads(ORIG_STATE="GA", COLOUR="red")

    assert tms.commands == []


def test_a_filter_value_that_could_reshape_the_request_is_rejected():
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).query_loads(ORIG_CITY="Atlanta|CMD:LOAD_BOOK")

    assert tms.commands == []


def test_a_malformed_load_id_is_rejected_before_sending():
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).get_load("LD00896|CMD:LOAD_BOOK")

    assert tms.commands == []


def test_a_datetime_filter_is_rendered_as_a_protocol_timestamp():
    with FakeTms() as tms:
        make_client(tms).query_loads(
            PICKUP_DT=datetime(2026, 10, 6, 22, 18, tzinfo=timezone.utc)
        )

    assert tms.requests[0]["PICKUP_DT"] == "20261006221800"


# --- Booking ------------------------------------------------------------


def test_a_successful_booking_carries_the_reference():
    with FakeTms() as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED
    assert result.confirmed
    assert result.booking_ref == "BR-20261001-0001"
    assert result.booked_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert not result.ambiguous


def test_the_booking_sends_the_server_side_rate():
    with FakeTms() as tms:
        make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert tms.requests[0]["AGREED_RATE"] == "2200"
    assert tms.requests[0]["MC_NUM"] == "123456"
    assert tms.requests[0]["LOAD_ID"] == LOAD_ID


def test_an_invalid_rate_is_rejected_and_never_retried():
    with FakeTms(book_result=BOOK_INVALID_RATE) as tms:
        with pytest.raises(TmsError) as caught:
            make_client(tms).book_load(LOAD_ID, "123456", 999999)

    assert caught.value.code == "INVALID_RATE"
    assert len(tms.commands) == 1


def test_an_already_booked_load_is_reported_unconfirmed():
    with FakeTms(book_result=BOOK_ALREADY) as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED_UNCONFIRMED
    assert result.booking_ref is None
    assert not result.confirmed
    assert len(tms.commands) == 1


def test_a_rate_is_validated_before_sending():
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).book_load(LOAD_ID, "123456", 0)

    assert tms.commands == []


@pytest.mark.parametrize("mc_number", ["", "12a456", "123456789", "123-456"])
def test_an_mc_number_is_validated_before_sending(mc_number):
    with FakeTms() as tms:
        with pytest.raises(TmsInvalidInput):
            make_client(tms).book_load(LOAD_ID, mc_number, 2200)

    assert tms.commands == []


def test_a_booking_is_never_blindly_retried_by_the_generic_path():
    """Even if a caller reaches `execute` directly, LOAD_BOOK is not idempotent."""
    with FakeTms(mode=TIMEOUT, hold_seconds=0.5) as tms:
        with pytest.raises(TmsTimeout):
            make_client(tms).execute(
                "LOAD_BOOK", {"LOAD_ID": LOAD_ID, "MC_NUM": "123456", "AGREED_RATE": 2200}
            )

    assert len(tms.commands) == 1


# --- The ambiguous booking ---------------------------------------------


def test_an_ambiguous_booking_that_landed_is_handed_to_a_human():
    """The write landed, the response was lost: LOAD_GET shows the load is gone."""
    with FakeTms(script=[PARTIAL], booking_lands_despite_fault=True) as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED_UNCONFIRMED
    assert result.booking_ref is None
    assert result.ambiguous
    # We asked the server instead of guessing, and did not book twice.
    assert tms.commands == ["LOAD_BOOK", "LOAD_GET"]


def test_an_ambiguous_booking_that_did_not_land_is_retried_once():
    with FakeTms(script=[PARTIAL], booking_lands_despite_fault=False) as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED
    assert result.confirmed
    assert result.ambiguous  # honest: the outcome needed a second look
    assert tms.commands == ["LOAD_BOOK", "LOAD_GET", "LOAD_BOOK"]


def test_a_load_that_left_open_after_a_timeout_is_not_booked_twice():
    with FakeTms(
        script=[TIMEOUT], booking_lands_despite_fault=False, load_status="BOOKED", hold_seconds=0.5
    ) as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED_UNCONFIRMED
    assert tms.commands == ["LOAD_BOOK", "LOAD_GET"]


def test_two_ambiguous_attempts_stop_and_ask_a_human():
    """Never a retry loop: the second ambiguous outcome ends the call."""
    with FakeTms(script=[PARTIAL, NORMAL, PARTIAL], booking_lands_despite_fault=False) as tms:
        result = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert result.status is BookingStatus.BOOKED_UNCONFIRMED
    assert result.booking_ref is None
    assert tms.commands == ["LOAD_BOOK", "LOAD_GET", "LOAD_BOOK"]


def test_an_ambiguous_booking_can_be_retried_by_a_later_call():
    """After ALREADY_BOOKED the load is consumed, and we say so rather than guess."""
    with FakeTms(book_result=BOOK_ALREADY) as tms:
        first = make_client(tms).book_load(LOAD_ID, "123456", 2200)
        second = make_client(tms).book_load(LOAD_ID, "123456", 2200)

    assert first.status is BookingStatus.BOOKED_UNCONFIRMED
    assert second.status is BookingStatus.BOOKED_UNCONFIRMED


def test_a_booking_that_cannot_be_resolved_raises_rather_than_guesses():
    """If LOAD_GET itself is down we have no way to know, so we do not claim anything."""
    with FakeTms(script=[PARTIAL, PARTIAL, PARTIAL, PARTIAL, PARTIAL]) as tms:
        with pytest.raises(TmsPartialResponse):
            make_client(tms).book_load(LOAD_ID, "123456", 2200)
