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

---

# Phase 4 — hardening and deployment

---

## 38. Rate limiting sits above the per-call lock, keyed by MC

**Problem.** The OTP lock is three attempts *per call*. A caller who opens a new call — a
new `run_id`, which costs nothing — gets three more. Six digits is a million
possibilities, but the lock was never the thing standing between a patient caller and the
code; the *rate* was, and nothing bounded it.

**Choice.** A sliding-window limiter on `/otp/verify`, five attempts per hour per MC,
counted across every call. The key is the MC once one is bound to the session, and the run
id before that, so an unstarted call is not free to probe either.

**Rejected.** Keying by `run_id` (defeated by the same trick it exists to stop), and
counting only *failed* attempts (which would leave a successful guess uncounted on a path
where the guess is the whole attack).

**Why.** A blocked attempt is not recorded, so hammering the endpoint does not extend the
caller's own ban — the window closes on schedule. And a correct code costs an attempt too,
because the limit is about how many times anyone may knock, not about how many times they
were wrong.

---

## 39. `TMS_MODE=fake` refuses to start with `ENV=production`

**Problem.** `TMS_MODE=fake` is a development switch with no guard. Set in a deployment, it
serves recorded fixture loads to real carriers: a real conversation, a real negotiation, a
real booking attempt, against freight that does not exist. Nothing about it looks broken
from the outside.

**Choice.** `Container.build` raises `ConfigurationError` when `is_production` and the mode
is `fake`. The container exits 1 and the platform reports a failed deploy.

**Rejected.** A log warning (nobody reads it during a deploy), and removing the switch
entirely (the platform's workflow tests genuinely need it).

**Why.** A misconfiguration that is silently plausible is worse than a crash. Failing at
startup turns "we quoted a carrier a load that does not exist" into "the deploy is red",
which is the failure everyone notices.

---

## 40. The image reads `PORT`, runs as a non-root user, and is pinned to one replica

**Problem.** Three deployment defaults that each turn into a confusing outage: a hardcoded
port behind a platform that injects its own (the service starts and is never reachable —
looks like a hang, not a config error); running as root (unnecessary blast radius); and
scaling out (which silently breaks every in-memory thing in this service).

**Choice.** `CMD ["sh", "-c", "exec uvicorn ... --port ${PORT:-8000}"]` with the healthcheck
reading the same variable; a `appuser` uid 10001; and `deploy.replicas: 1` in compose with
the reason written next to it.

**Rejected.** Hardcoding 8000 and documenting "set the target port" — a foot-gun whose
symptom is indistinguishable from a broken service. Also rejected: `VOLUME` in the
Dockerfile, which would create an anonymous volume nobody can find; the compose file
declares a named one instead.

**Why.** The `exec` matters too: without it uvicorn is not PID 1 and a shutdown signal goes
to the shell, so the container is killed rather than stopped, mid-call.

---

## 41. Railway over Render, because the free tier that sleeps cannot answer a phone

**Problem.** The service must run in a US region, and the obvious move is the free tier of
whichever host.

**Choice.** Railway. Its services do not spin down when idle — they run and bill, or they
are deleted. Render's free web tier sleeps after 15 minutes without traffic and pays a
~20–60 s cold start.

**Rejected.** Render free plus a keep-alive cron, which is the usual workaround and is not
something to build a phone line on: the first tool call of a call would hang through the
cold start, and a carrier does not wait 40 seconds.

**Why.** For a voice agent the cold start is not a latency cost, it is a failed call — and
it fails in a way that looks like a broken integration rather than a sleeping host. The
honest trade is continuous billing on Railway rather than a free tier that cannot answer.

---

---

## 42. Production readiness is a set of guards, not one guard

**Problem.** The fake-TMS guard covered the misconfiguration that is most embarrassing,
but not the ones that are merely fatal: an empty `SERVICE_API_KEY` starts and answers 503
to every tool, and an empty `OTP_HMAC_SECRET` starts and refuses every code. Both look like
an outage from the platform's side, and neither names the variable that is wrong.

**Choice.** One `_enforce_production_guards` runs on any `ENV=production` and refuses to
start on a fake TMS, an empty API key, or an empty OTP secret — in that order, so the
message names the most consequential problem first.

**Rejected.** Guards scattered at the point of use. `OtpService` already refused an empty
secret, but the failure named the class rather than the environment, and the API key had no
guard at all.

**Why.** A deployment that starts and cannot work is worse than one that does not start:
the first one gets debugged at 2am against a live carrier. Ordering matters too — when
several things are wrong at once, the message should be about the one that would have
quoted a carrier a load that does not exist.

---

## 43. CI runs the suite with no secrets at all

**Problem.** The suite is green because it was run on one machine, by hand. Nothing stops a
commit that breaks it from reaching the branch.

**Choice.** A GitHub Actions job on every push and pull request, installing with
`pip install -e ".[dev]"` and running `pytest`. **No secrets, no service containers, no
`.env`.**

**Rejected.** Passing repository secrets into the job "so it matches local". Every test
already runs against the in-process fake TMS and a mocked FMCSA — that is why the suite
needs nothing. A test that only passes with a developer's configuration is not a test, and
a job with credentials is a job that can leak them.

**Why.** The absence of configuration is the point, not a limitation to work around. It
also means the workflow file stays readable, and a fork can run it.

---

## 44. An unparsed FMCSA response is logged with its keys and values

**Problem.** FMCSA answers only from a US region, so every field name in the parser was a
guess with a `TODO: verify` next to it. The deploy that could settle those guesses returned
`incomplete_response` — the response arrived, and at least one key we read is not the key
FMCSA sends. From the outside that failure is opaque: the reason code says a field is
missing, not which one, and nothing downstream exposes the body.

**Choice.** When the payload cannot be interpreted, log the carrier record — names **and**
values — on one line, capped at 2000 characters, with the web key redacted if it ever
appears in the body. `top_level` (the record's own key names) is printed separately so it
survives truncation.

**Rejected.** *Log the key names only.* A name alone cannot settle a guess: `statusCode`
is only proven by seeing the code it carries, and `_ACTIVE_STATUS` is a guess about values.
*Log the raw body unbounded.* One pathological record floods the log it is meant to be
readable in. *Add a debug endpoint that returns `raw`.* It would have to be reachable by
the agent or by an unauthenticated caller, and the one thing this service must never leak
is a value it has decided not to tell the agent.

**Why.** This is public registry data, not a credential, and the log is the only place a
US-region response can be read from Argentina. The line is a deliverable, so it is capped,
redacted where it could matter, written only on the failure path (a recognised payload
still logs nothing), and pinned by tests.

---

## 45. Eligibility reads the authority statuses, not `statusCode`

**Problem.** The recorded response carries `statusCode: "A"` — an active carrier record —
next to `commonAuthorityStatus: "I"` and `contractAuthorityStatus: "N"`. Reading
`statusCode` as the authority would have booked a carrier with no operating authority,
which is the failure this check exists to prevent. The parser also read `outOfService`,
a field the response does not have.

**Choice.** Eligible means `allowedToOperate == "Y"` **and** at least one of
`commonAuthorityStatus` / `contractAuthorityStatus` is `"A"` **and** `oosDate` is null,
each with its own reason. Either kind of authority will do: a carrier may haul on common
or on contract.

**Rejected.** *Keep `statusCode` as a second signal.* It answers a different question — is
this an active carrier record — and adding it back would refuse carriers on a field that
says nothing about authority. *Require common authority specifically.* Contract carriers
would be refused for being contract carriers.

**Why.** The evidence is the recorded payload itself, not the documentation. `statusCode`
stays parsed, in `raw`, where it costs nothing and can explain a decision to a rep later.

---

## 46. Missing liability insurance is a rejection, not an alert

**Problem.** The recorded carrier has `bipdInsuranceRequired: "Y"` and
`bipdInsuranceOnFile: "0"`: FMCSA says the cover is required and shows nothing filed. A
broker that dispatches a carrier without it is exposed on every mile of that load.

**Choice.** A rejection with its own reason, `insurance_not_on_file`, checked after the
authority. `"u"` (unknown) on the requirement is **not** a rejection — FMCSA is stating no
requirement, so there is nothing to hold the carrier to.

**Rejected.** *A flag for the rep, letting the call continue.* There is no channel for it.
The service books autonomously at the end of a call; a warning nothing reads is not a
check. *Ignore FMCSA's insurance fields and rely on a certificate of insurance.* That is
what a rep does by hand, and it halts the same call anyway — but later, after the
conversation has been had, and by someone who was not on the line.

**Why.** The costs are not symmetric. A wrong rejection routes a call to a rep, who sees
the reason code and fixes it in seconds. A wrong acceptance dispatches a load behind no
insurance. The known cost is that FMCSA filings lag reality, so carriers with valid cover
can be refused and sent to a human — the direction to be wrong in.

---

## 47. The agent is told four fields, and none of them is from the record

**Problem.** The response carried the DOT number and the reason code alongside the name.
The FMCSA record behind it also holds an address, an EIN and six insurance fields. The
agent speaks its response out loud and the platform keeps a transcript; anything handed to
it can end up in both.

**Choice.** `{eligible, result_code, legal_name, say}`. Nothing else. The reason travels
inside `result_code` as `fmcsa_rejected:<reason>`, which is the closed set CLAUDE.md §8
already defines, and the record's other fields stay in the event log where a rep can read
them.

**Rejected.** *Return `dot_number` too, "it is public".* Public is not the test; the test
is whether handing it to the agent helps at all. *Drop `result_code` as well.* It is how
the platform routes a refusal, and dropping it would leave a rejection with no
machine-readable reason anywhere.

**Why.** The agent cannot leak what it was never handed. `ein` is stripped on the way in
as well — it survives nowhere in this service, not even in `raw` — because the cheapest
way to be sure a tax id cannot surface is not to carry it.

---

## 48. `authority_age_days` is unknown, and stays unknown until it is sourced

**Problem.** Onboarding's first rule is the age of the carrier's authority. The recorded
payload has no authority date: no `addDate`, no `authDate`, no census date. The parser was
reading five candidate spellings of a field that does not exist.

**Choice.** Stop parsing a date. `authority_age_days` is `None` for every carrier, which
CLAUDE.md §8 already defines as "unknown", which lands every lead on `Review`.

**Rejected.** *Keep the candidate date keys.* They are guesses that have now been tested
and found wrong; leaving them in is how a wrong answer survives contact with a real
payload. *Use `mcs150Outdated` or a safety-review date as a proxy.* Neither is the
authority date, and a proxy that is usually right is a rule that is sometimes wrong, and
silently so.

**Why.** `Review` is the honest answer — a human looks. The cost is that qualification is
currently a constant, and that is visible in `PROGRESS.md` as the last open `TODO: verify`
rather than hidden behind a plausible-looking number.

---

---

## 49. A missing TMS address fails at startup, and names the variable

**Problem.** A deploy whose environment is missing `TMS_HOST` (or `TMS_PORT`/`TMS_TOKEN`)
crashed on the first `TmsClient` build with a bare `ValueError: "TMS host is required"`.
Render saw a container that never became healthy and reported "timed out waiting for a
health check" — a hung service, not the misconfiguration it is.

**Choice.** Validate the live-TMS connectivity in `Container.build`, before the client is
constructed, and raise the same `ConfigurationError` the production guard uses — naming the
variable and the fix. It is checked always, not gated on `ENV=production`, because live
mode needs the address in every environment.

**Rejected.** *Add it to `_enforce_production_guards` only.* The bare `docker run -e
PORT=10000` reproduction runs as development (no `ENV`), so a production-only check would
still crash with the unnamed `ValueError`. *Let the client keep raising `ValueError`.* The
message cannot name the env var without a circular import (`container` imports `TmsClient`).

**Why.** The port handling was already correct; the failure mode was the crash-loop, and a
crash-loop is indistinguishable from a hang. Naming `TMS_HOST` turns a confusing symptom
into an answer in the log line.

---

## 50. A counter retry replays the round instead of consuming one

**Problem.** The webhook layer may retry `counter_offer` after a lost response. Before
this, every `counter()` call was a round — a retry of the same ask burned one of the three
rounds, and a retry of an accept/close returned "already closed" instead of the decision
the agent already had.

**Choice.** The engine remembers the last counter (ask, action, amount, time) on the
negotiation. A repeat of the same ask inside a 15 s window replays the stored decision —
checked before the open/closed gate, so an accepted or closed round also replays — without
advancing the round counter or emitting a round event.

**Rejected.** *Key the replay only on the ask, forever.* A carrier who repeats their
number a minute later is a genuine new round, not a retry. *Let the client send a round
number.* That would hand the agent a value it can drop, replay or invent — the thing the
`run_id` design exists to prevent.

**Why.** A voice round trip takes longer than 15 s; a webhook retry is far shorter. The
window is what tells the two apart, and it makes `counter_offer` safe to retry (retries =
1 in `docs/platform-tools.md`).

---

## 51. `send_otp` sends to the verified MC, not a named one

**Problem.** `send_otp` took an `mc_number` from the agent, so the agent could verify one
carrier and then ask for a code to another.

**Choice.** `verify_carrier` records the checked MC on the session; `send_otp` drops the
`mc_number` parameter and sends to that recorded MC. No prior verification returns `locked`
(`otp_failed`). Sending an `mc_number` at all is a 422.

**Rejected.** *Keep the parameter and compare it to the session.* That still lets the agent
name a carrier, and the only safe reading of "the agent supplied it" is "it is not to be
trusted" — so do not even accept it.

**Why.** The code goes to the carrier this call verified, and there is no field through
which a different one can be named.

---

## 52. The agent never sees the posted rate

**Problem.** `search_loads`/`get_load` returned `rate` (the posted rate). When the posted
rate sits above `MAX_BUY`, the opening offer is `0.90 × C`, so quoting the posted rate
would hand the carrier a number above the ceiling.

**Choice.** `Load.public_dict()` drops `rate`, and the detail's spoken line says nothing
about price. The number the agent may quote comes only from `open_negotiation`.

**Rejected.** *Keep `rate` and tell the agent not to say it.* An instruction is a leak
waiting to happen; a field that is not serialised cannot be spoken.

**Why.** The ceiling stays hidden behind the same wall as the rate: neither ever reaches
the agent-facing response.

---

## 53. Only `handoff` ends the call

**Problem.** Several tools looked terminal, but most "ends" are conditional and some should
hand off rather than hang up.

**Choice.** `handoff` is the only tool with **end call = yes**; its `Message`
("Connecting you with a rep now, hang tight.") is said before the call runs, because an
end-call cuts the conversation the moment the tool returns. `counter_offer` `close` and
`search_loads` `no_match` end the call *without* a handoff (the gap/demand are recorded
server-side); `book_load` has no end call because the agent must say `booked` vs
`booking_pending` and then hand off.

**Why.** End call must never depend on a result the agent still has to read out loud —
that is what the prompt is for.

---

## 54. Every tool response carries a result code for Twin

**Problem.** `counter_offer` returned `{action, amount, say}` with no result code, so a
close (the terminal `negotiation_failed` outcome) could not be counted on the same column
as the other outcomes in Twin.

**Choice.** Add `result_code` to `counter_offer`: `negotiation_failed` when `action` is
`close`, and `null` for `accept`/`counter` (those leave the call open). Like `verify_carrier`,
the code rides in the response for the platform to read and is hidden from the agent in
Tool Call Result — the agent speaks `say`.

**Rejected.** *Return `negotiation_failed` only.* `accept`/`counter` are not terminal, so
they stay `null`; a code for them would fabricate an outcome that has not happened yet.

**Why.** One column of result codes is what lets Twin aggregate how calls end without
reading per-tool action strings.

---

## 55. A `schema-sample` run id returns canned shapes without side effects

**Problem.** The HappyRobot "Generate" button fires a real request to capture a tool's
response shape, but there is no session for it, so a normal handler writes an `error` field
and the platform captures an empty schema.

**Choice.** When `run_id` is exactly `"schema-sample"`, each tool returns a 200 with a
canned response carrying every field of its real response (obvious dummy values). The check
sits at the top of each handler, before any session/TMS/FMCSA/event work; auth still runs
first because it is a router-level dependency.

**Rejected.** *A dedicated schema endpoint.* It would drift from the real responses. *Return
the sample without auth.* The API key gate must hold for every request, sample or not.

**Why.** The schema is captured from the same endpoint the agent calls, so it cannot go
stale.

---

## 56. Search returns the full load detail

**Problem.** The pitch needed `get_load` after `search_loads` to get `delivery_dt`, `weight`,
`commodity`, `pieces`, `dims` and `notes` — an extra round trip the voice flow does not need.

**Choice.** `search_loads` fetches each matched load's full detail (LOAD_GET per result, up
to 3) and returns `LoadDetail.public_dict()`, so one tool carries everything the agent needs
to pitch. `get_load` is dropped from the platform.

**Rejected.** *Keep the summary and add fields to LOAD_QUERY.* The TMS only discloses the
detail columns on LOAD_GET.

**Why.** Fewer tools, one fewer round trip, and the rate/ceiling stay hidden because
`public_dict()` already strips both.

---

## 57. POST /bookings is idempotent per run_id

**Problem.** The platform's POST node does not allow zero retries, so a retried booking
would call `LOAD_BOOK` twice and permanently consume a second load.

**Choice.** The booking outcome (status + ref) is stored on the session, and a single
`booking_lock` serialises the whole check-and-book. A retry — after completion or while the
first is still in flight — returns the stored answer and never issues a second `LOAD_BOOK`.

**Rejected.** *Let the platform "just not retry".* It cannot; the minimum is one retry.
*Make the TMS call itself idempotent.* The TMS is not ours, and `ALREADY_BOOKED` is not the
same as "the same successful answer".

**Why.** One `LOAD_BOOK` per agreed rate, per call — the rate is stored server-side and so
is the outcome.
