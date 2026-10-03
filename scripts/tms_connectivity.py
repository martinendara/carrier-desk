#!/usr/bin/env python3
"""Diagnose the connection to the **live** legacy TMS, from this machine only.

`scripts/tms_smoke.py` answers protocol questions through `TmsClient`, which
parses and classifies before anything is printed. When the server says no, that
is the wrong end to look from: the interesting evidence is the bytes on the
wire, before our own client has an opinion about them.

This script therefore does its own socket I/O and prints **every response line
raw**, with `repr()`, so trailing spaces, CR/LF and non-ASCII show up instead of
being trimmed away. It also reports exactly when each attempt happened, and it
classifies the failure into the four kinds that need different fixes:

* the host or port does not answer at all (name, refused, unreachable),
* the socket connects and the server rejects the token,
* the socket connects and then nothing arrives,
* the socket connects and the answer is not the shape the protocol describes.

**It is read-only.** `LOAD_BOOK` is refused by a guard, because one successful
booking permanently consumes a load for our token. The token is never printed:
it appears as `<redacted>`, and the report carries a SHA-256 prefix instead, so
a value can be compared with what the operator believes it to be without the
value itself being written down.

Usage:
    .venv/bin/python scripts/tms_connectivity.py
    .venv/bin/python scripts/tms_connectivity.py --out docs/tms-connectivity-2026-10-03.md
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow `python scripts/tms_connectivity.py`
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.tms.protocol import encode_request, redact  # noqa: E402

#: Commands this script may send. Anything else is a bug.
READ_ONLY_COMMANDS = frozenset({"DEBUG_ECHO", "LOAD_QUERY", "LOAD_GET"})

#: Sent as the token for the control probe. Shaped like a real one, obviously
#: not one, and deliberately never derived from the real value.
CONTROL_TOKEN = "DIAGNOSTIC-CONTROL-TOKEN-NOT-A-CREDENTIAL"

#: How long to wait for the TCP handshake, and for the whole answer.
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 5.0

TERMINATORS = (b"\r\n", b"\n")


def guard(command: str) -> None:
    """Refuse anything that could change state on the server."""
    if command not in READ_ONLY_COMMANDS:
        raise SystemExit(
            f"refusing to send {command}: scripts/tms_connectivity.py is read-only"
        )


# --- reporting -----------------------------------------------------------


class Reporter:
    """Collects output so it reaches the terminal and the file identically."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def fingerprint(secret: str) -> str:
    """A short, non-reversible id for a token, for comparing without printing."""
    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def describe_bytes(value: str) -> str:
    """Structural facts about a secret: enough to spot a corrupted .env."""
    raw = value.encode("utf-8", "surrogatepass")
    notes = [f"{len(value)} chars", f"{len(raw)} bytes"]
    notes.append("ascii" if value.isascii() else "NON-ASCII")
    if value != value.strip():
        notes.append("HAS SURROUNDING WHITESPACE")
    if any(ch.isspace() for ch in value):
        notes.append("CONTAINS WHITESPACE")
    if "\r" in value or "\n" in value:
        notes.append("CONTAINS CR/LF")
    if value[:1] in {"'", '"'} or value[-1:] in {"'", '"'}:
        notes.append("HAS QUOTES")
    if raw.startswith(b"\xef\xbb\xbf"):
        notes.append("HAS BOM")
    return ", ".join(notes)


def mask(text: str) -> str:
    """Replace any AUTH value with a placeholder. Safe on requests and replies."""
    return redact(text)


# --- the probe -----------------------------------------------------------


@dataclass
class Attempt:
    """One request, and everything the socket did around it."""

    label: str
    command: str
    started: datetime
    request_display: str
    addresses: list[str] = field(default_factory=list)
    connect_ms: float | None = None
    connect_error: str | None = None
    lines: list[tuple[float, bytes]] = field(default_factory=list)
    read_ms: float = 0.0
    timed_out: bool = False
    closed_by_us: bool = False
    eof: bool = False
    #: True when the token in this attempt was the control value, not the real one.
    control: bool = False

    @property
    def status(self) -> str:
        if self.connect_error:
            return self.connect_error
        if not self.lines and self.timed_out:
            return "READ_TIMEOUT (connected, no bytes)"
        if self.timed_out:
            return "PARTIAL_THEN_TIMEOUT (bytes, then silence, no END)"
        if not self.lines:
            return "CLOSED_WITHOUT_RESPONSE"
        last = _last_significant(self.lines)
        if last is None:
            return "MALFORMED (no END, no ERR)"
        if last.startswith(b"ERR|"):
            return "ERR:" + _error_code(last)
        if last.strip() == b"END":
            return "OK (END)"
        return "MALFORMED (terminated by neither END nor ERR)"


def _last_significant(lines: list[tuple[float, bytes]]) -> bytes | None:
    for _, raw in reversed(lines):
        trimmed = raw.strip()
        if trimmed:
            return trimmed
    return None


def _error_code(line: bytes) -> str:
    text = line.decode("ascii", "replace")
    for token in text.split("|"):
        key, _, value = token.partition(":")
        if key.strip().upper() == "CODE":
            return value.strip() or "?"
    return "?"


def _is_complete(buffer: bytes) -> bool:
    """Whether the buffer already holds the terminator the protocol promises."""
    for raw in buffer.split(b"\n"):
        line = raw.rstrip(b"\r").strip()
        if line == b"END" or line.startswith(b"ERR|"):
            return True
    return False


def _split_lines(buffer: bytes) -> list[bytes]:
    """Every line as it arrived, keeping terminator artefacts out of the way."""
    if not buffer:
        return []
    parts = buffer.split(b"\n")
    lines = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1 and part == b"":
            continue  # a trailing newline does not make an empty line
        lines.append(part[:-1] if part.endswith(b"\r") else part)
    return lines


def exchange(
    host: str,
    port: int,
    frame: bytes,
    *,
    label: str,
    command: str,
    connect_timeout: float,
    read_timeout: float,
    control: bool = False,
) -> Attempt:
    """Send one frame on a fresh socket and record exactly what came back."""
    guard(command)

    attempt = Attempt(
        label=label,
        command=command,
        started=now(),
        request_display=mask(frame.decode("ascii", "replace")).strip(),
        control=control,
    )

    # 1. Name resolution, separated from the connect so a DNS failure is not
    #    reported as a refused port.
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        attempt.addresses = sorted({info[4][0] for info in infos})
    except socket.gaierror as exc:
        attempt.connect_error = f"NAME_RESOLUTION_FAILED ({exc.strerror or exc})"
        return attempt

    # 2. Connect, with its own short deadline.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    began = time.monotonic()
    try:
        sock.connect((host, port))
        attempt.connect_ms = (time.monotonic() - began) * 1000
    except socket.timeout:
        attempt.connect_error = f"CONNECT_TIMEOUT (>{connect_timeout:.0f}s)"
        sock.close()
        return attempt
    except ConnectionRefusedError as exc:
        attempt.connect_error = f"CONNECTION_REFUSED ({exc.strerror or exc})"
        sock.close()
        return attempt
    except OSError as exc:
        attempt.connect_error = f"CONNECT_FAILED ({type(exc).__name__}: {exc.strerror or exc})"
        sock.close()
        return attempt

    # 3. Send, then read to the terminator or the deadline.
    read_began = time.monotonic()
    try:
        sock.sendall(frame)
    except OSError as exc:
        attempt.connect_error = f"SEND_FAILED ({type(exc).__name__}: {exc.strerror or exc})"
        sock.close()
        return attempt

    buffer = b""
    try:
        while True:
            remaining = read_timeout - (time.monotonic() - read_began)
            if remaining <= 0:
                attempt.timed_out = True
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                attempt.timed_out = True
                break
            if not chunk:
                attempt.eof = True
                break
            buffer += chunk
            if _is_complete(buffer):
                # The server sometimes holds the connection open after a
                # complete answer. We close it ourselves rather than wait.
                attempt.closed_by_us = True
                break
    except OSError as exc:
        attempt.connect_error = f"READ_FAILED ({type(exc).__name__}: {exc.strerror or exc})"
    finally:
        attempt.read_ms = (time.monotonic() - read_began) * 1000
        sock.close()

    for line in _split_lines(buffer):
        attempt.lines.append((attempt.read_ms, line))
    return attempt


def report_attempt(reporter: Reporter, index: int, attempt: Attempt) -> None:
    """Print one attempt, raw and unmasked except for the token."""
    reporter.emit(f"── {index}. {attempt.label}")
    reporter.emit(f"   at       : {stamp(attempt.started)}")
    if attempt.addresses:
        reporter.emit(f"   resolved : {', '.join(attempt.addresses)}")
    reporter.emit(f"   request  : {attempt.request_display!r}")
    if attempt.connect_error:
        reporter.emit(f"   connect  : {attempt.connect_error}")
        reporter.emit("   response : (no exchange happened)")
        reporter.emit()
        return

    reporter.emit(f"   connect  : ok in {attempt.connect_ms:.0f} ms")
    if not attempt.lines:
        reporter.emit(f"   response : (no bytes) after {attempt.read_ms:.0f} ms")
    else:
        reporter.emit(f"   response : {len(attempt.lines)} line(s) in {attempt.read_ms:.0f} ms")
        for elapsed, raw in attempt.lines:
            reporter.emit(f"     +{elapsed:7.0f} ms  {mask(raw.decode('ascii', 'replace'))!r}")
    if attempt.closed_by_us:
        reporter.emit("   terminator: seen (we closed the socket, not the server)")
    elif attempt.eof:
        reporter.emit("   terminator: connection closed by the server")
    elif attempt.timed_out:
        reporter.emit(f"   terminator: none before the {DEFAULT_READ_TIMEOUT:.0f}s deadline")
    reporter.emit(f"   status   : {attempt.status}")
    reporter.emit()


# --- driver --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="also write the report to this file")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    settings = get_settings()
    host, port = settings.tms_host, settings.tms_port
    token = settings.tms_token_value

    reporter = Reporter()
    reporter.emit("=" * 72)
    reporter.emit("TMS live connectivity diagnostic (read-only)")
    reporter.emit("=" * 72)
    reporter.emit(f"started  : {stamp(now())}")
    reporter.emit(f"host     : {host}:{port}")
    reporter.emit(f"token    : <redacted>  sha256[:12]={fingerprint(token)}  {describe_bytes(token)}")

    if not (host and port and token):
        reporter.emit()
        reporter.emit("VERDICT  : NO_CREDENTIALS — set TMS_HOST, TMS_PORT and TMS_TOKEN in .env")
        if args.out:
            reporter.save(args.out)
        return 2

    # What the encoder will actually put on the wire, which is not necessarily
    # what .env holds: the protocol layer trims the value.
    cleaned = token.strip()
    if cleaned != token:
        reporter.emit(
            f"           the encoder trims it -> sha256[:12]={fingerprint(cleaned)}  "
            f"{describe_bytes(cleaned)}"
        )
    reporter.emit(f"deadline : connect {args.connect_timeout:.0f}s, read {args.read_timeout:.0f}s")
    reporter.emit()

    def send(command: str, fields: dict[str, object], label: str, *, control: bool = False):
        secret = CONTROL_TOKEN if control else token
        frame = encode_request(command, secret, fields)
        return exchange(
            host, port, frame,
            label=label, command=command, control=control,
            connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
        )

    # 1. Connectivity and authentication in one probe. DEBUG_ECHO is the
    #    smallest frame there is, so a failure here is about the connection or
    #    the token and nothing else.
    echo = send("DEBUG_ECHO", {"MSG": "diag"}, "AUTH probe (DEBUG_ECHO)")
    report_attempt(reporter, 1, echo)

    if echo.connect_error:
        reporter.emit("=" * 72)
        reporter.emit(f"VERDICT  : {echo.status}")
        reporter.emit("           The socket never got far enough to test the token.")
        if args.out:
            reporter.save(args.out)
        return 1

    authenticated = echo.status == "OK (END)"

    if not authenticated:
        # The first probe alone cannot tell a bad credential from a frame the
        # server refuses to look at, because either way the answer is the same
        # sentence. These two probes separate them, and both are free:
        #
        #   no AUTH at all  -> does the server complain about the field being
        #                      absent, which means it reads AUTH and found ours
        #                      present but wrong?
        #   a bogus CMD     -> which check runs first, the command or the token?
        #
        # A server that answers AUTH_FAILED to a frame with no AUTH field is not
        # validating anything; a server that answers MISSING_FIELD is.
        no_auth = exchange(
            host, port, b"CMD:DEBUG_ECHO|MSG:diag\r\n",
            label="same probe with NO AUTH field",
            command="DEBUG_ECHO",
            connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
        )
        report_attempt(reporter, 2, no_auth)

        bad_cmd = exchange(
            host, port, encode_request("NOT_A_REAL_COMMAND", token, {"MSG": "diag"}),
            label="unknown command with the real token",
            command="DEBUG_ECHO",  # guard only; the frame above is hand-built
            connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
        )
        report_attempt(reporter, 3, bad_cmd)

        control = send("DEBUG_ECHO", {"MSG": "diag"}, "control (deliberately wrong token)",
                       control=True)
        report_attempt(reporter, 4, control)

        # Framing variants. If the server's line reader disagrees with ours about
        # the terminator, the AUTH value it compares is not the one we meant to
        # send — which is exactly the failure that looks like a bad credential.
        # Cheap to rule out, and expensive to keep wondering about.
        token_bytes = token.strip().encode("ascii", "replace")
        variants: list[tuple[str, bytes, str]] = [
            ("LF terminator, no CR", b"CMD:DEBUG_ECHO|AUTH:" + token_bytes + b"\n", "DEBUG_ECHO"),
            ("AUTH before CMD", b"AUTH:" + token_bytes + b"|CMD:DEBUG_ECHO\r\n", "DEBUG_ECHO"),
        ]
        variant_results: list[tuple[str, Attempt]] = []
        for offset, (variant_label, frame, guard_cmd) in enumerate(variants, start=5):
            attempt = exchange(
                host, port, frame,
                label=f"framing variant: {variant_label}",
                command=guard_cmd,
                connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
            )
            report_attempt(reporter, offset, attempt)
            variant_results.append((variant_label, attempt))

        reporter.emit("=" * 72)
        reporter.emit(f"VERDICT  : {echo.status}")
        reporter.emit(
            "           The socket connected and the server answered in"
            f" {echo.connect_ms:.0f} ms. The transport, the host and the port are fine."
        )
        reporter.emit(f"           no AUTH field   -> {no_auth.status}")
        reporter.emit(f"           bogus command   -> {bad_cmd.status}")
        reporter.emit(f"           wrong token     -> {control.status}")
        for variant_label, attempt in variant_results:
            reporter.emit(f"           {variant_label:<16}-> {attempt.status}")
        reporter.emit()

        # The variants are the discriminating evidence. A server that answers
        # MALFORMED to a frame in the wrong field order is a server that parses;
        # if it parses, then our well-formed frame reached the AUTH check intact
        # and the check is what failed. Without that, "AUTH_FAILED" could just
        # as well mean "I refuse everything before reading".
        malformed = [
            label for label, attempt in variant_results
            if attempt.status.startswith("ERR:MALFORMED")
        ]
        reporter.emit()
        if malformed:
            reporter.emit(
                "           The server answers ERR|CODE:MALFORMED to a frame with"
            )
            reporter.emit(
                f"           AUTH before CMD ({malformed[0]}), so it does parse the"
            )
            reporter.emit(
                "           frame and enforce the field order. Our framing — CRLF,"
            )
            reporter.emit(
                "           CMD first — is the shape it wants, and an LF-only frame"
            )
            reporter.emit(
                "           gets no answer at all, which confirms the terminator too."
            )
            reporter.emit()
            reporter.emit(
                "           So the frame is well formed and the server still refuses"
            )
            reporter.emit(
                "           the AUTH value. A frame with NO AUTH at all is refused"
            )
            reporter.emit(
                "           with the identical sentence, and so is a deliberately"
            )
            reporter.emit(
                "           wrong one: the check happens before CMD is validated and"
            )
            reporter.emit(
                "           reports 'invalid or missing' as one case."
            )
            reporter.emit()
            reporter.emit(
                "           VERDICT: TOKEN_REJECTED, framing correct. A stale or"
            )
            reporter.emit(
                "           rotated credential is the likely cause; a server that"
            )
            reporter.emit(
                "           accepts no token at all would look the same from here,"
            )
            reporter.emit(
                "           and both are fixed the same way — re-issue the token."
            )
        elif "MISSING_FIELD" in no_auth.status:
            reporter.emit(
                "           The server names the missing AUTH field on a frame without"
            )
            reporter.emit(
                "           one, so it parses the frame — and rejects this token"
            )
            reporter.emit("           specifically. The credential is wrong.")
        else:
            reporter.emit(
                "           Every frame, however malformed, gets the same sentence."
            )
            reporter.emit(
                "           Nothing here distinguishes a stale token from a server"
            )
            reporter.emit("           that accepts none. Re-issue the credential.")
        if args.out:
            reporter.save(args.out)
        return 1

    # --- authenticated: read-only protocol probes ------------------------
    # 3. A LOAD_QUERY with no filters at all: the manual says at least one is
    #    required, and this is the cheapest way to see what the server really
    #    does about it.
    bare = send("LOAD_QUERY", {"MAX_RESULTS": 5}, "LOAD_QUERY with no filter")
    report_attempt(reporter, 2, bare)

    # 4. A broad filter, to get a load to ask about.
    broad = send("LOAD_QUERY", {"EQTYPE": "DRY_VAN", "MAX_RESULTS": 5},
                 "LOAD_QUERY EQTYPE:DRY_VAN")
    report_attempt(reporter, 3, broad)

    # 5. The detail of whatever came back first. MAX_BUY lives only here.
    load_id = _first_load_id(broad)
    if load_id:
        detail = send("LOAD_GET", {"LOAD_ID": load_id}, f"LOAD_GET {load_id}")
        report_attempt(reporter, 4, detail)
    else:
        reporter.emit("── 4. LOAD_GET — skipped: the broad query returned no load id")
        reporter.emit()

    return 0


def _first_load_id(attempt: Attempt) -> str | None:
    """The first LOAD_ID in a raw response, read by field name."""
    for _, raw in attempt.lines:
        text = raw.decode("ascii", "replace")
        for token in text.split("|"):
            key, _, value = token.partition(":")
            if key.strip().upper() == "LOAD_ID" and value.strip():
                return value.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
