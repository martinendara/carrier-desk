"""TwinWriter: event id, row mapping, and the background send."""

from __future__ import annotations

import json
import logging

import httpx

from app.events import Event, EventType, InMemoryEventSink
from app.twin import TwinWriter, build_row, event_id


def _writer(backup, transport, **kwargs):
    client = httpx.Client(transport=transport)
    options = dict(
        enabled=True, api_key="test-key", client=client, timeout=1.0, max_attempts=1
    )
    options.update(kwargs)
    return TwinWriter(backup, **options)


# --- id and row mapping --------------------------------------------------


def test_event_id_is_stable():
    event = Event(run_id="r", type=EventType.OTP_SENT, payload={"mc_number": "1"})
    assert event_id(event) == event_id(event)


def test_event_id_differs_per_payload():
    a = Event(run_id="r", type=EventType.SEARCH_PERFORMED, payload={"results": 1})
    b = Event(run_id="r", type=EventType.SEARCH_PERFORMED, payload={"results": 2})
    assert event_id(a) != event_id(b)


def test_build_row_maps_negotiation_fields():
    event = Event(
        run_id="r",
        type=EventType.NEGOTIATION_OPENED,
        payload={"load_id": "LD1", "ceiling": 2500, "opening_offer": 2000},
    )
    row = build_row(event)
    assert row["run_id"] == "r"
    assert row["event_type"] == "negotiation_opened"
    assert row["load_id"] == "LD1"
    assert row["max_buy"] == 2500
    assert row["offer"] == 2000
    assert row["details"] == {"load_id": "LD1", "ceiling": 2500, "opening_offer": 2000}


def test_build_row_computes_the_protected_margin():
    event = Event(
        run_id="r",
        type=EventType.BOOKING_ATTEMPTED,
        payload={"agreed_rate": 2200, "ceiling": 2500},
    )
    assert build_row(event)["margin"] == 300


# --- the writer ----------------------------------------------------------


def test_twin_writer_sends_and_keeps_the_backup():
    backup = InMemoryEventSink()
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "insertedCount": 1})

    writer = _writer(backup, httpx.MockTransport(handler))
    writer.emit("r", EventType.OTP_SENT, mc_number="123456")
    writer.close()

    assert len(backup.events) == 1
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["rows"][0]["event_type"] == "otp_sent"
    assert body["rows"][0]["mc_number"] == "123456"
    assert requests[0].headers["Authorization"] == "Bearer test-key"


def test_twin_writer_failure_keeps_the_backup_and_retries(caplog):
    backup = InMemoryEventSink()
    attempts: list[httpx.Request] = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("boom")

    with caplog.at_level(logging.WARNING, logger="app.twin"):
        writer = _writer(backup, httpx.MockTransport(handler), max_attempts=3)
        writer.emit("r", EventType.OTP_SENT, mc_number="123456")
        writer.close()

    assert len(attempts) == 3  # one attempt plus two retries
    assert len(backup.events) == 1  # the JSONL backup still has it
    assert "JSONL has it" in caplog.text
