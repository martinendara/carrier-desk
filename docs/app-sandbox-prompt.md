# Build "Carrier Desk" in the HappyRobot Apps Dev Sandbox

You are building a Next.js full-stack app (App Router) on the HappyRobot Apps
template. The template already wires auth and the Twin gateway via server
routes — use its existing Twin helper. Do not add new infrastructure.

## Objective

An internal dashboard named **Carrier Desk** for the operations manager at
HappyRobot Logistics (a freight brokerage). It surfaces what happened on inbound
carrier calls. Two views:

1. **Overview** — the week at a glance.
2. **Call record** — the detail of a single call, reached by clicking a row in
   the "Recent calls" table on Overview.

There are **no** "Signals" page and **no** "Rep queue" page. Do not build them.
Every screen carries a small, muted badge: **`Demo data · QA simulation`**.

## Data access (hard rule)

Read **only from Twin**, and **always server-side**. Use the template's Twin
helper from **route handlers and server components** only. **Never** call the
Twin gateway from the browser / client components. A view that has no rows must
render a friendly empty state, and a query that fails must render a friendly
error — never a blank page.

Do **not** invent columns. If a value the design wants is not in a view, render
an em dash **`—`**.

The views already exist in Twin. Here are their exact columns and the types
PostgreSQL returns (from `twin/schema.sql`):

### `v_kpi_overview` — exactly one row

| column | type | used by |
|---|---|---|
| `north_star_pct` | numeric (nullable) | North Star KPI |
| `total_margin_usd` | bigint | Margin protected KPI |
| `avg_margin_usd` | numeric | Margin protected KPI subline |
| `identity_risks_stopped` | bigint | Identity risks stopped KPI |
| `median_seconds_to_first_offer` | double precision | Time to first offer |
| `onboarding_leads` | bigint | (context, not a headline) |
| `bookings_over_ceiling` | bigint | Guardrail rule 3 |
| `otp_bypasses` | bigint | Guardrail rule 2 |
| `ceiling_revelations` | integer | Guardrail rule 1 (always 0) |

### `v_funnel` — six rows, one per stage

| column | type | used by |
|---|---|---|
| `stage` | text (`calls`, `fmcsa_ok`, `otp_ok`, `load_found`, `agreed`, `booked`) | funnel bar |
| `count` | bigint | funnel bar |

### `v_calls` — one row per `run_id`

| column | type | used by |
|---|---|---|
| `run_id` | text | recent calls table, Call record link |
| `started_at` | timestamptz | table + Call record header |
| `ended_at` | timestamptz | Call record duration |
| `mc_number` | text | table + Call record header |
| `carrier_name` | text | table + Call record header |
| `otp_verified` | boolean | guardrail check, funnel context |
| `outcome` | text (`booked`, `booking_pending`, `negotiation_failed`, `no_match`, `routed_onboarding`, `fmcsa_rejected`, `otp_failed`, `in_progress`) | table status, Call record header |
| `load_id` | text | Call record load panel |
| `opening_offer` | bigint | Call record negotiation panel |
| `agreed_rate` | bigint | table, Call record negotiation panel |
| `ceiling` | bigint | Call record negotiation panel |
| `protected_margin_usd` | bigint | Call record negotiation panel |
| `protected_margin_pct` | numeric | negotiation consistency chart |
| `rounds_used` | integer | Call record negotiation panel |
| `seconds_to_first_offer` | integer | Time to first offer |
| `booking_status` | text (`booked`, `booked_unconfirmed`) | table status |

### `v_call_timeline` — one row per event, ordered

| column | type | used by |
|---|---|---|
| `run_id` | text | filter for the Call record |
| `occurred_at` | timestamptz | timeline x-axis |
| `event_type` | text | timeline icon/color |
| `id` | text | (row key) |
| `details` | jsonb | extra fields the label does not spell out |
| `label` | text | the human-readable timeline text — render it verbatim |

## Design — replicate the mockup

Replicate `Carrier Desk — HappyRobot Logistics.html` (the "Overview" and "Call
record" screens). Layout and copy below are from that mockup; match them.

**Tokens.** Background `#F4F3EF` (warm off-white), cards `#FFFFFF`, text primary
`#14161A`, secondary grays `#5E6168` / `#9C9A94`, accent orange/rust `#A3471A`
(links, active nav) with a lighter `#E2733A` and tint `#FBEADF`, success green
`#1F7A55`, borders/divider `#E3E0D8`. Font: **Geist** for text, **Geist Mono**
for numbers/metrics. Rounded cards, subtle borders, generous whitespace, a
left-hand navigation rail.

**Shell (both screens).** Left rail: wordmark **Carrier Desk** over
**HappyRobot Logistics · Operations**; nav items `Overview`, `Call records`
(no Signals, no Rep queue). Top bar: status pill **Agent live — Answering every
call, 24/7** and a period selector **Last 7 days · Sep 25 – Oct 1, 2026**.

### Overview

1. **Greeting row.** H1 **`Good morning. Here is your carrier desk.`** and, on
   the right, the badge **`Demo data · QA simulation`** and a button
   **`Export week`**.
2. **Main KPI row (four cards).** From `v_kpi_overview`:
   - **North Star** — big number `north_star_pct` with a `%`, subline
     **`of verified carriers booked a load at or under ceiling`**, and the
     `n`/`m` context line (derived: bookings vs. OTP-verified calls; render what
     you can from the views, else `—`).
   - **Margin protected** — big `total_margin_usd` formatted as USD, subline
     **`Kept under ceiling across N deals. $X per load on average.`**
     (`avg_margin_usd`).
   - **Identity risks stopped** — big `identity_risks_stopped`, subline
     **`Calls stopped before seeing a load. N failed FMCSA, M failed the code
     sent to the contact on file.`** (N/M from `v_call_timeline` /
     `v_calls`; render `—` if you cannot derive them).
   - **Peak-hour coverage** — render **`—`** (this metric is not in the views).
3. **Guardrails row.** Heading **`Guardrails · 24 adversarial scenarios run`**
   (the "24" is a demo constant), then three zero-metrics side by side, each a
   big `0` with a label: **`ceiling disclosures`** (`ceiling_revelations`),
   **`verification bypasses`** (`otp_bypasses`), **`bookings above ceiling`**
   (`bookings_over_ceiling`).
4. **Funnel.** Heading **`Where calls go`** and sub **`Every call, from first
   ring to booking. Each drop has a reason.`** A horizontal funnel of the six
   `v_funnel` stages. Under each stage, the drop line:
   - `calls` → **`Web call, no wait`**
   - `fmcsa_ok` → **`N inactive on FMCSA`**
   - `otp_ok` → **`N to onboarding · M failed`**
   - `load_found` → **`N logged as unserved demand`**
   - `agreed` → **`N no deal after 3 rounds`**
   - `booked` → **`N pending · TMS timeout`**
   Derive each N/M from the events; render `—` where you cannot.
5. **Side metrics.** **`Time to first offer`** → `median_seconds_to_first_offer`
   formatted as a duration with `median` underneath. The mockup's "Legacy TMS"
   and "Capacity freed" cards are **not in the views** — omit them.
6. **Negotiation consistency.** Heading **`Negotiation consistency`**, sub
   **`Share of the negotiating room kept on each booking. Same rules on every
   call, so deals land in one tight band.`** A 0–100% band using
   `v_calls.protected_margin_pct` (median marker, and the "most deals within"
   spread). If no rows, empty state.
7. **Recent calls table.** Heading **`Recent calls`**. Columns: **Started**
   (`started_at`), **Carrier** (`carrier_name`), **MC** (`mc_number`),
   **Outcome** (`outcome`), **Agreed** (`agreed_rate`), **Status**
   (`booking_status`). Each row links to the Call record for that `run_id`.
   Sort by `started_at` descending; show the latest ~25. Empty state if none.

### Call record

Reached at a route like `/calls/[run_id]`, filtered on `run_id`.

1. **Header.** Breadcrumb **`Call records / <run_id>`**, then the carrier's
   name (`carrier_name`), **`MC <mc_number>`**, an outcome pill
   (`outcome` / `booking_status`), the call time and duration (from `started_at`
   / `ended_at`). Buttons **`Play recording`** and **`Export record`** can be
   present but non-functional (no recording data yet) — or omitted.
2. **Timeline.** Heading **`What happened, step by step`**, sub **`Every event
   is recorded as it happens. This is the record you use to settle a dispute.`**
   Render `v_call_timeline` rows in `occurred_at` order, showing the time and
   the `label` verbatim. Empty state if none.
3. **Negotiation panel.** From `v_calls`: **`Agreed`** (`agreed_rate`), **`in
   round N of 3`** (`rounds_used`), **`Opened at`** (`opening_offer`),
   **`Ceiling`** (`ceiling`, with a subtle **`· internal only`** tag), **`Kept
   under ceiling`** (`protected_margin_usd`), and **`Rule applied`** (urgent vs
   normal — read `details->>'urgent'` from the `negotiation_opened` event in
   `v_call_timeline`; show `—` if absent).
4. **Load panel.** From `v_calls.load_id` and the load's fields in the event
   `details`. Render `load_id`, and the fields you can (equipment, lane,
   distance, pickup, delivery) if they are present in the events; otherwise
   `—` for each missing field.
5. **Guardrail checks.** A checklist, each row a check or cross derived from the
   events: **`Ceiling never mentioned to the carrier`**,
   **`Identity confirmed before any load was offered`** (`otp_verified`),
   **`No offer above the ceiling`** (`agreed_rate <= ceiling`),
   **`Within 3 negotiation rounds`** (`rounds_used <= 3`).

## Empty and error states

- `v_kpi_overview` returns one row, so it never needs an empty state — but a
  failed query shows a friendly error card.
- `v_funnel`, `v_calls`, `v_call_timeline`: if a view returns zero rows, render
  a centered, muted empty state (e.g. **`No calls yet this period.`** or
  **`No events recorded for this call.`**), not a blank screen.
- Any thrown Twin query renders a card like **`Couldn't load this data right
  now.`** with a retry, and never breaks the rest of the page.

## Style and copy rules

- All UI copy in English, exactly as written above.
- Numbers in `Geist Mono`; prose in `Geist`.
- Keep the `Demo data · QA simulation` badge visible on both screens.
- Never leak internal fields: `ceiling` is shown only in the negotiation panel
  with its "internal only" tag; never show it in the agent-facing sense.
- Do not add columns or fields that are not in the views above. Missing → `—`.
