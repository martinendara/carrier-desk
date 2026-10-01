"""The smoke script must not be able to book anything, even by accident.

These tests are about a safety property rather than behaviour: the script runs
against the live server, where one successful LOAD_BOOK permanently consumes a
load. A future edit that adds a write command should fail here.
"""

from __future__ import annotations

import pytest
import tms_smoke

from app.tms.client import LOAD_BOOK, LOAD_GET, LOAD_QUERY


def test_the_read_only_allowlist_covers_exactly_three_commands():
    assert tms_smoke.READ_ONLY_COMMANDS == {"DEBUG_ECHO", "LOAD_QUERY", "LOAD_GET"}


def test_the_commands_the_script_uses_are_allowed():
    tms_smoke.guard(LOAD_QUERY)
    tms_smoke.guard(LOAD_GET)
    tms_smoke.guard("DEBUG_ECHO")


def test_booking_is_refused():
    with pytest.raises(SystemExit):
        tms_smoke.guard(LOAD_BOOK)


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        tms_smoke.guard("SOMETHING_NEW")


def test_the_probe_helper_refuses_to_send_a_booking():
    """Even if a probe were written for it, the guard sits in the helper."""
    with pytest.raises(SystemExit):
        tms_smoke.probe(
            client=None,  # never reached: the guard runs first
            reporter=tms_smoke.Reporter(),
            index=99,
            label="should never run",
            command=LOAD_BOOK,
            fields={"LOAD_ID": "LD00896"},
        )


def test_the_reporter_writes_what_it_printed(tmp_path):
    reporter = tms_smoke.Reporter()
    reporter.emit("hello")

    target = tmp_path / "report.txt"
    reporter.save(target)

    assert target.read_text(encoding="utf-8") == "hello\n"
