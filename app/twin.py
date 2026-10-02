"""Stream call events to HappyRobot Twin in the background.

The service writes events to JSONL (the durable backup) and, when enabled,
forwards them to Twin. Twin is never allowed to block or break the call: each
event is queued and sent from a daemon thread with a short timeout and two
retries, and a send that keeps failing is logged loudly while the JSONL keeps
the record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from typing import Any

import httpx

from app.events.models import Event, EventType

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://platform.happyrobot.ai/api/v2"
DEFAULT_TABLE = "call_events"


def _clean_key(api_key: str) -> str:
    """Strip whitespace and surrounding quotes from a pasted key.

    A key copied from a terminal or a config file can arrive with a trailing
    newline, surrounding spaces, or accidental single/double quotes — all of
    which make the Bearer header wrong.
    """
    return api_key.strip().strip("'\"")


def _is_duplicate(response: httpx.Response) -> bool:
    """Whether a non-2xx is really "this id already exists" rather than a fault."""
    if response.status_code >= 500:
        return False
    try:
        text = response.text.lower()
    except Exception:
        return False
    return any(token in text for token in ("duplicate", "already exist", "unique"))


def event_id(event: Event) -> str:
    """A stable id, so retries and backfill do not insert duplicates.

    Derived from the event content rather than random, so the same JSONL line
    always maps to the same Twin row.
    """
    raw = json.dumps(
        [event.run_id, event.type.value, event.ts.isoformat(), event.payload],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_row(event: Event) -> dict[str, Any]:
    """Map one event to one Twin row. The full payload lives in ``details``."""
    p = event.payload
    row: dict[str, Any] = {
        "id": event_id(event),
        "run_id": event.run_id,
        "event_type": event.type.value,
        "occurred_at": event.ts.isoformat(),
        "mc_number": p.get("mc_number"),
        "load_id": p.get("load_id"),
        "offer": p.get("opening_offer", p.get("offer")),
        "carrier_ask": p.get("carrier_ask"),
        "round": p.get("round"),
        "action": p.get("action", p.get("outcome", p.get("status"))),
        "result_code": p.get("result_code"),
        "agreed_rate": p.get("agreed_rate"),
        "max_buy": p.get("ceiling", p.get("max_buy")),
        "reason": p.get("reason"),
        "details": p,
    }
    if row["max_buy"] is not None and row["agreed_rate"] is not None:
        row["margin"] = row["max_buy"] - row["agreed_rate"]
    else:
        row["margin"] = None
    return row


class TwinWriter:
    """Fans events out: JSONL backup (always) plus Twin (background, if enabled)."""

    def __init__(
        self,
        backup,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        table: str = DEFAULT_TABLE,
        enabled: bool = False,
        timeout: float = 3.0,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self._backup = backup
        self._url = f"{base_url.rstrip('/')}/twin/tables/{table}/rows"
        self._api_key = _clean_key(api_key)
        self._enabled = enabled
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client = client
        self._queue: queue.Queue[Event] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if enabled:
            self._start()

    # --- EventSink -------------------------------------------------------

    def emit(self, run_id: str, event_type: EventType, **payload: Any) -> Event:
        event = self._backup.emit(run_id, event_type, **payload)
        if self._enabled:
            self._queue.put(event)
        return event

    # --- lifecycle -------------------------------------------------------

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="twin-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._send(event)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def flush(self) -> None:
        """Drain the queue synchronously. Used by tests."""
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self._send(event)

    # --- sending ---------------------------------------------------------

    def _send(self, event: Event) -> None:
        row = build_row(event)
        for attempt in range(self._max_attempts):
            try:
                inserted = self._post(row)
                if not inserted:
                    logger.info("twin row %s already present; skipping", row["id"])
                return
            except httpx.HTTPStatusError as exc:
                # A 4xx other than a duplicate (409 is handled in `_post`) is
                # not worth retrying; a 5xx is.
                if exc.response.status_code < 500:
                    self._log_failure(row, exc)
                    return
                if attempt == self._max_attempts - 1:
                    self._log_failure(row, exc)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts - 1:
                    self._log_failure(row, exc)
            if attempt < self._max_attempts - 1:
                time.sleep(0.2 * (2**attempt))

    def _post(self, row: dict[str, Any]) -> bool:
        """Insert one row. True if inserted, False if the id already exists."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {"values": row}
        if self._client is not None:
            response = self._client.post(
                self._url, json=body, headers=headers, timeout=self._timeout
            )
        else:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(self._url, json=body, headers=headers)
        if response.status_code == 409 or _is_duplicate(response):
            return False
        response.raise_for_status()
        return True

    def _log_failure(self, row: dict[str, Any], exc: Exception) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "twin upload failed for %s after %d attempts: %s: %s (url=%s status=%s); JSONL has it",
            row["id"],
            self._max_attempts,
            type(exc).__name__,
            exc,
            self._url,
            status,
        )
