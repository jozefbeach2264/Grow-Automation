-- Background refresh policies for the continuous aggregates (production only).
-- Idempotent via if_not_exists. Requires the timescaledb background scheduler
-- (present by default once shared_preload_libraries='timescaledb'). Real-time
-- aggregation already keeps reads correct; these just keep the materialized part
-- fresh so long-range reads stay cheap.
-- Statements separated by "-- @@" (see trend_schema.sql).
SELECT add_continuous_aggregate_policy('trend_hourly',
  start_offset      => INTERVAL '3 days',
  end_offset        => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour',
  if_not_exists     => TRUE)
-- @@
SELECT add_continuous_aggregate_policy('trend_daily',
  start_offset      => INTERVAL '30 days',
  end_offset        => INTERVAL '1 day',
  schedule_interval => INTERVAL '6 hours',
  if_not_exists     => TRUE)
