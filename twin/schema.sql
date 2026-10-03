-- carrier-desk call activity in HappyRobot Twin (PostgreSQL).
-- Paste this whole file into the Twin SQL console. It is re-runnable: the table
-- is created if missing, and every view is dropped and recreated.

-- One row per service event. `details` (jsonb) carries the full event payload;
-- the scalar columns are the fields the dashboard filters and joins on.
CREATE TABLE IF NOT EXISTS call_events (
    id           text PRIMARY KEY,           -- deterministic; retries/backfill are idempotent
    run_id       text NOT NULL,
    event_type   text NOT NULL,
    occurred_at  timestamptz NOT NULL,
    mc_number    text,
    load_id      text,
    offer        bigint,
    carrier_ask  bigint,
    round        int,
    action       text,                        -- negotiation action / booking status / outcome
    result_code  text,
    agreed_rate  bigint,
    max_buy      bigint,                      -- the ceiling (C)
    margin       bigint,                      -- C - agreed_rate
    reason       text,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS call_events_run_idx ON call_events (run_id, occurred_at);
CREATE INDEX IF NOT EXISTS call_events_type_idx ON call_events (event_type);

-- Views, dropped in reverse-dependency order and recreated in dependency order.
-- Every view is listed, not just the ones that changed: dropping is what makes
-- this file re-runnable, and a view that is not dropped keeps its old columns
-- when the definition under it moves.
DROP VIEW IF EXISTS v_kpi_overview CASCADE;
DROP VIEW IF EXISTS v_onboarding_leads CASCADE;
DROP VIEW IF EXISTS v_rep_queue CASCADE;
DROP VIEW IF EXISTS v_rate_per_mile CASCADE;
DROP VIEW IF EXISTS v_signal_recoverable CASCADE;
DROP VIEW IF EXISTS v_signal_price_check CASCADE;
DROP VIEW IF EXISTS v_signal_unserved_demand CASCADE;
DROP VIEW IF EXISTS v_funnel CASCADE;
DROP VIEW IF EXISTS v_calls CASCADE;
DROP VIEW IF EXISTS v_call_timeline CASCADE;
DROP VIEW IF EXISTS v_run_source CASCADE;

-- ---------------------------------------------------------------------------
-- v_run_source: one row per run — the three-valued `source` every view exposes.
--
--   qa     the run_id is prefixed `qa-` (the QA driver and the seed mode)
--   review any event of the call carried the reviewer flag (evaluation MC)
--   live   everything else
--
-- Defined once, here. `v_calls` and every signal view read the same answer, so
-- the definition cannot drift between them. `qa-` wins over the reviewer flag:
-- a QA scenario that reuses the reviewer MC is still a QA call.
-- ---------------------------------------------------------------------------
CREATE VIEW v_run_source AS
SELECT
    run_id,
    CASE
        WHEN run_id LIKE 'qa-%' THEN 'qa'
        WHEN bool_or((details->>'reviewer')::boolean IS TRUE) THEN 'review'
        ELSE 'live'
    END AS source
FROM call_events
GROUP BY run_id;

-- ---------------------------------------------------------------------------
-- v_call_timeline: the events of one call in order, with a readable label.
-- ---------------------------------------------------------------------------
CREATE VIEW v_call_timeline AS
SELECT
    run_id,
    occurred_at,
    event_type,
    id,
    details,
    CASE event_type
        WHEN 'fmcsa_checked' THEN
            'FMCSA: ' || COALESCE(details->>'legal_name', mc_number, '') ||
            CASE WHEN (details->>'eligible')::boolean
                 THEN ' eligible'
                 ELSE ' rejected (' || COALESCE(details->>'reason', '') || ')'
            END
        WHEN 'otp_sent' THEN
            CASE WHEN (details->>'reviewer')::boolean THEN
                'Reviewer test code (no message sent)'
            ELSE
                'OTP sent to ' || COALESCE(details->>'destination', '') ||
                CASE WHEN (details->>'resend')::boolean THEN ' (resend)' ELSE '' END
            END
        WHEN 'otp_verified' THEN 'OTP verified'
        WHEN 'otp_failed' THEN
            'OTP failed (' || COALESCE(details->>'reason', '') || ', ' ||
            COALESCE((details->>'attempts_left')::text, '') || ' left)'
        WHEN 'search_performed' THEN
            CASE WHEN COALESCE((details->>'results')::int, 0) = 1
                 THEN 'Search: 1 result'
                 ELSE 'Search: ' || COALESCE((details->>'results')::int, 0)::text || ' results'
            END
        WHEN 'load_pitched' THEN 'Pitched ' || COALESCE(load_id, 'a load')
        WHEN 'negotiation_opened' THEN
            'Offer $' || to_char((details->>'opening_offer')::numeric, 'FM999,999,999')
        WHEN 'negotiation_round' THEN
            CASE details->>'action'
                WHEN 'clarify' THEN
                    'asked to confirm the number' ||
                    CASE WHEN details->>'carrier_ask' IS NOT NULL
                         THEN ' (heard $' || to_char((details->>'carrier_ask')::numeric, 'FM999,999,999') || ')'
                         ELSE '' END
                WHEN 'accept' THEN
                    'Round ' || COALESCE(details->>'round', '') || ': carrier asked $' ||
                    to_char((details->>'carrier_ask')::numeric, 'FM999,999,999') || ' · accepted'
                WHEN 'close' THEN
                    'Round ' || COALESCE(details->>'round', '') || ': carrier asked $' ||
                    to_char((details->>'carrier_ask')::numeric, 'FM999,999,999') || ' · no deal'
                ELSE
                    'Round ' || COALESCE(details->>'round', '') || ': carrier asked $' ||
                    to_char((details->>'carrier_ask')::numeric, 'FM999,999,999') ||
                    ' · system countered $' || to_char((details->>'offer')::numeric, 'FM999,999,999')
            END
        WHEN 'negotiation_closed' THEN
            'Negotiation ' || COALESCE(details->>'outcome', '') ||
            CASE WHEN details->>'agreed_rate' IS NOT NULL
                 THEN ' at $' || to_char((details->>'agreed_rate')::numeric, 'FM999,999,999')
                 ELSE '' END
        WHEN 'booking_attempted' THEN
            'Booking at $' || to_char((details->>'agreed_rate')::numeric, 'FM999,999,999')
        WHEN 'booking_result' THEN 'Booking ' || COALESCE(details->>'status', '')
        WHEN 'handoff_created' THEN 'Handoff: ' || COALESCE(details->>'reason', '')
        WHEN 'lead_created' THEN 'Lead: ' || COALESCE(details->>'temperature', '')
        ELSE event_type
    END AS label
FROM call_events
ORDER BY run_id, occurred_at;

-- ---------------------------------------------------------------------------
-- v_calls: one row per call, with the fields the Call record card shows.
--
-- (v_funnel is defined after this view: it reads v_calls so that a call's exit
-- reason and the stage it exited at have one definition rather than two.)
-- ---------------------------------------------------------------------------
CREATE VIEW v_calls AS
WITH bounds AS (
    SELECT run_id, min(occurred_at) AS started_at, max(occurred_at) AS ended_at
    FROM call_events GROUP BY run_id
),
fmcsa AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name' AS carrier_name,
           (details->>'eligible')::boolean AS eligible,
           details->>'reason' AS fmcsa_reason
    FROM call_events WHERE event_type = 'fmcsa_checked'
    ORDER BY run_id, occurred_at
),
otp AS (
    SELECT run_id,
           bool_or(event_type = 'otp_verified') AS otp_verified
    FROM call_events WHERE event_type IN ('otp_verified', 'otp_failed')
    GROUP BY run_id
),
opened AS (
    SELECT DISTINCT ON (run_id) run_id, load_id, occurred_at AS opened_at,
           (details->>'opening_offer')::bigint AS opening_offer,
           (details->>'ceiling')::bigint AS ceiling
    FROM call_events WHERE event_type = 'negotiation_opened'
    ORDER BY run_id, occurred_at
),
closed AS (
    SELECT DISTINCT ON (run_id) run_id,
           (details->>'agreed_rate')::bigint AS agreed_rate,
           details->>'outcome' AS outcome
    FROM call_events WHERE event_type = 'negotiation_closed'
    ORDER BY run_id, occurred_at
),
rounds AS (
    SELECT run_id, count(*) AS rounds_used
    FROM call_events
    WHERE event_type = 'negotiation_round' AND details->>'action' <> 'clarify'
    GROUP BY run_id
),
booked AS (
    SELECT DISTINCT ON (run_id) run_id, details->>'status' AS booking_status
    FROM call_events WHERE event_type = 'booking_result'
    ORDER BY run_id, occurred_at
),
-- The best result count of the call, not the first search's. A carrier who
-- hears "nothing out of Hawaii" and then asks about Illinois has had one
-- search with results, and labelling that call `no_match` because the *first*
-- attempt was empty would be wrong.
searched AS (
    SELECT run_id, max((details->>'results')::int) AS results
    FROM call_events WHERE event_type = 'search_performed'
    GROUP BY run_id
),
handed AS (
    SELECT DISTINCT ON (run_id) run_id, details->>'reason' AS handoff_reason
    FROM call_events WHERE event_type = 'handoff_created'
    ORDER BY run_id, occurred_at
),
-- The last *step of the call* the run was engaged in. A different question from
-- how far it got through the funnel: a search that found nothing is still a
-- search. This is what `dropped_at_stage` reports.
last_step AS (
    SELECT run_id,
        CASE
            WHEN bool_or(event_type IN ('booking_attempted', 'booking_result'))
                THEN 'booking'
            -- An accepted negotiation puts the call at the booking step even if
            -- the booking never happened: the call was waiting on the TMS, not
            -- still haggling. Without this, a call cut off between the agreement
            -- and LOAD_BOOK would report `negotiation`, which is the step it had
            -- already finished.
            WHEN bool_or(event_type = 'negotiation_closed'
                         AND details->>'outcome' = 'accepted')
                THEN 'booking'
            WHEN bool_or(event_type IN ('negotiation_opened', 'negotiation_round',
                                        'negotiation_closed'))
                THEN 'negotiation'
            WHEN bool_or(event_type = 'search_performed') THEN 'search'
            WHEN bool_or(event_type IN ('otp_sent', 'otp_verified', 'otp_failed'))
                THEN 'otp'
            ELSE 'fmcsa'
        END AS last_stage
    FROM call_events
    GROUP BY run_id
),
-- One row per call, fully shaped. Nested so that `dropped_at_stage` can be
-- derived from the `outcome` this query computes instead of re-deriving the
-- terminal-event test — the two would drift the first time either changed.
shaped AS (
    SELECT
        b.run_id,
        -- qa / review / live, resolved in one place (v_run_source). Aliased
        -- `src` rather than `s`: `v_calls` already uses `s` for `searched`.
        src.source,
        b.started_at,
        b.ended_at,
        f.mc_number,
        f.carrier_name,
        o.otp_verified IS TRUE AS otp_verified,
        -- final outcome, in the priority order a rep reads it
        CASE
            WHEN bk.booking_status = 'booked' THEN 'booked'
            WHEN bk.booking_status = 'booked_unconfirmed' THEN 'booking_pending'
            WHEN cl.outcome = 'closed' THEN 'negotiation_failed'
            WHEN s.results = 0 THEN 'no_match'
            WHEN h.handoff_reason = 'onboarding' THEN 'routed_onboarding'
            WHEN f.eligible IS FALSE THEN 'fmcsa_rejected'
            WHEN o.otp_verified IS FALSE THEN 'otp_failed'
            -- No terminal event. A voice call does not last 15 minutes and the
            -- service never sees a hangup, so a call whose last event is older
            -- than that was cut off rather than still running — the carrier put
            -- the phone down and nothing will ever write another event for it.
            --
            -- A call that ended in a handoff is excluded, whatever the reason.
            -- `booked` / `booking_pending` / `negotiation_failed` / `onboarding`
            -- are already decided above; what is left here is `requested` (and
            -- `fmcsa_rejected`, decided above too). A rep was asked to take the
            -- call, so it reached a deliberate end state and is not `dropped`.
            WHEN b.ended_at < now() - interval '15 minutes'
                 AND h.run_id IS NULL THEN 'dropped'
            ELSE 'in_progress'
        END AS outcome,
        ls.last_stage,
        -- How many funnel stages the call got through: 0 = calls, 5 = booked.
        -- Built from the same CTEs the outcome is, so the two cannot disagree.
        CASE
            WHEN bk.booking_status = 'booked' THEN 5
            WHEN bk.booking_status IS NOT NULL OR cl.outcome = 'accepted' THEN 4
            WHEN COALESCE(s.results, 0) > 0 THEN 3
            WHEN o.otp_verified IS TRUE THEN 2
            WHEN f.eligible IS TRUE THEN 1
            ELSE 0
        END AS funnel_step,
        op.load_id,
        op.opening_offer,
        cl.agreed_rate,
        op.ceiling,
        op.ceiling - cl.agreed_rate AS protected_margin_usd,
        CASE WHEN (op.ceiling - op.opening_offer) > 0 THEN
            round(100.0 * (op.ceiling - cl.agreed_rate) / (op.ceiling - op.opening_offer), 1)
        END AS protected_margin_pct,
        r.rounds_used,
        extract(epoch FROM (op.opened_at - b.started_at))::int AS seconds_to_first_offer,
        bk.booking_status
    FROM bounds b
    JOIN v_run_source src USING (run_id)
    LEFT JOIN fmcsa f  USING (run_id)
    LEFT JOIN otp o    USING (run_id)
    LEFT JOIN opened op USING (run_id)
    LEFT JOIN closed cl USING (run_id)
    LEFT JOIN rounds r  USING (run_id)
    LEFT JOIN booked bk USING (run_id)
    LEFT JOIN searched s USING (run_id)
    LEFT JOIN handed h   USING (run_id)
    LEFT JOIN last_step ls USING (run_id)
)
SELECT
    run_id, source, started_at, ended_at, mc_number, carrier_name, otp_verified,
    outcome,
    -- Only meaningful for a dropped call, and NULL everywhere else: the step the
    -- call was in when it was cut off. Naming a stage for a call that finished
    -- normally would read as "this is where it broke", which is not true.
    CASE WHEN outcome = 'dropped' THEN last_stage END AS dropped_at_stage,
    last_stage,
    funnel_step,
    load_id, opening_offer, agreed_rate, ceiling,
    protected_margin_usd, protected_margin_pct, rounds_used,
    seconds_to_first_offer, booking_status
FROM shaped;

-- ---------------------------------------------------------------------------
-- v_funnel: every call accounted for at every step, with the reason it left.
--
-- Long format, one row per (stage, bucket):
--
--   entered    calls that reached this step
--   passed     calls that went on to the next step
--   exited     calls that stopped here, one row per reason
--   completed  calls that finished the funnel (a confirmed booking)
--   in_flight  calls that reached this step and have no outcome yet
--
-- `entered = passed + completed + in_flight + sum(exited)` holds for every
-- stage, by construction rather than by arithmetic that could drift: a call
-- leaves the funnel at exactly one step, and `funnel_step` (v_calls) is that
-- step. So
--
--   entered(S)   = |funnel_step >= S|
--   passed(S)    = |funnel_step >= S + 1|
--   exited(S)    = |funnel_step = S and it has an exit reason|
--   completed(S) = |funnel_step = S and outcome = 'booked'|
--   in_flight(S) = |funnel_step = S and neither of the above|
--
-- `completed` is only ever non-zero on the last stage, and `passed` is only
-- ever zero there: there is no step after `booked` to pass on to.
--
-- `reason` is the call's own outcome (`fmcsa_rejected`, `otp_failed`,
-- `no_match`, `negotiation_failed`, `booking_pending`, `routed_onboarding`) or
-- `dropped_<stage>` for a call cut off mid-step. Filter `bucket = 'exited'`
-- for the reasons; `bucket = 'entered'` reproduces the old column-per-stage
-- shape of this view.
-- ---------------------------------------------------------------------------
CREATE VIEW v_funnel AS
WITH steps(name, ordinal) AS (
    VALUES ('calls', 0), ('fmcsa_ok', 1), ('otp_ok', 2),
           ('load_found', 3), ('agreed', 4), ('booked', 5)
),
classified AS (
    SELECT
        funnel_step,
        outcome,
        CASE
            -- No exit to report: the call either finished the funnel or is
            -- still in it. Which of the two is the `completed` / `in_flight`
            -- split below.
            WHEN outcome IN ('booked', 'in_progress') THEN NULL
            WHEN outcome = 'dropped' THEN 'dropped_' || dropped_at_stage
            ELSE outcome
        END AS exit_reason
    FROM v_calls
),
buckets AS (
    SELECT s.name, s.ordinal, 'entered' AS bucket, NULL::text AS reason,
           count(c.*) AS calls
    FROM steps s
    LEFT JOIN classified c ON c.funnel_step >= s.ordinal
    GROUP BY s.name, s.ordinal

    UNION ALL

    SELECT s.name, s.ordinal, 'passed', NULL, count(c.*)
    FROM steps s
    LEFT JOIN classified c ON c.funnel_step >= s.ordinal + 1
    GROUP BY s.name, s.ordinal

    UNION ALL

    SELECT s.name, s.ordinal, 'completed', NULL, count(*)
    FROM steps s
    JOIN classified c ON c.funnel_step = s.ordinal AND c.outcome = 'booked'
    GROUP BY s.name, s.ordinal

    UNION ALL

    -- In-flight calls reached this step and nothing has decided them yet:
    -- either the call is genuinely still running, or it ended in a handoff
    -- whose reason has no outcome of its own (`requested`).
    SELECT s.name, s.ordinal, 'in_flight', NULL, count(*)
    FROM steps s
    JOIN classified c ON c.funnel_step = s.ordinal
                     AND c.exit_reason IS NULL AND c.outcome <> 'booked'
    GROUP BY s.name, s.ordinal

    UNION ALL

    SELECT s.name, s.ordinal, 'exited', c.exit_reason, count(*)
    FROM steps s
    JOIN classified c ON c.funnel_step = s.ordinal AND c.exit_reason IS NOT NULL
    GROUP BY s.name, s.ordinal, c.exit_reason
)
SELECT stage, ordinal, bucket, reason, calls
FROM (
    SELECT name AS stage, ordinal, bucket, reason, calls FROM buckets
) rows
ORDER BY ordinal,
         CASE bucket WHEN 'entered' THEN 0 WHEN 'passed' THEN 1
                     WHEN 'exited' THEN 2 ELSE 3 END,
         reason;

-- ---------------------------------------------------------------------------
-- v_kpi_overview: one row of headline numbers.
-- ---------------------------------------------------------------------------
CREATE VIEW v_kpi_overview AS
WITH calls AS (SELECT * FROM v_calls),
north AS (
    SELECT count(*) FILTER (WHERE outcome IN ('booked', 'booking_pending')
                            AND agreed_rate <= ceiling) AS booked_at_or_under,
           count(*) FILTER (WHERE otp_verified) AS otp_verified_calls
    FROM calls
),
margins AS (
    SELECT sum(protected_margin_usd) AS total_margin_usd,
           round(avg(protected_margin_usd)) AS avg_margin_usd
    FROM calls
),
identity AS (
    SELECT
        (SELECT count(DISTINCT run_id) FROM call_events
          WHERE event_type = 'fmcsa_checked' AND (details->>'eligible')::boolean IS FALSE) AS fmcsa_stopped,
        (SELECT count(*) FROM (
            SELECT run_id FROM call_events WHERE event_type = 'otp_failed'
            EXCEPT
            SELECT run_id FROM call_events WHERE event_type = 'otp_verified'
        ) s) AS otp_stopped
),
time_to_offer AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds_to_first_offer)
             AS median_seconds_to_first_offer
    FROM calls WHERE seconds_to_first_offer IS NOT NULL AND source = 'live'
),
onboarding AS (
    SELECT count(*) AS onboarding_leads
    FROM call_events WHERE event_type = 'handoff_created' AND details->>'reason' = 'onboarding'
),
rules AS (
    SELECT
        count(*) FILTER (WHERE agreed_rate > max_buy) AS bookings_over_ceiling,
        count(*) FILTER (WHERE (event_type = 'negotiation_opened' OR event_type = 'booking_result')
                         AND run_id NOT IN (SELECT run_id FROM call_events WHERE event_type = 'otp_verified'))
             AS otp_bypasses
    FROM call_events
)
SELECT
    CASE WHEN n.otp_verified_calls > 0
         THEN round(100.0 * n.booked_at_or_under / n.otp_verified_calls, 1)
    END AS north_star_pct,
    m.total_margin_usd,
    m.avg_margin_usd,
    i.fmcsa_stopped + i.otp_stopped AS identity_risks_stopped,
    t.median_seconds_to_first_offer,
    o.onboarding_leads,
    r.bookings_over_ceiling,
    r.otp_bypasses,
    0 AS ceiling_revelations  -- asserted by tests; never recorded, never leaked
FROM north n, margins m, identity i, time_to_offer t, onboarding o, rules r;

-- ===========================================================================
-- Signals
--
-- Everything below answers a question a human would otherwise ask by reading
-- calls one at a time. Each view exposes `source` (qa / review / live) so a
-- dashboard can filter without a second definition of what a QA run is.
--
-- All of these read `details` (jsonb), so every field they touch has to be one
-- the service actually emits. Where a field is missing today, the comment says
-- so and names the minimal change that would supply it rather than inventing a
-- proxy that looks like data.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- v_signal_unserved_demand: lanes a carrier asked for and we had nothing on.
--
-- A zero-result search is the only event that records a *want* with no supply.
-- Grouped by equipment + origin + destination, which are the canonical TMS keys
-- the search actually carried (`search_performed.details.filters`). A dimension
-- the carrier did not specify is NULL and groups with the other NULLs — which
-- is correct: "anywhere" is not a lane.
--
-- `search_performed` carries no `mc_number`, so the carrier is resolved from
-- the run's `fmcsa_checked` event. That is a join, not a gap: every search is
-- gated behind a verified OTP, which is gated behind a verify.
-- ---------------------------------------------------------------------------
CREATE VIEW v_signal_unserved_demand AS
WITH carrier AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name' AS carrier_name
    FROM call_events
    WHERE event_type = 'fmcsa_checked' AND mc_number IS NOT NULL
    ORDER BY run_id, occurred_at
),
unserved AS (
    SELECT
        ce.run_id,
        ce.occurred_at,
        ce.details->'filters'->>'EQTYPE'     AS equipment,
        ce.details->'filters'->>'ORIG_CITY'  AS origin_city,
        ce.details->'filters'->>'ORIG_STATE' AS origin_state,
        ce.details->'filters'->>'DEST_CITY'  AS dest_city,
        ce.details->'filters'->>'DEST_STATE' AS dest_state,
        c.mc_number,
        c.carrier_name,
        s.source
    FROM call_events ce
    JOIN v_run_source s USING (run_id)
    LEFT JOIN carrier c USING (run_id)
    WHERE ce.event_type = 'search_performed'
      AND COALESCE((ce.details->>'results')::int, 0) = 0
)
SELECT
    equipment,
    origin_city,
    origin_state,
    dest_city,
    dest_state,
    source,
    count(*)                        AS searches,
    count(DISTINCT mc_number)       AS distinct_carriers,
    min(occurred_at)                AS first_seen,
    max(occurred_at)                AS last_seen,
    (array_agg(run_id ORDER BY occurred_at DESC))[1] AS last_run_id,
    array_remove(array_agg(DISTINCT mc_number), NULL) AS carrier_mcs
FROM unserved
GROUP BY equipment, origin_city, origin_state, dest_city, dest_state, source;

-- ---------------------------------------------------------------------------
-- v_signal_price_check: loads that keep costing us rounds and never book.
--
-- Two signals, either of which is enough:
--   failed_negotiations >= 2   we walked away from this load twice
--   times_opened        >= 3   we opened it three times without closing it
--   pitches             >= 3   it was pitched three times without booking
--
-- `pitches` comes from `load_pitched`, which today is emitted only by
-- `GET /loads/{load_id}` — a route the platform no longer calls (DECISIONS §56
-- moved the detail into the search response). It is therefore 0 for every load
-- in current data. The column and its predicate are here so that the day the
-- event is emitted from `/loads/search` (the minimal change proposed in the
-- handover note) this view is already right, with no edit.
--
-- Loads that ever booked are excluded: the question is what we are failing to
-- sell, not what we sold.
-- ---------------------------------------------------------------------------
CREATE VIEW v_signal_price_check AS
WITH carrier AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name' AS carrier_name
    FROM call_events
    WHERE event_type = 'fmcsa_checked' AND mc_number IS NOT NULL
    ORDER BY run_id, occurred_at
),
opens AS (
    SELECT load_id, count(*) AS times_opened, max(occurred_at) AS last_opened
    FROM call_events
    WHERE event_type = 'negotiation_opened' AND load_id IS NOT NULL
    GROUP BY load_id
),
closes AS (
    SELECT
        load_id,
        count(*) FILTER (WHERE details->>'outcome' = 'closed') AS failed_negotiations,
        avg((details->>'gap')::numeric)
            FILTER (WHERE details->>'outcome' = 'closed') AS avg_gap_usd,
        avg((details->>'gap')::numeric
            / NULLIF((details->>'ceiling')::numeric, 0))
            FILTER (WHERE details->>'outcome' = 'closed') AS avg_gap_ratio,
        max(occurred_at) FILTER (WHERE details->>'outcome' = 'closed') AS last_closed
    FROM call_events
    WHERE event_type = 'negotiation_closed' AND load_id IS NOT NULL
    GROUP BY load_id
),
pitches AS (
    SELECT load_id, count(*) AS pitches
    FROM call_events
    WHERE event_type = 'load_pitched' AND load_id IS NOT NULL
    GROUP BY load_id
),
booked AS (
    SELECT DISTINCT load_id
    FROM call_events
    WHERE event_type = 'booking_result' AND details->>'status' = 'booked'
),
-- The most recent time this load was opened, for the lane and the equipment.
latest AS (
    SELECT DISTINCT ON (load_id)
        load_id, run_id, occurred_at,
        details->>'origin'         AS origin,
        details->>'destination'    AS destination,
        details->>'equipment_type' AS equipment_type,
        (details->>'miles')::int   AS miles
    FROM call_events
    WHERE event_type = 'negotiation_opened' AND load_id IS NOT NULL
    ORDER BY load_id, occurred_at DESC
)
SELECT
    l.load_id,
    l.origin,
    l.destination,
    l.equipment_type,
    l.miles,
    s.source,
    c.mc_number,
    c.carrier_name,
    COALESCE(o.times_opened, 0)        AS times_opened,
    COALESCE(p.pitches, 0)             AS pitches,
    COALESCE(cl.failed_negotiations, 0) AS failed_negotiations,
    round(cl.avg_gap_usd)              AS avg_gap_usd,
    round(cl.avg_gap_ratio * 100, 1)   AS avg_gap_pct,
    GREATEST(o.last_opened, cl.last_closed, l.occurred_at) AS last_seen
FROM latest l
JOIN v_run_source s ON s.run_id = l.run_id
LEFT JOIN carrier c ON c.run_id = l.run_id
LEFT JOIN opens   o USING (load_id)
LEFT JOIN closes  cl USING (load_id)
LEFT JOIN pitches p USING (load_id)
WHERE l.load_id NOT IN (SELECT load_id FROM booked)
  AND (COALESCE(cl.failed_negotiations, 0) >= 2
       OR COALESCE(p.pitches, 0) >= 3
       OR COALESCE(o.times_opened, 0) >= 3);

-- ---------------------------------------------------------------------------
-- v_signal_recoverable: negotiations we lost by a hair.
--
-- `negotiation_closed` already carries everything this needs — the carrier's
-- final ask, the ceiling (C) and the gap (ask − C) — so no event change is
-- required. A close inside 5% of the ceiling is a load a rep can very likely
-- save with one phone call, which is why it is separated from the general
-- `negotiation_failed` pile.
--
-- The predicate is written as `gap < 0.05 * ceiling` rather than
-- `gap / ceiling < 0.05`: same answer, and it cannot divide by zero.
-- ---------------------------------------------------------------------------
CREATE VIEW v_signal_recoverable AS
WITH carrier AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name' AS carrier_name
    FROM call_events
    WHERE event_type = 'fmcsa_checked' AND mc_number IS NOT NULL
    ORDER BY run_id, occurred_at
)
SELECT
    ce.run_id,
    ce.load_id,
    ce.details->>'negotiation_id' AS negotiation_id,
    ce.occurred_at,
    s.source,
    c.mc_number,
    c.carrier_name,
    op.details->>'origin'         AS origin,
    op.details->>'destination'    AS destination,
    op.details->>'equipment_type' AS equipment_type,
    (op.details->>'miles')::int   AS miles,
    (ce.details->>'carrier_ask')::bigint AS carrier_ask,
    (ce.details->>'ceiling')::bigint     AS ceiling,
    -- What the carrier wanted above our ceiling. This is the whole question for
    -- a rep: a small number is one phone call away from being a booking.
    (ce.details->>'gap')::bigint         AS gap_usd,
    round(100.0 * (ce.details->>'gap')::numeric
          / NULLIF((ce.details->>'ceiling')::numeric, 0), 2) AS gap_pct
FROM call_events ce
JOIN v_run_source s USING (run_id)
LEFT JOIN carrier c USING (run_id)
LEFT JOIN call_events op
       ON op.event_type = 'negotiation_opened'
      AND op.details->>'negotiation_id' = ce.details->>'negotiation_id'
WHERE ce.event_type = 'negotiation_closed'
  AND ce.details->>'outcome' = 'closed'
  AND (ce.details->>'gap')::numeric < 0.05 * (ce.details->>'ceiling')::numeric;

-- ---------------------------------------------------------------------------
-- v_rate_per_mile: what we paid per mile against what was posted, by lane.
--
-- Both numbers already exist on the two negotiation events and are joined on
-- `negotiation_id`: `negotiation_opened` carries `miles` and `posted_rate`,
-- `negotiation_closed` carries `agreed_rate`. No event change needed.
--
-- Only accepted negotiations appear — a lane we never agreed on has no
-- "agreed rate per mile" to compare. `avg_delta_rpm` is agreed minus posted,
-- so a positive number means we paid above the board.
-- ---------------------------------------------------------------------------
CREATE VIEW v_rate_per_mile AS
WITH opened AS (
    SELECT
        details->>'negotiation_id' AS negotiation_id,
        run_id,
        details->>'origin'         AS origin,
        details->>'destination'    AS destination,
        details->>'equipment_type' AS equipment_type,
        (details->>'miles')::numeric       AS miles,
        (details->>'posted_rate')::numeric AS posted_rate
    FROM call_events
    WHERE event_type = 'negotiation_opened'
),
agreed AS (
    SELECT
        details->>'negotiation_id' AS negotiation_id,
        (details->>'agreed_rate')::numeric AS agreed_rate
    FROM call_events
    WHERE event_type = 'negotiation_closed'
      AND details->>'outcome' = 'accepted'
      AND details->>'agreed_rate' IS NOT NULL
)
SELECT
    o.origin || ' → ' || o.destination AS lane,
    o.equipment_type,
    s.source,
    count(*)                                  AS negotiations,
    round(avg(o.miles), 0)                    AS avg_miles,
    round(avg(o.posted_rate), 0)              AS avg_posted_rate,
    round(avg(a.agreed_rate), 0)              AS avg_agreed_rate,
    round(avg(o.posted_rate / o.miles), 3)    AS avg_posted_rpm,
    round(avg(a.agreed_rate / o.miles), 3)    AS avg_agreed_rpm,
    round(avg((a.agreed_rate - o.posted_rate) / o.miles), 3) AS avg_delta_rpm,
    round(avg(o.posted_rate - a.agreed_rate), 0) AS avg_saved_vs_posted
FROM opened o
JOIN agreed a USING (negotiation_id)
JOIN v_run_source s ON s.run_id = o.run_id
WHERE o.miles IS NOT NULL AND o.miles > 0
GROUP BY o.origin || ' → ' || o.destination, o.equipment_type, s.source;

-- ---------------------------------------------------------------------------
-- v_rep_queue: the derivation package a senior rep needs, one row per call
-- that was handed off because of a booking.
--
-- Scoped to `booked` and `booking_pending`, which is the queue the rep works:
-- what is waiting on a person is a booking that is done or uncertain. The
-- other handoff reasons (`negotiation_failed`, `onboarding`, `fmcsa_rejected`,
-- `requested`) are one predicate away if the queue is ever widened.
--
-- `handoff_created` carries only `{ticket_id, reason}` — no carrier, no load,
-- no rate — so everything else is joined from the events of the same run. That
-- is deliberate: the handoff event is a pointer, and duplicating the package
-- into it would mean two copies of the rate that could disagree.
-- ---------------------------------------------------------------------------
CREATE VIEW v_rep_queue AS
WITH carrier AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name' AS carrier_name
    FROM call_events
    WHERE event_type = 'fmcsa_checked' AND mc_number IS NOT NULL
    ORDER BY run_id, occurred_at
),
-- `booking_attempted` is the event that carries the rate, the ceiling and the
-- load together, and it is written before LOAD_BOOK — so it survives even a
-- booking whose response was lost.
attempted AS (
    SELECT DISTINCT ON (run_id)
        run_id, load_id,
        (details->>'agreed_rate')::bigint AS agreed_rate,
        (details->>'ceiling')::bigint     AS ceiling
    FROM call_events
    WHERE event_type = 'booking_attempted'
    ORDER BY run_id, occurred_at DESC
),
result AS (
    SELECT DISTINCT ON (run_id)
        run_id,
        details->>'status'       AS booking_status,
        details->>'booking_ref'  AS booking_ref,
        (details->>'ambiguous')::boolean AS ambiguous
    FROM call_events
    WHERE event_type = 'booking_result'
    ORDER BY run_id, occurred_at DESC
),
lane AS (
    SELECT DISTINCT ON (run_id)
        run_id, load_id,
        details->>'origin'         AS origin,
        details->>'destination'    AS destination,
        details->>'equipment_type' AS equipment_type,
        (details->>'miles')::numeric AS miles
    FROM call_events
    WHERE event_type = 'negotiation_opened'
    ORDER BY run_id, occurred_at DESC
)
SELECT
    h.occurred_at                     AS queued_at,
    h.details->>'ticket_id'           AS ticket_id,
    h.run_id,
    h.details->>'reason'              AS reason,
    s.source,
    c.mc_number,
    c.carrier_name,
    COALESCE(a.load_id, l.load_id)    AS load_id,
    l.origin,
    l.destination,
    l.equipment_type,
    l.miles,
    a.agreed_rate,
    a.ceiling,
    a.ceiling - a.agreed_rate         AS margin_usd,
    round(a.agreed_rate / NULLIF(l.miles, 0), 3) AS agreed_rpm,
    r.booking_status,
    r.booking_ref,
    r.ambiguous
FROM call_events h
JOIN v_run_source s USING (run_id)
LEFT JOIN carrier   c USING (run_id)
LEFT JOIN attempted a USING (run_id)
LEFT JOIN result    r USING (run_id)
-- Joined on run_id, not load_id: `lane` is DISTINCT ON (run_id), so matching on
-- a load id would fan out across every other call that touched the same load.
LEFT JOIN lane      l ON l.run_id = h.run_id
WHERE h.event_type = 'handoff_created'
  AND h.details->>'reason' IN ('booked', 'booking_pending')
ORDER BY h.occurred_at;

-- ---------------------------------------------------------------------------
-- v_onboarding_leads: carriers with no contact on file, and what we know.
--
-- **This view is built on two sources today, and one of them is a placeholder.**
-- `lead_created` is defined in the event set (CLAUDE.md §9) and never emitted:
-- the `routed_onboarding` path in `/otp/send` writes no event at all, and
-- `app/onboarding/qualification.py` is never called outside the tests. So the
-- only surviving trace of an onboarding call is the `handoff_created` event
-- with `reason = 'onboarding'`, which the agent has to ask for.
--
-- A run counts as a lead if either event exists. When `lead_created` starts
-- being emitted, `temperature` / `lead_reason` / `authority_age_days` /
-- `matching_loads` fill in with no edit to this view — and `evidence` says
-- which of the two put the row here.
-- ---------------------------------------------------------------------------
CREATE VIEW v_onboarding_leads AS
WITH runs AS (
    SELECT DISTINCT run_id
    FROM call_events
    WHERE event_type = 'lead_created'
       OR (event_type = 'handoff_created' AND details->>'reason' = 'onboarding')
),
lead AS (
    SELECT DISTINCT ON (run_id) run_id, occurred_at, details
    FROM call_events
    WHERE event_type = 'lead_created'
    ORDER BY run_id, occurred_at
),
handed AS (
    SELECT DISTINCT ON (run_id) run_id, occurred_at,
           details->>'ticket_id' AS ticket_id
    FROM call_events
    WHERE event_type = 'handoff_created' AND details->>'reason' = 'onboarding'
    ORDER BY run_id, occurred_at
),
carrier AS (
    SELECT DISTINCT ON (run_id) run_id, mc_number,
           details->>'legal_name'  AS carrier_name,
           (details->>'eligible')::boolean AS fmcsa_eligible,
           details->>'reason'      AS fmcsa_reason
    FROM call_events
    WHERE event_type = 'fmcsa_checked'
    ORDER BY run_id, occurred_at
),
searched AS (
    SELECT run_id,
           count(*) AS searches,
           max((details->>'results')::int) AS best_result_count
    FROM call_events
    WHERE event_type = 'search_performed'
    GROUP BY run_id
)
SELECT
    r.run_id,
    s.source,
    COALESCE(c.mc_number, l.details->>'mc_number') AS mc_number,
    c.carrier_name,
    c.fmcsa_eligible,
    c.fmcsa_reason,
    l.details->>'temperature'  AS temperature,
    l.details->>'reason'       AS lead_reason,
    (l.details->>'authority_age_days')::int AS authority_age_days,
    (l.details->>'matching_loads')::int     AS matching_loads,
    COALESCE(l.occurred_at, h.occurred_at)  AS occurred_at,
    h.ticket_id,
    COALESCE(se.searches, 0)        AS searches,
    COALESCE(se.best_result_count, 0) AS best_result_count,
    CASE WHEN l.run_id IS NOT NULL THEN 'lead_created' ELSE 'handoff_onboarding' END AS evidence
FROM runs r
JOIN v_run_source s USING (run_id)
LEFT JOIN lead     l  USING (run_id)
LEFT JOIN handed   h  USING (run_id)
LEFT JOIN carrier  c  USING (run_id)
LEFT JOIN searched se USING (run_id);
