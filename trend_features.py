"""Compact multi-window trend analysis over the TimescaleDB trend store.

This is what lets the AI actually *analyze* the trend data: instead of the single
previous-cycle `_trend()` delta, it gets per-metric level + range + rate-of-change
over hours, computed cheaply from the hypertable / continuous aggregates.

Best-effort: every entry point returns None (or "") if Postgres is unavailable or
empty, so callers simply omit the block -- the advisor's contract is unchanged when
the DB isn't there.
"""
from __future__ import annotations

import trend_db

DEFAULT_METRICS = ("ph", "tds_ppm", "ec_us", "water_temp_f")


def _slope_per_hr(points) -> float | None:
    """Least-squares slope (value per hour) over (bucket_datetime, value) points."""
    pts = [(b, float(v)) for b, v in points if v is not None]
    if len(pts) < 2:
        return None
    t0 = pts[0][0]
    xs = [(b - t0).total_seconds() / 3600.0 for b, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    return (n * sxy - sx * sy) / denom


def trend_features(metrics=DEFAULT_METRICS, device: str | None = None,
                   window_hours: float = 24, slope_hours: float = 6, conn=None) -> dict | None:
    """Per-metric {last, avg, min, max, slope_per_hr, n} over the last `window_hours`,
    with the slope fit over the last `slope_hours`. None if unavailable/empty."""
    if not trend_db.available():
        return None
    own = conn is None
    try:
        conn = conn or trend_db.connect()
    except Exception:
        return None
    try:
        out: dict = {}
        for m in metrics:
            params = [m, window_hours] + ([device] if device else [])
            row = conn.execute(
                "SELECT avg(value), min(value), max(value), count(*) FROM trend_samples "
                "WHERE metric = %s AND ts >= now() - (%s * interval '1 hour')"
                + (" AND device = %s" if device else ""),
                params,
            ).fetchone()
            avg, mn, mx, n = row
            if not n:
                continue
            last = trend_db.latest(m, device=device, conn=conn)
            hb = trend_db.bucketed(m, since_hours=slope_hours, device=device, view="hourly", conn=conn)
            slope = _slope_per_hr([(b[0], b[1]) for b in hb])
            out[m] = {
                "last": round(float(last[1]), 3) if last else None,
                "avg": round(float(avg), 3),
                "min": round(float(mn), 3),
                "max": round(float(mx), 3),
                "slope_per_hr": round(slope, 4) if slope is not None else None,
                "n": int(n),
            }
        if not out:
            return None
        return {"window_hours": window_hours, "slope_hours": slope_hours, "metrics": out}
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def format_block(features: dict | None) -> str:
    """One compact line per metric for the AI system prompt / HUD. "" if no features."""
    if not features or not features.get("metrics"):
        return ""
    w = features["window_hours"]
    sh = features["slope_hours"]
    lines = [f"TREND ANALYSIS ({w:g}h window, TimescaleDB):"]
    for m, f in features["metrics"].items():
        slope = f.get("slope_per_hr")
        if slope is None:
            rate = ""
        else:
            dirn = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
            rate = f", {slope:+g}/hr over {sh:g}h ({dirn})"
        lines.append(
            f"  {m}: now {f['last']}, {w:g}h avg {f['avg']} (min {f['min']}, max {f['max']}){rate}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    feats = trend_features()
    print(json.dumps(feats, indent=2, default=str))
    print()
    print(format_block(feats))
