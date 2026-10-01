# Walkthrough — how a request travels through the TMS adapter

Written to be read out loud. Each section follows one step further in, in the same order a
real call takes. Phase 1 only: the TMS adapter. There is no HTTP API yet.

The whole adapter is four small files under `app/`, plus a fake server in `tests/`.

```
settings ─▶ TmsClient.query_loads ─▶ encode_request ─▶ socket ─▶ _read_response
                                                            │
              Load ◀─ from_record ◀─ parse_response ◀─ parse_record ◀─ lines
```

---

## 1. `app/config.py` — where the connection details come from

Nothing in the repository hardcodes a host, a port or a token. `Settings` reads them from
the environment, falling back to `.env` during development, and the field names match the
variables documented in `.env.example` (`TMS_HOST`, `TMS_PORT`, `TMS_TOKEN`, ...).

Two details worth knowing:

- The token is a `SecretStr`. Its `repr` is `**********`, so a stray `print(settings)` or a
  logged traceback cannot leak it. The plain value only appears when code explicitly calls
  `settings.tms_token_value`.
- Defaults are the values the brief commits to (3 s connect, 5 s read deadline, 3 attempts),
  so a missing variable degrades to the documented behaviour rather than crashing.

---

## 2. `TmsClient.query_loads(...)` — the front door

```python
client.query_loads(ORIG_STATE="GA", max_results=5)
```

Filter names are case-insensitive. Before anything else, the method checks that every name
it was given is a real filter and that at least one was supplied. An unknown field name is
rejected **locally**, which matters because the server silently discards fields it does not
recognise — a typo there looks exactly like "no filter applied", and the caller would never
find out.

A `datetime` passed as `PICKUP_DT` is rendered as `YYYYMMDDHHMMSS`; anything else goes to
the wire as a string.

---

## 3. `protocol.encode_request` — a call becomes bytes

Builds `CMD:<command>|AUTH:<token>|<FIELD>:<VALUE>|...\r\n`, with `CMD` first and `AUTH`
second as the protocol requires.

This is the security boundary. Every field name and value is checked first:

- no `|`, `\r` or `\n` — any of them could reshape the request line or smuggle a second
  request into one write;
- ASCII only;
- field names must look like field names (`^[A-Z][A-Z0-9_]*$`), because they are
  interpolated into a position the server parses structurally;
- the finished frame must fit in 4096 bytes.

Anything that fails raises `TmsInvalidInput` before a socket exists, and **the error message
never contains the value** — the same code path carries the auth token, and a traceback must
not become a leak. `redact()` exists for the opposite direction: it rewrites `AUTH:<token>`
to `AUTH:<redacted>` so a request line can be logged or printed safely.

---

## 4. `TmsClient._one_attempt` — the socket

One connection per request; the server does not support reuse, so there is no pool to get
wrong. Connect timeout 3 s, then `sendall`, then read.

Connection failures (refused, unreachable, reset) are reclassified as `TmsTimeout`. From
the caller's point of view they are the same event — the TMS did not answer usefully — and
they should follow the same retry policy.

---

## 5. `TmsClient._read_response` — reading until `END`

Reads in chunks and splits them into lines, stopping the moment a line is `END` or starts
with `ERR|`. Three things here are deliberate:

- **We close the socket ourselves** as soon as we see the terminator. The server sometimes
  holds the connection open after a complete response ("delayed termination"); waiting for
  its FIN would burn the entire deadline on a perfectly good answer.
- **The last line is not always newline-terminated.** The recorded server ends with `END`
  and no `\r\n`. If the leftover buffer trims to `END` (or starts with `ERR|`), it is
  treated as the terminator instead of being waited on.
- **A single unterminated line longer than 4096 bytes is malformed**, not a slow server.
  Without that cap a framing fault would sit until the deadline and be reported as a
  timeout — the wrong diagnosis.

When the deadline passes, what arrived decides the classification: nothing at all is
`TmsTimeout`; anything at all is `TmsPartialResponse`.

---

## 6. `protocol.parse_response` and `parse_record` — lines become dictionaries

`parse_response` walks the lines and returns records, with two hard rules:

- an `ERR|CODE:<code>|MSG:<msg>` line raises `TmsError(code)`;
- reaching the end without `END` raises `TmsPartialResponse`.

That second rule is the important one. A truncated search must never come back as "fewer
loads available" — the carrier would be told there is no freight when there is.

`parse_record` splits on `|`, splits each token on the **first** `:` (so a note may contain
colons), trims keys and values, upper-cases the field names, and keeps fields it does not
recognise. `RATE:2073    ` and `RATE:0002150` both become `2073` through `int()`, which
already ignores both kinds of padding.

---

## 7. `models.Load.from_record` — dictionaries become objects

The only place that knows which fields are required, which are numbers and which are
timestamps. A record missing `RATE`, or carrying `RATE: N/A`, raises `TmsMalformed` rather
than defaulting: a load without a rate cannot be pitched, and guessing one would put an
invented number in front of a carrier.

ZIP codes stay strings on purpose — `00123` must not become `123`.

Fields the server sends that we do not model yet are kept in `extra`, so a new column shows
up in the payload instead of vanishing.

---

## 8. `LoadDetail.public_dict` — what the agent is allowed to see

`LOAD_GET` returns `MAX_BUY`. That value never leaves the server (principle 1), and the
guarantee lives here rather than at each call site: `public_dict()` drops the modelled
`max_buy` field *and* any unknown field spelled `MAX_BUY` in any case. A new endpoint that
serialises a load with `public_dict()` is safe by default.

`max_buy` is also marked `repr=False`, so an accidental log of the object does not print it.

---

## 9. `TmsClient._execute` — the retry policy, wrapped around all of the above

Every attempt is timed and logged with the command, the attempt number, the fault class and
the latency — never the token, never `MAX_BUY`. The decision to retry lives in one function,
`_is_retryable`:

| Fault | Retried? |
|---|---|
| `TmsTimeout`, `TmsPartialResponse`, `TmsMalformed` | yes, for idempotent commands |
| `TmsError(SERVER_ERROR)`, or an error code we do not know | yes |
| `TmsError(AUTH_FAILED / MISSING_FIELD / UNKNOWN_LOAD / UNKNOWN_CMD ...)` | no |
| `TmsInvalidInput` | no — it is our bug, and retrying would repeat it |

Backoff is ~0.3 s, 0.8 s, 1.5 s with ±20% jitter, so a hundred simultaneous calls do not
retry in lockstep. The `sleep` and random source are injectable, which is how the tests run
in milliseconds instead of seconds.

---

## 10. `TmsClient.book_load` — the one command that is not idempotent

`LOAD_BOOK` is sent with retries **off**. If the attempt dies in transport, `book_load` does
not guess — it calls `LOAD_GET` and reads the load's status:

- load no longer `OPEN` → our booking landed. Return `booked_unconfirmed` with no reference,
  for a senior rep to confirm.
- load still `OPEN` → the attempt did not land. Retry `LOAD_BOOK` **once**, never in a loop.
- `ALREADY_BOOKED`, or a second ambiguous outcome → `booked_unconfirmed` and stop.

A clean rejection (`INVALID_RATE`) is re-raised immediately: it is an answer, not an
accident. Success is only success when it carries a `BOOKING_REF`; a `STATUS:BOOKED` with no
reference is downgraded to `booked_unconfirmed` rather than reported as confirmed.

If the recovery `LOAD_GET` itself fails, the exception propagates. We have no evidence at
all, so we report a failure instead of inventing certainty — see DECISIONS.md §11.

---

## 11. `tests/fake_tms.py` — the server the tests talk to

A real TCP server on an ephemeral local port that replays the recorded fixtures with the
padding the live server actually uses. It exists because the risky code is the fault
handling, and the live server will not produce faults on demand — while a test suite aimed
at the live host would permanently consume demo loads on its first `LOAD_BOOK`.

Its modes are `normal`, `timeout` (read the request, never answer), `partial` (a valid
prefix, then close without `END`), `malformed` (a token with no colon), and `delayed` (a
complete response, then hold the connection open). It can also return `ALREADY_BOOKED` and
`INVALID_RATE`, and — the subtle one — simulate a booking whose *response* was lost but
whose *write* landed, which is what the ambiguous-booking tests turn on.

It records every request it receives, with `AUTH` replaced by `<redacted>`, so tests can
assert on what was sent without a token ever reaching a file.

---

## 12. `scripts/tms_smoke.py` — the only thing that touches the live server

Runs read-only probes and prints a report: one `LOAD_QUERY`, one `LOAD_GET` on a load that
currently exists, `EQTYPE:FLATBED`, a `PICKUP_DT` filter probe, and `MAX_RESULTS:50` to find
the server's real ceiling.

The `PICKUP_DT` probe is the interesting one. Because unknown fields are silently discarded,
a filter name that does not exist returns exactly what no filter would. So the script asks
twice — once with an impossible date and once with a date known to be in the data — and
compares against the baseline. Same count on both means the field is being ignored and the
name is wrong.

A `guard()` function refuses any command except `DEBUG_ECHO`, `LOAD_QUERY` and `LOAD_GET`,
so no future edit can make this script book a load by accident.
