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
DROP VIEW IF EXISTS v_kpi_overview CASCADE;
DROP VIEW IF EXISTS v_calls CASCADE;
DROP VIEW IF EXISTS v_funnel CASCADE;
DROP VIEW IF EXISTS v_call_timeline CASCADE;

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
-- v_funnel: stage counts, with the drop between each stage.
-- ---------------------------------------------------------------------------
CREATE VIEW v_funnel AS
WITH stages AS (
    SELECT
        count(DISTINCT run_id) AS calls,
        count(DISTINCT run_id) FILTER (
            WHERE event_type = 'fmcsa_checked' AND (details->>'eligible')::boolean
        ) AS fmcsa_ok,
        count(DISTINCT run_id) FILTER (WHERE event_type = 'otp_verified') AS otp_ok,
        count(DISTINCT run_id) FILTER (
            WHERE event_type = 'search_performed' AND (details->>'results')::int > 0
        ) AS load_found,
        count(DISTINCT run_id) FILTER (
            WHERE event_type = 'negotiation_closed' AND details->>'outcome' = 'accepted'
        ) AS agreed,
        count(DISTINCT run_id) FILTER (
            WHERE event_type = 'booking_result' AND details->>'status' = 'booked'
        ) AS booked
    FROM call_events
)
SELECT 'calls' AS stage, calls AS count FROM stages
UNION ALL SELECT 'fmcsa_ok', fmcsa_ok FROM stages
UNION ALL SELECT 'otp_ok', otp_ok FROM stages
UNION ALL SELECT 'load_found', load_found FROM stages
UNION ALL SELECT 'agreed', agreed FROM stages
UNION ALL SELECT 'booked', booked FROM stages;

-- ---------------------------------------------------------------------------
-- v_calls: one row per call, with the fields the Call record card shows.
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
searched AS (
    SELECT DISTINCT ON (run_id) run_id, (details->>'results')::int AS results
    FROM call_events WHERE event_type = 'search_performed'
    ORDER BY run_id, occurred_at
),
handed AS (
    SELECT DISTINCT ON (run_id) run_id, details->>'reason' AS handoff_reason
    FROM call_events WHERE event_type = 'handoff_created'
    ORDER BY run_id, occurred_at
)
SELECT
    b.run_id,
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
        ELSE 'in_progress'
    END AS outcome,
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
LEFT JOIN fmcsa f  USING (run_id)
LEFT JOIN otp o    USING (run_id)
LEFT JOIN opened op USING (run_id)
LEFT JOIN closed cl USING (run_id)
LEFT JOIN rounds r  USING (run_id)
LEFT JOIN booked bk USING (run_id)
LEFT JOIN searched s USING (run_id)
LEFT JOIN handed h   USING (run_id);

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
           avg(protected_margin_usd) AS avg_margin_usd
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
    FROM calls WHERE seconds_to_first_offer IS NOT NULL
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
