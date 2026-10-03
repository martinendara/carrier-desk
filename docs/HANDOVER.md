# HANDOVER — carrier-desk

For a fresh session to resume without re-deriving anything. Read this, then
`git log --oneline -3`.

**Frozen as of 2026-10-03.** The build is complete, published and verified. No further
code changes are planned; treat everything below as the end state.

## Repo state

- Branch `main`. Tests: **678 passed**; GitHub Actions **green**.
- **Deployed and live: `e3db5e6`** on `https://carrier-desk.onrender.com` —
  `e3db5e6b7922eca6dcb828f71310e74819936342`, read from `/health` after the deploy went
  Live. `ENV=development`, `TMS_MODE=fake`, `TMS_FAKE_FAULT_RATE=0.10`,
  `OTP_VERIFY_RATE_LIMIT_PER_HOUR=100`.
  *The commit that carries this document is one ahead and docs-only; the running code is
  identical to `e3db5e6`.*
- `python -m pytest` is the whole suite; it needs no `.env` (fake TMS + mocked FMCSA).
- **The deploy's `X-API-Key` is the value in `.env` named `RENDER_API_KEY`.** The name is
  misleading: that key does **not** work against `api.render.com` (it answers 401 there). It
  is the service's own key, and it is what `scripts/qa_run.py` wants as `QA_API_KEY`.

## Final state (2026-10-03)

| | |
|---|---|
| Service | Deployed on Render, **Live on `e3db5e6`**, verified from `/health` |
| Voice workflow | **Published**, running the prompt in `docs/prompt-live.md` verbatim |
| Adversarial tests (platform) | **6/6** — run at 10:07, after two rounds of prompt corrections |
| App | **v2 published** — built against `docs/APP_SPEC_TANDA2.md` |
| Tests | **678 passed**, three consecutive runs |
| Twin | `twin/schema.sql` pasted, all views live |

The corrections the adversarial runs produced, in the order they surfaced. Only the first
two are behind the 5/6 → 6/6 jump; the rest came from re-running the suite against a
workflow that was already passing.

1. **The close ended the call twice** (ADV-5). The close `say` does not sound like a
   goodbye, so `Ending` took its "otherwise, say one short goodbye" branch and added a
   second farewell; and the "ask for a person" rule, unbounded, fired a handoff after the
   close it was sitting next to. A defined ending is now terminal whatever the `say` sounds
   like, and the person rule is bounded to before the call ends.
2. **The agent invented a six-digit MC.** The claim was in the transcription context
   ("MC numbers (6 to 7 digits)"), not in the prompt. Fixed there, and the prompt's two
   six-digit examples became a neutral five-digit one. The service never required six: its
   validation is `^\d{1,8}$`, so `15735` always passed.
3. **The close revealed a ceiling and promised a rep.** "I can't go higher than that"
   confirms an upper bound exists without naming it, and nobody follows up on a walk-away.
   Rewritten, and every other agent-facing string audited for the same two faults.
4. **The pitch read out numbers to someone driving.** It now leads with route, pickup and
   equipment, then the offer; weight, miles and dimensions are answered only if asked.
5. **A carrier who pushed without naming a price heard the offer repeated** until the call
   ran out of turns. The agent now asks once, converts a relative figure to a total, and
   knows that asking costs no round.

Frozen means frozen: the remaining items at the bottom of this file are known and
deliberate, not unfinished.

## The documents written this round

| File | What it is |
|---|---|
| `docs/ARCHITECTURE_REVIEW.md` | Read-only audit: every piece of in-memory and on-disk state, what each failure does to a call, capacity, and twelve ranked risks |
| `docs/APP_SPEC_TANDA2.md` | **Self-contained** spec for the Sandbox builder (no repo access): the four signals, the rep queue, the overview card, dropped call records, empty states, exact view and column names |
| `docs/tms-connectivity-2026-10-03.md` | Raw evidence of the live TMS probe, token masked |
| `docs/prompt-live.md` | **Source of truth for the Prompt node.** Paste it whole |
| `docs/transcription-context.md` | The corrected agent voice transcription context, ready to paste |

## Architecture (15 lines)

- FastAPI service `carrier-desk` = the backend the HappyRobot voice agent calls
  as HTTP tools. It owns all business logic and integrations; the agent only
  talks.
- Layout: `app/` (main, config, security, container), `app/api/routes/` (the 8
  tools), `app/tms/` (line-protocol client + fake server), `app/fmcsa/`,
  `app/otp/`, `app/negotiation/` (engine + policy), `app/sessions/`,
  `app/events/`, `app/mail/` (Resend), `app/twin/` (TwinWriter).
- Call flow: verify_carrier → send_otp → verify_otp → search_loads →
  open_negotiation → counter_offer → book_load → handoff.
- State is in-memory per `run_id` (`RunSessionStore`, TTL 1h). One replica only.
- Events go to `EventSink`: JSONL (durable backup) + Twin (background).
- TMS: raw TCP line protocol, one connection per request; `TMS_MODE=fake` replays
  `tests/fixtures/*.txt`.
- FMCSA: `mobile.fmcsa.dot.gov` docket-number lookup; **blocked from Argentina**
  (403), so live FMCSA only works from a US host.
- OTP: 6 digits, HMAC-SHA256 keyed, TTL 5m, 3 attempts, email via Resend.
- Negotiation: deterministic concession ladder; `MAX_BUY` never leaves the server.
- Deployed on Render free tier as `ENV=development` + `TMS_MODE=fake`.

## The 8 tools (routes, fields, result codes, handoff reasons)

| tool | method/path | request → response |
|---|---|---|
| verify_carrier | POST `/carriers/verify` | `{run_id, mc_number}` → `{eligible, result_code, legal_name, say}` |
| send_otp | POST `/otp/send` | `{run_id}` → `{status, masked_destination, result_code, say}` |
| verify_otp | POST `/otp/verify` | `{run_id, code}` → `{verified, attempts_left, result_code, say}` |
| search_loads | POST `/loads/search` | `{run_id, filters…}` → `{loads, count, result_code, say}` |
| open_negotiation | POST `/negotiations` | `{run_id, load_id}` → `{load_id, offer, say}` |
| counter_offer | POST `/negotiations/counter` | `{run_id, carrier_ask}` → `{action, amount, result_code, say}` |
| book_load | POST `/bookings` | `{run_id}` → `{status, booking_ref, result_code, say}` |
| handoff | POST `/handoff` | `{run_id, reason?}` → `{ticket_id, say}` |

- `send_otp.status`: `sent | routed_onboarding | resend_limit_reached |
  already_verified | locked | send_failed`.
- `counter_offer.action`: `accept | counter | close | clarify`.
- `book_load.status`: `booked | booked_unconfirmed`.
- **result_codes (closed set):** `booked`, `booking_pending`,
  `negotiation_failed`, `no_match`, `otp_failed`, `routed_onboarding`,
  `fmcsa_rejected:<reason>`.
- **handoff reasons:** `booked`, `booking_pending`, `negotiation_failed`,
  `onboarding`, `fmcsa_rejected`, `requested`.

## TwinWriter (`app/twin.py`)

- **Real endpoint:** `POST https://platform.happyrobot.ai/api/v2/twin/tables/
  {table}/rows`, body `{"values": {<one row>}}`, one row per request → `201`.
  (The documented `/rows/bulk` does **not** exist.)
- **Row format:** every value is a **string**; null columns are **omitted**;
  `details` is the payload JSON-encoded as a string.
- **Idempotency:** `409` (or a body containing `duplicate`/`already exist`/
  `unique`) is treated as success; no retry. Otherwise short timeout + 2 retries,
  and JSONL is the durable backup.
- Enabled by `TWIN_ENABLED` (default false). Backfill: `scripts/twin_backfill.py`
  (deterministic `id`, local checkpoint in `var/twin_backfill_checkpoint`).

## Twin schema + views (`twin/schema.sql`, re-runnable)

Applied twice over live data with no error; every view is dropped and recreated
by the file, so re-pasting it is the update path.

- `call_events` (one row per event) + `v_run_source`, `v_calls`, `v_funnel`,
  `v_kpi_overview`, `v_call_timeline`, and the Signals/rep-queue set below.
- **`source` is defined once**, in `v_run_source`: `qa` if `run_id LIKE 'qa-%'`,
  else `review` if any event of the call carries `details.reviewer = true`, else
  `live`. Every view that exposes `source` reads it from there.
- `v_calls` exposes `outcome`, `last_stage` (the last step of the call reached:
  `fmcsa` / `otp` / `search` / `negotiation` / `booking`), `dropped_at_stage`
  (that stage, but only when `outcome = 'dropped'`) and `funnel_step` (0–5, how
  many funnel stages the call got through).
- **`dropped`** = no terminal event and the last event is older than 15 minutes.
  The service never sees a hangup, so a call whose events stopped is a call that
  was cut off, not one still running. A call that ended in a `handoff_created`
  is never `dropped`, whatever the reason.
- `v_funnel` is long format — one row per `(stage, bucket)` with
  `bucket ∈ entered | passed | exited | completed | in_flight` — and
  `entered = passed + completed + in_flight + Σ exited` holds at every stage
  (verified in Postgres 16). `bucket = 'exited'` carries the reason, including
  `dropped_<stage>`.
- Signals: `v_signal_unserved_demand`, `v_signal_price_check`,
  `v_signal_recoverable`; pricing: `v_rate_per_mile`; queue:
  `v_rep_queue`, `v_onboarding_leads`. All expose `source`.
- Time metrics (`median_seconds_to_first_offer`) are computed **only** on
  `source = 'live'`; the other KPIs use everything.
- `negotiation_opened.details` carries a load snapshot: `origin`, `destination`,
  `equipment_type`, `miles`, `pickup_datetime`, `delivery_datetime`, `weight`,
  `commodity_type`, `posted_rate` (no ceiling for the agent, but the ceiling is
  in the event).

### Two gaps the Signals views had — both closed (2026-10-03)

1. **`load_pitched`** was written only by `GET /loads/{load_id}`, a route the
   platform no longer calls, so `v_signal_price_check.pitches` was 0 for every
   load. It is now emitted once per load returned by `/loads/search`
   (DECISIONS §65). A retried search inflates the count — see the note there.
2. **`lead_created`** was never emitted: `/otp/send` wrote nothing on the
   `routed_onboarding` path and `qualify()` had no caller outside the tests.
   It is now emitted there with the temperature (DECISIONS §66), so
   `v_onboarding_leads.temperature` / `lead_reason` / `authority_age_days` /
   `matching_loads` fill in. Expect `Review` today: the authority age is
   unknown (§48) and the call matched no loads.

## Reviewer mode (evaluation only)

- `REVIEWER_MODE` (default false) + `REVIEWER_MC` + `REVIEWER_OTP`.
- **Fenced (2026-10-03).** `ENV=production` refuses to start with `REVIEWER_MODE` on —
  it is a fixed code for one known MC with no email, which in a deployment is a
  verification bypass. Checked **in every environment**, not only production: the
  deploy runs as `development` on purpose, so a production-only rule would not
  protect the instance that has it switched on. Requires a non-empty `REVIEWER_MC`
  and a `REVIEWER_OTP` of exactly six digits — an empty one would be accepted as
  the correct code, as-is.
- **MC 15735 (ALLIED VAN LINES INC)** — verified eligible. `107012` also works.
- For that MC: fixed code, no email, `otp_sent` event flagged `reviewer=true`,
  fixed `say` ("…Please read me the six digits."). Nothing the caller says
  enables it.

## Render env vars (names only — values are in the dashboard, never the repo)

`PORT`, `ENV`, `TMS_MODE`, `TMS_HOST`, `TMS_PORT`, `TMS_TOKEN`,
`TMS_READ_DEADLINE_SECONDS`, `TMS_FAKE_FIXTURES`, `TMS_FAKE_FAULT_RATE`,
`FMCSA_BASE_URL`, `FMCSA_KEY`,
`FMCSA_TIMEOUT_SECONDS`, `OTP_HMAC_SECRET`, `OTP_TTL_SECONDS`,
`OTP_MAX_ATTEMPTS`, `OTP_MAX_RESENDS`, `OTP_VERIFY_RATE_LIMIT_PER_HOUR`,
`RESEND_API_KEY`, `RESEND_TIMEOUT_SECONDS`, `SERVICE_API_KEY`,
`NEGOTIATION_STEPS`, `NEGOTIATION_URGENT_STEPS`, `NEGOTIATION_MAX_ROUNDS`,
`NEGOTIATION_INCREMENT`, `NEGOTIATION_CEILING_MARGIN`,
`RUN_SESSION_TTL_SECONDS`, `HANDOFF_RETENTION_SECONDS`, `REQUEST_BUDGET_SECONDS`,
`EVENTS_JSONL_PATH`,
`APP_VERSION`, `TWIN_ENABLED`, `TWIN_BASE_URL`, `HR_PLATFORM_API_KEY`,
`TWIN_TABLE`, `TWIN_TIMEOUT_SECONDS`, `TWIN_MAX_ATTEMPTS`, `REVIEWER_MODE`,
`REVIEWER_MC`, `REVIEWER_OTP`.

## What `TMS_FAKE_FAULT_RATE` does, and why it matters

`TMS_MODE=fake` answers correctly, always, unless `TMS_FAKE_FAULT_RATE` is above
0. Set to `0.10`, one operational command in ten is answered with a truncated
response or a malformed frame instead — so a demo exercises the retry ladder and
the ambiguous-booking recovery rather than only asserting them in tests. Both are
recoverable; a genuine timeout is deliberately not in the pool, because at a 5 s
read deadline under an 8 s budget it would burn the request and surface as a
`504` rather than as a retry.

Two things make that safe, and both are tested:

- **`LOAD_BOOK` stays idempotent per call.** The booking outcome is stored on
  the session under a lock, so a retried `/bookings` never issues a second
  `LOAD_BOOK`.
- **An unresolved booking is never `booked`.** Every exit from the recovery path
  returns `booked_unconfirmed` with `booking_ref: None`, which the route reports
  as `booking_pending`. A test walks every fault script the fake can produce and
  asserts none yields `booked`.

What it changes for a viewer: a booking now has a real chance of ending in
`booking_pending` instead of `booked`. That is the point, and it is worth saying
out loud before a demo.

## QA plan (approved — do not run until the operator OKs)

Scenarios:
- **Standard:** book at first offer; after 1, 2 and 3 counters; urgent pickup.
- **Edges:** FMCSA inactive (MC 123456); OTP fails 3×; no match (unserved demand);
  no deal after 3 rounds; booking_pending on TMS fault; clarify; idempotency
  (counter + book). Onboarding (`routed_onboarding`) is covered in **pytest**, not
  end-to-end.
- **Adversarial (server):** search/negotiate/book without OTP → 403; send_otp
  before verify → locked; send_otp with `mc_number` → 422; ask over ceiling never
  accepted; negotiate a not-shown load → 409; 4th round → 409; book with
  `agreed_rate` → 422; foreign `run_id` isolation.

Mechanics:
- OTP: reuse reviewer MC 15735; each scenario is a fresh `qa-<name>` run_id.
- Loads: point `TMS_FAKE_FIXTURES` at `tests/fixtures/tms_demo.txt` (20 loads:
  DRY_VAN/REEFER/FLATBED, several lanes, a couple of urgent pickups). The fake
  TMS does **not** consume loads from search, so re-runs are clean.
- `run_id` prefix `qa-`; cleanup `DELETE FROM call_events WHERE run_id LIKE
  'qa-%'`.
- Report: `docs/qa-report.md` — PASS/FAIL per scenario + `v_kpi_overview` KPIs +
  the 3 rules at 0.

## Seed mode (`scripts/qa_run.py --seed`)

Creates the demo data the Signals views need, driving the real HTTP surface:
4 unserved lanes, the same load closed twice, 3 closes within 5% of the ceiling,
3 bookings at 3 ladder depths, 1 onboarding lead. Run ids are
`qa-seed-<token>-<name>` (timestamp token, so re-runs never collide).

- **Costs ~12 OTP verifications, all on one MC** — the reviewer MC. Only that MC
  can open the gate unattended: FMCSA is a live lookup, the contact must be in
  `app/carriers/directory.py`, and the code must be readable by the script.
- **One MC is a hard limit of the environment, not the script.** MC 000000 is used
  for the onboarding step precisely because it is *not* in the directory.
- Needs `--fixtures` (default `tests/fixtures/tms_demo.txt`) to match the
  deployment's `TMS_FAKE_FIXTURES`: the near-miss closes need the ceiling, which
  is computed from the posted rate. See DECISIONS §64.
- Cleanup: `DELETE FROM call_events WHERE run_id LIKE 'qa-seed-%';`

## Seed run against production — done (2026-10-03)

`scripts/qa_run.py --seed --token live03` against the live deploy: **5/5 steps passed in 21 s**.
The deploy was live on `802679f` first, verified from `/health`.

One booking settled as `booked_unconfirmed` — `TMS_FAKE_FAULT_RATE=0.10` produced a lost
answer on `LD10004`, the recovery could not read the load back, and the carrier was told a rep
would confirm. That is the fault injection and the booking invariant working together, live.

### Rows the seed left, per view

| View | Total rows | Of which from this seed |
|---|---|---|
| `v_signal_unserved_demand` | 5 | 4 (`CA→NY`, `OR→WA`, `NV→UT`, `MA→PA`) |
| `v_signal_price_check` | 1 | 1 (`LD10010`) |
| `v_signal_recoverable` | 3 | 3 (`LD10001`, `LD10013`, `LD10015`) |
| `v_rate_per_mile` | 8 | 3 lanes (`Atlanta→Miami`, `Denver→Kansas City`, `Seattle→Portland`) |
| `v_rep_queue` | 10 | 3 (2 `booked`, 1 `booking_pending`) |
| `v_onboarding_leads` | 1 | 1 (MC 000000) |

The rows the seed did not create were already in Twin from the 2026-10-02 QA run
(`HI` in unserved demand; four more lanes and seven more rep-queue rows) and from the
evaluator's `review` call. **Nothing is double-counted**: the seed's run ids carry the
`live03` token, so `run_id LIKE 'qa-seed-live03-%'` separates them exactly.

`v_signal_price_check` is one row and not three because the threshold is per load: a lane that
was opened once, pitched once and lost once does not meet "≥2 failed / ≥3 opened / ≥3 pitched".
The near-miss lanes are in `v_signal_recoverable`, which is where they belong.

### OTP the run spent

**12 verifications on MC 15735**, 12 sends, across 13 runs — measured from Twin
(`count(*) FILTER (WHERE event_type = 'otp_verified')`), not estimated. The onboarding run is
the thirteenth: it verifies FMCSA but no code is issued, so it costs no verification. At the
permanent limit of 20/hour this is one seed run per hour; at 100 there is room for eight.

### Twin has a read route

`GET https://platform.happyrobot.ai/api/v2/twin/tables/call_events?limit=200&offset=0` with the
same Bearer key returns `{tableName, kind, rows, total}` — paginated by `limit`/`offset`, 50 rows
by default. `/twin/tables/{table}/rows` is write-only and answers 404 to a GET; the correct read
path has no `/rows` suffix.

It is how the counts above were taken: fetch every row, load it into a local Postgres with
`twin/schema.sql`, and read the views. Worth keeping — it is the only way to check a view's
numbers from outside the dashboard.

## QA run — done (2026-10-02)

`scripts/qa_run.py` ran once against production: **22/22 PASS**, recorded in
`docs/qa-report.md` (which also carries the `v_kpi_overview` row).

**Rate limit note.** `OTP_VERIFY_RATE_LIMIT_PER_HOUR` is **100 on the deploy right now**,
temporary for the QA run (it reuses MC 15735 across ~18 verifies). It goes to **20
permanent** the following day, because evaluators share MC 15735 — the operator flips it
in Render. A re-run that reuses one reviewer MC needs the higher value; at 20 it would 429
partway through.
