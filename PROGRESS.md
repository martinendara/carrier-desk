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
8. ~~`SERVICE_API_KEY` empty in production does not fail startup~~ — fixed in the
   follow-up above, along with the OTP secret.
9. ~~No CI.~~ — `.github/workflows/tests.yml` runs the suite on every push.
10. **The deploy is documented, not executed.** The one-command path and the required
    variables are untested against the platform's own behaviour.

### Done

All four phases of CLAUDE.md §10.

### Follow-up (same phase)

- **The production guard is now a set.** `ENV=production` also refuses to start with an
  empty `SERVICE_API_KEY` or an empty `OTP_HMAC_SECRET`, both of which previously started
  and failed at request time in a way that looks like an outage. Ordering puts the fake-TMS
  message first when several are wrong. Four tests added — **428 passed**.
- **CI.** `.github/workflows/tests.yml` runs `pytest` on every push and pull request, with
  the badge in the README. No secrets and no `.env`: every test runs against the in-process
  fake TMS and a mocked FMCSA, so the job needs nothing from the environment. That absence
  is the point — it is what makes the badge mean something.

---

## Post-deploy — the service is live, and FMCSA answered (2026-10-01)

**Status:** deployed by Martín on Render (free tier, Ohio) with `ENV=development` and
`TMS_MODE=fake`, which is a development instance rather than the production path. The
runbook in `docs/DEPLOY.md` was run against it. The service is reachable and behaves. The
FMCSA field names are still not confirmed — but the response now lands in the log.

### Runbook checks

| Check | Result |
|---|---|
| `GET /health` | `{"status":"ok"}` |
| Wrong `X-API-Key` | 401 `unauthorized` |
| `POST /loads/search` before OTP | 403 `not_verified` |
| `POST /carriers/verify`, `mc_number: "abc"` | 422 `invalid_mc` |
| Unknown path | 404 `http_error` |
| `POST /otp/send` | 200 — masked destination for the agent, code for the platform |
| `POST /carriers/verify`, real MC | 200, but `fmcsa_rejected:incomplete_response` |

No cold start was observed: the instance was already awake and every call answered in
0.3–0.5 s.

### The finding

**FMCSA answers from Ohio.** The parser reads `legalName` and `dotNumber` correctly and then
refuses every real carrier, because at least one of `allowedToOperate` / `outOfService` /
`statusCode` is not a key the docket-number response carries.

| MC | What FMCSA returned | What we decided |
|---|---|---|
| 123456 | `B MARRON LOGISTICS LLC`, DOT 3177404 | `incomplete_response` |
| 778899 | `BLUE TRUCKING LLC`, DOT 2280021 | `incomplete_response` |
| 445566 | `S & E TRUCKING COMPANY LLC`, DOT 1068411 | `incomplete_response` |
| 998877 | `CLABORN ENTERPRISES LLC`, DOT 2864989 | `incomplete_response` |
| 000000 | — | `not_found` — the FMCSA 404 path works |

Failing closed on every real carrier is still failing closed, and the consequence is that
100% of live calls would route to a human. `docs/DEPLOY.md` predicted exactly this outcome.

### Anomaly, unresolved

The **first five authenticated requests returned 500** `internal_error`. The same requests,
minutes later, returned 200/403/422 as designed. Not reproducible locally, nor in a clean
`docker build` of the same `Dockerfile` with the same `--env-file`. Whether that was the
free instance waking, a rollout, or something else is unknown. A 500 during the start window
is worse than a slow one: it reads as a broken integration rather than a cold host. The
host's `unhandled error on <path>` line from that window is what would settle it.

### Change: the payload goes to the log

`app/fmcsa/client.py` now logs the carrier record — keys **and** values — on the two paths
where a response is not understood, capped at 2000 characters and with the web key redacted
if the body ever echoed it. `top_level` (the record's own key names) is printed separately
so the names survive truncation, and a recognised payload still logs nothing. See
`DECISIONS.md` §44 and the "After the first deploy" section of `docs/DEPLOY.md`.

**433 passed**, 5 of them new: keys and values reach the log, top-level names survive
truncation, an unrecognised wrapper is logged, the web key never reaches the log, and a
healthy payload is not dumped.

### Open issues

Updated from the phase 4 list:

1. **TMS token rejected** — unchanged. The deploy runs `TMS_MODE=fake`, so no live load has
   been fetched there either.
2. **FMCSA field names** — partly closed. `legalName` and `dotNumber` are **confirmed**
   against real responses. `allowedToOperate`, `outOfService`, `statusCode`, the authority
   date and `_ACTIVE_STATUS` remain open, pending the log line from the deploy.
3. ~~FMCSA has never been called successfully~~ — **closed.** Called from a US region; it
   answers, the 404 path works, and the eligibility decision runs end to end.
4. **OTP delivery is not wired to the platform yet.** Unchanged.
5. **Process-local state** (sessions, handoff queue, rate limiter) — unchanged; still one
   replica, and the free tier restarts the process on every spin-down.
6. **A restart loses in-flight calls.** Unchanged.
7. **The deploy is no longer documented-but-unexecuted.** What is still untested is
   `ENV=production` with `TMS_MODE=live` against the real platform.
8. **The free tier sleeps.** Render's free web tier spins down after 15 minutes of no
   traffic, which is why `docs/DEPLOY.md` recommends Railway for anything with a phone line
   on it. The instance that is up now is a development deploy with a fake TMS.

---

## The parser meets a real payload (2026-10-01)

**Status:** the recorded response closed the `TODO: verify` markers the deploy opened. Two
of the guesses were wrong in ways that mattered, and one field the decision depended on
turns out not to exist at all.

### What the recorded payload settled

| Field | Verdict |
|---|---|
| `legalName`, `dotNumber` | correct — and `dotNumber` is an **integer**, not the string the docs describe |
| `allowedToOperate` | correct (`"Y"` / `"N"`) |
| `statusCode` | exists, but is **not the authority**: the record carries `"A"` with both authority statuses inactive |
| `outOfService` | **does not exist.** The nearest field is `oosDate`, which is null for this carrier |
| `commonAuthorityStatus`, `contractAuthorityStatus` | the real authority signal — `"I"` and `"N"` here |
| `bipdInsuranceRequired` / `bipdInsuranceOnFile` | the required liability cover, and nothing filed |
| authority date | **not in the record at all** — see the open issue below |

In the end, this carrier is refused for its authority, not for the insurance: the record
is allowed to operate and its carrier record is active, and neither kind of operating
authority is in force.

### The rule now

`allowedToOperate == "Y"` and at least one of common / contract authority `"A"` and
`oosDate` null — with insurance after those, a reason each:
`not_allowed_to_operate`, `authority_inactive`, `out_of_service`, `insurance_not_on_file`.
Both new decisions are in `DECISIONS.md` (§45 the authority, §46 the insurance, §47 the
four fields the agent is handed, §48 the unknown age).

### What the agent is handed

`{eligible, result_code, legal_name, say}`. Nothing else: no DOT number, no reason code,
no insurance, no address, no tax id. `ein` is stripped on the way in, so it is not in
`raw`, not in an event and not in a log line.

### Fixtures

Six synthetic files deleted. One recorded response is the source
(`tests/fixtures/fmcsa_carrier_recorded.json`), and every other carrier in the tests is
that payload with named fields changed — `fmcsa_payload(**overrides)` in `conftest.py`.
An eligible carrier and each of the four rejections are one key apart, and the test that
asserts each reason shows which key.

The wrapper around the carrier object is the one thing still assumed: what the log carried
was the carrier record, because that is what our own parser had already stripped.

**438 passed** (5 new tests).

### Verified against the deploy

`POST /carriers/verify` with MC 123456 against the Render instance, after it picked
up the commit:

```json
{"eligible": false, "result_code": "fmcsa_rejected:authority_inactive",
 "legal_name": "B MARRON LOGISTICS LLC",
 "say": "That authority isn't active right now."}
```

Which is the whole point of the exercise: **refused for its authority, not for
`incomplete_response`.** Four fields, as intended — no DOT number, no reason code, no
insurance, no address. Eighteen consecutive calls, all identical.

The first call took 22.7 s (the free tier's cold start), the rest 0.3–0.7 s. That
confirms the shape of the earlier anomaly too: a sleeping instance pays the cost on the
first call, and the 500s seen before it was warm are still unexplained but are not the
steady state.

`insurance_not_on_file` is not reachable with this MC — the authority check comes first
and this carrier fails it. It was reached with another one: **MC 778899** (BLUE TRUCKING
LLC) comes back `fmcsa_rejected:insurance_not_on_file`, which is the first time a live
carrier has exercised that reason, and the first proof from the outside that the authority
check passes for a carrier that holds one.

---

## Diagnosing a 422, and the authority record (2026-10-01)

Two things that were only invisible because the service could not see what a remote system
sent.

### The 422 line

The platform's test panel returns 422 and there was no way to tell which field it sent
wrong. The handler logged `exc.errors()` whole, which carries the offending input under
`"input"` — so the line was both a poor diagnosis and a place a caller's value could land.

It now prints one entry per failed field: path, error type, and pydantic's own wording.

```
WARNING app.api.errors: invalid request on /carriers/verify: body.mc_number: missing (Field required)
WARNING app.api.errors: invalid request on /carriers/verify: body.mc_number: string_type (Input should be a valid string)
WARNING app.api.errors: invalid request on /negotiations/counter: body.negotiation_id: extra_forbidden (Extra inputs are not permitted)
```

**A likely cause, from `docs/workflow-sample.json`.** The tool node builds its body out of
use-case variables: `{"run_id": "{{$var:….run_id}}", "mc_number": "{{$var:….mc_number}}"}`.
A panel run with either variable unset sends a body that is wrong in a way only this log
can name — and if both are unset, the first failure reported will be `run_id`.

### The authority record

The last open `TODO: verify`. The carrier payload has no authority date, and the candidate
source is the per-DOT authority endpoint — whose field names are as unverified as the
carrier payload's were. So it is read the same way: fetch it, print it, keys and values,
capped, web key redacted.

```
WARNING app.fmcsa.client: FMCSA authority record for DOT 3177404. TODO: verify the field
names, then parse the grant date into authority_age_days. payload={"content": [...]}
```

It is fetched **only for a carrier that holds an authority**, which is what makes a date
meaningful and keeps the second round trip off every call refused before then.
`AuthorityCheck.authority_active` carries that fact, since "no authority" and "never got
that far" are different things. The call is capped at 2 s and swallowed on failure: the
verdict is already decided by the time it runs.

**What is not proven.** The Render log has not been read, so whether the endpoint's path
and shape are right is still open. The only external signal is latency, and it is weak:
MC 778899 (two calls) answers in a median ~0.46 s against MC 123456's ~0.37 s — a delta of
about 70 ms, which is one fast FMCSA round trip and is *consistent* with the extra call
without being evidence of it. A wrong path would look the same from outside; it will show
up in the log as a 404, which is the line the log prints for exactly that reason.

**446 passed** (8 new tests).

### Open issues

Updated:

2. **FMCSA field names** — closed for everything the payload carries. `legalName`,
   `dotNumber`, `allowedToOperate`, `statusCode`, the two authority statuses, `oosDate` and
   the insurance pair are all read from a real response now.
3. ~~FMCSA has never been called successfully~~ — closed.
4. **The authority date is the last `TODO: verify`.** The record has no authority date, so
   `authority_age_days` is `None` for every carrier and onboarding sends every lead to
   `Review`. The candidate source is the per-DOT authority endpoint
   (`/carriers/{dotNumber}/authority`), which is **unverified** — it has not been called
   from a US region. Until it is, `Review` is the answer, not a guess.
5. **OTP delivery is not wired to the platform yet.** Unchanged.
6. **Process-local state** (sessions, handoff queue, rate limiter) — unchanged.
7. **A restart loses in-flight calls.** Unchanged.
8. **The free tier sleeps.** Unchanged.

---

## QA run — script (2026-10-02)

**Status:** the QA driver is written and smoke-tested; the production run is gated on the
operator (see below).

### What was added

- `scripts/qa_run.py` — drives the 8-tool HTTP surface exactly as the voice agent would,
  asserting each scenario's expected outcome. Reads `QA_BASE_URL` / `QA_API_KEY` /
  `QA_REVIEWER_MC` / `QA_REVIEWER_OTP` from the environment, so nothing secret enters the
  repo. Exit code 0 iff every scenario passes.
- 22 end-to-end scenarios: 5 standard (book at first offer, after 1/2/3 counters, urgent),
  6 edges (FMCSA inactive, OTP 3× fail, no match, no deal, clarify, idempotent counter and
  book), 11 adversarial (no-OTP 403s, send before verify, `mc_number`/`agreed_rate` 422,
  over-ceiling never accepted, not-shown load 409, 4th round 409, cross-run isolation 409).

### Re-runnability (the simple answer)

The run is repeatable because the deployment serves `TMS_MODE=fake` seeded from
`tms_demo.txt` (20 loads), and the fake server never removes a booked load from search —
booking does not deplete the pool. Every scenario uses a fresh `qa-<name>` run_id, and the
`qa-` prefix keeps its events out of the live KPIs (`source='qa'`). Nothing to build; the
script just relies on those two invariants.

### Two scenarios are pytest-only, not end-to-end

- **Onboarding (`routed_onboarding`)** — per the approved plan, covered in pytest
  (`test_api.py`, `test_otp.py`, `test_call_flow.py`), not driven against production.
- **`booking_pending` on a TMS fault** — needs an injected `LOAD_BOOK` fault, which the
  production fake TMS (normal mode) never produces. Covered in pytest (`test_api.py`).

### Smoke test

`scripts/qa_run.py` was run against a local instance (fake TMS + mocked FMCSA + reviewer
mode): **22/22 passed**. Full suite: **554 passed**.

### Before the production run (operator checkpoint)

1. Raise `OTP_VERIFY_RATE_LIMIT_PER_HOUR` (default 5) or exempt reviewer mode — the QA
   reuses reviewer MC 15735 for ~18 verifies and will otherwise 429 after the 5th.
2. Confirm the deploy is at the commit with `tms_demo.txt` and has
   `TMS_FAKE_FIXTURES=tests/fixtures/tms_demo.txt`.
3. Paste `twin/schema.sql` into Twin (the `source` column and live-only time metric are in
   it), and clean prior `qa-%` rows before the run.

---

## Architecture review, Twin signals, and a seed mode (2026-10-03)

**Status:** all three complete and verified. Nothing pushed; the deploy still runs the
previous build.

### 1. `docs/ARCHITECTURE_REVIEW.md` (read-only)

An audit of everything the service keeps in memory or on local disk, and what each failure
does to a live call: two instances, a redeploy mid-call, Twin down, the TMS slow or down,
FMCSA down. Capacity against ~500 loads/week with Monday-AM and Friday-PM peaks. Twelve
ranked risks with severity, PoC relevance and a phase-2 fix.

Three findings worth acting on before anything else, all free:

1. **`/health` is a synchronous handler** (`app/main.py`), so it shares the 40-thread pool
   with every TMS call. A TMS stall can starve the healthcheck and get a working service
   restarted — which loses every in-flight call.
2. **The event log is not durable on the current deploy.** `EVENTS_JSONL_PATH` points inside
   the container and there is no `render.yaml` and no mounted volume, so the JSONL that
   `docs/DEPLOY.md` calls "the durable record" dies with the instance.
3. **Nothing reaps expired sessions or handoff tickets.** `purge_expired` has no caller in
   `app/`; the handoff queue only ever appends, and each ticket holds a ceiling.

Conclusion on capacity: one instance has roughly two orders of magnitude of headroom at
this volume. The binding constraint is restarts and cold starts, not throughput.

### 2. Twin views (`twin/schema.sql`)

Six new views, all exposing `source`, all re-runnable:
`v_signal_unserved_demand`, `v_signal_price_check`, `v_signal_recoverable`,
`v_rate_per_mile`, `v_rep_queue`, `v_onboarding_leads`. Plus `v_run_source` (one definition
of `qa`/`review`/`live`, now read by `v_calls` too), the `dropped` outcome and
`dropped_at_stage` in `v_calls`, and a rewritten `v_funnel` that explains every drop.

**Verified on Postgres 16.15**, not by inspection: the file was applied twice over live
data with no error, and `entered = passed + completed + in_flight + Σ exited` was asserted
at all six funnel stages. Two bugs surfaced only because of that:

- `v_rep_queue` joined its lane CTE on `load_id` when the CTE was `DISTINCT ON (run_id)`,
  which fanned every row out across every other call that touched the same load.
- `v_calls.searched` read the first search rather than the best, so a call that looked at
  Hawaii and then booked out of Illinois would have read `no_match` (DECISIONS §63).

**Two fields the views want and the events do not carry** (both reported, neither changed —
they need a code change and a redeploy, and the user asked to be told first):

1. `load_pitched` is emitted only by `GET /loads/{load_id}`, which the platform no longer
   calls, so `v_signal_price_check.pitches` is 0 for every load. Minimal fix: emit it per
   shown load in `/loads/search`, where the loads are already recorded.
2. `lead_created` is never emitted at all, and `app/onboarding/qualification.py::qualify`
   has no caller outside the tests. `v_onboarding_leads` therefore builds on
   `handoff_created` with `reason = 'onboarding'`. Minimal fix: call `qualify()` in
   `/otp/send` on the `routed_onboarding` path and emit `lead_created`.

### 3. Seed mode (`scripts/qa_run.py --seed`)

Five steps creating four unserved lanes, the same load closed twice, three closes within 5%
of the ceiling, three bookings at three different depths of the ladder, and an onboarding
lead. Every run id is `qa-seed-<token>-<name>` with a timestamp token, so a re-run never
collides with the previous one's session.

Verified end to end against a local instance with the demo fixture and a stubbed FMCSA
(FMCSA blocks Argentina, so a real lookup here fails closed): **5/5 steps pass**, the 96
events they produce load into Postgres, and all six views come back populated. The QA
scenarios still pass **22/22** in the same session, so the refactor of the registry did not
regress them.

Two assumptions are checked rather than assumed: the seed's ceiling formula is asserted
equal to `_fake_max_buy` for all 20 demo loads, and each lane's filters are asserted to
isolate exactly the intended load.

### 4. Live TMS connectivity diagnostic

`scripts/tms_connectivity.py` (new) re-probed the real server from the local machine with
raw socket I/O. **The token is still refused** — same `AUTH_FAILED` as 2026-10-01, same
~165 ms round trip — but the probing settled three framing questions that were open,
because a malformed frame gets a *different* error than a refused token:

| Frame | Answer |
|---|---|
| well-formed, real token | `ERR\|CODE:AUTH_FAILED\|MSG:Invalid or missing auth token` |
| well-formed, **no AUTH field** | same `AUTH_FAILED` |
| well-formed, unknown command | same `AUTH_FAILED` |
| well-formed, **wrong token** | same `AUTH_FAILED` |
| **LF terminator, no CR** | **no bytes at all** (5 s deadline) |
| **AUTH before CMD** | **`ERR\|CODE:MALFORMED\|MSG:Malformed frame`** |

Confirmed on the wire: **`\r\n` is required**, and **`CMD` must come first** — putting
`AUTH` first is rejected as `MALFORMED`, which proves the server parses the frame before it
authenticates. Authentication also runs *before* command validation. "Invalid" and
"missing" are one case, so the message cannot be read as a statement about this token
specifically; a stale credential and a server that accepts none look identical from
outside, and both are fixed by re-issuing it.

Recorded in `docs/tms-protocol.md` and `docs/tms-connectivity-2026-10-03.md`.
**No `LOAD_BOOK` was sent**, and the four open protocol questions are still unanswered —
they need a working token. `tests/test_connectivity_guard.py` (15 tests) pins the
read-only allowlist and the masking.

### 5. A flaky test, found and fixed

`tests/test_fmcsa.py` had two tests that asserted on `caplog.text` without waiting for the
background authority fetch — `check()` fires it on a daemon thread by design and returns
immediately. Roughly one run in ten, the log line had not been emitted yet and
`caplog.text` was empty. Pre-existing (reproduced on clean `HEAD`), and fixed by adding
`client.join_recon()`, which `FmcsaClient` already documents as "used by tests" and which
a third test in the same file was already calling.

Ten consecutive full runs since: **569 passed, ten times out of ten.**

### Tests

```
569 passed
```

(No new coverage for the views or the seed — the suite does not run the scripts. Both were
verified by running them, which is recorded above. `test_connectivity_guard.py` adds 15 for
the diagnostic's safety properties.)

---

## Follow-up: the missing events, a faulting fake, and the review's cheap fixes (2026-10-03)

**Status:** all five items done except the multi-MC one, which is **not done** — see below.
Nothing pushed until the operator says so.

### 1. The two events the Signal views were missing

- **`load_pitched`** is now emitted once per load returned by `/loads/search`. It came only
  from `GET /loads/{load_id}`, which the platform stopped calling, so `pitches` was 0 for
  every load (DECISIONS §65).
- **`lead_created`** is now emitted by `/otp/send` on the `routed_onboarding` branch, with
  the output of `qualify()`: temperature, reason, authority age, matching loads. The
  authority age is kept on the run session by `/carriers/verify` so the later step does not
  ask FMCSA again (DECISIONS §66). Today that resolves to `Review` for two independent and
  correct reasons — unknown authority age, and a call that matched no loads.

### 2. `TMS_FAKE_FAULT_RATE`, and the retry log

`TMS_FAKE_FAULT_RATE` (default 0) makes the fake answer that share of operational commands
with a random truncated response or malformed frame. `DEBUG_ECHO` is never faulted; `live`
mode ignores it (DECISIONS §67).

**A deviation worth naming.** The pool holds *recoverable* faults only — a genuine timeout is
deliberately excluded. The read deadline is 5 s under an 8 s request budget, so a real
timeout eats the budget in one attempt and the retry never fits: every injected timeout would
surface as a `504` instead of a recovered retry, and a demo that fails one call in ten shows
nothing about the ladder. Say the word and it goes back in, but it changes what the knob
demonstrates.

**The metric was not real, and now is.** `logger.info` in this package was **discarded before
being formatted**: nothing configured logging, the root logger has no handler and sits at
WARNING, and uvicorn configures only its own loggers. The TMS client logs failures at WARNING
and successes at INFO — so in production only the failures existed, and a retry that worked
looked exactly like a retry that never happened. `configure_logging()` now sets the `app`
logger to INFO with its own handler (DECISIONS §68), and both lines carry the same four
facts: command, attempt n/N, outcome, latency — plus `retrying` / `no further attempt` on the
failure side and `recovered after N failed attempt(s)` on the success side.

```
tms LOAD_QUERY attempt 1/3 failed: timeout after 5.001s - retrying
tms LOAD_QUERY attempt 2/3 ok: 1 record(s) in 0.041s - recovered after 1 failed attempt(s)
```

**The booking invariant holds, and now under every fault sequence.** When `LOAD_BOOK`'s answer
is lost *and* the `LOAD_GET` recovery fails too, the outcome is `booked_unconfirmed` with no
reference → `booking_pending`, never `booked`. This reverses DECISIONS §11, which chose to let
the exception propagate: that gave the carrier a generic error line and produced no rep
ticket, for no gain. Every exit from the recovery returns no reference, so the guarantee is
about the claim rather than the branch. §11 carries a pointer to §69, and a test walks every
script the fake can produce and asserts none of them yields `booked`.

### 3. The architecture review's cheap fixes

- **`/health` is `async def`** (DECISIONS §70). A sync handler shares the 40-thread pool with
  every TMS call, so a TMS stall could starve the probe and get a healthy service restarted —
  which drops every call in flight.
- **Sessions and handoff tickets are swept on the write path** (DECISIONS §71): the store
  purges once past 500 entries, the queue drops tickets past `HANDOFF_RETENTION_SECONDS`
  (default 24 h). No thread, no lifecycle.
- **The event log's non-durability is documented, not fixed.** On Render there is no volume,
  so `events.jsonl` dies with the instance; Twin is the durable store. A persistent disk is
  $0.25/GB/month and was ruled out for the POC.

### 4. Multiple reviewer MCs — **not done**

The estimate is **30–45 minutes** of code plus tests (turn `REVIEWER_MC` into a list,
`OtpService` membership check, settings validation, ~10 tests, plus a seed that uses a
different MC per lane). That alone is over the 20-minute budget, and it is blocked anyway:

**There are only two MCs with a recorded eligible verdict** — `15735` (ALLIED VAN LINES INC,
the current reviewer MC, 21 recorded checks) and `107012` (NORTH AMERICAN VAN LINES INC).
Everything else on record was refused: `123456` authority_inactive, `778899`
insurance_not_on_file, `445566`/`998877` incomplete_response, `000000` not_found. FMCSA is
403 from Argentina, so new candidates cannot be discovered from here, and enumerating them
against the deploy means guessing MC numbers at the live FMCSA quota.

**What would unblock it:** three or four MC numbers the operator knows are real, active and
eligible. With those, the code change is the 30–45 minutes above and the seed can give each
lane its own carrier.

### 5. One thing noticed while doing the above

`REVIEWER_MODE` has **no production guard.** `_enforce_production_guards` refuses a fake TMS
and empty secrets, but a deploy with `ENV=production` and `REVIEWER_MODE=true` would start and
issue a fixed code for the configured MC. Not changed, because the current deploy is
`ENV=development` and enabling reviewer mode in production is exactly what an evaluator might
want — but it is the same class of guard as §42 and worth a decision.

### Tests

```
596 passed
```

New: 7 for the fake's fault rate, 5 for `load_pitched` / `lead_created`, 2 for the booking
invariant under fault, 4 for the retry log and the logging fix, 1 for the async health
endpoint, 8 for the sweeps.

### DIAG removed

`_diag_middleware` and its registration (`app/main.py`), the `DIAG search` line
(`app/api/routes/loads.py`) and the `DIAG auth` label (`app/security.py`) are gone. The 401
log line stays — it is a security signal — reworded to say which of the two it was without
the DIAG marker.

### Open issues

1. **`requested` handoffs stay `in_progress` forever.** They have no outcome of their own in
   the closed result-code set, and a handoff is a terminal event, so they are neither
   `dropped` nor finished. Adding a `handed_off` outcome would fix it; that widens a closed
   vocabulary and waits for a decision.
2. **A dashboard that reads `v_funnel` needs updating.** The shape changed from one row per
   stage to one row per `(stage, bucket)`.
3. **The seed needs ~12 OTP verifications on one MC** (all on the reviewer MC). See below.
4. Everything the architecture review lists, in particular R4 (`/health`), R3 (durable
   events) and R6 (no reaper) — all free, none done yet.

### Before the seed run (operator checkpoint)

- `OTP_VERIFY_RATE_LIMIT_PER_HOUR` is **100** right now: comfortable. The seed spends **12**
  verifications on MC 15735 and the 22 QA scenarios spend ~19 more, so both in one hour is
  ~31. At the planned permanent value of **20**, one seed run fits and the QA scenarios
  would already 429 — the two cannot share an hour at 20.
- The deploy must be at a commit whose `TMS_FAKE_FIXTURES` points at `tms_demo.txt`, because
  the seed's ceiling arithmetic is derived from that file and from nothing else.
- Clear a previous seed with `DELETE FROM call_events WHERE run_id LIKE 'qa-seed-%';`.
