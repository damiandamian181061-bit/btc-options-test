-- Phase 1 gate: database volume after day one.
-- Run on the server (psql inside the db container; prints no secret):
--   infra/dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < infra/sql/volume.sql

\echo 'Database size'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

\echo 'Hypertables (table, indexes, total; compressed chunks are included once compression runs)'
SELECT hypertable_name,
       pg_size_pretty(table_bytes) AS table_size,
       pg_size_pretty(index_bytes) AS index_size,
       pg_size_pretty(total_bytes) AS total_size
FROM timescaledb_information.hypertables h,
     LATERAL hypertable_detailed_size(format('%I.%I', h.hypertable_schema, h.hypertable_name)::regclass)
ORDER BY total_bytes DESC;

\echo 'Ordinary tables'
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND relname IN ('instruments', 'option_trades', 'delivery_prices', 'connection_events')
ORDER BY pg_total_relation_size(c.oid) DESC;

\echo 'Rows per UTC day'
SELECT 'option_quotes' AS table_name, date_trunc('day', ts)::date AS day, count(*) AS row_count
FROM option_quotes GROUP BY 2
UNION ALL
SELECT 'underlying_ticks', date_trunc('day', ts)::date, count(*) FROM underlying_ticks GROUP BY 2
UNION ALL
SELECT 'option_trades', date_trunc('day', ts)::date, count(*) FROM option_trades GROUP BY 2
ORDER BY 1, 2;

\echo 'Connection events by type'
SELECT source, event, count(*) AS events, min(ts) AS first, max(ts) AS last
FROM connection_events GROUP BY 1, 2 ORDER BY 1, 2;
