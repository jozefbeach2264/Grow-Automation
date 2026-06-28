-- grow-automation trend store (TimescaleDB). Idempotent.
-- Statements are separated by a "-- @@" line so trend_db.py can run them one at a
-- time in autocommit (continuous-aggregate DDL cannot run inside a transaction).
-- Run standalone:  psql -d grow -f sql/trend_schema.sql
CREATE TABLE IF NOT EXISTS trend_samples (
  ts     TIMESTAMPTZ      NOT NULL,
  device TEXT             NOT NULL,
  metric TEXT             NOT NULL,   -- ph | tds_ppm | water_temp_f | ec_us | water_level | water_leak | ...
  value  DOUBLE PRECISION,
  source TEXT             NOT NULL DEFAULT 'poll'   -- poll | csv | ble
)
-- @@
SELECT create_hypertable('trend_samples', 'ts', if_not_exists => TRUE)
-- @@
-- One physical reading per (device, metric, ts) regardless of which path wrote it
-- (poll / csv / backfill) -- prevents double-counting in the aggregates.
CREATE UNIQUE INDEX IF NOT EXISTS trend_samples_uq
  ON trend_samples (device, metric, ts)
-- @@
-- Hourly rollup. materialized_only=false => real-time aggregation, so queries are
-- correct (materialized history + not-yet-materialized recent raw) without a refresh.
CREATE MATERIALIZED VIEW IF NOT EXISTS trend_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 hour', ts) AS bucket, device, metric,
       avg(value)        AS avg,
       min(value)        AS min,
       max(value)        AS max,
       first(value, ts)  AS first,
       last(value, ts)   AS last,
       count(*)          AS n
FROM trend_samples
GROUP BY 1, 2, 3
-- @@
CREATE MATERIALIZED VIEW IF NOT EXISTS trend_daily
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 day', ts) AS bucket, device, metric,
       avg(value)        AS avg,
       min(value)        AS min,
       max(value)        AS max,
       first(value, ts)  AS first,
       last(value, ts)   AS last,
       count(*)          AS n
FROM trend_samples
GROUP BY 1, 2, 3
