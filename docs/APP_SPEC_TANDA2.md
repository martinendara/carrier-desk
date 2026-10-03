# APP SPEC — Tanda 2 (Signals, Rep queue, Overview, Call records)

**For the Sandbox / App builder. This document is self-contained: it assumes no access to
the carrier-desk repository.** Everything needed is here — view names, column names, the SQL
that produces each block, the ordering, the empty states.

---

## 0. The one rule

> **No number on any screen may come from anywhere except a view below.**

No client-side arithmetic over individual fields, no counting rows already drawn on screen
with a different filter, no "roughly", no placeholders. If a number is wanted and no view
below provides it, the screen does not show it until a view provides it.

Two consequences worth stating out loud:

- **Totals come from a query, not from adding up the visible rows.** A list showing the ten
  most recent rows must not claim "10" is the total.
- **`0` is a legitimate answer and must be rendered as `0`**, never as a blank or a dash. A
  blank cell reads as "no data"; `0` reads as "we measured it and there were none". They mean
  different things and only one of them is true.

---

## 1. The data contract

All views live in the same PostgreSQL database, in the same schema, and are read-only.

| View | What it is |
|---|---|
| `v_signal_unserved_demand` | Lanes a carrier asked for and we had nothing on |
| `v_signal_price_check` | Loads that keep costing rounds and never book |
| `v_signal_recoverable` | Negotiations we lost by a hair |
| `v_rate_per_mile` | What we paid per mile against what was posted |
| `v_rep_queue` | Calls handed to a rep, with the derivation package |
| `v_onboarding_leads` | Carriers with no contact on file, qualified |
| `v_kpi_overview` | One row of headline numbers |
| `v_calls` | One row per call, including `dropped` |

### 1.1 `source` — read this before filtering anything

Every view exposes a column named **`source`**, with exactly three values:

| `source` | Meaning |
|---|---|
| `live` | A real call |
| `review` | An evaluation call (the evaluator's carrier) |
| `qa` | A demonstration or test call |

**Default to showing all three.** The demonstration data that fills these screens is recorded
with `source = 'qa'` on purpose, so a screen filtered to `live` will look empty during a demo
and the demo will look broken. Render `source` as a small chip on each row and offer it as a
filter, but never as a default that hides `qa`.

Use `live` as the filter only where the label says so explicitly — the overview's time metric
is the one such case, and the view already applies it.

### 1.2 Actions: export or link, never a dead button

The App cannot run anything on the backend. Every action on this screen is therefore one of:

1. **An export** — the block's query, as CSV, of exactly the rows the user is looking at.
2. **A link to the Call record** — for a block whose rows carry a `run_id` (see below).

**Do not render a button that does not do one of those two things.** A button that appears to
act and does nothing is worse than no button: an operator will press it during a demo and
conclude the feature is broken.

| View | Carries `run_id`? | Allowed action |
|---|---|---|
| `v_signal_unserved_demand` | yes, as `last_run_id` | link to the Call record **and** export |
| `v_signal_recoverable` | yes, as `run_id` | link to the Call record **and** export |
| `v_signal_price_check` | **no** | export only |
| `v_rate_per_mile` | **no** | export only |
| `v_rep_queue` | yes, as `run_id` | link to the Call record **and** export |
| `v_onboarding_leads` | yes, as `run_id` | link to the Call record **and** export |

`v_signal_price_check` and `v_rate_per_mile` are grouped by a load and by a lane, not by a
call, so there is no single Call record to open. Export is the action. Do not add a link that
would open an arbitrary call.

---

## 2. Signals

Four signals. The badge, the ordering and the empty states are specified once at the end of
each block.

### 2.1 Signal 1 — Unserved demand

**The question it answers.** Which lanes are carriers asking for that we have nothing on?

**Source.** `v_signal_unserved_demand`. One row per (equipment, origin, destination, source).
A row exists only because a search returned zero loads.

```sql
SELECT equipment, origin_city, origin_state, dest_city, dest_state,
       source, searches, distinct_carriers, first_seen, last_seen,
       last_run_id, carrier_mcs
FROM v_signal_unserved_demand
ORDER BY searches DESC, distinct_carriers DESC, last_seen DESC;
```

**Columns, in the order the table shows them.**

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `equipment` | text | `DRY_VAN` → `Dry van`. **`NULL` means the carrier did not say** — render `Any equipment`, never blank |
| 2 | `origin_city` | text | part of the lane; `NULL` = not stated |
| 3 | `origin_state` | text | part of the lane; `NULL` = not stated |
| 4 | `dest_city` | text | part of the lane; `NULL` = not stated |
| 5 | `dest_state` | text | part of the lane; `NULL` = not stated |
| 6 | `source` | text | chip: `live` / `review` / `qa` |
| 7 | `searches` | bigint | the count of searches that matched nothing here |
| 8 | `distinct_carriers` | bigint | how many different carriers asked |
| 9 | `first_seen` | timestamptz | relative ("3 days ago") |
| 10 | `last_seen` | timestamptz | relative; sort default |
| 11 | `last_run_id` | text | link to the Call record |
| 12 | `carrier_mcs` | text[] | the MCs that asked; render as chips, max 3 then `+N` |

**The lane cell** is the visual anchor. Render it as:

```
Rochester, NY  →  anywhere        ·  Dry van
Chicago, IL    →  Memphis, TN     ·  Any equipment
```

If a city is `NULL` but the state is known, show the state alone. If both are `NULL`, show
`anywhere`. **Never render `null`, an empty string, or a bare `→`.**

**Action.** `View calls` (link to the Call record for `last_run_id`) + `Export CSV`.

**Empty state.** Heading: *No unserved lanes*. Body: *No carrier has searched for a lane we
could not cover yet.* Do not hide the block; an operator needs to see that it was checked.

---

### 2.2 Signal 2 — Price check

**The question it answers.** Which loads keep costing us rounds and never book?

**Source.** `v_signal_price_check`. One row per load that has either two or more failed
negotiations, three or more times opened, or three or more pitches — and has never booked.

```sql
SELECT load_id, origin, destination, equipment_type, miles, source,
       mc_number, carrier_name, times_opened, pitches, failed_negotiations,
       avg_gap_usd, avg_gap_pct, last_seen
FROM v_signal_price_check
ORDER BY failed_negotiations DESC, times_opened DESC, last_seen DESC;
```

**Columns.**

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `load_id` | text | monospace |
| 2 | `origin` | text | lane, left |
| 3 | `destination` | text | lane, right |
| 4 | `equipment_type` | text | `DRY_VAN` → `Dry van` |
| 5 | `miles` | integer | grouped (`1,228 mi`) |
| 6 | `source` | text | chip |
| 7 | `mc_number` | text | monospace |
| 8 | `carrier_name` | text | the last carrier who tried |
| 9 | `times_opened` | bigint | how often we opened a negotiation on it |
| 10 | `pitches` | bigint | how often we showed it |
| 11 | `failed_negotiations` | bigint | how often we walked away |
| 12 | `avg_gap_usd` | numeric | money; **can be `NULL`** → render `—` |
| 13 | `avg_gap_pct` | numeric | one decimal + `% `; **can be `NULL`** → `—` |
| 14 | `last_seen` | timestamptz | relative |

**`avg_gap_usd` and `avg_gap_pct` are `NULL` when every open on that load is still in flight** —
there is no closed round to average. `—` is correct there and is not a violation of the rule
in §0: it is "not applicable", not "unknown".

**Action.** Export CSV. **No Call-record link** — a load is not a call (§1.2).

**Empty state.** *Nothing stuck.* *No load is repeatedly failing without booking.*

---

### 2.3 Signal 3 — Recoverable

**The question it answers.** Which negotiations did we lose by so little that one phone call
could save them?

**Source.** `v_signal_recoverable`. One row per negotiation that closed with the carrier's
final ask **less than 5% above** what we could pay.

```sql
SELECT run_id, load_id, negotiation_id, occurred_at, source,
       mc_number, carrier_name, origin, destination, equipment_type, miles,
       carrier_ask, ceiling, gap_usd, gap_pct
FROM v_signal_recoverable
ORDER BY gap_pct ASC, gap_usd ASC;
```

**Ordering matters here and is not arbitrary**: cheapest to save first. `gap_pct` ascending
puts the closest misses at the top, which is the order a rep should work them in.

**Columns.**

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `run_id` | text | the Call record link target |
| 2 | `load_id` | text | monospace |
| 3 | `negotiation_id` | text | internal; tooltip only, do not show as a column |
| 4 | `occurred_at` | timestamptz | relative |
| 5 | `source` | text | chip |
| 6 | `mc_number` | text | monospace |
| 7 | `carrier_name` | text | |
| 8 | `origin` | text | lane, left |
| 9 | `destination` | text | lane, right |
| 10 | `equipment_type` | text | |
| 11 | `miles` | integer | grouped |
| 12 | `carrier_ask` | bigint | money — what the carrier wanted |
| 13 | `ceiling` | bigint | money — the most we could pay |
| 14 | `gap_usd` | bigint | money — the difference; **this is the headline number** |
| 15 | `gap_pct` | numeric | one decimal + `%` |

**Do not render `ceiling` as a bare label.** A rep sees it, the carrier never does. Label it
explicitly: **`Max we could pay`**. It must never appear in anything the carrier could hear or
read — not an SMS, not a call summary, not a link preview.

**Action.** `Open call` (Call record for `run_id`) + `Export CSV`.

**Empty state.** *No near misses.* *No negotiation has closed within 5% of the limit.*

---

### 2.4 Signal 4 — Rate per mile

**The question it answers.** Are we paying more per mile than the board says, and on which
lanes?

**Source.** `v_rate_per_mile`. One row per (lane, equipment, source), over **accepted
negotiations only** — a lane we never agreed on has no agreed rate to compare.

```sql
SELECT lane, equipment_type, source, negotiations, avg_miles,
       avg_posted_rate, avg_agreed_rate, avg_posted_rpm, avg_agreed_rpm,
       avg_delta_rpm, avg_saved_vs_posted
FROM v_rate_per_mile
ORDER BY avg_delta_rpm DESC, negotiations DESC;
```

**Columns.**

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `lane` | text | already formatted `Origin → Destination` |
| 2 | `equipment_type` | text | |
| 3 | `source` | text | chip |
| 4 | `negotiations` | bigint | **the sample size — show it, it is the honest caveat** |
| 5 | `avg_miles` | numeric | grouped, `mi` |
| 6 | `avg_posted_rate` | numeric | money |
| 7 | `avg_agreed_rate` | numeric | money |
| 8 | `avg_posted_rpm` | numeric | 3 decimals, `$/mi` |
| 9 | `avg_agreed_rpm` | numeric | 3 decimals, `$/mi` |
| 10 | `avg_delta_rpm` | numeric | 3 decimals, signed; positive = we paid above the board |
| 11 | `avg_saved_vs_posted` | numeric | money, signed |

**Render `negotiations` next to every rate.** A lane with one negotiation is one phone call,
not a market rate. Where `negotiations = 1`, show a muted `1 call — indicative only`.

**Action.** Export CSV. **No Call-record link** (§1.2).

**Empty state.** *No agreed rates yet.* *Rate per mile appears once a negotiation is agreed.*

---

### 2.5 The Signals badge

The count in the Signals menu badge is the number of open items across the three
action-oriented signals:

```sql
SELECT
  (SELECT count(*) FROM v_signal_unserved_demand)
+ (SELECT count(*) FROM v_signal_price_check)
+ (SELECT count(*) FROM v_signal_recoverable) AS active_signals;
```

**`v_rate_per_mile` is deliberately excluded.** It is a benchmark — a number to look at, not
an item to work. Counting it would inflate the badge with rows nothing can be done about, and
a badge that never reaches zero stops being read.

If the product decision is to count all four, the change is one more `+ (SELECT count(*) FROM
v_rate_per_mile)`.

**Render `0` as no badge at all**, not as a badge reading `0`.

---

## 3. Rep queue

Two sections, one screen: **bookings** (work that exists) and **onboarding leads** (carriers we
could not verify). They are separate lists because they are separate jobs.

### 3.1 Bookings — high priority

**Source.** `v_rep_queue`, filtered to the two booking reasons.

```sql
SELECT queued_at, ticket_id, run_id, reason, source,
       mc_number, carrier_name, load_id, origin, destination, equipment_type, miles,
       agreed_rate, ceiling, margin_usd, agreed_rpm,
       booking_status, booking_ref, ambiguous
FROM v_rep_queue
ORDER BY (reason = 'booking_pending') DESC, queued_at DESC;
```

**Priority.** `booking_pending` sorts **above** `booked`, and that ordering is the point:
a `booking_pending` row is a booking nobody has confirmed and a carrier waiting to hear,
while a `booked` row is finished business being filed. Render `booking_pending` with a
warning treatment and `booked` with a success treatment.

**The derivation package** — everything a rep needs to take over the call without opening it:

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `queued_at` | timestamptz | relative; sort default within priority |
| 2 | `ticket_id` | text | monospace, the queue reference |
| 3 | `run_id` | text | the Call record link target |
| 4 | `reason` | text | `booking_pending` → `Needs confirmation`; `booked` → `Booked` |
| 5 | `source` | text | chip |
| 6 | `mc_number` | text | monospace |
| 7 | `carrier_name` | text | |
| 8 | `load_id` | text | monospace |
| 9 | `origin` | text | lane, left |
| 10 | `destination` | text | lane, right |
| 11 | `equipment_type` | text | |
| 12 | `miles` | numeric | grouped, `mi` |
| 13 | `agreed_rate` | bigint | money |
| 14 | `ceiling` | bigint | money — label **`Max we could pay`** (§2.3) |
| 15 | `margin_usd` | bigint | money — `4` below |
| 16 | `agreed_rpm` | numeric | 3 decimals, `$/mi`; **can be `NULL`** → `—` |
| 17 | `booking_status` | text | `booked` / `booked_unconfirmed` |
| 18 | `booking_ref` | text | monospace; **`NULL` → `No reference yet`** |
| 19 | `ambiguous` | boolean | `true` → show a muted `Unconfirmed by the load board` |

Notes for the render:

1. **`booking_status = 'booked_unconfirmed'`** means the load board did not confirm. It is
   **never** to be presented as a confirmed booking, and the row must not be styled as
   success. The carrier was told a rep would confirm — this is that confirmation.
2. **`booking_ref` is `NULL` for exactly those rows.** Never render an empty cell; render
   `No reference yet`.
3. **`ambiguous` is a modifier, not a status** — a small note, not a column of its own.
4. **`margin_usd`** is `ceiling − agreed_rate`: what the negotiation kept. Money, no
   decimals. It is internal and carries the same rule as `ceiling`: never let it reach
   anything the carrier could receive.

**Action.** `Open call` (Call record for `run_id`) + `Export CSV`.

**Empty state.** *No bookings waiting.* *Every booking has been filed.*

### 3.2 Onboarding leads

**Source.** `v_onboarding_leads`. One row per call that reached the onboarding path — a
carrier with no contact on file, so no code was ever sent.

```sql
SELECT run_id, source, mc_number, carrier_name, fmcsa_eligible, fmcsa_reason,
       temperature, lead_reason, authority_age_days, matching_loads,
       occurred_at, ticket_id, searches, best_result_count, evidence
FROM v_onboarding_leads
ORDER BY
  CASE temperature WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END,
  occurred_at DESC;
```

**Columns.**

| # | Column | Type | How to render |
|---|---|---|---|
| 1 | `run_id` | text | the Call record link target |
| 2 | `source` | text | chip |
| 3 | `mc_number` | text | monospace |
| 4 | `carrier_name` | text | **can be `NULL`** → `Name not on record` |
| 5 | `fmcsa_eligible` | boolean | see `2` below |
| 6 | `fmcsa_reason` | text | internal code; **tooltip only, not a column** |
| 7 | `temperature` | text | the badge — see `1` below |
| 8 | `lead_reason` | text | the rule that fired; tooltip |
| 9 | `authority_age_days` | integer | **`NULL` today for every carrier** → `Unknown` |
| 10 | `matching_loads` | integer | grouped |
| 11 | `occurred_at` | timestamptz | relative |
| 12 | `ticket_id` | text | monospace; **`NULL`** → `No ticket` |
| 13 | `searches` | bigint | |
| 14 | `best_result_count` | integer | |
| 15 | `evidence` | text | `lead_created` / `handoff_onboarding`; tooltip only |

1. **`temperature`** is `hot` / `warm` / `review`. Render as a badge: `Hot` (urgent,
   call today), `Warm` (worth a call), `Review` (a human decides).
   **Expect `Review` almost always today**, and say why in the empty-ish state rather than
   letting it look like a bug: the authority date is not available from the carrier record,
   so the qualification rule answers `Review` by default. That is the rule working.
2. **`fmcsa_eligible`** is `false` for most of these — that is *why* they are here. Do not
   style it as an error. Label the column `Carrier record` with values
   `Not eligible` / `Eligible` rather than a red cross.
3. **`authority_age_days` is `NULL` for every row today.** Render `Unknown`. Do not compute a
   substitute and do not hide the column — an operator needs to see that the input is
   missing, because that is the one open item behind the `Review` temperature.

**Action.** `Open call` (Call record for `run_id`) + `Export CSV`.

**Empty state.** *No onboarding leads.* *Every carrier who called had a contact on file.*

---

## 4. Overview

### 4.1 Replace the `Answered instantly` card with `Unserved demand`

Remove the `Answered instantly` card. In its place:

**Title.** `Unserved demand`
**Source.** `v_signal_unserved_demand` — the same view as Signal 1.

```sql
SELECT sum(searches) AS unserved_searches,
       count(DISTINCT mc) AS distinct_carriers
FROM (
  SELECT searches, unnest(carrier_mcs) AS mc FROM v_signal_unserved_demand
) s;
```

- **Big number:** `unserved_searches` — *searches that found nothing*.
- **Sub-line:** `distinct_carriers` — *different carriers*.
- **Link:** the whole card is a link to Signals (§2.1), and the card carries a `→`.

Label the two numbers precisely, because they are easy to confuse:

```
Unserved demand
12                              ← searches that found nothing
5 different carriers            ← carriers behind them
```

**Empty state.** Big number `0`, sub-line `No carrier has asked for a lane we could not
cover`. The card stays visible; a zero here is a good result and should be seen.

### 4.2 The Signals badge

As specified in §2.5. If the badge is rendered on the Overview as well as in the menu, both
read the same query.

### 4.3 The cards that stay

The other Overview cards are unchanged and continue to read `v_kpi_overview` — one row, no
filters, every column defined there. In particular, `median_seconds_to_first_offer` is
computed on `source = 'live'` only, inside the view; **do not apply a `source` filter to it in
the App** or the two filters will compound.

---

## 5. Call records — showing `dropped`

Every call row comes from `v_calls`. A call whose events stopped has
`outcome = 'dropped'`, and `dropped_at_stage` says where it stopped:

| `dropped_at_stage` | Label to render |
|---|---|
| `fmcsa` | `Dropped after FMCSA` |
| `otp` | `Dropped during verification` |
| `search` | `Dropped during search` |
| `negotiation` | `Dropped during negotiation` |
| `booking` | `Dropped after agreement` |
| `NULL` | `Dropped` |

Rendering rules:

1. **`dropped_at_stage` is `NULL` for every call that did not drop** — for the vast majority
   of rows. It is not a column to show on all rows; it is a detail that appears only when
   `outcome = 'dropped'`.
2. **Never render `NULL` as a label.** When `outcome = 'dropped'` and the stage is `NULL`,
   the call never recorded a step at all; render the fallback `Dropped`.
3. **What `dropped` means:** the service never sees a hangup. A call whose last event is more
   than 15 minutes old and which has no terminal event is a call that was cut off — the
   carrier put the phone down — not one still running. That is why the state exists and why
   it is not an error to be styled red: use a neutral, muted treatment.
4. **`dropped` is not `negotiation_failed`.** A dropped call in the negotiation stage may have
   been going fine. Do not infer an outcome from the stage; report the stage.
5. **`funnel_step`** (0–5) and **`last_stage`** are also exposed if a funnel visual is wanted.
   `last_stage` is the last step the call was *engaged in*; it is populated for every call,
   not only dropped ones.

For the full call timeline, `v_call_timeline` gives one row per event of a call, ordered by
`run_id, occurred_at`, with an `id`, an `event_type`, a `details` payload and a human-readable
`label`.

---

## 6. Empty states, in one place

Every block must render its own empty state. **A hidden block and an empty block look
identical to an operator and mean different things** — one says "nothing to report", the
other says "something did not load".

| Block | Heading | Body |
|---|---|---|
| Unserved demand | No unserved lanes | No carrier has searched for a lane we could not cover yet. |
| Price check | Nothing stuck | No load is repeatedly failing without booking. |
| Recoverable | No near misses | No negotiation has closed within 5% of the limit. |
| Rate per mile | No agreed rates yet | Rate per mile appears once a negotiation is agreed. |
| Rep queue — bookings | No bookings waiting | Every booking has been filed. |
| Rep queue — leads | No onboarding leads | Every carrier who called had a contact on file. |
| Overview — unserved demand | *(keep the card)* | Big number `0`, sub-line `No carrier has asked for a lane we could not cover`. |
| Signals badge | *(no badge)* | Render nothing, not a `0` badge. |

---

## 7. Column reference

Exact column names and order, as the views return them. Use these spellings — they are what
the queries above select.

```
v_signal_unserved_demand   equipment, origin_city, origin_state, dest_city, dest_state,
                           source, searches, distinct_carriers, first_seen, last_seen,
                           last_run_id, carrier_mcs

v_signal_price_check       load_id, origin, destination, equipment_type, miles, source,
                           mc_number, carrier_name, times_opened, pitches,
                           failed_negotiations, avg_gap_usd, avg_gap_pct, last_seen

v_signal_recoverable       run_id, load_id, negotiation_id, occurred_at, source,
                           mc_number, carrier_name, origin, destination, equipment_type,
                           miles, carrier_ask, ceiling, gap_usd, gap_pct

v_rate_per_mile            lane, equipment_type, source, negotiations, avg_miles,
                           avg_posted_rate, avg_agreed_rate, avg_posted_rpm,
                           avg_agreed_rpm, avg_delta_rpm, avg_saved_vs_posted

v_rep_queue                queued_at, ticket_id, run_id, reason, source, mc_number,
                           carrier_name, load_id, origin, destination, equipment_type,
                           miles, agreed_rate, ceiling, margin_usd, agreed_rpm,
                           booking_status, booking_ref, ambiguous

v_onboarding_leads         run_id, source, mc_number, carrier_name, fmcsa_eligible,
                           fmcsa_reason, temperature, lead_reason, authority_age_days,
                           matching_loads, occurred_at, ticket_id, searches,
                           best_result_count, evidence

v_kpi_overview             north_star_pct, total_margin_usd, avg_margin_usd,
                           identity_risks_stopped, median_seconds_to_first_offer,
                           onboarding_leads, bookings_over_ceiling, otp_bypasses,
                           ceiling_revelations

v_calls                    run_id, source, started_at, ended_at, mc_number, carrier_name,
                           otp_verified, outcome, dropped_at_stage, last_stage,
                           funnel_step, load_id, opening_offer, agreed_rate, ceiling,
                           protected_margin_usd, protected_margin_pct, rounds_used,
                           seconds_to_first_offer, booking_status
```

### 7.1 Values a column can hold

| Column | Values |
|---|---|
| `source` | `live`, `review`, `qa` |
| `v_calls.outcome` | `booked`, `booking_pending`, `negotiation_failed`, `no_match`, `routed_onboarding`, `fmcsa_rejected`, `otp_failed`, `dropped`, `in_progress` |
| `v_calls.dropped_at_stage`, `last_stage` | `fmcsa`, `otp`, `search`, `negotiation`, `booking` |
| `v_calls.booking_status` | `booked`, `booked_unconfirmed` |
| `v_rep_queue.reason` | `booked`, `booking_pending` |
| `v_onboarding_leads.temperature` | `hot`, `warm`, `review` |
| `v_signal_unserved_demand.equipment` | `DRY_VAN`, `REEFER`, `FLATBED`, or `NULL` when the carrier did not say |
