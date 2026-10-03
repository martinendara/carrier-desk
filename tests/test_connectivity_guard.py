"""The connectivity diagnostic must not be able to book anything either.

`scripts/tms_connectivity.py` speaks to the live server over a raw socket, so it
has no client-level safeguard at all — the guard in the script is the only thing
between a future edit and a consumed load. These tests pin it, and pin the two
things the report must never contain: the token, and a claim it did not observe.
"""

from __future__ import annotations

import pytest
import tms_connectivity

from app.tms.client import LOAD_BOOK, LOAD_GET, LOAD_QUERY


def test_the_read_only_allowlist_covers_exactly_three_commands():
    assert tms_connectivity.READ_ONLY_COMMANDS == {"DEBUG_ECHO", "LOAD_QUERY", "LOAD_GET"}


def test_the_commands_the_script_uses_are_allowed():
    tms_connectivity.guard(LOAD_QUERY)
    tms_connectivity.guard(LOAD_GET)
    tms_connectivity.guard("DEBUG_ECHO")


def test_booking_is_refused():
    with pytest.raises(SystemExit):
        tms_connectivity.guard(LOAD_BOOK)


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        tms_connectivity.guard("SOMETHING_NEW")


def test_the_control_token_is_not_a_credential():
    """The control must never be mistaken for the real token by a later edit."""
    assert "DIAGNOSTIC" in tms_connectivity.CONTROL_TOKEN


def test_mask_hides_the_auth_value_and_leaves_the_rest():
    frame = "CMD:DEBUG_ECHO|AUTH:super-secret-value|MSG:diag"
    masked = tms_connectivity.mask(frame)

    assert "super-secret-value" not in masked
    assert masked == "CMD:DEBUG_ECHO|AUTH:<redacted>|MSG:diag"


def test_the_fingerprint_does_not_reveal_the_value():
    secret = "a-token-that-must-not-appear"
    digest = tms_connectivity.fingerprint(secret)

    assert len(digest) == 12
    assert secret not in digest
    assert tms_connectivity.fingerprint(secret) == digest  # stable across runs
    assert tms_connectivity.fingerprint(secret + "x") != digest


def test_describe_bytes_spots_a_corrupted_env_value():
    assert "23 chars" in tms_connectivity.describe_bytes("x" * 23)
    assert "HAS SURROUNDING WHITESPACE" in tms_connectivity.describe_bytes(" tok ")
    assert "CONTAINS CR/LF" in tms_connectivity.describe_bytes("tok\n")
    assert "NON-ASCII" in tms_connectivity.describe_bytes("toké")
    assert "HAS QUOTES" in tms_connectivity.describe_bytes('"tok"')


def test_a_complete_response_is_recognised_in_both_terminator_styles():
    assert tms_connectivity._is_complete(b"LOAD_ID:LD1|RATE:100\r\nEND")
    assert tms_connectivity._is_complete(b"ERR|CODE:AUTH_FAILED|MSG:no")
    # The server does not always terminate END with CRLF.
    assert tms_connectivity._is_complete(b"LOAD_ID:LD1\r\nEND")


def test_an_incomplete_response_is_not_mistaken_for_a_complete_one():
    assert not tms_connectivity._is_complete(b"LOAD_ID:LD1|RATE:100\r\n")
    assert not tms_connectivity._is_complete(b"")


def test_split_lines_does_not_invent_a_trailing_empty_line():
    assert tms_connectivity._split_lines(b"a\r\nb\r\n") == [b"a", b"b"]
    assert tms_connectivity._split_lines(b"") == []


def test_the_error_code_is_read_by_name_not_position():
    """The frame is `ERR|CODE:X|MSG:...`, but position is not what makes it work."""
    assert tms_connectivity._error_code(b"ERR|CODE:AUTH_FAILED|MSG:nope") == "AUTH_FAILED"
    assert tms_connectivity._error_code(b"ERR|MSG:nope|CODE:MALFORMED") == "MALFORMED"
    assert tms_connectivity._error_code(b"ERR|MSG:nope") == "?"


def test_an_attempt_with_no_connect_reports_the_connect_error():
    attempt = tms_connectivity.Attempt(
        label="x", command="DEBUG_ECHO",
        started=tms_connectivity.now(), request_display="CMD:DEBUG_ECHO",
        connect_error="CONNECTION_REFUSED (Connection refused)",
    )
    assert attempt.status == "CONNECTION_REFUSED (Connection refused)"


def test_an_attempt_that_connected_and_went_quiet_is_a_read_timeout():
    attempt = tms_connectivity.Attempt(
        label="x", command="DEBUG_ECHO",
        started=tms_connectivity.now(), request_display="CMD:DEBUG_ECHO",
        connect_ms=12.0, timed_out=True,
    )
    assert attempt.status == "READ_TIMEOUT (connected, no bytes)"


def test_bytes_without_a_terminator_are_a_partial_response_not_a_timeout():
    attempt = tms_connectivity.Attempt(
        label="x", command="LOAD_QUERY",
        started=tms_connectivity.now(), request_display="CMD:LOAD_QUERY",
        connect_ms=12.0, timed_out=True,
        lines=[(10.0, b"LOAD_ID:LD1|RATE:100")],
    )
    assert attempt.status.startswith("PARTIAL_THEN_TIMEOUT")
