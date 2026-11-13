-- Phase 1 gate: is there any gap over 5 minutes in option_quotes in the last 24 hours?
--
-- Each snapshot minute that has at least one option_quotes row counts as present. A gap is the
-- time from one present minute to the next: normally 1 minute. The gate reads "no gap over 5
-- minutes" strictly: consecutive snapshots are never more than 5 minutes apart (so at most 4
-- minutes in a row may be missing). The window edges count as present, so missing minutes at
-- the start or end of the window count too. The window is the 24 hours ending at the last
-- full minute.
--
-- Run on the server (psql inside the db container; prints no secret):
--   infra/dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < infra/sql/gap_check.sql
WITH bounds AS (
    SELECT date_trunc('minute', now()) - interval '1 minute' AS window_end,
           date_trunc('minute', now()) - interval '1 minute' - interval '24 hours' AS window_start
),
present AS (
    SELECT q.ts, count(*) AS option_rows
    FROM option_quotes q, bounds b
    WHERE q.ts >= b.window_start AND q.ts <= b.window_end
    GROUP BY q.ts
),
points AS (
    SELECT window_start AS ts FROM bounds
    UNION
    SELECT ts FROM present
    UNION
    SELECT window_end FROM bounds
),
steps AS (
    SELECT lag(ts) OVER (ORDER BY ts) AS previous_ts, ts, ts - lag(ts) OVER (ORDER BY ts) AS step
    FROM points
)
SELECT
    (SELECT window_start FROM bounds) AS window_start,
    (SELECT window_end FROM bounds) AS window_end,
    (SELECT count(*) FROM present) AS minutes_present,
    (SELECT 24 * 60 + 1) AS minutes_expected,
    (SELECT min(option_rows) FROM present) AS min_rows_per_minute,
    (SELECT max(option_rows) FROM present) AS max_rows_per_minute,
    max(step) AS longest_gap,
    count(*) FILTER (WHERE step > interval '5 minutes') AS gaps_over_5_minutes,
    coalesce(
        string_agg(
            to_char(previous_ts, 'YYYY-MM-DD HH24:MI') || ' -> ' || to_char(ts, 'HH24:MI'),
            ', ' ORDER BY ts
        ) FILTER (WHERE step > interval '1 minute'),
        'none'
    ) AS breaks,
    CASE WHEN coalesce(max(step), interval '1 day') <= interval '5 minutes'
         THEN 'PASS' ELSE 'FAIL' END AS gate
FROM steps
WHERE previous_ts IS NOT NULL;
