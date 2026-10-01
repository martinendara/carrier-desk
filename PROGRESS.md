# Progress log

One checkpoint per phase. Newest at the bottom.

---

## Phase 1 — TMS adapter (2026-10-01)

**Status:** code complete and tested. The live smoke run is **blocked on credentials**
(see Open issues).

### What was built

| File | Purpose |
|---|---|
| `app/config.py` | Settings from env / `.env`; the token is a `SecretStr` so it cannot be logged by accident |
| `app/tms/protocol.py` | Pure wire layer: `encode_request` with sanitisation, `parse_record`/`parse_response`, the exception hierarchy, `redact()` |
| `app/tms/models.py` | `Load`, `LoadDetail`, `BookResult`; `public_dict()` is where `MAX_BUY` is guaranteed to be dropped |
| `app/tms/client.py` | Sockets, deadlines, fault classification, retry policy, ambiguous-booking recovery |
| `tests/fake_tms.py` | Local TCP server replaying the fixtures with injectable faults |
| `scripts/tms_smoke.py` | Read-only probe of the live server, guarded against every command except `DEBUG_ECHO`/`LOAD_QUERY`/`LOAD_GET` |

Environment: Python 3.12.1 in `.venv/`, `pyproject.toml` with pydantic v2,
pydantic-settings and pytest.

### Tests

```
116 passed in 2.46s
```

Coverage by area:

- **Both wire formats.** The manual style (`LD0000045821`, `RATE:0002150`) and the live
  style (`LD00896     `, `RATE:2073    `) parse to the same values. The recorded fixtures
  are parsed end to end in the tests.
- **Every fault class** of the brief against the fake server: no bytes before the deadline
  (`TmsTimeout`), closed before `END` (`TmsPartialResponse`), framing/field violations and
  an unterminated oversized line (`TmsMalformed`), `ERR|CODE:` (`TmsError`), and delayed
  termination (must not burn the deadline).
- **Retry policy.** `AUTH_FAILED`/`MISSING_FIELD`/`UNKNOWN_LOAD`/`UNKNOWN_CMD` are not
  retried; `SERVER_ERROR` and unknown codes are; idempotent commands recover on a second
  attempt.
- **Ambiguous `LOAD_BOOK`.** Response lost and the write landed → `booked_unconfirmed` with
  no reference, and no second booking. Response lost and the write did not land → exactly
  one retry. Two ambiguous attempts → stop and hand to a human. `INVALID_RATE` → never
  retried. `ALREADY_BOOKED` → `booked_unconfirmed`.
- **No request leaks.** The token is redacted in the fake server's request log; validation
  errors never echo the value they rejected.
- **`MAX_BUY` never escapes.** `LoadDetail.public_dict()` drops it, including the
  mis-cased unknown-field path.

The suite runs in ~2.5 s. That took two fixes: `ThreadingTCPServer.block_on_close` and a
short `serve_forever` poll interval were costing 20 s of sleeping across ~40 fake servers.

### Bugs found by the tests, and fixed

1. **`execute()` returned raw wire lines, not parsed records**, so `Load.from_record` was
   handed a string. Parsing now happens per attempt, which also means a framing fault is
   correctly retried for idempotent commands.
2. **`LoadDetail` leaked `MAX_BUY` into `public_dict()`.** The base `from_record` filed it
   as an unknown field, and `extra` is merged into the public payload. This is the one
   invariant the whole design rests on; it is now recomputed against the subclass's field
   list *and* filtered again on the way out.
3. **The client hung on a held-open connection.** The live server does not always terminate
   `END` with `\r\n`, so a complete response sat in the buffer until the 5 s deadline and
   came back as a partial response. The reader now accepts an unterminated trailing `END`.

### Smoke test against the live server

**Blocked.** Every command is rejected:

```
ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token
```

The transport is fine — the host is reachable and answers in ~360 ms at the application
layer. The rejection is not on our side:

- A hand-built frame (`CMD:DEBUG_ECHO|AUTH:<token>\r\n`, 45 bytes, written straight to a
  socket with nothing from `app/tms` involved) gets the same `AUTH_FAILED`.
- The `.env` is parsed cleanly: one `TMS_TOKEN` line, no BOM, no `\r`, no `export` prefix,
  no quotes, no surrounding whitespace, and the loaded value is byte-identical to the file.

The same token reportedly answered `AUTH:OK` on 2026-09-30, which points at a rotated or
expired demo token. Consequently the four open questions in `docs/tms-protocol.md` are
**still unanswered**: the real `MAX_RESULTS` ceiling, whether `PICKUP_DT` is the right
filter name, whether `FLATBED` is a real equipment value, and today's padding.

`LOAD_BOOK` was never sent. `scripts/tms_smoke.py` refuses any command outside
`DEBUG_ECHO`, `LOAD_QUERY` and `LOAD_GET`, and `tests/test_smoke_guard.py` enforces that.

### Open issues

1. **TMS token rejected** — blocks the smoke observations and any live verification.
   Re-run `.venv/bin/python scripts/tms_smoke.py --out docs/smoke-<date>.txt` with a fresh
   token. It answers all four questions in one pass.
2. **Timestamp zone unconfirmed** — `DECISIONS.md` §2 assumes UTC. The evidence for that is
   the manual's description of the `LOAD_BOOK` `TIMESTAMP` field, not an observation.
3. **Parse of `MALFORMED` as an error code** — treated as non-retryable, matching the
   documented code list. Not observed on the wire.
4. **Untested against the real fault behaviour** — the fault modes come from the manual's
   table. None has been observed live (the 2026-09-30 run saw none in three calls).

### Not started

Phase 3 (HTTP API) and phase 4 (Docker, deployment). Phase 2 followed in the next section.

---

## Phase 2 — FMCSA, OTP, negotiation, onboarding, events (2026-10-01)

**Status:** code complete and tested. Units only — there is no HTTP layer yet. The live
smoke run is still blocked on the TMS token (see phase 1).

### What was built

| Module | Purpose |
|---|---|
| `app/sessions/` | `RunSession` keyed by `run_id`: OTP state, loads shown, active negotiation, round counter. In-memory store with a monotonic TTL. |
| `app/fmcsa/` | Authority check. Fails closed on outage, on an error status, and on a payload whose signals we cannot read. |
| `app/otp/` | Email OTP: `secrets` codes, HMAC-SHA256 with the `run_id` mixed in, 5-minute TTL, 3 attempts, 1 resend. Two response views, one per audience. |
| `app/negotiation/` | `policy.py` is the arithmetic (pure, exact rationals); `engine.py` is the round machine over the run session. |
| `app/onboarding/` | Hot / Warm / Review as a pure function. |
| `app/events/` | `EventSink` with in-memory and JSONL implementations. Twin deferred. |
| `app/carriers/directory.py` | Seeded contacts. The only source of an OTP destination. |
| `app/results.py` | The closed set of result codes. |

CLAUDE.md sections 6, 7 and 8 were replaced and a new section 9 (event log) added, per the
patch. Section 8 now carries the `run_id` contract and the closed result-code list.

### Tests

```
335 passed in 2.44s
```

| File | Tests | Covers |
|---|---|---|
| `test_negotiation.py` | 49 | every edge case in the patch, plus the leak check |
| `test_protocol.py` | 45 | phase 1 wire format, both padding styles |
| `test_otp.py` | 43 | generation, HMAC, masking, budget, resend, lock |
| `test_client_faults.py` | 41 | phase 1 fault classes and ambiguous booking |
| `test_fmcsa.py` | 36 | eligibility, fail-closed paths, defensive key parsing |
| `test_sessions.py` | 29 | keying, TTL, shown loads, the two-negotiation budget |
| `test_results.py` | 26 | the closed set and reason sanitisation |
| `test_models.py` | 24 | phase 1 record typing |
| `test_onboarding.py` | 16 | the decision table and its ordering |
| `test_events.py` | 13 | shape, sinks, surviving an unwritable disk |
| `test_call_flow.py` | 7 | a whole call end to end |
| `test_smoke_guard.py` | 6 | the smoke script cannot book |

The mandatory negotiation cases are all present: ceiling above and below the posted rate, a
narrow margin where rounding alone would nullify the concession, an ask below the opening,
accepting the current offer, three failed rounds, urgent versus normal, a fourth round
refused, and a `load_id` that was never shown in the call.

The cross-cutting check the patch asked for is
`test_a_full_call_never_puts_the_ceiling_in_front_of_the_agent`: it walks FMCSA, OTP, the
pitch and three rounds, collects every agent-facing value, and asserts the ceiling appears in
none of them as a number or as a key name — while the event log holds it.

### Bug found by the tests, and fixed

`fmcsa_rejected:<reason>` sanitised with `str.isalnum()`, which is true for `é`. A reason
string can be read out loud by the agent, so the filter now also requires ASCII.

### Deliberate interpretations, all recorded in DECISIONS.md

- **The opening offer is capped at `C − 25`** too, not just the steps (§20). The two rules in
  the brief conflict only when the posted rate is within one increment of the ceiling.
- **A `close` reports the last offer, never the ceiling** (§21). The response shape has an
  `amount` and the tempting value would be the one that must never leak.
- **Accepting the final round at exactly `C` is allowed** (§22), which means the agent can
  hear a number equal to the ceiling when the carrier asked for it. Named explicitly rather
  than hidden behind a test that avoids the case.
- **A resend does not refund attempts, and a locked session cannot be resent** (§17).
- **A missing field from FMCSA is a rejection** (`incomplete_response`), not a pass (§27).

### Open issues

1. **TMS token rejected** — unchanged from phase 1; blocks the smoke observations. Consequence
   for phase 2: no live load has ever been fetched, so `LoadDetail.max_buy` has only ever been
   read from the recorded fixture.
2. **Every FMCSA field name is `TODO: verify`.** The fixtures are synthetic. A wrong date key
   does not fail loudly — it makes `authority_age_days` `None`, which routes the lead to
   `Review`. See `tests/fixtures/README.md`.
3. **No live FMCSA call has ever been made** from this machine (403 from Argentina). The
   fail-closed paths are tested; the happy path is tested only against our own guess.
4. **OTP delivery is not wired.** The service produces `{code, destination}`; whether the
   platform's Tool Call Result can keep that away from the agent's context is still unconfirmed
   — the same open decision phase 1 recorded.
5. **Gating is not yet enforced.** §8 requires a verified OTP for the same MC before search,
   negotiation and booking. That belongs to the API layer in phase 3 and is **not yet written
   anywhere** — the engine deliberately does not know about OTP.
6. **Single-instance sessions.** `RunSessionStore` is a process-local dictionary; Redis is the
   production path (§28).
7. **Timestamp zone still unconfirmed** (phase 1, DECISIONS §2). Urgency now depends on it:
   a wrong zone shifts which pickups count as urgent.

### Not started

Phase 3 (HTTP API, auth, gating) and phase 4 (Docker, deployment).

---

## Phase 3 — HTTP API and auth (2026-10-01)

**Status:** complete and tested. The service runs, the full call works end to end against
the in-process fake TMS, and the README's curl examples were executed against a local
server rather than written from memory.

### What was built

| Module | Purpose |
|---|---|
| `app/main.py` | `create_app()` factory, `/health`, router assembly, error handlers |
| `app/security.py` | `X-API-Key` dependency, constant-time compare, refuses everything when unconfigured |
| `app/api/deps.py` | container access, the per-request `Deadline`, `require_verified` |
| `app/api/schemas.py` | request/response models, `extra="forbid"` on every request |
| `app/api/errors.py` | domain exception → `{error, say}`, one handler per exception base |
| `app/api/phrasing.py` | every line the agent might say, in one auditable file |
| `app/api/routes/` | carriers, otp, loads, negotiations, bookings + handoff |
| `app/container.py` | constructs the service from `Settings`, including the fake TMS |
| `app/deadline.py` | the per-request time budget |
| `app/handoff/` | mocked senior-rep queue |
| `app/tms/fake_server.py` | moved out of `tests/` so `TMS_MODE=fake` can run it in-process |

The `run_id` contract holds throughout: no request accepts an `otp_session_id`, a
`negotiation_id` or a round number, and sending one is a 422 because every request model
sets `extra="forbid"`.

### Tests

```
395 passed in 5.65s
```

`tests/test_api.py` adds 60. The ones that matter most:

- **`test_every_route_that_needs_otp_is_gated`** walks the app's own route table and
  asserts every non-exempt route answers 403 to an unverified call. A route added later
  fails this test rather than shipping unauthenticated. (Getting this test to actually see
  the routes took two fixes: FastAPI no longer flattens included routers, so it recurses
  through `original_router`.)
- **`test_no_load_response_ever_contains_max_buy`** serialises the whole search and detail
  response and checks the ceiling is absent as a key or a value.
- **`test_the_agent_facing_fields_never_carry_the_code`** and
  `test_the_full_address_is_never_in_the_spoken_line` pin the OTP split.
- **`test_a_tms_fault_becomes_a_speakable_error`** asserts the response body contains no
  error code, host, command name or traceback.
- **`test_a_slow_tms_is_cut_off_by_the_budget`** proves the request budget stops the retry
  ladder and reports itself.
- Bookings: confirmed → `booked`; lost response that landed → `booking_pending` with no
  reference, never `booked`; `ALREADY_BOOKED` → `booking_pending`; a rate in the body → 422.

### Bugs found by the tests, and fixed

1. **`to_api_error` never reached the routes.** The generic `Exception` handler is
   re-raised by Starlette, so every domain failure surfaced as a stack trace instead of a
   sentence. Handlers are now registered per exception base.
2. **`TmsInvalidInput` was mapped to 502.** It is a `TmsFault`, and the `TmsFault` check
   came first — so our own bad request reported as a broken upstream. Order fixed and
   commented, because it will be re-broken by an innocent reordering.
3. **Running out of budget reported as a plain timeout.** The budget did stop the retries,
   but the error that surfaced was the first attempt's `TmsTimeout`. `_execute` now
   distinguishes "the budget is what stopped us" and raises `TmsBudgetExhausted` (504).
4. The fake TMS bound its port twice when `start_fake_tms()` was wrapped in `with`; `start`
   is now idempotent.

### Verified end to end

The README's curl sequence was run against a local uvicorn (loopback only) with
`TMS_MODE=fake`: health → verify → OTP send → verify → search (3 loads, no `MAX_BUY`
anywhere) → detail → open at 5125 → counter → accept at 5525 → booked with a reference →
handoff. Two amounts in the README had been written from memory and were wrong; they are
now the values that actually came back.

FMCSA from this machine returns the fail-closed answer, which is what the README documents
for a non-US region.

### Deliberate readings, recorded in DECISIONS.md

- **Load detail is gated too** (§30), though §8 does not list it: pitching freight to an
  unverified caller is what verification is for.
- **Fetching detail does not count as showing a load** (§31): otherwise "negotiate what you
  were shown" becomes "negotiate anything you can name".
- **`/handoff` is not gated** — it is the escape hatch for a carrier who can never verify,
  and gating it would make "route this to a human" a dead end.
- **A booking with no agreed rate is a 409, not a 403**: the carrier did nothing wrong.

### Open issues

Carried over from phases 1 and 2, still true:

1. **TMS token rejected** — no live load has ever been fetched.
2. **Every FMCSA field name is `TODO: verify`**; the fixtures are synthetic.
3. **FMCSA has never been called successfully** from here.
4. **OTP delivery is not wired to the platform.** The two views are returned in separate
   fields; mapping `delivery` into a hidden Tool Call Result field is the platform's step.
   Now confirmed possible by the platform docs, still unverified in the platform itself.

New in phase 3:

5. **Sessions and the handoff queue are per-process.** A second worker would not see the
   first one's calls. Redis behind `RunSessionStore` is the production path; the handoff
   queue has the same problem and the same answer.
6. **`/handoff` is the only durable record of a handoff.** The queue is in memory, so the
   `handoff_created` event is what survives a restart.
7. **No rate limiting on `/otp/verify`.** The per-call lock is three attempts, but a caller
   can start a new call (`run_id`) and try again. A per-MC limit needs shared state.
8. **`TMS_MODE=fake` is a development switch with no guard.** It is read from the
   environment like everything else; nothing stops it being set in a deployment, where it
   would serve fixture loads to real carriers.
9. **The search response shape for the agent is a list of full records.** Three loads is
   fine to speak, but the pitch text is assembled in the route, and it is the only place
   in the API where phrasing is built from data rather than fixed.

### Not started

Phase 4: Docker, docker-compose, deployment to a US region, live FMCSA verification.

---

## Phase 4 — hardening and deployment (2026-10-01)

**Status:** complete. Built, run and verified locally in Docker. **Not deployed** — that
needs your account.

### What was built

| Item | Where |
|---|---|
| OTP rate limit, 5/hour per MC across calls | `app/rate_limit.py`, applied in `app/api/routes/otp.py` |
| Startup guard: `ENV=production` + `TMS_MODE=fake` refuses to boot | `app/container.py` |
| `Dockerfile` — non-root, `PORT`-aware, healthcheck | repo root |
| `docker-compose.yml` — one replica, named volume for the event log | repo root |
| `.dockerignore` — `.env` never enters the build context | repo root |
| Deploy runbook | `docs/DEPLOY.md` |

### Tests

```
424 passed in 5.73s
```

`tests/test_rate_limit.py` adds 15 for the limiter; `tests/test_api.py` grew by 14:
the limit holding across calls and run ids, `Retry-After` being present and sane, a
different MC being unaffected, the blocked line not leaking its mechanism, a *correct*
code still costing an attempt, and the production guard accepting `production`/`prod` in
any case while leaving `live` + production alone.

### Verified in Docker

- `docker build` succeeds; the image is non-root (`uid=10001(appuser)`) and contains no
  `.env` (`ls -a /app | grep .env` → 0).
- `docker compose up` reaches `healthy` and serves a **full call** through the container:
  verify → OTP send → verify → search (3 loads, no `MAX_BUY`) → open at 5125 → counter →
  accept at 5125 → `booked` with a reference → handoff ticket.
- The event log lands on the named volume (`/data/events.jsonl`).
- `ENV=production TMS_MODE=fake` → `ConfigurationError`, container exit code **1**.
- `PORT=9111` is honoured, and the healthcheck follows it (`healthy`).

### The sleep question

Answered in `docs/DEPLOY.md`. **Railway does not sleep idle services** — they run and bill
continuously, with a ~$1/month recurring credit on the free plan, so ~$5/month Hobby is the
practical minimum. **Render's free web tier sleeps after 15 minutes** of no traffic with a
~20–60 s cold start, which for a voice agent is not a latency cost but a failed call. That
ruled Render out. Sources are linked in the doc.

### Open issues

Carried over:

1. **TMS token rejected** — no live load has ever been fetched.
2. **Every FMCSA field name is `TODO: verify`**; fixtures are synthetic.
3. **FMCSA has never been called successfully** from here. A US deploy is what closes this,
   and `docs/DEPLOY.md` describes the exact call that does it.
4. **OTP delivery is not wired to the platform yet.**
5. **Sessions, the handoff queue and now the rate limiter are all process-local**, which is
   why `docs/DEPLOY.md` pins one replica. Redis behind the same interfaces is the path to
   more.
6. **A restart loses in-flight calls and the handoff queue.** The `handoff_created` event
   on the volume is the durable record.

New in phase 4:

7. **The rate limiter is per process**, so a second replica would double the effective
   limit. It pairs with the one-replica constraint rather than standing alone.
8. **`SERVICE_API_KEY` empty in production does not fail startup** — it starts and then
   answers 503 to everything. The fake-TMS guard is the only startup guard; this one is a
   candidate for the same treatment.
9. **No CI.** Nothing runs the suite on push; it is green because it was run here.
10. **The deploy is documented, not executed.** The one-command path and the required
    variables are untested against the platform's own behaviour.

### Done

All four phases of CLAUDE.md §10.
