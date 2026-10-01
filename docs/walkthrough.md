# Walkthrough — how a request travels through the TMS adapter

Written to be read out loud. Each section follows one step further in, in the same order a
real call takes.

- **Sections 1–12: phase 1**, the TMS adapter — four small files under `app/tms/`, plus a
  fake server in `tests/`.
- **Sections 13–21: phase 2**, verification, OTP, negotiation, onboarding and events.
- **Sections 22–29: phase 3**, the HTTP API, the verification gate and the error surface.

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

---

# Phase 2 — how a call flows now

Still no HTTP layer: these are the units the API will call, in the order a call reaches them.

```
run_id ─▶ RunSessionStore ─┬─▶ FmcsaClient.check      (who is this carrier)
                           ├─▶ OtpService.send/verify (prove it)
                           ├─▶ shown loads            (what they were pitched)
                           └─▶ NegotiationEngine      (what we will pay)
                                     │
                              EventSink ◀── everything that got decided
```

---

## 13. `app/sessions/` — one call, one bag of state

Everything hangs off the `run_id` the platform injects into every tool. There is no
`otp_session_id` and no `negotiation_id` travelling through the conversation, which means
the agent cannot drop one, replay one, or make one up. The agent talks; the server
remembers.

`RunSession` holds the MC being verified, the OTP challenge, the list of loads that were
shown, and the negotiations. Two rules live on it because they are about the call rather
than about any one feature:

- `record_shown_load` / `was_shown` — a load is negotiable **only** if it was pitched during
  this same call. A `load_id` is a short, guessable string; without this, anything driving
  the conversation could name any load in the system.
- `can_open_negotiation` — two negotiations per call, the original plus one alternative.

`RunSessionStore` is a dictionary with a TTL and a lock. The TTL runs on a **monotonic**
clock, so a wall-clock jump cannot expire a live call. It is deliberately process-local:
one worker, one dictionary, documented as a POC constraint with Redis behind the same
interface as the production path.

State types (`OtpChallenge`, `NegotiationSession`) live here rather than in the feature
packages, which keeps the dependency arrow one-way — `otp`, `negotiation` and `onboarding`
import from `sessions`, and `sessions` imports from none of them.

---

## 14. `app/carriers/directory.py` — the only place an OTP can go

A seeded table of MC number → contact. `send` looks the destination up here or nowhere:
the caller can say any address they like and it changes nothing. An MC that is not in the
table has no contact on file, and that is a different outcome (`routed_onboarding`), not an
error.

Exact-match lookup, deliberately: a fuzzy match would let a caller reach a contact that is
not theirs by naming something close to it.

---

## 15. `app/fmcsa/client.py` — verify, then fail closed

`check(mc)` validates the MC locally (digits, 1–8) so a malformed one never reaches the
network, then fetches the carrier and returns an `AuthorityCheck`.

The order of the checks is the one in CLAUDE.md §5: found, allowed to operate, authority
active, not out of service. A 404 means FMCSA does not know the MC — an answer, and a
different result code from an outage.

**Everything fails closed.** A timeout, a 403 (which is what this domain returns to the
whole of Argentina), a 5xx, a block page arriving as `200 text/html`, or a payload whose
fields we cannot read — all of them mean `eligible=False`. The failure modes are
`verification_unavailable` for "could not ask" and `incomplete_response` for "asked and
could not understand the answer".

The parser is defensive because every field name in it is a guess: it accepts several
spellings of each key, several date formats, booleans as `Y`/`N`/`true`/`1`, and a carrier
object wrapped in `content`, in a list, or bare. None of this has been verified against a
real response — see `tests/fixtures/README.md`.

---

## 16. `app/otp/security.py` — codes, digests and masking

Three pure functions, and the reasoning behind the two that matter is in the module: the
code comes from `secrets.randbelow`, not a modulo of a float; the stored value is an
HMAC-SHA256 keyed with a server secret, with the `run_id` mixed into the message so a
digest is bound to the call it was issued for; and comparison is `hmac.compare_digest` so a
wrong code cannot be found one byte at a time.

`mask_email` turns `dispatch@ridgelinefreight.com` into `d***@ridgelinefreight.com` — enough
for the agent to say where the code went, and not enough for someone on the call to learn
the address of a carrier they are not.

---

## 17. `app/otp/service.py` — issue, verify, and two audiences

`send` and `resend` produce an `OtpDispatch` with two named views:

- `delivery_view` → `{destination, code}`. For the platform's delivery node.
- `agent_view` → `{masked_destination}`. This is what may reach the conversation.

The code field is `repr=False`, so a stray log of the object does not print it, and no event
carries it. Splitting the response by audience is the server's job even though acting on the
split is the platform's: two named fields is a shape that cannot be merged by accident.

The budget is three attempts and one resend, and the two interact deliberately — **a resend
does not refund attempts**, and a locked session cannot be resent at all. Otherwise three
guesses becomes six, and the lock becomes removable by asking for a new code.

`send` called twice spends the resend budget, so the limit cannot be bypassed by hammering
the send endpoint instead of the resend one.

---

## 18. `app/negotiation/policy.py` — the arithmetic, and nothing else

No state, no clock of its own. `floor_to`, `is_urgent`, `opening_offer` and
`concession_step`, all checkable by hand against CLAUDE.md §7.

Two things worth knowing when reading it:

- `opening_offer` applies the `C − 25` cap to the opening too, not just to the steps. The
  brief states the margin as a global rule; it only ever binds when the posted rate sits
  within one increment of the ceiling (DECISIONS.md §20).
- The steps arrive as `Fraction`, so `0.40 × gap` is exactly 2/5 of the gap. With floats,
  a step could land a hair below a multiple of 25 and `floor25` would drop a whole
  increment — for some gaps and not others.

---

## 19. `app/negotiation/engine.py` — the rounds

`open(run_id, load)` checks that the load was shown in this call and that the call still has
a negotiation left, requires a ceiling, computes the opening offer and whether the pickup is
urgent, and records a `negotiation_opened` event that is allowed to contain the ceiling.

`counter(run_id, carrier_ask)` is one round, and the server counts them — the agent cannot
skip one, and a fourth request finds nothing open. The three cases are the ones in §7:

1. at or below the current offer → accept at the request;
2. rounds 1 and 2 → accept if the request is at or below the step, otherwise counter at the
   step;
3. the last round → accept anything at or below the ceiling, otherwise close and record the
   gap.

`Decision.agent_view` is `{action, amount}` and nothing else. On a close, the amount is the
**last offer**, never the ceiling — the helpful-looking value there is the one that must
never leak (DECISIONS.md §21).

The engine deliberately knows nothing about OTP. The gating rule in §8 belongs to the API
layer in phase 3, and is listed as an open issue in `PROGRESS.md` so it cannot be forgotten.

---

## 20. `app/onboarding/qualification.py` — a rule, not a judgement

`qualify(authority_age_days, matching_loads)` returns Hot, Warm or Review. Review
short-circuits first: an unknown age, an authority younger than 90 days, or a call where
nothing matched. Only then do the matching-load thresholds apply.

An unknown age is treated as **unqualified**, not as old enough — the 90-day rule exists to
protect against brand-new authorities, and "probably fine" is how one gets handed a load.

---

## 21. `app/events/` — what the service decided

`Event = {run_id, ts, type, payload}`, with the type list closed in `EventType`. Three
sinks: `NullEventSink` (the default, so nothing upstream has to be optional),
`InMemoryEventSink` (tests and demos), and `JsonlEventSink` (one JSON object per line,
opened per write rather than held open)

A failed write logs and returns rather than raising: losing an audit line must not drop a
carrier who is mid-negotiation. Twin is deliberately absent — its authentication is not
confirmed, and guessing at it would bake a wrong shape into every call site.

Events **may** contain the ceiling, because they are internal and never travel back to the
agent. That is the whole reason `test_call_flow.py` exists: it walks a complete call, gathers
every agent-facing value, and asserts the ceiling is not among them — as a number or as a
key name — while the event log has it.

---

# Phase 3 — the HTTP surface

```
POST /…  ─▶ X-API-Key ─▶ route ─▶ require_verified ─▶ service ─▶ agent view
                             │                          │
                        Deadline (8 s)              EventSink
```

---

## 22. `app/main.py` — the factory

`create_app(settings)` builds the container, installs the error handlers, mounts the router
behind the API key, and defines `/health` directly on the app so it stays unauthenticated.

There is deliberately **no** module-level `app`. Building the container reads configuration
that must fail loudly when it is wrong, and doing that at import time would make this module
unimportable in exactly the situation where you want to import it and read the error. Hence
`uvicorn --factory app.main:create_app`.

---

## 23. `app/security.py` — one key, compared in constant time

`build_api_key_dependency(key)` returns the dependency that every tool route is mounted
behind. Two details:

- `hmac.compare_digest`, so the key cannot be recovered by timing a series of guesses.
- An **unconfigured** key refuses every request rather than allowing them. A guard that
  opens when it is not set is not a guard.

The 401 says nothing about whether the header was missing or wrong; an unauthenticated
caller does not need to know which.

---

## 24. `app/container.py` — the wiring, and the TMS mode switch

One place where the store, the event sink, the TMS client, the OTP service, the negotiation
engine, the FMCSA client and the handoff queue are constructed from `Settings`. The route
handlers stay about the conversation.

`TMS_MODE=fake` is the switch the platform's workflow tests need: the service starts the
fixture-replaying TCP server from `app/tms/fake_server.py` on a loopback port and points
the **ordinary** `TmsClient` at it. Same socket code, same deadlines, same framing, same
retry policy — only the data is canned. A stub client returning objects would skip exactly
the code that is riskiest.

`EVENTS_JSONL_PATH=memory` swaps the event sink for the in-memory one, which is what the
tests use and what a demo can read back.

---

## 25. `app/deadline.py` — a budget for the whole request

A voice tool that hangs is a dropped call, and the platform's webhook timeout is not
documented. One `Deadline` is created per request and threaded into every TMS call the
request makes — so a booking that takes three TMS round trips still fits inside one budget.

The client spends it two ways: an attempt's read window is clamped to what is left, and a
retry is skipped when its backoff would not fit. When the budget is what stopped the retry,
the failure is reported as `TmsBudgetExhausted` (504) rather than as the underlying timeout
— because that is the one failure here the platform can usefully retry.

---

## 26. `app/api/deps.py` — the gate

`require_verified(session)` is called explicitly by each gated handler. It is not a FastAPI
dependency because every gated route takes a different request body, and a dependency would
have to re-declare it — FastAPI would then see two body parameters.

What makes that safe is a test rather than a mechanism:
`test_every_route_that_needs_otp_is_gated` walks the app's own route table and asserts that
every route outside a short exception list answers 403 to an unverified call. A route added
later fails the test instead of shipping open.

The exception list is short and each entry has a reason: `/health` (liveness),
`/carriers/verify` and `/otp/*` (they happen before there is anything to verify), and
`/handoff` — which is the escape hatch for a carrier who can never verify, so gating it
would turn "route this to a human" into a dead end.

---

## 27. `app/api/routes/` — what each endpoint does

**`carriers.py`** looks the MC up and maps each way FMCSA can refuse onto a sentence. It
deliberately does *not* touch `session.mc_number`: that field is what binds a verified OTP
to an MC, and only `OtpService.send` may set it.

**`otp.py`** returns the two views in separate, described fields — `masked_destination` for
the agent, `delivery: {destination, code}` for the platform's send node. The spoken line
never contains either the code or the full address.

**`loads.py`** has the search and the detail. Search is where loads are recorded as *shown*,
which is what makes them negotiable; the detail fetch deliberately does **not** record,
because otherwise "negotiate what you were shown" would collapse into "negotiate anything
you can name", and load ids are guessable. Every load goes out through `public_dict()`.

**`negotiations.py`** fetches the load server-side so `MAX_BUY` exists only inside this
process, opens the negotiation, and answers each carrier request with `{action, amount}`.
The round counter lives on the server, so a caller cannot skip a round.

**`bookings.py`** reads the agreed rate out of the session — there is no rate field in the
request, so there is nothing to substitute — and returns `booked` only when the reference
came back. Anything uncertain is `booking_pending`. The handoff endpoint lives here too and
puts a ticket in the in-memory queue with a summary a rep can act on.

---

## 28. `app/api/errors.py` and `app/api/phrasing.py` — two audiences

`phrasing.py` holds every line the agent might say. It is one file so it can be read in one
sitting and checked for the thing that matters: none of it describes anything internal.

`errors.py` maps each domain exception onto a status code, a machine code and one of those
sentences. Handlers are registered per **base** class (`TmsFault`, `SessionError`,
`NegotiationError`, `McValidationError`, `ValueError`), so a new exception type added under
one of them is mapped automatically instead of escaping as a stack trace — which is exactly
what happened the first time, because Starlette re-raises whatever reaches the generic
`Exception` handler.

The detail goes to the log. The response gets a sentence.

---

## 29. `app/tms/fake_server.py` — now shared with development

Moved out of `tests/` when `TMS_MODE=fake` needed it. It replays the recorded fixtures,
injects the five fault modes, and returns `ALREADY_BOOKED` / `INVALID_RATE` on demand.

One addition for the development role: it synthesises a `LOAD_GET` record for the loads the
fixture only captured as `LOAD_QUERY` lines, including a `MAX_BUY` above the posted rate.
Without that you could search and not negotiate, which is the only thing the fake is for.
The invented fields are labelled as such in the module — nothing about them claims to match
the live server.

---

# Phase 4 — hardening and deployment

---

## 30. `app/rate_limit.py` — the limit above the per-call lock

The OTP lock in `app/otp/service.py` is three attempts **per call**. Opening a new call gets
three more, and a call is free, so the lock was never what stood between a patient caller
and the code. This is the thing that does: a sliding window of five attempts per hour, keyed
by MC once one is bound to the session and by run id before that.

Two decisions in it are worth reading before changing it:

- A **blocked attempt is not recorded**, so hammering the endpoint does not keep pushing the
  caller's own ban out. The window closes on schedule regardless.
- A **correct code still costs an attempt**. The limit is about how many times anyone may
  knock, not how many times they were wrong.

The dictionary of keys is capped, because a caller looping over fake MC numbers is otherwise
a slow way to run the service out of memory.

---

## 31. `app/container.py` — the startup guard

`Container.build` refuses to construct when `ENV=production` and `TMS_MODE=fake`. A
fixture-serving deployment does not look broken from the outside: real conversation, real
negotiation, real booking attempt, against freight that does not exist. Failing at startup
turns that into a red deploy, which is the failure everyone notices.

The guard is on `is_production`, which accepts `production`/`prod` in any case, and it is
the *only* startup guard — an empty `SERVICE_API_KEY` still starts and answers 503, which
`PROGRESS.md` lists as a candidate for the same treatment.

---

## 32. Dockerfile, compose and the one-replica constraint

The image reads `PORT` (falling back to 8000) rather than hardcoding it, because a platform
that injects its own port would leave a service that starts and is never reachable — a
symptom that reads as a hang rather than a misconfiguration. The healthcheck reads the same
variable. `exec` in front of uvicorn so it is PID 1 and gets the shutdown signal, rather
than the shell getting it and the container being killed mid-call.

It runs as `appuser` (uid 10001), and `.env` is in `.dockerignore`, so configuration can
only arrive through the environment at run time.

`docker-compose.yml` pins **one replica**, with the reason written next to it. Run sessions,
the negotiation counters, the OTP limiter and the handoff queue are all process memory: a
second replica would hold half the state, and a tool call that landed on the wrong one would
look like a call that had never started. That is a correctness requirement, and it is in the
runbook so nobody "fixes" it during a traffic spike.

`docs/DEPLOY.md` is the runbook: the one command, the variables to set on the host (never
the ones from a developer's `.env`), and the two post-deploy checks — the second of which,
a real `/carriers/verify` from a US region, is the first time the FMCSA parser meets a real
response.
