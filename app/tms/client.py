"""TCP client for the legacy TMS: deadlines, fault classification, retries.

Design notes that matter for correctness:

* **One connection per request.** The server closes after responding and does
  not support reuse, so there is no pool and no keep-alive to get wrong.
* **We close the socket ourselves** as soon as we see ``END``/``ERR``. The
  server sometimes holds the connection open afterwards ("delayed
  termination"); waiting for its FIN would burn our whole deadline on a
  perfectly good response.
* **The read deadline (default 5 s) is shorter than the server's 30 s idle
  timeout** on purpose: a carrier waiting on the phone will not wait 30
  seconds. See DECISIONS.md.
* **A response without ``END`` is a failure.** A truncated search is never
  reported as "fewer loads available".
"""

from __future__ import annotations

import logging
import random
import re
import socket
import time
from collections.abc import Iterable
from datetime import date, datetime, time as midnight
from typing import Callable, Mapping, Sequence

from app.deadline import Deadline
from app.tms.models import BookResult, BookingStatus, Load, LoadDetail
from app.tms.protocol import (
    MAX_FRAME_BYTES,
    TmsBudgetExhausted,
    TmsError,
    TmsFault,
    TmsInvalidInput,
    TmsMalformed,
    TmsPartialResponse,
    TmsTimeout,
    encode_request,
    ends_response,
    format_timestamp,
    parse_response,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

# --- Commands -----------------------------------------------------------

LOAD_QUERY = "LOAD_QUERY"
LOAD_GET = "LOAD_GET"
LOAD_BOOK = "LOAD_BOOK"

#: Filters the manual documents for LOAD_QUERY. At least one is required.
FILTER_FIELDS = frozenset(
    {
        "ORIG_CITY",
        "ORIG_STATE",
        "ORIG_ZIP",
        "DEST_CITY",
        "DEST_STATE",
        "DEST_ZIP",
        "EQTYPE",
        "PICKUP_DT",
    }
)

#: Commands that can be repeated safely.
IDEMPOTENT_COMMANDS = frozenset({LOAD_QUERY, LOAD_GET})

#: Error codes worth another attempt: the failure is on the server's side and
#: the request was not rejected on its merits.
RETRYABLE_ERROR_CODES = frozenset({"SERVER_ERROR"})

#: Error codes that mean "this request is wrong, sending it again cannot help".
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "AUTH_FAILED",
        "UNKNOWN_CMD",
        "MISSING_FIELD",
        "UNKNOWN_LOAD",
        "ALREADY_BOOKED",
        "INVALID_RATE",
        "MALFORMED",
    }
)

#: Backoff before attempt 2, 3, ... in seconds.
DEFAULT_BACKOFF: tuple[float, ...] = (0.3, 0.8, 1.5)
DEFAULT_JITTER = 0.2

_RECV_CHUNK = 4096
_LOAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_MC_RE = re.compile(r"^\d{1,8}$")


def is_retryable_error_code(code: str) -> bool:
    """Whether an ``ERR|CODE:`` is worth retrying.

    Unknown codes are treated as retryable: an undocumented failure is more
    likely to be transient than a request we got wrong, and the retry budget
    is small.
    """
    if code in RETRYABLE_ERROR_CODES:
        return True
    if code in NON_RETRYABLE_ERROR_CODES:
        return False
    return True


class TmsClient:
    """Blocking client for the TMS line protocol.

    All keyword options exist so tests can run fast and deterministically;
    production code should rely on the defaults.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        connect_timeout: float = 3.0,
        read_deadline: float = 5.0,
        max_attempts: int = 3,
        total_budget: float | None = None,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        jitter: float = DEFAULT_JITTER,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if not host:
            raise ValueError("TMS host is required")
        if not port:
            raise ValueError("TMS port is required")
        if not token:
            raise ValueError("TMS token is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._host = host
        self._port = port
        self._token = token
        self._connect_timeout = connect_timeout
        self._read_deadline = read_deadline
        self._max_attempts = max_attempts
        #: Wall-clock ceiling for a whole command, retries included. None means
        #: "rely on the per-attempt deadlines alone".
        self._total_budget = total_budget
        self._backoff = tuple(backoff)
        self._jitter = jitter
        self._sleep = sleep
        self._random = random_source

    @classmethod
    def from_settings(cls, settings, **overrides) -> "TmsClient":
        """Build a client from :class:`app.config.Settings`."""
        options = {
            "connect_timeout": settings.tms_connect_timeout_seconds,
            "read_deadline": settings.tms_read_deadline_seconds,
            "max_attempts": settings.tms_max_attempts,
            "total_budget": settings.request_budget_seconds,
        }
        options.update(overrides)
        return cls(
            settings.tms_host,
            settings.tms_port,
            settings.tms_token_value,
            **options,
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial, but keeps the token out
        return f"TmsClient(host={self._host!r}, port={self._port!r}, token=<redacted>)"

    # --- Public commands ------------------------------------------------

    def query_loads(
        self,
        *,
        max_results: int | None = None,
        deadline: Deadline | None = None,
        **filters: object,
    ) -> list[Load]:
        """Search loads by name, e.g. ``query_loads(ORIG_STATE="GA", max_results=5)``.

        Filter names are case-insensitive. ``PICKUP_DT`` accepts a
        ``datetime``/``date`` (rendered as the protocol timestamp) or a raw
        string, so the smoke script can probe what the server actually wants.
        """
        fields: dict[str, object] = {}
        for name, value in filters.items():
            if value is None or value == "":
                continue
            key = str(name).strip().upper()
            if key not in FILTER_FIELDS:
                raise TmsInvalidInput(
                    f"unknown filter {name!r}; allowed: {', '.join(sorted(FILTER_FIELDS))}"
                )
            fields[key] = self._render_filter_value(key, value)

        if not fields:
            # The server would answer MISSING_FIELD, but catching it here keeps
            # a pointless round trip (and a pointless retry) off the wire.
            raise TmsInvalidInput("at least one filter is required for LOAD_QUERY")

        if max_results is not None:
            fields["MAX_RESULTS"] = int(max_results)

        records = self.execute(LOAD_QUERY, fields, deadline=deadline)
        return [Load.from_record(record) for record in records]

    def get_load(self, load_id: str, *, deadline: Deadline | None = None) -> LoadDetail:
        """Fetch one load, including the fields reserved for a single lookup."""
        self._validate_load_id(load_id)
        records = self.execute(LOAD_GET, {"LOAD_ID": load_id}, deadline=deadline)
        if not records:
            raise TmsMalformed(f"LOAD_GET for {load_id} returned no record")
        return LoadDetail.from_record(records[0])

    def book_load(
        self,
        load_id: str,
        mc_number: str,
        agreed_rate: int,
        *,
        deadline: Deadline | None = None,
    ) -> BookResult:
        """Book a load. **Consumes the load permanently for this token.**

        There is no blind retry. If the attempt dies in transport we cannot
        know whether it landed, so we ask the server what the load's status is
        now and let that answer, rather than booking twice or guessing.

        Never call this against the live server outside a deliberate,
        human-approved manual test.
        """
        self._validate_load_id(load_id)
        self._validate_mc(mc_number)
        rate = int(agreed_rate)
        if rate <= 0:
            raise TmsInvalidInput("agreed_rate must be a positive integer")

        fields = {"LOAD_ID": load_id, "MC_NUM": mc_number, "AGREED_RATE": rate}
        ambiguous = False

        try:
            lines = self._execute(LOAD_BOOK, fields, retryable=False, deadline=deadline)
        except TmsError as exc:
            # A well-formed rejection: the request reached a working server.
            if exc.code == "ALREADY_BOOKED":
                # Bookings are keyed to our token and monotonic, so this load
                # is gone for us. Whether it was our earlier attempt or not,
                # only a human can confirm the reference.
                logger.warning("LOAD_BOOK for %s reports ALREADY_BOOKED", load_id)
                return self._unconfirmed(load_id, ambiguous=False)
            raise
        except (TmsTimeout, TmsPartialResponse, TmsMalformed):
            # Only these three mean the frame went out and we lost the answer.
            # TmsBudgetExhausted is not here on purpose: it is raised before the
            # attempt, so nothing was sent and there is nothing to resolve.
            ambiguous = True
            logger.warning("LOAD_BOOK for %s failed in transport; resolving via LOAD_GET", load_id)
            return self._resolve_ambiguous_booking(load_id, fields, deadline)

        return self._parse_booking(lines, load_id=load_id, ambiguous=ambiguous)

    # --- Booking helpers ------------------------------------------------

    def _resolve_ambiguous_booking(
        self,
        load_id: str,
        fields: Mapping[str, object],
        deadline: Deadline | None = None,
    ) -> BookResult:
        """Decide the outcome of a LOAD_BOOK whose response we never saw."""
        load = self.get_load(load_id, deadline=deadline)  # idempotent: normal retries

        if not load.is_open:
            # The load left OPEN, and the only booking call in flight was ours.
            return self._unconfirmed(load_id, ambiguous=True)

        # Still open, so our attempt did not land. One more try, never a loop:
        # a second ambiguous outcome is handed to a human rather than retried.
        try:
            lines = self._execute(LOAD_BOOK, fields, retryable=False, deadline=deadline)
        except TmsError as exc:
            if exc.code == "ALREADY_BOOKED":
                return self._unconfirmed(load_id, ambiguous=True)
            raise
        except (TmsTimeout, TmsPartialResponse, TmsMalformed):
            return self._unconfirmed(load_id, ambiguous=True)

        return self._parse_booking(lines, load_id=load_id, ambiguous=True)

    @staticmethod
    def _parse_booking(
        lines: list[dict[str, str]], *, load_id: str, ambiguous: bool
    ) -> BookResult:
        if not lines:
            raise TmsMalformed("LOAD_BOOK returned no record")

        record = lines[0]
        status = record.get("STATUS", "").strip().upper()
        booking_ref = (record.get("BOOKING_REF") or "").strip() or None
        if status != "BOOKED" or booking_ref is None:
            # Success is only success when it carries a reference. Anything
            # else needs a human, which is exactly what BOOKED_UNCONFIRMED is.
            logger.warning("LOAD_BOOK returned an unusable confirmation: status=%r", status)
            return BookResult(
                load_id=load_id,
                status=BookingStatus.BOOKED_UNCONFIRMED,
                booking_ref=booking_ref,
                ambiguous=ambiguous,
            )

        timestamp = (record.get("TIMESTAMP") or "").strip()
        return BookResult(
            load_id=record.get("LOAD_ID", load_id),
            status=BookingStatus.BOOKED,
            booking_ref=booking_ref,
            booked_at=parse_timestamp("TIMESTAMP", timestamp) if timestamp else None,
            ambiguous=ambiguous,
        )

    @staticmethod
    def _unconfirmed(load_id: str, *, ambiguous: bool) -> BookResult:
        return BookResult(
            load_id=load_id,
            status=BookingStatus.BOOKED_UNCONFIRMED,
            booking_ref=None,
            ambiguous=ambiguous,
        )

    # --- Request plumbing -----------------------------------------------

    def execute(
        self,
        command: str,
        fields: Mapping[str, object] | None = None,
        *,
        deadline: Deadline | None = None,
    ) -> list[dict[str, str]]:
        """Send a command and return parsed records, retrying if it is safe.

        ``LOAD_BOOK`` is the only command we ever send that is not idempotent,
        and it never comes through here with retries enabled.
        """
        retryable = command in IDEMPOTENT_COMMANDS
        return self._execute(command, fields, retryable=retryable, deadline=deadline)

    def _execute(
        self,
        command: str,
        fields: Mapping[str, object] | None,
        *,
        retryable: bool,
        deadline: Deadline | None = None,
    ) -> list[dict[str, str]]:
        # A caller-supplied deadline spans the whole HTTP request and is shared
        # across commands; otherwise one is started for this command alone.
        budget = deadline
        if budget is None and self._total_budget:
            budget = Deadline(self._total_budget)

        attempts = self._max_attempts if retryable else 1
        last_fault: TmsFault | None = None

        for attempt in range(1, attempts + 1):
            if budget is not None and budget.expired:
                # Out of the caller's time before the frame went out. Nothing
                # happened on the server, so this is not an ambiguous outcome.
                raise TmsBudgetExhausted(f"no time left to send {command}")

            started = time.monotonic()
            try:
                lines = self._one_attempt(command, fields, budget)
            except TmsFault as fault:
                last_fault = fault
                wants_retry = self._is_retryable(fault) and attempt < attempts
                delay = self._next_delay(attempt) if wants_retry else 0.0
                # The time budget, not the retry policy, is what stops us here:
                # sleeping would spend time the caller does not have on a retry
                # that could not finish anyway.
                out_of_time = wants_retry and budget is not None and not budget.allows(delay)
                retrying = wants_retry and not out_of_time

                logger.warning(
                    "tms %s attempt %d/%d %s in %.3fs%s",
                    command,
                    attempt,
                    attempts,
                    self._describe(fault),
                    time.monotonic() - started,
                    " - retrying" if retrying else "",
                )
                if out_of_time:
                    # Reported as a budget failure rather than the underlying
                    # timeout: the caller needs to know the request ran out of
                    # its own clock, which is a different (and retryable) thing.
                    raise TmsBudgetExhausted(
                        f"time budget exhausted during {command}"
                    ) from fault
                if not retrying:
                    raise
                self._sleep(delay)
                continue

            logger.info(
                "tms %s attempt %d/%d ok: %d record(s) in %.3fs",
                command,
                attempt,
                attempts,
                len(lines),
                time.monotonic() - started,
            )
            return lines

        assert last_fault is not None  # the loop only exits through return/raise
        raise last_fault

    def _one_attempt(
        self,
        command: str,
        fields: Mapping[str, object] | None,
        deadline: Deadline | None = None,
    ) -> list[dict[str, str]]:
        """One request over one connection. Raises a :class:`TmsFault` subclass."""
        frame = encode_request(command, self._token, fields)
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout
            ) as sock:
                sock.sendall(frame)
                lines = self._read_response(sock, deadline)
        except TimeoutError as exc:
            # Covers both the connect timeout and a recv that lost the race
            # with our own deadline handling below.
            raise TmsTimeout(f"TMS did not answer within {self._read_deadline:.1f}s") from exc
        except OSError as exc:
            # Refused, unreachable, reset. Classified as a timeout because from
            # the caller's point of view it is the same transient transport
            # failure, and it is retryable for idempotent commands.
            raise TmsTimeout(f"TMS connection failed ({type(exc).__name__})") from exc

        # Parsing happens per attempt, on purpose: a framing fault is a
        # retryable condition for an idempotent command.
        return parse_response(lines)

    def _read_response(self, sock: socket.socket, budget: Deadline | None = None) -> list[str]:
        """Read until ``END``/``ERR``, then stop. The caller closes the socket.

        The per-attempt read deadline is clamped to whatever is left of the
        request budget: an attempt must never outlive the caller's time.
        """
        window = self._read_deadline
        if budget is not None:
            window = min(window, budget.remaining)
        deadline = time.monotonic() + window
        buffer = b""
        lines: list[str] = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._incomplete(lines, buffer)

            sock.settimeout(remaining)
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except TimeoutError as exc:
                raise self._incomplete(lines, buffer) from exc

            if not chunk:
                # Server closed. A terminator may still be sitting in the
                # buffer unterminated; accept it rather than lose a good answer.
                if self._consume_terminator(buffer, lines):
                    return lines
                raise TmsPartialResponse(
                    f"connection closed after {len(lines)} line(s) without END"
                )

            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = self._decode(raw)
                lines.append(line)
                if ends_response(line):
                    return lines

            # The last line is not always newline-terminated. Check what is
            # left before blocking again: on a connection the server holds
            # open, waiting here would turn a good response into a timeout.
            if self._consume_terminator(buffer, lines):
                return lines

            if len(buffer) > MAX_FRAME_BYTES:
                # An unterminated line this long is a framing fault, not a slow
                # server. Without this cap it would sit until the deadline and
                # be misreported as a timeout.
                raise TmsMalformed("response line exceeds the maximum frame size")

    def _consume_terminator(self, buffer: bytes, lines: list[str]) -> bool:
        """If the unterminated leftover *is* ``END``/``ERR``, take it and finish."""
        tail = self._decode(buffer).strip()
        if tail and ends_response(tail):
            lines.append(tail)
            return True
        return False

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            # Tolerates a bare LF as well as the documented CRLF: the manual
            # says CRLF, but a legacy server that drifts should not look like a
            # hung connection.
            return raw.decode("ascii").rstrip("\r")
        except UnicodeDecodeError as exc:
            raise TmsMalformed("response contains non-ASCII bytes") from exc

    def _incomplete(self, lines: list[str], buffer: bytes) -> TmsFault:
        """Classify a deadline that passed while the response was unfinished."""
        if lines or buffer.strip():
            return TmsPartialResponse(
                f"no END before the {self._read_deadline:.1f}s deadline"
            )
        return TmsTimeout(f"no bytes before the {self._read_deadline:.1f}s deadline")

    @staticmethod
    def _is_retryable(fault: TmsFault) -> bool:
        if isinstance(fault, TmsInvalidInput):
            # Our own bug, not the network's. Retrying would repeat it.
            return False
        if isinstance(fault, TmsBudgetExhausted):
            # There is no time to retry; `_execute` already checked.
            return False
        if isinstance(fault, TmsError):
            return is_retryable_error_code(fault.code)
        return isinstance(fault, (TmsTimeout, TmsPartialResponse, TmsMalformed))

    @staticmethod
    def _describe(fault: TmsFault) -> str:
        """A log-safe description: class name, plus the code for TmsError."""
        if isinstance(fault, TmsError):
            return f"TmsError({fault.code})"
        return type(fault).__name__

    def _next_delay(self, attempt: int) -> float:
        """Exponential-ish backoff with jitter, indexed by the attempt just made."""
        index = min(attempt - 1, len(self._backoff) - 1)
        base = self._backoff[index] if self._backoff else 0.0
        if self._jitter <= 0:
            return base
        return base * (1 + self._jitter * (2 * self._random() - 1))

    # --- Input validation -----------------------------------------------

    @staticmethod
    def _validate_load_id(load_id: str) -> None:
        if not _LOAD_ID_RE.match(str(load_id).strip()):
            raise TmsInvalidInput(f"invalid load id {load_id!r}")

    @staticmethod
    def _validate_mc(mc_number: str) -> None:
        if not _MC_RE.match(str(mc_number).strip()):
            raise TmsInvalidInput("MC number must be 1-8 digits")

    @staticmethod
    def _render_filter_value(name: str, value: object) -> object:
        """Render a filter value for the wire.

        A `date` becomes midnight UTC of that day: the manual calls the field a
        "pickup date" without pinning down the granularity, so the smoke script
        probes the live server and docs/tms-protocol.md records the answer.
        """
        if isinstance(value, datetime):
            return format_timestamp(value)
        if isinstance(value, date):
            return format_timestamp(datetime.combine(value, midnight()))
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            raise TmsInvalidInput(f"filter {name} must be a single value")
        return value
