"""Encoding and parsing for the legacy TMS line protocol.

This module is pure: bytes in, dictionaries out. It never opens a socket, so
the entire wire format can be tested without a server (see `tests/fake_tms.py`
for the server side of that).

Two rules from the manual drive the design:

* Requests are ``CMD:<command>|AUTH:<token>|<FIELD>:<VALUE>|...\\r\\n``, ASCII only,
  at most 4096 bytes including the terminator.
* Responses are zero or more record lines followed by ``END``, or a single
  ``ERR|CODE:<code>|MSG:<msg>`` line. **A response that does not end in ``END``
  is a failure**, never "fewer results than expected".

Field names are matched by name, never by position or width. The live server
and the manual disagree on padding (see docs/tms-protocol.md, "Observed vs.
manual"), so every value is trimmed and numeric parsing tolerates both
zero-padded and space-padded numbers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, Mapping

# --- Wire constants -----------------------------------------------------

MAX_FRAME_BYTES = 4096
LINE_TERMINATOR = b"\r\n"
FIELD_SEPARATOR = "|"
KEY_VALUE_SEPARATOR = ":"

#: Characters that would let a value reshape the request line or smuggle a
#: second frame into one write.
FORBIDDEN_IN_VALUE = ("|", "\r", "\n")

#: Timestamps are ``YYYYMMDDHHMMSS`` and are treated as UTC (see DECISIONS.md).
TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"

# Field names are interpolated into a position the server parses structurally,
# so they are restricted to the shape the protocol documents.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


# --- Exceptions ---------------------------------------------------------


class TmsFault(Exception):
    """Base class for every way a TMS call can fail.

    Having one base lets callers write ``except TmsFault`` when they only want
    to report "the TMS did not answer usefully", while the specific classes
    below still drive the retry policy.
    """


class TmsInvalidInput(TmsFault):
    """The *request* we were about to send is not something we are willing to send.

    Raised by :func:`encode_request` before a socket is opened. It is kept
    separate from :class:`TmsMalformed` (which describes a bad *response*) so
    that the retry policy never treats a local validation bug as a transient
    network fault and hammers the server with the same broken frame.
    """


class TmsTimeout(TmsFault):
    """No usable bytes arrived before the deadline, or the connection failed."""


class TmsPartialResponse(TmsFault):
    """The server stopped talking before ``END``/``ERR``."""


class TmsMalformed(TmsFault):
    """Framing or field violations: bad delimiter, no colon, non-ASCII, oversized."""


class TmsError(TmsFault):
    """The server answered with ``ERR|CODE:<code>``.

    This is a well-formed answer from a working server: the request was
    understood and rejected. ``code`` is what the retry policy inspects.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"TMS error {code}: {message}" if message else f"TMS error {code}")


# --- Encoding -----------------------------------------------------------


def _clean_name(name: str) -> str:
    """Normalise and validate a field name."""
    text = str(name).strip().upper()
    if not _NAME_RE.match(text):
        raise TmsInvalidInput(f"invalid field name {name!r}")
    return text


def _clean_value(name: str, value: object) -> str:
    """Trim and validate a field value.

    Error messages name the field but never echo the value: the same code path
    carries the auth token, and a traceback must not become a leak.
    """
    text = str(value).strip()
    if not text.isascii():
        raise TmsInvalidInput(f"field {name} contains non-ASCII characters")
    if any(char in text for char in FORBIDDEN_IN_VALUE):
        raise TmsInvalidInput(f"field {name} contains a reserved character")
    return text


def encode_request(
    command: str,
    auth_token: str,
    fields: Mapping[str, object] | None = None,
) -> bytes:
    """Build one request frame.

    ``CMD`` comes first and ``AUTH`` second, as the manual requires; the
    remaining fields keep the order the caller passed them in.

    Raises :class:`TmsInvalidInput` if any part of the frame would violate the
    protocol. Nothing is sent in that case.
    """
    cmd = _clean_name(command)
    token = _clean_value("AUTH", auth_token)
    if not token:
        raise TmsInvalidInput("AUTH token is empty")

    parts = [f"CMD:{cmd}", f"AUTH:{token}"]
    for name, value in (fields or {}).items():
        parts.append(f"{_clean_name(name)}:{_clean_value(name, value)}")

    frame = (FIELD_SEPARATOR.join(parts) + LINE_TERMINATOR.decode("ascii")).encode("ascii")
    if len(frame) > MAX_FRAME_BYTES:
        raise TmsInvalidInput(
            f"request frame is {len(frame)} bytes, limit is {MAX_FRAME_BYTES}"
        )
    return frame


def redact(text: str) -> str:
    """Replace the auth token in a request line or frame so it can be logged."""
    return re.sub(r"AUTH:[^|\r\n]*", "AUTH:<redacted>", str(text))


# --- Parsing ------------------------------------------------------------


def parse_record(line: str) -> dict[str, str]:
    """Parse one record line into a dictionary.

    Splits on ``|``, then on the **first** ``:`` of each token, and trims keys
    and values. Unknown fields are kept: the server may add columns, and a
    record is never parsed by position.

    A token without a colon, or with an empty key, is a framing violation.
    """
    record: dict[str, str] = {}
    for token in line.split(FIELD_SEPARATOR):
        key, separator, value = token.partition(KEY_VALUE_SEPARATOR)
        if not separator:
            raise TmsMalformed(f"record token without a separator: {token.strip()!r}")
        key = key.strip().upper()
        if not key:
            raise TmsMalformed("record token with an empty field name")
        record[key] = value.strip()
    if not record:
        raise TmsMalformed("empty record line")
    return record


def parse_error_line(line: str) -> tuple[str, str]:
    """Parse ``ERR|CODE:<code>|MSG:<msg>`` into ``(code, message)``."""
    tokens = line.strip().split(FIELD_SEPARATOR)
    if tokens[0].strip().upper() != "ERR":
        raise TmsMalformed(f"not an error line: {line.strip()!r}")

    fields: dict[str, str] = {}
    for token in tokens[1:]:
        key, separator, value = token.partition(KEY_VALUE_SEPARATOR)
        if not separator:
            raise TmsMalformed(f"error token without a separator: {token.strip()!r}")
        fields[key.strip().upper()] = value.strip()

    code = fields.get("CODE")
    if not code:
        raise TmsMalformed("error line without a CODE field")
    return code, fields.get("MSG", "")


def ends_response(line: str) -> bool:
    """True if this line terminates a response (``END`` or an ``ERR|`` line)."""
    stripped = line.strip()
    return stripped == "END" or stripped.startswith(f"ERR{FIELD_SEPARATOR}")


def parse_response(lines: Iterable[str]) -> list[dict[str, str]]:
    """Parse a complete response into records.

    ``lines`` must include the terminating ``END``. Blank lines are ignored as
    noise. Raises :class:`TmsError` for ``ERR|`` responses and
    :class:`TmsPartialResponse` if the stream ends without a terminator, so a
    truncated search can never be reported as "fewer loads available".
    """
    records: list[dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "END":
            return records
        if stripped.startswith(f"ERR{FIELD_SEPARATOR}"):
            code, message = parse_error_line(stripped)
            raise TmsError(code, message)
        records.append(parse_record(stripped))
    raise TmsPartialResponse("response ended without END")


# --- Value conversion ---------------------------------------------------


def to_int(field_name: str, value: str) -> int:
    """Convert a numeric field, tolerating zero-padding and space-padding.

    ``int()`` already ignores surrounding whitespace and leading zeros, which
    is exactly what the two observed formats need (``RATE:2073    `` from the
    live server, ``RATE:0002150`` in the manual).
    """
    try:
        return int(value.strip())
    except (TypeError, ValueError) as exc:
        raise TmsMalformed(f"field {field_name} is not a number: {value.strip()!r}") from exc


def parse_timestamp(field_name: str, value: str) -> datetime:
    """Parse ``YYYYMMDDHHMMSS`` into a timezone-aware UTC datetime."""
    text = value.strip()
    try:
        naive = datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise TmsMalformed(f"field {field_name} is not a YYYYMMDDHHMMSS timestamp: {text!r}") from exc
    return naive.replace(tzinfo=timezone.utc)


def format_timestamp(moment: datetime) -> str:
    """Render a datetime as the protocol's ``YYYYMMDDHHMMSS`` (UTC)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime(TIMESTAMP_FORMAT)
