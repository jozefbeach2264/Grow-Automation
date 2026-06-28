"""TimescaleDB trend store for grow-automation.

The queryable, time-series home for reservoir trend data so the AI can analyze it
(hourly/daily rollups, rate-of-change, time-in-band) instead of re-reading a flat
JSONL file. This is the *analysis* store; the JSONL store (`ac_infinity_history.py`)
stays the always-on local buffer, so the control loop never depends on Postgres.

Design:
  - One hypertable `trend_samples(ts, device, metric, value, source)` -- long/narrow,
    matching the BLE `sensor_readings` shape. Dedup via a unique index + ON CONFLICT.
  - Continuous aggregates `trend_hourly` / `trend_daily` (real-time, so reads are
    correct without a manual refresh).
  - All writes are best-effort: callers wrap them so a DB hiccup never breaks control.

Connection: `DATABASE_URL`, else a local default (unix socket, trust auth, db `grow`).
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import psycopg
except ImportError:                       # keep the module importable without the driver
    psycopg = None

REPO_DIR = Path(__file__).resolve().parent
SCHEMA_SQL = REPO_DIR / "sql" / "trend_schema.sql"
POLICIES_SQL = REPO_DIR / "sql" / "trend_policies.sql"
_SENTINEL = "\n-- @@\n"

DEFAULT_DSN = "postgresql:///grow?host=/run/postgresql"

# Reservoir metrics pulled from a live poller snapshot (mirrors record_snapshot()).
_SNAPSHOT_METRICS = ("ph", "tds_ppm", "water_temp_f", "ec_us", "ec_ms", "water_level")
# Metrics carried by a CSV-export Sample (ac_infinity_history.Sample).
_SAMPLE_METRICS = ("ph", "tds_ppm", "water_temp_f", "out_temp_f", "out_humidity", "out_vpd")


def dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


def available() -> bool:
    """True if the driver is importable (not whether the server is reachable)."""
    return psycopg is not None


def connect(autocommit: bool = False):
    if psycopg is None:
        raise RuntimeError("psycopg not installed -- `pacman -S python-psycopg`")
    return psycopg.connect(dsn(), autocommit=autocommit, connect_timeout=3)


def _statements(path: Path) -> list[str]:
    return [s.strip() for s in path.read_text(encoding="utf-8").split(_SENTINEL) if s.strip()]


# --- schema --------------------------------------------------------------------

def ensure_schema(conn=None, *, schema: str | None = None) -> None:
    """Create the hypertable + continuous aggregates (idempotent). cagg DDL can't
    run in a transaction, so this forces autocommit. `schema` builds everything in a
    throwaway namespace (used by the tests); production uses the default search_path."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.autocommit = True
        if schema:
            # keep public in the path -- timescaledb's functions (create_hypertable,
            # time_bucket, first/last, ...) live there.
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.execute(f'SET search_path TO "{schema}", public')
        for stmt in _statements(SCHEMA_SQL):
            conn.execute(stmt)
    finally:
        if own:
            conn.close()


def ensure_policies(conn=None) -> None:
    """Add the cagg refresh policies (production). Idempotent."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.autocommit = True
        for stmt in _statements(POLICIES_SQL):
            conn.execute(stmt)
    finally:
        if own:
            conn.close()


def refresh(view: str = "trend_hourly", conn=None) -> None:
    """Force-materialize a continuous aggregate over all time (after a backfill)."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.autocommit = True            # CALL refresh_continuous_aggregate can't be in a txn
        conn.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
    finally:
        if own:
            conn.close()


# --- row builders --------------------------------------------------------------

def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def snapshot_rows(snapshot: dict, source: str = "poll"):
    """(ts, device, metric, value, source) rows from a live poller snapshot."""
    from ac_infinity_history import _parse_snapshot_ts          # reuse the ts parser
    ts = _parse_snapshot_ts(snapshot.get("timestamp"))
    rows = []
    for dev in snapshot.get("devices", []):
        sensors = dev.get("sensors", {})
        device = dev.get("name", "?")
        for metric in _SNAPSHOT_METRICS:
            v = _num(sensors.get(metric))
            if v is not None:
                rows.append((ts, device, metric, v, source))
        leak = sensors.get("water_leak")
        if leak is not None:
            rows.append((ts, device, "water_leak", 1.0 if leak else 0.0, source))
    return rows


def sample_rows(device: str, samples, source: str = "csv"):
    """(ts, device, metric, value, source) rows from ac_infinity_history.Sample objects."""
    rows = []
    for s in samples:
        for metric in _SAMPLE_METRICS:
            v = _num(getattr(s, metric, None))
            if v is not None:
                rows.append((s.ts, device, metric, v, source))
        if s.water_leak is not None:
            rows.append((s.ts, device, "water_leak", 1.0 if s.water_leak else 0.0, source))
    return rows


# --- writes (best-effort) ------------------------------------------------------

def insert_samples(rows, conn=None) -> int:
    """Insert (ts, device, metric, value, source) rows, deduped by the unique index.
    Skips null values. Returns rows submitted."""
    rows = [r for r in rows if r[3] is not None]
    if not rows:
        return 0
    own = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO trend_samples (ts, device, metric, value, source) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (device, metric, ts) DO NOTHING",
                rows,
            )
        if own:
            conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def record_snapshot_db(snapshot: dict, conn=None) -> int:
    return insert_samples(snapshot_rows(snapshot), conn=conn)


def ingest_samples_db(device: str, samples, source: str = "csv", conn=None) -> int:
    return insert_samples(sample_rows(device, samples, source), conn=conn)


# --- reads ---------------------------------------------------------------------

def bucketed(metric: str, bucket: str = "1 hour", since_hours: float = 24,
             device: str | None = None, view: str = "raw", conn=None):
    """Time-bucketed (bucket, avg, min, max, last, n) rows for a metric over the last
    `since_hours`. view='hourly'|'daily' reads the continuous aggregate; 'raw' runs an
    on-the-fly time_bucket() for an arbitrary bucket width."""
    own = conn is None
    conn = conn or connect()
    try:
        params: list = []
        if view in ("hourly", "daily"):
            tbl = "trend_hourly" if view == "hourly" else "trend_daily"
            sql = (f"SELECT bucket, avg, min, max, last, n FROM {tbl} "
                   "WHERE metric = %s AND bucket >= now() - (%s * interval '1 hour')")
            params = [metric, since_hours]
        else:
            sql = ("SELECT time_bucket(%s, ts) AS bucket, avg(value), min(value), "
                   "max(value), last(value, ts), count(*) "
                   "FROM trend_samples "
                   "WHERE metric = %s AND ts >= now() - (%s * interval '1 hour')")
            params = [bucket, metric, since_hours]
        if device:
            sql += " AND device = %s"
            params.append(device)
        sql += " GROUP BY bucket" if view == "raw" else ""
        sql += " ORDER BY bucket"
        return conn.execute(sql, params).fetchall()
    finally:
        if own:
            conn.close()


def latest(metric: str, device: str | None = None, conn=None):
    """(ts, value) of the most recent sample for a metric, or None."""
    own = conn is None
    conn = conn or connect()
    try:
        sql = "SELECT ts, value FROM trend_samples WHERE metric = %s"
        params: list = [metric]
        if device:
            sql += " AND device = %s"
            params.append(device)
        sql += " ORDER BY ts DESC LIMIT 1"
        return conn.execute(sql, params).fetchone()
    finally:
        if own:
            conn.close()


def stats(conn=None) -> dict:
    """Quick counts for the CLI / health checks."""
    own = conn is None
    conn = conn or connect()
    try:
        total = conn.execute("SELECT count(*) FROM trend_samples").fetchone()[0]
        span = conn.execute("SELECT min(ts), max(ts) FROM trend_samples").fetchone()
        by_metric = conn.execute(
            "SELECT metric, count(*) FROM trend_samples GROUP BY metric ORDER BY 2 DESC"
        ).fetchall()
        return {"total": total, "span": span, "by_metric": by_metric}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "ensure":
        ensure_schema()
        print("schema ensured")
    elif cmd == "policies":
        ensure_policies()
        print("policies ensured")
    elif cmd == "stats":
        s = stats()
        print(f"total samples : {s['total']}")
        print(f"span          : {s['span'][0]} -> {s['span'][1]}")
        for m, n in s["by_metric"]:
            print(f"  {m:14}: {n}")
    else:
        print(f"usage: python3 trend_db.py [ensure|policies|stats]")
