"""The event log: shape, sinks, and surviving a disk that says no."""

from __future__ import annotations

from datetime import timezone

import pytest

from app.events import Event, EventType, JsonlEventSink, NullEventSink


def test_an_event_carries_the_run_and_a_utc_timestamp():
    event = Event(run_id="run-1", type=EventType.OTP_SENT)

    assert event.run_id == "run-1"
    assert event.ts.tzinfo is timezone.utc


def test_an_event_serialises_its_payload():
    event = Event(run_id="run-1", type=EventType.SEARCH_PERFORMED, payload={"results": 0})

    assert '"results":0' in event.to_jsonl()


def test_events_are_immutable():
    """A decision that was recorded must not be editable afterwards."""
    event = Event(run_id="run-1", type=EventType.OTP_SENT)

    with pytest.raises(Exception):
        event.run_id = "run-2"


# --- in memory ----------------------------------------------------------


def test_the_memory_sink_keeps_events_in_order(events):
    events.emit("run-1", EventType.FMCSA_CHECKED, eligible=True)
    events.emit("run-1", EventType.SEARCH_PERFORMED, results=0)

    assert [event.type for event in events.events] == [
        EventType.FMCSA_CHECKED,
        EventType.SEARCH_PERFORMED,
    ]


def test_the_memory_sink_filters(events):
    events.emit("run-1", EventType.OTP_SENT)
    events.emit("run-2", EventType.OTP_SENT)
    events.emit("run-1", EventType.OTP_VERIFIED)

    assert len(events.of_type(EventType.OTP_SENT)) == 2
    assert len(events.for_run("run-1")) == 2


def test_a_zero_result_search_is_still_recorded(events):
    """'We looked and there was nothing' is the fact a rep needs most."""
    events.emit("run-1", EventType.SEARCH_PERFORMED, filters={"ORIG_STATE": "GA"}, results=0)

    (event,) = events.of_type(EventType.SEARCH_PERFORMED)
    assert event.payload["results"] == 0


# --- null ---------------------------------------------------------------


def test_the_null_sink_still_returns_an_event():
    event = NullEventSink().emit("run-1", EventType.HANDOFF_CREATED, booking="BR-1")

    assert event.type is EventType.HANDOFF_CREATED
    assert event.payload == {"booking": "BR-1"}


# --- jsonl --------------------------------------------------------------


def test_the_jsonl_sink_writes_one_line_per_event(tmp_path):
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    sink.emit("run-1", EventType.OTP_SENT, destination="a***@b.co")
    sink.emit("run-1", EventType.OTP_VERIFIED)

    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_the_jsonl_sink_reads_back_what_it_wrote(tmp_path):
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    sink.emit("run-1", EventType.NEGOTIATION_OPENED, ceiling=2528)

    (event,) = sink.read_all()
    assert event.type is EventType.NEGOTIATION_OPENED
    assert event.payload["ceiling"] == 2528


def test_the_jsonl_sink_creates_missing_directories(tmp_path):
    sink = JsonlEventSink(tmp_path / "nested" / "deeper" / "events.jsonl")
    sink.emit("run-1", EventType.LEAD_CREATED, temperature="hot")

    assert sink.read_all()[0].payload["temperature"] == "hot"


def test_the_jsonl_sink_appends_across_instances(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit("run-1", EventType.OTP_SENT)
    JsonlEventSink(path).emit("run-2", EventType.OTP_SENT)

    assert len(JsonlEventSink(path).read_all()) == 2


def test_reading_a_file_that_does_not_exist_is_empty(tmp_path):
    assert JsonlEventSink(tmp_path / "nothing.jsonl").read_all() == []


def test_a_failed_write_does_not_break_the_call(tmp_path):
    """Losing an audit line must not drop a carrier who is mid-negotiation."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("i am a file", encoding="utf-8")
    sink = JsonlEventSink(blocker / "events.jsonl")

    event = sink.emit("run-1", EventType.OTP_SENT)

    assert event.type is EventType.OTP_SENT  # returned, not raised
