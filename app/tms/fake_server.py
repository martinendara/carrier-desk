"""A local TCP server that replays the recorded TMS fixtures with injected faults.

Two callers, one implementation:

* **Tests** must never touch the live server — every successful ``LOAD_BOOK``
  permanently consumes a load for our token, so a suite aimed at the real host
  would quietly destroy the demo data.
* **Development** runs with ``TMS_MODE=fake`` so a platform webhook test can
  drive a whole call without spending live loads. The server runs in-process,
  on a loopback port, and the real :class:`~app.tms.client.TmsClient` talks to
  it — so the socket, deadline and framing code all still run.

Either way the server behaves like the real one on the wire: same padding, same
``END`` framing, same error codes.

Modes
-----
``normal``     the recorded response, terminated by ``END``.
``timeout``    the request is read and then nothing is ever sent.
``partial``    a valid prefix of the response, then the connection closes
               without ``END``.
``malformed``  a framing violation (a token with no colon, an empty token,
               or a caller-supplied body).
``delayed``    the complete response, and then the connection is held open.

Faults are injected per request from ``script`` (consumed in order), so a test
can say "the first LOAD_BOOK attempt dies in transport, the LOAD_GET after it
works, the retry succeeds".
"""

from __future__ import annotations

import os
import random
import re
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The recorded live samples. The fake is a development and test component, so
#: it seeds itself from the recorded fixtures rather than carrying a second copy
#: of the same data. Override with TMS_FAKE_FIXTURES when the files move.
DEFAULT_FIXTURES = Path(
    os.environ.get("TMS_FAKE_FIXTURES", REPO_ROOT / "tests" / "fixtures" / "tms_samples.txt")
)

# Modes
NORMAL = "normal"
TIMEOUT = "timeout"
PARTIAL = "partial"
MALFORMED = "malformed"
DELAYED = "delayed"
MODES = frozenset({NORMAL, TIMEOUT, PARTIAL, MALFORMED, DELAYED})

# LOAD_BOOK outcomes
BOOK_SUCCESS = "success"
BOOK_ALREADY = "already_booked"
BOOK_INVALID_RATE = "invalid_rate"

#: Commands a randomly-injected fault may hit. DEBUG_ECHO is excluded on
#: purpose: it exists to prove the encoder, and the manual scopes faults to the
#: operational commands. A diagnostic that fails at random is not a diagnostic.
FAULTABLE_COMMANDS = frozenset({"LOAD_QUERY", "LOAD_GET", "LOAD_BOOK"})

#: Which faults a random injection picks between, and with what odds.
#:
#: Both are *recoverable* transport faults: the request reached the server, the
#: answer did not come back whole, and a retry can still succeed within the
#: request's time budget. That is the class the retry ladder and the
#: ambiguous-booking recovery exist for, so it is the class worth simulating.
#:
#: `TIMEOUT` is deliberately absent. The client's read deadline is 5 s and its
#: per-request budget is 8 s, so a genuine timeout burns most of the budget on
#: one attempt and the retry never fits — every injected timeout would surface
#: as a 504 rather than as a recovered retry. A demo that fails outright one
#: time in ten shows nothing about the ladder; a truncated answer shows the
#: ladder working, which is the point of the knob. `PARTIAL` is the closest
#: recoverable analogue of "the server went quiet".
#:
#: A server error (`ERR|CODE:`) is also absent: that path is covered explicitly
#: and deterministically, and a response is an answer rather than an accident.
RANDOM_FAULT_MODES = (PARTIAL, MALFORMED)

#: Filters the fake server understands. Anything else is silently discarded,
#: exactly as the manual describes -- which is why a misspelled filter looks
#: like "no filter" and trips MISSING_FIELD.
KNOWN_FILTERS = frozenset(
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

#: The default framing violation: a token that is not `KEY:VALUE`.
DEFAULT_MALFORMED_BODY = "LOAD_ID:LD00896|THIS_TOKEN_HAS_NO_COLON"

_TERMINATOR = "\r\n"
_STATUS_RE = re.compile(r"STATUS:[^|]*")


# --- Fixtures -----------------------------------------------------------


def load_fixture_blocks(path: Path | str = DEFAULT_FIXTURES) -> list[list[str]]:
    """Split the fixture file into responses on its ``END`` lines."""
    text = Path(path).read_text(encoding="utf-8")
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.strip() == "END":
            if current:
                blocks.append(current)
            current = []
            continue
        if line.strip():
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _blocks() -> tuple[list[str], str]:
    """Return ``(query_records, detail_record)`` from the recorded samples."""
    blocks = load_fixture_blocks()
    if not blocks:
        raise RuntimeError(f"no fixture blocks found in {DEFAULT_FIXTURES}")

    detail = next((block[0] for block in blocks if "MAX_BUY:" in block[0]), None)
    if detail is None:
        raise RuntimeError("no LOAD_GET sample (one with MAX_BUY) in the fixtures")

    # A query sample is any block that is not the detail line.
    query = next(block for block in blocks if block[0] != detail)
    return query, detail


@dataclass
class Reply:
    """What the fake server does with one request."""

    payload: bytes = b""
    #: Sleep this long before closing without sending anything (timeout mode).
    hold_before: float = 0.0
    #: Send the payload, then keep the connection open this long (delayed mode).
    hold_after: float = 0.0


# --- Request parsing (server side) --------------------------------------


def parse_request_line(line: str) -> dict[str, str]:
    """Parse a request frame into fields, the way the server would."""
    fields: dict[str, str] = {}
    for token in line.split("|"):
        key, separator, value = token.partition(":")
        if not separator:
            continue
        fields[key.strip().upper()] = value.strip()
    return fields


def _redact(fields: dict[str, str]) -> dict[str, str]:
    if "AUTH" in fields:
        fields = dict(fields, AUTH="<redacted>")
    return fields


# --- The server ---------------------------------------------------------


class FakeTms:
    """A throwaway TMS server bound to an ephemeral local port.

        with FakeTms(script=[PARTIAL, NORMAL]) as tms:
            client = TmsClient("127.0.0.1", tms.port, "test-token", ...)
    """

    def __init__(
        self,
        *,
        mode: str = NORMAL,
        script: list[str] | None = None,
        error_code: str | None = None,
        error_command: str | None = None,
        book_result: str = BOOK_SUCCESS,
        booking_lands_despite_fault: bool = True,
        load_status: str | None = None,
        hold_seconds: float = 1.0,
        raw_response: bytes | None = None,
        fault_rate: float = 0.0,
        random_value: Callable[[], float] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        for entry in script or []:
            if entry not in MODES:
                raise ValueError(f"unknown mode {entry!r} in script")
        if not 0.0 <= fault_rate <= 1.0:
            raise ValueError("fault_rate must be between 0 and 1")

        self.mode = mode
        self.script = list(script or [])
        self.error_code = error_code
        self.error_command = error_command
        self.book_result = book_result
        #: When True, a LOAD_BOOK whose response is lost still lands on the
        #: server -- the "we cannot know whether it worked" case.
        self.booking_lands_despite_fault = booking_lands_despite_fault
        self.load_status = load_status
        self.hold_seconds = hold_seconds
        self.raw_response = raw_response
        #: Probability that an operational command is answered with a fault
        #: instead of a payload. 0 means "only the script injects faults".
        self.fault_rate = fault_rate
        #: Injectable so a test can make the dice land where it wants.
        self._random = random_value or random.random
        #: Faults actually injected, for a test to assert the rate is live.
        self.injected_faults: list[str] = []
        self.host = host
        self.port = port

        self.query_records, self.detail_record = _blocks()
        self.detail_load_id = _field(self.detail_record, "LOAD_ID")

        #: Requests received, in order, with AUTH redacted.
        self.requests: list[dict[str, str]] = []
        self.commands: list[str] = []
        self._booked: set[str] = set()

        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> "FakeTms":
        if self._server is not None:
            # Idempotent: `start_fake_tms()` returns an already-running server,
            # and a caller wrapping it in `with` must not try to bind twice.
            return self
        server = _Server((self.host, self.port), _Handler)
        server.fake = self
        self._server = server
        self.port = server.server_address[1]
        # A short poll interval: the default 0.5 s is charged to every
        # `stop()`, and these servers are started and stopped dozens of times.
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeTms":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- request handling -----------------------------------------------

    def build_reply(self, line: str) -> Reply:
        """Decide the answer to one request line."""
        fields = parse_request_line(line)
        command = fields.get("CMD", "")
        self.requests.append(_redact(fields))
        self.commands.append(command)

        if self.error_code and (self.error_command is None or self.error_command == command):
            return Reply(payload=self._encode(self._error_frame(self.error_code)))

        mode = self._next_mode(command)
        body = self._body_for(command, fields, mode)

        if mode == TIMEOUT:
            return Reply(hold_before=self.hold_seconds)
        if mode == MALFORMED:
            return Reply(payload=self._malformed_payload())
        if mode == PARTIAL:
            return Reply(payload=self._truncate(body))
        if mode == DELAYED:
            return Reply(payload=self._encode(body), hold_after=self.hold_seconds)
        return Reply(payload=self._encode(body))

    def _next_mode(self, command: str = "") -> str:
        """The mode for the next request: scripted first, then the dice, then the default."""
        if self.script:
            return self.script.pop(0)
        if (
            self.fault_rate > 0
            and self.mode == NORMAL
            and command in FAULTABLE_COMMANDS
            and self._random() < self.fault_rate
        ):
            mode = RANDOM_FAULT_MODES[0 if self._random() < 0.5 else 1]
            self.injected_faults.append(mode)
            return mode
        return self.mode

    def _body_for(self, command: str, fields: dict[str, str], mode: str) -> str:
        if command == "LOAD_QUERY":
            return self._query_body(fields)
        if command == "LOAD_GET":
            return self._get_body(fields.get("LOAD_ID", ""))
        if command == "LOAD_BOOK":
            # A fault that swallows the response may still have let the write
            # land. That is the whole point of the ambiguous-booking tests.
            delivered = mode in {NORMAL, DELAYED}
            lands = delivered or self.booking_lands_despite_fault
            return self._book_body(fields.get("LOAD_ID", ""), lands=lands)
        if command == "DEBUG_ECHO":
            return self._echo_body(fields)
        return self._error_frame("UNKNOWN_CMD")

    # --- response bodies ------------------------------------------------

    def _query_body(self, fields: dict[str, str]) -> str:
        filters = {k: v for k, v in fields.items() if k in KNOWN_FILTERS and v}
        if not filters:
            return self._error_frame("MISSING_FIELD", "At least one filter required")

        records = [r for r in self.query_records if self._matches(r, filters)]
        max_results = fields.get("MAX_RESULTS")
        if max_results and max_results.isdigit():
            records = records[: int(max_results)]
        return self._lines(records)

    def _get_body(self, load_id: str) -> str:
        record = self._detail_record_for(load_id)
        if record is None:
            return self._error_frame("UNKNOWN_LOAD", "load not found")

        if load_id in self._booked:
            record = self._with_status(record, "BOOKED")
        elif self.load_status:
            record = self._with_status(record, self.load_status)
        return self._lines([record])

    def _detail_record_for(self, load_id: str) -> str | None:
        """The recorded detail line, or a synthesised one for a known load.

        The fixture only captured `LOAD_GET` for one load, but the real server
        answers for any load it has. Synthesising keeps `TMS_MODE=fake` usable
        end to end — without it you could search and not negotiate, which is the
        only thing the fake is for.

        The invented fields are marked here rather than in the data: `MAX_BUY`
        above the posted rate, and a delivery date three days out. Nothing about
        them is claimed to match the live server.
        """
        if load_id == self.detail_load_id:
            return self.detail_record

        query_line = next(
            (line for line in self.query_records if _field(line, "LOAD_ID") == load_id), None
        )
        if query_line is None:
            return None

        fields = dict(_tokens(query_line))
        rate = int(fields.get("RATE", "0") or 0)

        # Insert the detail-only columns in the order the real server uses:
        # DELIVERY_DT after the pickup, the cargo fields after RATE, DIMS after
        # MILES, NOTES before STATUS, and MAX_BUY last.
        insertions = {
            "PICKUP_DT": [("DELIVERY_DT", _plus_days(fields.get("PICKUP_DT", ""), 3))],
            "RATE": [
                ("WEIGHT", "41000   "),
                ("COMMODITY", "General freight              "),
                ("PIECES", "24    "),
            ],
            "MILES": [("DIMS", "53ft x 8ft x 9ft              ")],
            "DIMS": [("NOTES", " " * 40)],
            "STATUS": [("MAX_BUY", f"{_fake_max_buy(rate)}    ")],
        }

        line: list[str] = []
        for key, value in _tokens(query_line):
            line.append(f"{key}:{value}")
            line.extend(f"{extra_key}:{extra_value}" for extra_key, extra_value in insertions.get(key, ()))
        return "|".join(line)

    def _book_body(self, load_id: str, *, lands: bool = True) -> str:
        if self.book_result == BOOK_INVALID_RATE:
            return self._error_frame("INVALID_RATE", "rate outside the accepted range")
        if self.book_result == BOOK_ALREADY:
            return self._error_frame("ALREADY_BOOKED", "load already booked")

        # A booking that lands is remembered even when its response is lost,
        # so a follow-up LOAD_GET reports BOOKED -- the ambiguous case.
        if lands:
            self._booked.add(load_id)
        return self._lines(
            [
                f"LOAD_ID:{load_id:<12}|BOOKING_REF:BR-20261001-0001|"
                f"STATUS:BOOKED    |TIMESTAMP:20261001120000"
            ]
        )

    def _echo_body(self, fields: dict[str, str]) -> str:
        # FIELDS_PARSED counts every accepted pair, CMD and AUTH included.
        return self._lines(
            [f"ECHO|AUTH:OK|FIELDS_PARSED:{len(fields)}|MSG:{fields.get('MSG', '')}"]
        )

    def _error_frame(self, code: str, message: str = "") -> str:
        return f"ERR|CODE:{code}|MSG:{message}"

    def _malformed_payload(self) -> bytes:
        if self.raw_response is not None:
            return self.raw_response
        return self._encode(DEFAULT_MALFORMED_BODY + _TERMINATOR + "END")

    def _truncate(self, body: str) -> bytes:
        """Send a valid prefix of the response and then stop, without END."""
        lines = [line for line in body.split(_TERMINATOR) if line.strip()]
        if lines and lines[-1].strip() == "END":
            lines = lines[:-1]
        if not lines:
            return b""
        keep = max(1, len(lines) // 2)
        return self._encode(_TERMINATOR.join(lines[:keep]) + _TERMINATOR)

    # --- helpers --------------------------------------------------------

    def _lines(self, records: list[str]) -> str:
        return "".join(record + _TERMINATOR for record in records) + "END"

    @staticmethod
    def _encode(body: str) -> bytes:
        return body.encode("ascii")

    @staticmethod
    def _with_status(record: str, status: str) -> str:
        return _STATUS_RE.sub(f"STATUS:{status:<8}", record, count=1)

    @staticmethod
    def _matches(record: str, filters: dict[str, str]) -> bool:
        for name, wanted in filters.items():
            actual = _field(record, name)
            if name in {"ORIG_CITY", "DEST_CITY"}:
                # City matching is forgiving on the real server.
                if wanted.lower() not in actual.lower():
                    return False
            elif name == "PICKUP_DT":
                # Prefix match, so a bare date selects that whole day.
                if not actual.startswith(wanted):
                    return False
            elif actual != wanted:
                return False
        return True


def _field(record: str, name: str) -> str:
    """Read one field out of a raw record line, by name."""
    match = re.search(rf"(?:^|\|){re.escape(name)}:([^|]*)", record)
    return match.group(1).strip() if match else ""


def _tokens(record: str) -> list[tuple[str, str]]:
    """Split a record line into ordered (KEY, value) pairs, keeping padding."""
    pairs = []
    for token in record.split("|"):
        key, separator, value = token.partition(":")
        if separator:
            pairs.append((key.strip().upper(), value))
    return pairs


def _ceil25(value: int) -> int:
    return -(-value // 25) * 25


def _fake_max_buy(rate: int) -> int:
    """A ceiling above the posted rate, invented by the fake.

    The recorded sample had MAX_BUY about 22% over RATE; this reproduces the
    shape, not the number. Nothing here claims to match the live server.
    """
    return _ceil25(rate + max(300, rate // 5))


def _plus_days(timestamp: str, days: int) -> str:
    """`YYYYMMDDHHMMSS` shifted by whole days. Returns the input if unparseable."""
    try:
        moment = datetime.strptime(timestamp.strip(), "%Y%m%d%H%M%S")
    except ValueError:
        return timestamp
    return (moment + timedelta(days=days)).strftime("%Y%m%d%H%M%S")


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: D102 - socketserver entry point
        line = self._read_request()
        if line is None:
            return
        reply = self.server.fake.build_reply(line)  # type: ignore[attr-defined]

        if reply.hold_before:
            time.sleep(reply.hold_before)
            return
        if reply.payload:
            self.request.sendall(reply.payload)
        if reply.hold_after:
            time.sleep(reply.hold_after)

    def _read_request(self) -> str | None:
        self.request.settimeout(5.0)
        buffer = b""
        while b"\r\n" not in buffer and len(buffer) < 8192:
            try:
                chunk = self.request.recv(4096)
            except (TimeoutError, OSError):
                return None
            if not chunk:
                return None
            buffer += chunk
        line = buffer.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return line or None


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    #: Do not join handler threads on close. Several modes hold a connection
    #: open on purpose, and waiting for them would add their full delay to
    #: every test that merely wants the server gone.
    block_on_close = False


def start_fake_tms(**options) -> FakeTms:
    """Start a fake TMS on an ephemeral loopback port.

    Used by `TMS_MODE=fake`. The caller owns the returned object and must call
    `.stop()` — `app.main` does that on shutdown.
    """
    return FakeTms(host="127.0.0.1", port=0, **options).start()
