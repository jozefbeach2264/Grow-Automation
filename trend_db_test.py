#!/usr/bin/env python3
"""
Self-tests for trend_db.py (TimescaleDB trend store). Builds the schema in a
throwaway namespace inside the `grow` db, exercises insert/dedup/queries against
real TimescaleDB (hypertable + continuous aggregates), then drops the schema.

Requires the local Postgres/TimescaleDB set up by scripts/setup_timescaledb.sh.
Run: python3 trend_db_test.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import trend_db
import trend_features
from ac_infinity_history import Sample

_PASS = 0
_FAIL = 0
SCHEMA = f"ttest_{os.getpid()}"
NOW = datetime.now(timezone.utc)


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")


def count(conn, device, metric=None):
    sql = "SELECT count(*) FROM trend_samples WHERE device = %s"
    p = [device]
    if metric:
        sql += " AND metric = %s"
        p.append(metric)
    return conn.execute(sql, p).fetchone()[0]


def test_schema_objects(conn):
    # hypertable + both caggs exist in the test schema
    ht = conn.execute(
        "SELECT count(*) FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema = %s AND hypertable_name = 'trend_samples'", [SCHEMA]
    ).fetchone()[0]
    ca = conn.execute(
        "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
        "WHERE view_schema = %s", [SCHEMA]
    ).fetchone()[0]
    check("schema: trend_samples is a hypertable", ht == 1)
    check("schema: two continuous aggregates", ca == 2)


def test_insert_and_bucket(conn):
    dev = "d_insert"
    rows = [(NOW - timedelta(hours=h), dev, "ph", 6.0 + h * 0.1, "poll") for h in (0, 1, 2)]
    n = trend_db.insert_samples(rows, conn=conn)
    check("insert: 3 rows submitted", n == 3)
    check("insert: 3 rows stored", count(conn, dev, "ph") == 3)
    buckets = trend_db.bucketed("ph", bucket="1 hour", since_hours=6, device=dev, view="raw", conn=conn)
    check("raw bucket: 3 hourly buckets", len(buckets) == 3)
    # avg of the most recent bucket == 6.0
    last_bucket_avg = buckets[-1][1]
    check("raw bucket: newest avg ~6.0", abs(last_bucket_avg - 6.0) < 1e-9)


def test_dedup(conn):
    dev = "d_dedup"
    row = [(NOW, dev, "ph", 5.9, "poll")]
    trend_db.insert_samples(row, conn=conn)
    trend_db.insert_samples(row, conn=conn)          # identical -> ON CONFLICT DO NOTHING
    check("dedup: identical row stored once", count(conn, dev, "ph") == 1)


def test_null_skipped(conn):
    dev = "d_null"
    n = trend_db.insert_samples([(NOW, dev, "ph", None, "poll")], conn=conn)
    check("null: submit returns 0", n == 0)
    check("null: nothing stored", count(conn, dev) == 0)


def test_record_snapshot_db(conn):
    snap = {
        "timestamp": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "devices": [
            {"name": "d_snap", "sensors": {
                "ph": 6.1, "tds_ppm": 620, "water_temp_f": 68.0, "ec_us": 880, "water_leak": 0}},
            {"name": "air_only", "sensors": {"temp_f_tent": 80.0}},   # ignored (no hydro)
        ],
    }
    n = trend_db.record_snapshot_db(snap, conn=conn)
    check("snapshot: 5 metrics inserted", n == 5)            # ph, tds, water_temp, ec_us, water_leak
    check("snapshot: air-only device skipped", count(conn, "air_only") == 0)
    check("snapshot: water_leak stored as 0.0",
          conn.execute("SELECT value FROM trend_samples WHERE device='d_snap' AND metric='water_leak'").fetchone()[0] == 0.0)


def test_sample_rows_csv(conn):
    dev = "d_csv"
    samples = [
        Sample(ts=NOW - timedelta(minutes=2), ph=5.8, tds_ppm=500, water_temp_f=67.0,
               water_leak=False, out_temp_f=75.0, out_humidity=50.0, out_vpd=1.0),
        Sample(ts=NOW - timedelta(minutes=1), ph=5.9, tds_ppm=510, water_temp_f=67.1,
               water_leak=None, out_temp_f=None, out_humidity=None, out_vpd=None),
    ]
    n = trend_db.ingest_samples_db(dev, samples, source="csv", conn=conn)
    # row 1: ph,tds,water_temp,out_temp,out_hum,out_vpd,water_leak = 7 ; row 2: ph,tds,water_temp = 3
    check("csv rows: 10 metrics inserted", n == 10)
    check("csv rows: stored", count(conn, dev) == 10)


def test_continuous_aggregate(conn):
    dev = "d_cagg"
    rows = [(NOW - timedelta(hours=h), dev, "tds_ppm", 600 + h, "poll") for h in (0, 1, 2, 3)]
    trend_db.insert_samples(rows, conn=conn)
    # real-time aggregation: hourly view returns the data with no manual refresh
    hb = trend_db.bucketed("tds_ppm", since_hours=6, device=dev, view="hourly", conn=conn)
    total_n = sum(b[5] for b in hb)
    check("cagg: real-time hourly view returns data", len(hb) >= 1)
    check("cagg: bucket counts sum to all 4 samples", total_n == 4)


def test_latest(conn):
    dev = "d_latest"
    trend_db.insert_samples([
        (NOW - timedelta(hours=2), dev, "ph", 6.0, "poll"),
        (NOW, dev, "ph", 6.3, "poll"),
    ], conn=conn)
    ts, val = trend_db.latest("ph", device=dev, conn=conn)
    check("latest: returns newest value", abs(val - 6.3) < 1e-9)


def test_trend_features(conn):
    dev = "d_feat"
    # ph rises 5.5 -> 6.0 over the last 5h (h=5 oldest .. h=0 newest)
    rows = [(NOW - timedelta(hours=h), dev, "ph", 6.0 - 0.1 * h, "poll") for h in range(6)]
    trend_db.insert_samples(rows, conn=conn)
    feats = trend_features.trend_features(metrics=("ph",), device=dev,
                                          window_hours=24, slope_hours=6, conn=conn)
    check("features: ph present", bool(feats) and "ph" in feats["metrics"])
    f = feats["metrics"]["ph"]
    check("features: last is newest 6.0", abs(f["last"] - 6.0) < 1e-9)
    check("features: slope rising (>0)", f["slope_per_hr"] is not None and f["slope_per_hr"] > 0)
    check("features: n == 6", f["n"] == 6)
    check("features: format_block renders ph", "ph:" in trend_features.format_block(feats))


def main():
    conn = trend_db.connect(autocommit=True)
    try:
        trend_db.ensure_schema(conn, schema=SCHEMA)          # builds in throwaway schema + sets search_path
        for fn in (
            test_schema_objects,
            test_insert_and_bucket,
            test_dedup,
            test_null_skipped,
            test_record_snapshot_db,
            test_sample_rows_csv,
            test_continuous_aggregate,
            test_latest,
            test_trend_features,
        ):
            fn(conn)
    finally:
        for v in ("trend_daily", "trend_hourly"):
            conn.execute(f'DROP MATERIALIZED VIEW IF EXISTS "{SCHEMA}".{v} CASCADE')
        conn.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".trend_samples CASCADE')
        conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        conn.close()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
