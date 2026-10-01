"""Where events go.

Two implementations for now: memory (tests, and a quick look during a demo) and
JSONL on disk (survives the process). The Twin adapter is deliberately absent —
its authentication is not confirmed yet, and guessing at it would bake a wrong
shape into every call site. The interface is the seam it will plug into.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Protocol

from app.events.models import Event, EventType

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    """Somewhere an :class:`Event` can be written."""

    def emit(self, run_id: str, event_type: EventType, **payload: Any) -> Event:
        """Record one event and return it."""
        ...


class NullEventSink:
    """Discards everything. Useful as a default so nothing has to be optional."""

    def emit(self, run_id: str, event_type: EventType, **payload: Any) -> Event:
        return Event(run_id=run_id, type=event_type, payload=payload)


class InMemoryEventSink:
    """Keeps events in a list. For tests, and for reading back during a demo."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, run_id: str, event_type: EventType, **payload: Any) -> Event:
        event = Event(run_id=run_id, type=event_type, payload=payload)
        self.events.append(event)
        return event

    def of_type(self, event_type: EventType) -> list[Event]:
        return [event for event in self.events if event.type is event_type]

    def for_run(self, run_id: str) -> list[Event]:
        return [event for event in self.events if event.run_id == run_id]


class JsonlEventSink:
    """Appends one JSON object per line.

    Writing is serialised with a lock and the file is opened per event: the
    volume here is a handful of lines per call, so an always-open handle would
    buy nothing and cost a leaked descriptor whenever a worker is recycled.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, run_id: str, event_type: EventType, **payload: Any) -> Event:
        event = Event(run_id=run_id, type=event_type, payload=payload)
        self._write(event)
        return event

    def _write(self, event: Event) -> None:
        line = event.to_jsonl()
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            # Losing an audit line must not fail the call the carrier is on.
            # It is logged loudly instead: silent loss would be worse than
            # noise, because the event log is how we explain a decision later.
            logger.exception("could not write event to %s", self.path)

    def read_all(self) -> list[Event]:
        """Parse the file back. Used by tests and by the demo report."""
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(Event(**json.loads(line)))
        return events
