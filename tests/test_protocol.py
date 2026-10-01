"""Wire-format tests for the encoder and the parser.

These never open a socket: the protocol layer is pure, so the whole format can
be pinned down here, including both padding styles the live server and the
manual disagree about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fake_tms import load_fixture_blocks

from app.tms.models import Load, LoadDetail
from app.tms.protocol import (
    MAX_FRAME_BYTES,
    TmsError,
    TmsInvalidInput,
    TmsMalformed,
    TmsPartialResponse,
    encode_request,
    ends_response,
    format_timestamp,
    parse_error_line,
    parse_record,
    parse_response,
    parse_timestamp,
    redact,
    to_int,
)

# --- Both observed wire formats -----------------------------------------

#: The live server: short ids, values padded on the right with spaces.
LIVE_LINE = (
    "LOAD_ID:LD00896     |ORIG_CITY:Fort Lauderdale               |ORIG_STATE:FL"
    "|ORIG_ZIP:33301|DEST_CITY:Pittsburgh                    |DEST_STATE:PA"
    "|DEST_ZIP:15219|PICKUP_DT:20261006221800|EQTYPE:DRY_VAN   |RATE:2073    "
    "|MILES:989   |STATUS:OPEN    "
)

#: The manual: long zero-padded ids and zero-padded numbers, no padding.
MANUAL_LINE = (
    "LOAD_ID:LD0000045821|ORIG_CITY:Springfield|ORIG_STATE:IL|ORIG_ZIP:62704"
    "|DEST_CITY:Denver|DEST_STATE:CO|DEST_ZIP:80202|PICKUP_DT:20261005080000"
    "|EQTYPE:FLATBED|RATE:0002150|MILES:1004|STATUS:OPEN"
)


# --- Encoding -----------------------------------------------------------


def test_encode_puts_cmd_first_and_auth_second():
    frame = encode_request("LOAD_GET", "s3cret", {"LOAD_ID": "LD00896"})
    assert frame == b"CMD:LOAD_GET|AUTH:s3cret|LOAD_ID:LD00896\r\n"


def test_encode_preserves_caller_field_order():
    frame = encode_request("LOAD_QUERY", "t", {"EQTYPE": "DRY_VAN", "MAX_RESULTS": 5})
    assert frame == b"CMD:LOAD_QUERY|AUTH:t|EQTYPE:DRY_VAN|MAX_RESULTS:5\r\n"


def test_encode_trims_values():
    frame = encode_request("LOAD_GET", " t ", {"LOAD_ID": "  LD00896  "})
    assert frame == b"CMD:LOAD_GET|AUTH:t|LOAD_ID:LD00896\r\n"


def test_encode_uppercases_field_names():
    frame = encode_request("LOAD_GET", "t", {"load_id": "LD00896"})
    assert b"LOAD_ID:LD00896" in frame


def test_encode_is_ascii_only():
    frame = encode_request("LOAD_GET", "t", {"LOAD_ID": "LD00896"})
    assert frame.decode("ascii")  # would raise on a stray non-ASCII byte


@pytest.mark.parametrize(
    "value",
    [
        "LD1|AUTH:stolen",  # a second frame smuggled into the first
        "LD1\r\nCMD:LOAD_BOOK",  # a whole extra request
        "LD1\rCMD:X",
        "LD1\nCMD:X",
        "Café",  # non-ASCII
    ],
)
def test_encode_rejects_values_that_could_reshape_the_request(value):
    with pytest.raises(TmsInvalidInput):
        encode_request("LOAD_GET", "t", {"LOAD_ID": value})


def test_encode_rejects_a_bad_command_name():
    with pytest.raises(TmsInvalidInput):
        encode_request("LOAD_GET|AUTH:x", "t")


def test_encode_rejects_an_empty_token():
    with pytest.raises(TmsInvalidInput):
        encode_request("LOAD_GET", "")


def test_encode_rejects_an_oversized_frame():
    with pytest.raises(TmsInvalidInput):
        encode_request("DEBUG_ECHO", "t", {"MSG": "x" * MAX_FRAME_BYTES})


def test_encode_error_never_echoes_the_value():
    """A rejected value may be the auth token; tracebacks must not leak it."""
    with pytest.raises(TmsInvalidInput) as caught:
        encode_request("LOAD_GET", "top-secret-token|")

    assert "top-secret-token" not in str(caught.value)


def test_redact_hides_the_token_in_a_frame():
    frame = encode_request("LOAD_GET", "s3cret", {"LOAD_ID": "LD00896"})
    redacted = redact(frame.decode("ascii"))

    assert "s3cret" not in redacted
    assert "AUTH:<redacted>" in redacted
    assert "LOAD_ID:LD00896" in redacted  # the rest stays readable


# --- Parsing records ----------------------------------------------------


def test_parse_live_format_trims_padding():
    record = parse_record(LIVE_LINE)

    assert record["LOAD_ID"] == "LD00896"
    assert record["ORIG_CITY"] == "Fort Lauderdale"
    assert record["EQTYPE"] == "DRY_VAN"
    assert record["STATUS"] == "OPEN"


def test_parse_manual_format_keeps_the_long_id():
    record = parse_record(MANUAL_LINE)

    assert record["LOAD_ID"] == "LD0000045821"
    assert record["EQTYPE"] == "FLATBED"


def test_both_formats_yield_the_same_values():
    """The parser is padding-agnostic: same load, two styles, same numbers."""
    live = parse_record(LIVE_LINE)
    manual = parse_record(MANUAL_LINE)

    assert to_int("RATE", live["RATE"]) == 2073
    assert to_int("RATE", manual["RATE"]) == 2150
    assert to_int("MILES", manual["MILES"]) == 1004
    assert live["ORIG_STATE"] == live["ORIG_STATE"].strip()


def test_parse_record_splits_on_the_first_colon_only():
    record = parse_record("NOTES:Call dispatch: ask for Ray|STATUS:OPEN")

    assert record["NOTES"] == "Call dispatch: ask for Ray"
    assert record["STATUS"] == "OPEN"


def test_parse_record_keeps_unknown_fields():
    record = parse_record("LOAD_ID:LD1|NEW_COLUMN:surprise")

    assert record["NEW_COLUMN"] == "surprise"


def test_parse_record_normalises_key_case():
    assert parse_record("load_id:LD1")["LOAD_ID"] == "LD1"


def test_parse_record_blank_notes_is_an_empty_string():
    record = parse_record("NOTES:" + " " * 40 + "|STATUS:OPEN")

    assert record["NOTES"] == ""


@pytest.mark.parametrize(
    "line",
    [
        "LOAD_ID:LD1|BROKEN",  # token without a colon
        "LOAD_ID:LD1||STATUS:OPEN",  # empty token
        "LOAD_ID:LD1|:orphan",  # empty field name
    ],
)
def test_parse_record_rejects_framing_violations(line):
    with pytest.raises(TmsMalformed):
        parse_record(line)


# --- Parsing responses --------------------------------------------------


def test_parse_response_reads_records_until_end():
    records = parse_response([LIVE_LINE, LIVE_LINE, "END"])

    assert len(records) == 2
    assert records[0]["LOAD_ID"] == "LD00896"


def test_parse_response_ignores_blank_lines():
    records = parse_response(["", LIVE_LINE, "   ", "END", ""])

    assert len(records) == 1


def test_parse_response_with_no_records_is_an_empty_list():
    """A search that legitimately matched nothing is not an error."""
    assert parse_response(["END"]) == []


def test_parse_response_without_end_is_a_failure():
    """Rule 4: a truncated search must never look like 'fewer loads'."""
    with pytest.raises(TmsPartialResponse):
        parse_response([LIVE_LINE, LIVE_LINE])


def test_parse_response_raises_on_error_line():
    with pytest.raises(TmsError) as caught:
        parse_response(["ERR|CODE:UNKNOWN_LOAD|MSG:load not found"])

    assert caught.value.code == "UNKNOWN_LOAD"
    assert caught.value.message == "load not found"


def test_parse_error_line_reads_the_code_and_message():
    assert parse_error_line("ERR|CODE:MISSING_FIELD|MSG:At least one filter required") == (
        "MISSING_FIELD",
        "At least one filter required",
    )


def test_parse_error_line_tolerates_a_missing_message():
    assert parse_error_line("ERR|CODE:SERVER_ERROR") == ("SERVER_ERROR", "")


def test_parse_error_line_rejects_a_line_without_a_code():
    with pytest.raises(TmsMalformed):
        parse_error_line("ERR|MSG:no code here")


def test_ends_response_recognises_both_terminators():
    assert ends_response("END")
    assert ends_response("  END  ")
    assert ends_response("ERR|CODE:SERVER_ERROR|MSG:x")
    assert not ends_response(LIVE_LINE)


# --- Values -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2073    ", 2073),  # live: space padded
        ("0002150", 2150),  # manual: zero padded
        ("  498 ", 498),
        ("0", 0),
    ],
)
def test_to_int_accepts_both_padding_styles(raw, expected):
    assert to_int("RATE", raw) == expected


def test_to_int_rejects_a_non_number():
    with pytest.raises(TmsMalformed):
        to_int("RATE", "N/A")


def test_timestamps_are_utc_and_round_trip():
    moment = parse_timestamp("PICKUP_DT", "20261006221800")

    assert moment == datetime(2026, 10, 6, 22, 18, tzinfo=timezone.utc)
    assert moment.tzinfo is timezone.utc
    assert format_timestamp(moment) == "20261006221800"


def test_format_timestamp_converts_other_zones_to_utc():
    from datetime import timedelta

    local = datetime(2026, 10, 6, 19, 18, tzinfo=timezone(timedelta(hours=-3)))

    assert format_timestamp(local) == "20261006221800"


def test_parse_timestamp_rejects_a_bad_shape():
    with pytest.raises(TmsMalformed):
        parse_timestamp("PICKUP_DT", "2026-10-06T22:18:00")


# --- The recorded fixtures ---------------------------------------------


def test_the_recorded_query_fixture_parses():
    lines = load_fixture_blocks()[0]
    records = parse_response([*lines, "END"])
    loads = [Load.from_record(record) for record in records]

    assert len(loads) == 5
    assert {load.load_id for load in loads} == {
        "LD00859",
        "LD00873",
        "LD00880",
        "LD00881",
        "LD00896",
    }
    assert all(load.is_open for load in loads)


def test_the_recorded_detail_fixture_parses_including_max_buy():
    detail_line = next(
        line for block in load_fixture_blocks() for line in block if "MAX_BUY:" in line
    )
    load = LoadDetail.from_record(parse_record(detail_line))

    assert load.load_id == "LD00896"
    assert load.rate == 2073
    assert load.max_buy == 2528
    assert load.notes.startswith("No paperwork at pickup")
    assert load.commodity == "Lumber"
    assert load.pieces == 30


def test_fixture_file_is_ascii():
    """The protocol is ASCII; a fixture with a stray accent would lie to us."""
    path = Path(__file__).parent / "fixtures" / "tms_samples.txt"
    path.read_bytes().decode("ascii")
