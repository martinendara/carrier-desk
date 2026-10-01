# Design decisions

One entry per non-obvious choice: problem → choice → rejected alternative → why.
Phase 1 (TMS adapter) only.

---

## 1. Parse records by field name, never by position or width

**Problem.** The manual and the live server disagree about padding: the manual shows
`LOAD_ID:LD0000045821` and `RATE:0002150`; the live server returns `LOAD_ID:LD00896     `
and `RATE:2073    `. A parser built on fixed offsets would read the right answer in the
lab and garbage in production.

**Choice.** Split every line on `|`, then each token on the **first** `:`, and trim the key
and the value. Numeric fields go through `int()`, which ignores both leading zeros and
trailing spaces. Unknown fields are kept in a dictionary rather than dropped.

**Rejected.** Fixed-width slicing, mirroring the manual's column layout. It is what the
manual implies and it would break the first time the server changes a width — which has
already happened once.

**Why.** The wire format's only stable property is the `KEY:VALUE` shape. Everything else
is presentation.

---

## 2. Timestamps from the TMS are UTC

**Problem.** `PICKUP_DT:20261006221800` carries no zone. Treating it as local time would
silently shift every pickup by the offset of whatever machine runs the service — and this
service will run in a US region while the carrier is somewhere else again.

**Choice.** Parse `YYYYMMDDHHMMSS` and attach `timezone.utc`. Render outgoing timestamps in
UTC too. The value is never converted to local time anywhere in the pipeline.

**Rejected.** Naive datetimes, or assuming the server's local zone. Both defer the bug to
the booking step, where it becomes a carrier at the wrong dock at the wrong hour.

**Why.** `TIMESTAMP:20261001120000` on `LOAD_BOOK` is described in the manual as "UTC".
Using the same assumption for every timestamp keeps one rule instead of two.
**Open:** not yet confirmed against the live server — the smoke script does not prove it,
and the observation is recorded in `docs/tms-protocol.md`.

---

## 3. A 5-second read deadline against a 30-second idle timeout

**Problem.** The server closes idle connections after 30 s. Waiting that long is the safe
choice for a batch job and the wrong one for a voice agent: the carrier is holding a phone
to their ear while the agent says nothing.

**Choice.** Total read deadline of 5 s (configurable via `TMS_READ_DEADLINE_SECONDS`),
connect timeout of 3 s. Exceeding either is classified as a fault and handled by the retry
policy, not by waiting longer.

**Rejected.** Waiting for the server's own 30 s timeout, which would make a single dead
call eat half a minute of conversation.

**Why.** Conversation latency is the product. A retry against a healthy server costs
~300 ms; silence costs the call.

---

## 4. Retries only on idempotent commands, and only for certain failures

**Problem.** Not every failure deserves another request. Resending a request the server
already rejected wastes the caller's time; resending a booking can double-book a load.

**Choice.** `LOAD_QUERY` and `LOAD_GET` retry up to 3 attempts with jittered backoff
(~0.3 s, 0.8 s, 1.5 s) on `TmsTimeout`, `TmsPartialResponse`, `TmsMalformed`,
`TmsError(SERVER_ERROR)` and on **unknown** error codes. `AUTH_FAILED`, `MISSING_FIELD`,
`UNKNOWN_LOAD`, `UNKNOWN_CMD`, `ALREADY_BOOKED`, `INVALID_RATE` and `MALFORMED` are never
retried: they are answers, not accidents.

**Rejected.** Retrying everything, or retrying nothing. Retrying everything hammers a
server that has already given its verdict; retrying nothing turns one dropped packet into
a failed call.

**Why.** Unknown codes are treated as transient on purpose: an undocumented failure is
more likely to be a server hiccup than a request we got wrong, and the budget is only
three attempts. The asymmetry is deliberate — a wasted retry is cheap, a wrong "no" to a
carrier is not.

---

## 5. `LOAD_BOOK` is not idempotent, so it is never retried blindly

**Problem.** If the booking response is lost in transport, we cannot tell whether the load
was booked. Retrying may book twice; not retrying may abandon a load the carrier believes
is theirs.

**Choice.** On a transport fault, don't guess — ask. Call `LOAD_GET` and read `STATUS`:
- Load no longer `OPEN` → the booking landed. Return `status: booked_unconfirmed`,
  `booking_ref: null`, for a senior rep to confirm.
- Load still `OPEN` → the attempt did not land. Retry `LOAD_BOOK` **once**.
- A second ambiguous outcome, or `ALREADY_BOOKED`, → `booked_unconfirmed` and stop.

**Rejected.** A blind retry (risks a double booking), and giving up immediately (abandons
a load we may own). Also rejected: inventing a booking reference we never received.

**Why.** Bookings are keyed to our token and monotonic, so "no longer OPEN" is decisive
evidence that *our* call landed. When evidence is missing, the answer goes to a human
rather than being fabricated — that is what `booked_unconfirmed` exists for.

---

## 6. Local input validation raises its own exception, `TmsInvalidInput`

**Problem.** Section 4 of the brief lists framing violations under `TmsMalformed`, and
`TmsMalformed` is retryable. But a value containing `|` is *our* bug, not a network
hiccup — retrying sends the same broken frame two more times.

**Choice.** `encode_request` raises `TmsInvalidInput` (a sibling of `TmsMalformed` under
the shared `TmsFault` base) before any socket is opened. The retry policy treats it as
terminal, and `query_loads`/`get_load` reject bad filters and ids before dialing at all.

**Rejected.** Reusing `TmsMalformed` for both and relying on the caller to notice. One
exception name for two opposite situations is how a validation bug turns into three
requests and a confusing error.

**Why.** Failures need to be classified by what the caller should *do*, and "fix your
input" and "try again" are different actions. The four names in the brief (plus this one)
are preserved exactly.

---

## 7. A response without `END` is a failure — including the unterminated last line

**Problem.** Rule 4: a truncated search must never be presented as "fewer loads
available". Separately, the recorded server does not always put a `\r\n` after `END`, so a
strict line reader can wait forever on a response that is already complete.

**Choice.** `parse_response` raises `TmsPartialResponse` if the stream ends without a
terminator. The reader treats a leftover buffer that trims to exactly `END` or starts with
`ERR|` as complete. A single line longer than 4096 bytes with no terminator is
`TmsMalformed`, not a timeout — otherwise a framing fault would be misreported as a slow
server.

**Rejected.** Accepting whatever arrived ("best effort"), and waiting for the connection
to close. Best-effort turns a partial answer into a wrong answer; waiting costs the whole
deadline on a response we already have.

**Why.** One rule with no exceptions is easier to defend than a heuristic about when a
short result is real.

---

## 8. A deadline with partial data is a partial response, not a timeout

**Problem.** "No bytes before the deadline" and "some bytes, then silence" are both
deadlines passing, but they mean different things: the first is a server that never
answered, the second is a server that died mid-answer (or is holding the connection open
while we wait for an `END` that is never coming).

**Choice.** Classify by what arrived: nothing → `TmsTimeout`; anything → `TmsPartialResponse`.

**Rejected.** Calling both a timeout because they share a symptom.

**Why.** The distinction is the one a human debugging a call will ask about first, and it
costs nothing to record it. Both are retryable, so no behaviour depends on the difference.

---

## 9. `MAX_BUY` is excluded at the model layer, not at the call sites

**Problem.** Principle 1: the agent must never see `MAX_BUY`. A rule that lives only in
the HTTP layer is one refactor away from leaking.

**Choice.** `LoadDetail.max_buy` exists for the negotiation engine, is marked
`repr=False` so it stays out of casual logs, and `public_dict()` strips it — along with
any unknown field spelled `MAX_BUY` in any case.

**Rejected.** Filtering the field in each serializer. Every new endpoint is a new chance
to forget.

**Why.** The invariant is enforced where the data is defined, so a new caller is safe by
default rather than by discipline. A test fails if it ever regresses.

---

## 10. Tests run against a local fake server, never the live one

**Problem.** Every successful `LOAD_BOOK` permanently consumes a load for our token. A
test suite pointed at the real host would destroy the demo data on its first run, silently.

**Choice.** `tests/fake_tms.py` replays the recorded fixtures over a real TCP socket on an
ephemeral port, with modes for each fault class (`normal`, `timeout`, `partial`,
`malformed`, `delayed`) and the `ALREADY_BOOKED` / `INVALID_RATE` rejections. The live
server is touched only by `scripts/tms_smoke.py`, which refuses any command other than
`DEBUG_ECHO`, `LOAD_QUERY` and `LOAD_GET`.

**Rejected.** Mocking the socket, or pointing tests at the live host with a read-only
convention. Mocking tests our assumptions about the socket rather than the socket; a
convention is not a safeguard.

**Why.** The fault modes are the risky part of the code, and they are exactly what the
live server will not produce on demand. A real socket on localhost exercises the deadlines
and the framing for real, at zero cost to the live data.

---

## 11. Bookings that cannot be resolved raise; they are not invented

**Problem.** During ambiguous-booking recovery, the follow-up `LOAD_GET` can itself fail.
We then know neither whether the load is booked nor whether it is still available.

**Choice.** Let that exception propagate. The caller reports a failure; the human
follow-up happens through the error path.

**Rejected.** Returning `booked_unconfirmed` when we could not verify anything.

**Why.** `booked_unconfirmed` means "this load left the board and we believe it was us",
and a rep acts on that. If `LOAD_GET` is down we have no evidence at all, and reporting
false certainty is worse than reporting a failure. It is also safe to re-drive: a second
booking attempt on a consumed load returns `ALREADY_BOOKED`, which lands in
`booked_unconfirmed`.
