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
    assert row["max_buy"] == "2500"  # Twin's schema is all-text
    assert row["offer"] == "2000"
    assert json.loads(row["details"]) == {
        "load_id": "LD1",
        "ceiling": 2500,
        "opening_offer": 2000,
    }


def test_build_row_computes_the_protected_margin():
    event = Event(
        run_id="r",
        type=EventType.BOOKING_ATTEMPTED,
        payload={"agreed_rate": 2200, "ceiling": 2500},
    )
    assert build_row(event)["margin"] == "300"


def test_build_row_omits_nulls_and_stringifies():
    event = Event(
        run_id="r",
        type=EventType.NEGOTIATION_ROUND,
        payload={
            "load_id": "LD1",
            "round": 2,
            "action": "counter",
            "carrier_ask": 2300,
            "offer": 2225,
            "ceiling": 2528,
        },
    )
    row = build_row(event)
    assert row["round"] == "2"
    assert row["carrier_ask"] == "2300"
    assert row["offer"] == "2225"
    assert row["max_buy"] == "2528"
    assert row["action"] == "counter"
    assert all(value is not None for value in row.values())
    assert isinstance(row["details"], str)
    assert "reason" not in row  # null columns are omitted, not sent as null


# --- the writer ----------------------------------------------------------


def test_twin_writer_sends_and_keeps_the_backup():
    backup = InMemoryEventSink()
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"success": True})

    writer = _writer(backup, httpx.MockTransport(handler))
    writer.emit("r", EventType.OTP_SENT, mc_number="123456")
    writer.close()

    assert len(backup.events) == 1
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/twin/tables/call_events/rows")
    body = json.loads(requests[0].content)
    assert set(body) == {"values"}
    assert body["values"]["event_type"] == "otp_sent"
    assert body["values"]["mc_number"] == "123456"
    assert requests[0].headers["Authorization"] == "Bearer test-key"


def test_twin_writer_strips_a_pasted_key():
    backup = InMemoryEventSink()
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"success": True})

    writer = _writer(backup, httpx.MockTransport(handler), api_key='  "test-key"\n')
    writer.emit("r", EventType.OTP_SENT, mc_number="1")
    writer.close()

    assert requests[0].headers["Authorization"] == "Bearer test-key"


def test_twin_writer_treats_a_duplicate_as_success():
    backup = InMemoryEventSink()
    attempts: list[httpx.Request] = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(409, json={"error": "duplicate key value violates unique constraint"})

    writer = _writer(backup, httpx.MockTransport(handler), max_attempts=3)
    writer.emit("r", EventType.OTP_SENT, mc_number="1")
    writer.close()

    assert len(attempts) == 1  # a duplicate is success, not a retry
    assert len(backup.events) == 1


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
