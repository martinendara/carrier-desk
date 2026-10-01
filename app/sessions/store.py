"""In-memory store of run sessions, with a TTL.

Single instance on purpose for the POC: one process, one dictionary. That is a
real constraint, not a shortcut — a second worker would not see the first one's
sessions, so a call that landed on a different pod would look like it had never
started. The production path is Redis behind this same interface; see
DECISIONS.md.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.sessions.errors import SessionNotFound
from app.sessions.models import RunSession

#: A call is minutes long. An hour is generous enough that nothing ages out
#: mid-conversation, and short enough that abandoned sessions do not pile up.
DEFAULT_TTL_SECONDS = 3600

#: run_id comes from the platform, so it is validated rather than trusted: it
#: ends up as a dictionary key and in every event payload.
MAX_RUN_ID_LENGTH = 128


def validate_run_id(run_id: str) -> str:
    """Normalise and validate a platform-supplied run id."""
    text = str(run_id).strip()
    if not text:
        raise ValueError("run_id is required")
    if len(text) > MAX_RUN_ID_LENGTH:
        raise ValueError(f"run_id is longer than {MAX_RUN_ID_LENGTH} characters")
    if not text.isascii() or not text.isprintable():
        raise ValueError("run_id must be printable ASCII")
    return text


@dataclass
class _Entry:
    session: RunSession
    expires_at: float


class RunSessionStore:
    """Keeps one :class:`RunSession` per `run_id`.

    Expiry is lazy: an entry is checked when it is read, so there is no
    background reaper to forget to start, and `purge_expired` exists for the
    cases where a test or a health check wants the sweep to be explicit.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        # Monotonic in production: a wall-clock jump must not expire live calls.
        self._now = clock or time.monotonic

    def get(self, run_id: str) -> RunSession | None:
        """The session, or None if it never existed or has expired."""
        key = validate_run_id(run_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._now():
                del self._entries[key]
                return None
            return entry.session

    def require(self, run_id: str) -> RunSession:
        """The session, or :class:`SessionNotFound`."""
        session = self.get(run_id)
        if session is None:
            raise SessionNotFound(f"no active session for run_id {run_id!r}")
        return session

    def get_or_create(self, run_id: str) -> RunSession:
        """The session, starting one if this is the call's first tool."""
        key = validate_run_id(run_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > self._now():
                # Re-inserting extends the TTL: a call that is still talking
                # should not age out from under itself.
                entry.expires_at = self._now() + self._ttl
                return entry.session
            session = RunSession(run_id=key)
            self._entries[key] = _Entry(session=session, expires_at=self._now() + self._ttl)
            return session

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._entries.pop(validate_run_id(run_id), None)

    def purge_expired(self) -> int:
        """Remove expired sessions. Returns how many went."""
        with self._lock:
            now = self._now()
            stale = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in stale:
                del self._entries[key]
            return len(stale)

    def __len__(self) -> int:
        return len(self._entries)
