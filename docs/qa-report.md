# QA report — carrier-desk

- **Date:** 2026-10-02
- **Deploy:** `https://carrier-desk.onrender.com` — `/health` version `f82688a`
  (demo loads + three-value `source`).
- **Fixtures:** `TMS_MODE=fake`, `TMS_FAKE_FIXTURES=/app/tests/fixtures/tms_demo.txt`
  (20 loads).
- **Reviewer MC:** `15735` (fixed OTP). OTP rate limit raised to 100/hour/MC for this
  run (temporary — reverts to 20 permanent; evaluators share MC 15735).
- **Run ids:** `qa-<name>` — events land with `source='qa'` and are excluded from the
  live time metrics.

## Result — 22/22 PASS

### Standard — a booking at each depth of the ladder

| Scenario | Result | Note |
|---|---|---|
| book at first offer | ✅ PASS | booked at first offer $1050 |
| book after 1 counter | ✅ PASS | offer $2100 → counter $2250 → booked $2250 |
| book after 2 counters | ✅ PASS | offer $1550 → $1675 → $1775 → booked $1775 |
| book after 3 counters | ✅ PASS | round-3 accept at $1700 |
| urgent pickup | ✅ PASS | urgent lane booked at $1950 |

### Edges

| Scenario | Result | Note |
|---|---|---|
| FMCSA inactive (MC 123456) | ✅ PASS | `fmcsa_rejected:authority_inactive` |
| OTP fails 3× | ✅ PASS | locked after 3 wrong codes |
| no match (unserved demand) | ✅ PASS | unserved lane returns `no_match` |
| no deal after 3 rounds | ✅ PASS | closed with `negotiation_failed` |
| clarify (misheard ask) | ✅ PASS | offer $1650 read as a misheard 1 → clarify |
| idempotent counter | ✅ PASS | repeat ask replays the round, not a new one |
| idempotent book | ✅ PASS | retry returns the same `booking_ref` |

### Adversarial — server-side guards

| Scenario | Result | Note |
|---|---|---|
| search without OTP | ✅ PASS | 403 |
| negotiate without OTP | ✅ PASS | 403 |
| book without OTP | ✅ PASS | 403 |
| send_otp before verify | ✅ PASS | `locked` |
| send_otp with `mc_number` | ✅ PASS | 422 |
| ask over ceiling, never accepted | ✅ PASS | three over-ceiling asks, never accepted |
| negotiate a not-shown load | ✅ PASS | 409 (LD00859 was never pitched) |
| 4th round | ✅ PASS | 409 |
| book with `agreed_rate` | ✅ PASS | 422 |
| foreign `run_id` isolation | ✅ PASS | 409 (load shown to run A only) |

### Not end-to-end (pytest only, by design)

- **Onboarding (`routed_onboarding`)** — covered in `test_api.py`, `test_otp.py`,
  `test_call_flow.py`.
- **`booking_pending` on a TMS fault** — needs an injected `LOAD_BOOK` fault the
  production fake TMS never produces; covered in `test_api.py`.

## Guardrails — the three rules at zero

| Rule | Value | Why it holds |
|---|---|---|
| bookings above ceiling (`bookings_over_ceiling`) | **0** | the server never accepts an ask above the ceiling — `adv-ask-over-ceiling` proves it closes, and every book follows an `accept` at or under it |
| verification bypasses (`otp_bypasses`) | **0** | search/negotiate/book without OTP all 403 before any negotiation or booking event is written |
| ceiling disclosures (`ceiling_revelations`) | **0** | hardcoded `0` in `v_kpi_overview`; the ceiling never leaves the server |

## KPIs — `v_kpi_overview`

The one row Twin returned after the run (all calls: `qa` + `review` + `live`, per design —
only the time metric is `live`-only):

| Column | Value |
|---|---|
| `north_star_pct` | 44.4 |
| `total_margin_usd` | 2303 |
| `avg_margin_usd` | 288 (rounded from 287.875) |
| `identity_risks_stopped` | 2 |
| `median_seconds_to_first_offer` | 71 (live-only) |
| `onboarding_leads` | 0 |
| `bookings_over_ceiling` | 0 |
| `otp_bypasses` | 0 |
| `ceiling_revelations` | 0 |

### Notes

1. **`north_star_pct` reflects the QA scenario mix, not production performance.** It
   intentionally counts `no_match`, `negotiation_failed`, and the *verified* adversarial
   calls, so 44.4% is the QA's blended funnel — not the number a clean production funnel
   would show.
2. **`median_seconds_to_first_offer` is n=1.** It comes from the single `live` call in
   Twin; `qa` and `review` are excluded by design, so the median is that one call's time
   (71 s), not a distribution.
3. **`avg_margin_usd` is rounded to an integer in `v_kpi_overview`** (`round(avg(...))`),
   so it reads 288 rather than 287.875. The schema change is committed but not yet pasted
   into Twin.
