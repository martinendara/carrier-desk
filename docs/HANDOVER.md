# HANDOVER — carrier-desk

For a fresh session to resume without re-deriving anything. Read this, then
`git log --oneline -3`, then pick up from **"Next: the QA run"** below.

## Repo state

- Branch `main`. Tests: **554 passed**; GitHub Actions **green**.
- Commit: the commit this file ships in (`git rev-parse --short HEAD`).
- `python -m pytest` is the whole suite; it needs no `.env` (fake TMS + mocked FMCSA).

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

### Two gaps the Signals views expose

The seed run in `scripts/qa_run.py --seed` produces data for all of them, but
two fields the views would rather have are not emitted:

1. **`load_pitched`** is written only by `GET /loads/{load_id}`, a route the
   platform no longer calls (DECISIONS §56 moved the detail into the search
   response). So `v_signal_price_check.pitches` is 0 for every load. Minimal
   change: emit `LOAD_PITCHED` per load in `/loads/search`, where the shown
   loads are already recorded.
2. **`lead_created`** is defined in CLAUDE.md §9 and never emitted:
   `/otp/send` writes nothing on the `routed_onboarding` path, and
   `app/onboarding/qualification.py::qualify` has no caller outside the tests.
   `v_onboarding_leads` therefore builds on `handoff_created` with
   `reason = 'onboarding'`, and its `temperature` / `lead_reason` /
   `authority_age_days` columns are NULL. Minimal change: call `qualify()` in
   `/otp/send` when the dispatch is `routed_onboarding` and emit `lead_created`
   with its result.

## Reviewer mode (evaluation only)

- `REVIEWER_MODE` (default false) + `REVIEWER_MC` + `REVIEWER_OTP`.
- **MC 15735 (ALLIED VAN LINES INC)** — verified eligible. `107012` also works.
- For that MC: fixed code, no email, `otp_sent` event flagged `reviewer=true`,
  fixed `say` ("…Please read me the six digits."). Nothing the caller says
  enables it.

## Render env vars (names only — values are in the dashboard, never the repo)

`PORT`, `ENV`, `TMS_MODE`, `TMS_HOST`, `TMS_PORT`, `TMS_TOKEN`,
`TMS_READ_DEADLINE_SECONDS`, `TMS_FAKE_FIXTURES`, `FMCSA_BASE_URL`, `FMCSA_KEY`,
`FMCSA_TIMEOUT_SECONDS`, `OTP_HMAC_SECRET`, `OTP_TTL_SECONDS`,
`OTP_MAX_ATTEMPTS`, `OTP_MAX_RESENDS`, `OTP_VERIFY_RATE_LIMIT_PER_HOUR`,
`RESEND_API_KEY`, `RESEND_TIMEOUT_SECONDS`, `SERVICE_API_KEY`,
`NEGOTIATION_STEPS`, `NEGOTIATION_URGENT_STEPS`, `NEGOTIATION_MAX_ROUNDS`,
`NEGOTIATION_INCREMENT`, `NEGOTIATION_CEILING_MARGIN`,
`RUN_SESSION_TTL_SECONDS`, `REQUEST_BUDGET_SECONDS`, `EVENTS_JSONL_PATH`,
`APP_VERSION`, `TWIN_ENABLED`, `TWIN_BASE_URL`, `HR_PLATFORM_API_KEY`,
`TWIN_TABLE`, `TWIN_TIMEOUT_SECONDS`, `TWIN_MAX_ATTEMPTS`, `REVIEWER_MODE`,
`REVIEWER_MC`, `REVIEWER_OTP`.

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

## QA run — done (2026-10-02)

`scripts/qa_run.py` ran once against production: **22/22 PASS**, recorded in
`docs/qa-report.md` (which also carries the `v_kpi_overview` row).

**Rate limit note.** `OTP_VERIFY_RATE_LIMIT_PER_HOUR` is **100 on the deploy right now**,
temporary for the QA run (it reuses MC 15735 across ~18 verifies). It goes to **20
permanent** the following day, because evaluators share MC 15735 — the operator flips it
in Render. A re-run that reuses one reviewer MC needs the higher value; at 20 it would 429
partway through.
