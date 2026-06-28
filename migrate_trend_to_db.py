#!/usr/bin/env python3
"""Backfill the JSONL trend store into TimescaleDB (idempotent).

The JSONL store (`trend_data/acinfinity_history.jsonl`) already holds the merged CSV
imports + self-logged samples, so backfilling it covers the CSV archive too. Rows are
tagged source='backfill'; re-running is safe (ON CONFLICT (device, metric, ts)).

Run: python3 migrate_trend_to_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ac_infinity_history as H
import trend_db


def main():
    if not trend_db.available():
        print("psycopg not installed"); sys.exit(1)

    trend_db.ensure_schema()
    trend_db.ensure_policies()

    rows_in = H._read_store(H.STORE_PATH)
    if not rows_in:
        print(f"nothing to backfill -- {H.STORE_PATH} is empty"); return

    db_rows = []
    for r in rows_in:
        device = r.get("device", "?")
        db_rows.extend(trend_db.sample_rows(device, [H._row_sample(r)], source="backfill"))

    with trend_db.connect() as conn:
        n = trend_db.insert_samples(db_rows, conn=conn)
        conn.commit()

    # materialize the rollups over the backfilled history (real-time agg covers the
    # rest, but this keeps long-range reads cheap)
    trend_db.refresh("trend_hourly")
    trend_db.refresh("trend_daily")

    s = trend_db.stats()
    print(f"backfilled {n} metric-rows from {len(rows_in)} JSONL samples")
    print(f"PG total      : {s['total']}")
    print(f"PG span       : {s['span'][0]} -> {s['span'][1]}")
    for m, c in s["by_metric"]:
        print(f"  {m:14}: {c}")


if __name__ == "__main__":
    main()
