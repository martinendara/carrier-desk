# Design decisions

One entry per non-obvious choice: problem → choice → rejected alternative → why.

- Entries 1–11: phase 1, the TMS adapter.
- Entries 12+: phase 2, carrier verification, OTP, negotiation, onboarding and events.

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

---

# Phase 2 — carrier verification, OTP, negotiation, onboarding, events

---

## 12. The call is the unit of state, keyed by `run_id`

**Problem.** The first draft had the agent carry an `otp_session_id` between tools and a
`negotiation_id` into every counter. Anything the agent carries, the agent can drop, replay
or invent — and a language model will eventually do all three.

**Choice.** The platform injects a `run_id` into every tool, and the server keeps everything
per call: the OTP state, the loads shown, the active negotiation and the round counter. No
tool takes an id that the agent could have made up.

**Rejected.** Passing ids through the conversation and trusting them. It also makes every
prompt longer and every failure mode weirder.

**Why.** The agent's job is to talk. State that the agent cannot address is state the agent
cannot corrupt.

---

## 13. A load can only be negotiated if it was shown during that call

**Problem.** A `load_id` is a short, guessable string (`LD00896`). Without a check, whatever
is driving the conversation can name any load in the system and start negotiating on it —
including one that belongs to another carrier's call.

**Choice.** The session records every load pitched to the carrier. `open` refuses anything
else with `LoadNotShown`, even if the load exists and is open.

**Rejected.** Validating only that the load exists and is `OPEN`. That checks the load, not
the conversation.

**Why.** The rule is not about the load's validity, it is about who is entitled to it. The
carrier on the phone was pitched a specific set of loads, and that set is the whole universe
of what this call may book.

---

## 14. At most two negotiations per call

**Problem.** A call that can open negotiations without limit is a call that can shop for the
best load across an entire database at the brokerage's expense, one pitch at a time.

**Choice.** Two: the original load plus one alternative. The third raises
`NegotiationLimitReached`.

**Rejected.** One (too rigid — a carrier who genuinely dislikes the first load should hear
one more) and unlimited (no bound at all).

**Why.** Two is the smallest number that still covers the real conversation the voice agent
is having: pitch, and if that fails, one more.

---

## 15. OTP goes to the contact on file, by email, and nowhere else

**Problem.** The caller dictates everything else in the conversation. If the destination for
the code came from them, the check would verify that whoever is on the phone can read the
message they asked to receive — which is no check at all.

**Choice.** `carriers/directory.py` is the only source of a destination. The POC uses email
because no SMS provider is wired up. The agent is told only the masked address
(`m***@gmail.com`), which is enough to say where the code went and not enough to learn the
address of a carrier you are not.

**Rejected.** Taking a destination from the caller, and returning the full address to the
agent.

**Why.** A verification step that the untrusted party controls is theatre.

---

## 16. Codes are stored as HMAC-SHA256, not as a hash

**Problem.** A 6-digit code is a million possibilities. An unsalted SHA-256 of one is a
table away from being read, and one leaked table would expose every live code at once.

**Choice.** HMAC-SHA256 keyed with a server secret, with the `run_id` mixed into the message.
The service refuses to start with a secret shorter than 16 bytes.

**Rejected.** A plain digest, or storing the code itself. Also rejected: a per-code random
salt — it protects a stolen table but not a live guess, which is the actual threat here.

**Why.** Keying with a secret means a stolen table is worthless, and binding the `run_id`
means a digest captured in one call cannot be replayed in another. Refusing a short secret
is the difference between a control and the appearance of one.

---

## 17. A resend does not refund attempts

**Problem.** "3 attempts, 1 resend" has two readings: the resend gives a fresh code with a
fresh budget (up to six guesses), or the budget is per call (always three).

**Choice.** Three guesses per call, no matter how many codes were issued. A locked session
cannot be resent at all.

**Rejected.** Resetting the counter on resend, which doubles the guessing budget and, worse,
makes the lock removable by asking for a new code — exactly what the lock exists to prevent.

**Why.** The lock is the security control. Anything that lifts it turns three guesses into
six, and six into as many as you can ask for.

---

## 18. The OTP response has two views, and the service documents which is which

**Problem.** The platform's delivery node needs the plaintext code; the agent must never see
it. They are reachable from the same call, so a single flat response puts the code one
prompt-injection away from the conversation.

**Choice.** `OtpDispatch` exposes `delivery_view` (destination and code) and `agent_view`
(masked destination only) as separate properties. The code field is `repr=False`, so a stray
log of the object does not print it, and an event never carries it.

**Rejected.** Returning one dictionary and documenting "the platform should not pass the code
to the agent". A convention is not a boundary.

**Why.** The separation is the platform's job to act on, but the server decides the shape it
is asked to act on. Two named fields is a shape that cannot be merged by accident.

---

## 19. An MC with no contact on file gets no OTP and becomes a lead

**Problem.** A carrier with no contact recorded still needs somewhere to go. Sending a code
to a guessed address, or skipping verification and booking anyway, are both worse than
admitting we cannot verify them.

**Choice.** No contact means no code is generated. The call returns `routed_onboarding`, and
the lead is qualified (Hot / Warm / Review) for a human to follow up.

**Rejected.** Falling back to the number the caller gives, and failing the call outright.

**Why.** The onboarding path turns an unverifiable caller into something a human can work
with, instead of a dead end or a security hole.

---

## 20. The opening offer is also capped at `C − 25`

**Problem.** The brief defines the opening as `floor25(RATE)` when `RATE < C`, and separately
states "never offer more than `C − 25`". Those conflict when the posted rate sits within one
increment of the ceiling: `floor25(2526)` is 2525, which is above `2528 − 25`.

**Choice.** Apply the cap to the opening as well. It only binds in that pathological case —
a posted rate within $25 of the private ceiling — and capping is the safe reading.

**Rejected.** Reading the margin rule as applying only to the concession steps, which leaves
the first number the carrier hears above the margin the rule exists to protect.

**Why.** One rule that always holds beats two rules with an exception between them. The cost
is a slightly lower opening on data that should not exist in the first place.

---

## 21. A `close` reports the last offer, never the ceiling

**Problem.** The response shape is `{action, amount}`, including for `close`. The natural
value to put there is the ceiling — the most we would have paid — and that would hand the
agent the one number the whole design hides.

**Choice.** `close` returns the last offer made, which is always at least one increment below
`C` by construction.

**Rejected.** Returning the ceiling, and returning `0`. Zero leaves the agent with nothing to
say; the last offer is at least true and at least safe.

**Why.** It is tempting to be "helpful" at the moment a deal fails. That is precisely when
the ceiling must not slip out.

---

## 22. Accepting the carrier's own ask can equal the ceiling, and that is allowed

**Problem.** The last round accepts any request at or below `C`. A carrier who asks for
exactly `C` gets it, so the agent hears a number equal to the ceiling.

**Choice.** Accept it, and say so plainly here rather than pretend the invariant is absolute.
The agent learns the *rate that was agreed*, which is a legitimate business fact and is the
carrier's own number coming back. What it still cannot learn is that this number is a
ceiling — that we would have gone no higher.

**Rejected.** Clamping the final acceptance below `C` to keep the invariant airtight, which
would refuse the carrier a deal the brief says to take.

**Why.** Naming the exception is better than a test that quietly avoids the case. The
guarantee is "the agent is never told where the limit is", not "no number the agent sees ever
equals it".

---

## 23. Money is computed with exact rationals, not floats

**Problem.** The concession steps are decimals (0.40, 0.60, 0.90). Binary floating point
cannot represent them exactly — 0.6 is stored as 0.599999999999999978 — and the product with
the gap can land a hair below a multiple of 25, where `floor25` would drop a whole increment.

**Choice.** Parse the steps into `Fraction` and do all the arithmetic exactly. A step is 2/5
of the gap, not the nearest double to it.

**Rejected.** Floats, with a tolerance, or rounding at the end. A tolerance is a guess about
which cases need it.

**Why.** This decides what the brokerage pays. It should not depend on how a particular gap
rounds in binary.

---

## 24. No `MAX_BUY` means no negotiation

**Problem.** The server only returns `MAX_BUY` for flagged tokens. If it is ever absent, we
have a load we can pitch and no idea what we may pay for it.

**Choice.** `CeilingUnavailable`, and the load goes to a human. The same applies to a ceiling
too small to hold one increment under it.

**Rejected.** Falling back to `RATE` as the ceiling (which would mean a negotiation with no
room), or to some multiple of it (which would mean inventing the brokerage's limit).

**Why.** Every number the engine produces is derived from the ceiling. Without one there is
no defensible offer to make, and a made-up ceiling is worse than a missed load.

---

## 25. Qualification is a rule, and unknown authority age is Review

**Problem.** Deciding whether a carrier we could not sell to is worth a human's time is a
judgement call, and judgement calls are what language models are for — except that this one
has to give the same answer twice.

**Choice.** A pure function, evaluated in the documented order, with `Review` short-circuiting
first. An authority age we could not read is treated as unqualified, not as old enough.

**Rejected.** An LLM call, and treating a missing date as "probably fine".

**Why.** Same inputs, same answer, and the answer is explainable to the rep who receives the
lead. The safe direction for an unknown is to ask a human, and a brand-new authority is
exactly who the 90-day rule is protecting against.

---

## 26. Events are a separate store from the conversation, and may contain the ceiling

**Problem.** The audit trail wants the numbers the agent must never see. Putting both
concerns in one payload means either leaking or losing the detail.

**Choice.** The service records what it decided; the platform records what was said.
`EventSink` has an in-memory and a JSONL implementation, and `{run_id, ts, type, payload}`
events carry `ceiling` and `gap` freely. A test asserts no agent-facing response ever
contains a ceiling value.

**Rejected.** A single log that serves both, and stripping the numbers from the events out of
caution.

**Why.** The two have different audiences and different lifetimes; the conversation is the
platform's to keep. Twin is deferred until its authentication is confirmed, rather than
guessing a shape into every call site.

---

## 27. FMCSA fails closed on a missing field, not just on an outage

**Problem.** The brief says fail closed on an outage. It says nothing about a 200 response
that is missing `allowedToOperate` or `statusCode` — and with no real response recorded, that
is the *likely* case, because our key names are guesses.

**Choice.** A response whose signals we cannot read is `incomplete_response`: not eligible.
A missing authority *date* is milder — the carrier is still eligible, but the age is `None`,
which routes the lead to `Review`.

**Rejected.** Treating unreadable flags as permissive, which would make the whole check
decorative the moment a key name turns out to be wrong.

**Why.** The failure mode of the strict version is visible and recoverable — carriers land in
a human's queue with a logged reason. The failure mode of the permissive version is booking
an out-of-service carrier because a field was spelled differently than we guessed.

---

## 28. The run-session store is in-memory and single-instance

**Problem.** Session state is per-process. Two workers behind a load balancer would each hold
half the calls, and a tool call that landed on the wrong one would look like a call that had
never started.

**Choice.** One process, one dictionary, with a TTL. Documented as a POC constraint, with
`RunSessionStore` as the interface Redis will implement.

**Rejected.** Reaching for Redis now, before there is a second worker to justify it.

**Why.** A POC that is honest about its single-instance assumption is safer than one that
implies horizontal scalability it does not have. The TTL is monotonic-clock based, so a
wall-clock jump cannot expire a live call.

---

# Phase 3 — the HTTP API

---

## 29. The verification gate is an explicit call, guarded by a test that reads the route table

**Problem.** Search, negotiation, booking and detail must refuse an unverified call. The
tidy FastAPI way is a dependency, but every gated route takes a different request body, so
a dependency would have to re-declare it — and FastAPI would then see the body as two
parameters. The other tidy way, a decorator per route, is a check that has to be
remembered.

**Choice.** Each gated handler calls `require_verified(session)` explicitly, and
`test_api.py` walks the app's own route table: every route that is not on the short
"works before verification" list must answer 403 to an unverified call, and any route not
listed in the test's body map fails it.

**Rejected.** A dependency (fights the body model), a decorator (forgettable), and a
manual list of paths to check (stale the first time someone adds a route).

**Why.** The guarantee should not rest on someone remembering. Reading the route table
means a new endpoint is gated by default — the test fails until its author either gates it
or adds it to the exception list with a reason.

---

## 30. Load detail is gated too, though §8 does not say so

**Problem.** CLAUDE.md §8 lists search, negotiation and booking as requiring a verified
OTP. `GET /loads/{load_id}` is not on that list.

**Choice.** Gate it anyway.

**Rejected.** Following the list literally, which would let an unverified caller pull
origin, destination and rate for any load id it can guess.

**Why.** Pitching freight to an unverified caller is the thing verification exists to
prevent, and a detail fetch is the pitch. The stricter reading costs one 403 on a path
that has no other purpose.

---

## 31. Fetching a load's detail does not count as showing it

**Problem.** The natural convenience is to record a load as "shown" whenever its detail is
fetched. That would make the flow forgiving: an agent could name any load id, get the
detail, and negotiate.

**Choice.** Only `/loads/search` records shown loads. Detail is a read.

**Rejected.** Recording on both, which collapses "you may only negotiate what you were
shown" into "you may negotiate anything you can name" — and load ids are short and
guessable (`LD00896`).

**Why.** The rule is about what the carrier on the phone was actually offered. Search is
the moment that happens; a lookup afterwards is not a new offer.

---

## 32. The OTP response has two named views, and the server decides the shape

**Problem.** The platform needs the plaintext code for its delivery node; the agent must
never see it. One flat response puts the code one mis-mapped field away from the
conversation.

**Choice.** `delivery: {destination, code}` and `masked_destination`, as separate fields
with descriptions that say which is which. The spoken line never contains either the code
or the full address, and a test asserts both.

**Rejected.** One dictionary plus documentation, and putting the code behind a separate
endpoint the agent could also call.

**Why.** The platform does the mapping (into a hidden Tool Call Result field, which stays
available to later nodes without entering the agent's context). The server's job is to
hand over a shape that cannot be merged by accident.

---

## 33. `TMS_MODE=fake` runs the real client against an in-process server

**Problem.** Platform workflow tests fire real webhooks, so development has to run a whole
call without touching the live TMS — where every successful booking permanently consumes a
load.

**Choice.** `TMS_MODE=fake` starts the fixture-replaying TCP server from
`app/tms/fake_server.py` on a loopback port, and the ordinary `TmsClient` connects to it.
Same socket code, same deadlines, same framing, same retry policy. Only the data is canned.

**Rejected.** A stub client that returns objects directly — it would skip exactly the code
that is riskiest.

**Why.** Swapping the data source by configuration, not by code, is what makes the switch
safe to flip in a demo. The fake also synthesises `LOAD_GET` records for the loads the
fixture only captured as `LOAD_QUERY` lines, because otherwise you could search and not
negotiate, which is the only thing the fake is for.

---

## 34. One time budget per HTTP request, and running out of it is reported as such

**Problem.** The platform's webhook timeout is undocumented, and a voice tool that hangs is
a dropped call. The per-attempt read deadline does not bound anything on its own: three
attempts with backoff can take longer than the whole request should.

**Choice.** A `Deadline` created once per request (default 8 s), threaded into every TMS
call the request makes. An attempt's read window is clamped to what is left, and a retry is
skipped when the backoff would not fit. When the budget is what stopped the retry, the
failure is reported as `TmsBudgetExhausted` (504) rather than as the underlying timeout.

**Rejected.** A per-attempt deadline only, and a hard `asyncio.wait_for` around the handler
that cannot tell a slow TMS from a slow anything-else.

**Why.** A budget that spans the request is the only one that bounds the request. Reporting
it accurately matters because it is the one failure here the platform can usefully retry.

---

## 35. Failures answer with `{error, say}` and nothing else

**Problem.** A voice agent will read whatever it is given out loud, and the interesting
part of a failure — the TMS error code, the host, the stack — is exactly the part that
should not be spoken to a carrier.

**Choice.** Every failure is mapped to a short machine code and one sentence from
`app/api/phrasing.py`. The exception type and message go to the log. A generic handler
catches anything unmapped so the response shape holds even for a bug.

**Rejected.** Returning the exception message, and returning a bare status code the agent
has to invent words for.

**Why.** Two audiences, two artefacts: the carrier hears a sentence, the operator gets the
traceback. A single response cannot serve both, so it serves the one that is speaking.

---

## 36. An ambiguous booking is `booking_pending` in the response, and the rate is never in the request

**Problem.** Two ways to turn a good booking flow into a bad one: reporting `booked` for a
booking we could not confirm, and accepting a rate from the caller.

**Choice.** `/bookings` takes only `run_id`. The rate comes from the negotiation session.
An unconfirmed or reference-less booking returns `booked_unconfirmed` with
`result_code: booking_pending`; `booked` is returned only when the reference came back.

**Rejected.** Accepting `agreed_rate` in the body for convenience, and treating a
`STATUS:BOOKED` response without a reference as success.

**Why.** The client already refuses to guess; this is where the promise is kept in the
response the platform sees, and where the rate stops being something the agent could
substitute.

---

## 37. The app is a factory, with no module-level `app`

**Problem.** `uvicorn app.main:app` is the conventional incantation, and it makes the
module import the service at import time.

**Choice.** `uvicorn --factory app.main:create_app`. Building the container touches
configuration that must fail loudly — a missing OTP secret, a bad TMS mode — and the import
is exactly when you want to be able to read that error rather than have it happen inside
the server's own startup.

**Rejected.** A module-level `app = create_app()` for convention's sake.

**Why.** The tests import `create_app`; so does anyone debugging. A module that cannot be
imported without a fully configured environment is a module that is hard to debug.
