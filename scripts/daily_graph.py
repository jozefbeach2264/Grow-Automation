#!/usr/bin/env python3
"""
daily_graph.py — Generate a graph from logged controller data.

Reads sensor_readings (recent, all ports) and falls back to legacy readings
(historical, ports 4/6/7) for the pre-sensor_readings gap.

Sensor classification:
  type 0x21             → CO2 (ppm = stored_value × 100)
  type 0x41             → Light (raw unit from AC Infinity app)
  value < 5 (other)     → VPD (kPa)
  avg > 55 and max > 65 → Temperature (°F)
  otherwise             → Humidity (%RH)

Usage:
  python scripts/daily_graph.py                # today
  python scripts/daily_graph.py --date 2026-06-02
  python scripts/daily_graph.py --all
  python scripts/daily_graph.py --out my.png
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from aci_ble_lab.db import init_schema, query_port_readings, _conn

PORT_COLORS = ["#e05c2e", "#4da6e8", "#7ec87e", "#cc88cc",
               "#f0a030", "#60c0c0", "#c060c0", "#80a0e0"]


def last_n_hours_bounds(n: float) -> tuple[float, float, str]:
    end = datetime.now().timestamp()
    return end - n * 3600, end, f"last {n:.0f}h"


def all_bounds() -> tuple[float, float, str]:
    with _conn() as c:
        r = c.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
        sr = c.execute("SELECT MAX(ts) FROM sensor_readings").fetchone()
    candidates = [x for x in [r[1] if r else None, sr[0] if sr else None] if x]
    t_end = max(candidates) if candidates else None
    t_start = r[0] if r and r[0] else None
    if t_start is None:
        raise ValueError("No readings in DB yet.")
    return t_start, t_end or t_start, "all data"


def day_bounds(date_str: str) -> tuple[float, float, str]:
    if date_str.lower() in ("today", ""):
        d = datetime.now().date()
    else:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime(d.year, d.month, d.day, tzinfo=local_tz)
    end   = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), str(d)


def fetch_all_sensors(start_ts: float, end_ts: float) -> dict:
    """
    Returns {port: {"ts": [...], "values": [...], "types": [...]}}
    combining legacy readings (historical) and sensor_readings (recent).
    """
    port_data: dict[int, dict] = {}

    def append(port, ts, value, stype):
        if port not in port_data:
            port_data[port] = {"ts": [], "values": [], "types": []}
        port_data[port]["ts"].append(ts)
        port_data[port]["values"].append(value)
        port_data[port]["types"].append(stype)

    with _conn() as c:
        sr_start_row = c.execute(
            "SELECT MIN(ts) FROM sensor_readings WHERE ts BETWEEN ? AND ?",
            (start_ts, end_ts)
        ).fetchone()
        sr_start = sr_start_row[0] if sr_start_row and sr_start_row[0] else None
        legacy_end = sr_start if sr_start else end_ts

        for r in c.execute(
            "SELECT ts, p4_v1, p4_v2, p6_v1, p6_v2, p7_v1, p7_v2 "
            "FROM readings WHERE ts BETWEEN ? AND ? AND p4_v1 IS NOT NULL ORDER BY ts",
            (start_ts, legacy_end)
        ).fetchall():
            ts = r[0]
            if r[1]:
                v = ((r[1] << 8) | r[2]) / 100.0
                if 32 < v < 120:
                    append(4, ts, v, 0x6F)
            if r[3]:
                v = ((r[3] << 8) | r[4]) / 100.0
                if 0 < v <= 100:
                    append(6, ts, v, 0x6F)
            if r[6] is not None:
                v = (((r[5] or 0) << 8) | r[6]) / 100.0
                if 0 < v < 5.0:
                    append(7, ts, v, 0x67)

        for r in c.execute(
            "SELECT ts, port, sensor_type, value FROM sensor_readings "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts)
        ).fetchall():
            append(r[1], r[0], r[3], r[2])

    return port_data


def classify_port(port_entry: dict) -> str:
    """Determine measurement role from sensor type and value range."""
    types  = port_entry["types"]
    values = port_entry["values"]
    if not values:
        return "other"
    dominant_type = max(set(types), key=types.count)
    if dominant_type == 0x21:
        return "co2"
    if dominant_type == 0x41:
        return "light"
    if dominant_type == 0x61:
        avg = sum(values) / len(values)
        if avg > 30:
            return "water_temp"
        elif avg > 2:
            return "ph"
        else:
            return "ec"
    mx  = max(values)
    avg = sum(values) / len(values)
    if mx < 5.0:
        return "vpd"
    if avg > 55 and mx > 65:
        return "temp"
    return "humidity"


def elapsed_hours(ts: float, t0: float) -> float:
    return (ts - t0) / 3600.0


def draw_panel(ax, ports, port_data, t0, title, ylabel, fmt, ylim_fn=None, scale=1.0):
    plotted = False
    for i, port in enumerate(ports):
        d = port_data[port]
        if not d["values"]:
            continue
        xs = [elapsed_hours(ts, t0) for ts in d["ts"]]
        ys = [v * scale for v in d["values"]]
        ax.plot(xs, ys, color=PORT_COLORS[i % len(PORT_COLORS)],
                linewidth=1.2, label=f"Port {port}")
        plotted = True
    ax.set_title(title)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(fmt)
    if plotted and len(ports) > 1:
        ax.legend(loc="upper right", fontsize=8)
    if plotted and ylim_fn:
        all_vals = [v * scale for p in ports for v in port_data[p]["values"]]
        if all_vals:
            ylim_fn(ax, min(all_vals), max(all_vals))


def plot(date_str: str, port_filter: list[int] | None, out_path: str | None, all_data: bool = False, hours: float = 0):
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    if all_data:
        start_ts, end_ts, label = all_bounds()
    elif hours:
        start_ts, end_ts, label = last_n_hours_bounds(hours)
    else:
        start_ts, end_ts, label = day_bounds(date_str)

    t0        = start_ts
    port_data = fetch_all_sensors(start_ts, end_ts)
    port_rows = query_port_readings(start_ts, end_ts)

    if not port_data:
        print(f"No sensor readings for {label}.")
        return

    buckets: dict[str, list[int]] = {
        "temp": [], "humidity": [], "vpd": [], "co2": [], "light": [],
        "water_temp": [], "ph": [], "ec": [],
    }
    for port in sorted(port_data):
        role = classify_port(port_data[port])
        if role in buckets:
            buckets[role].append(port)

    fan_ports: dict[int, dict] = {}
    for r in port_rows:
        if port_filter and r["port"] not in port_filter:
            continue
        p = r["port"]
        if p not in fan_ports:
            fan_ports[p] = {"ts": [], "speed": []}
        fan_ports[p]["ts"].append(elapsed_hours(r["ts"], t0))
        spd = r["level_on"] if (r["work_type"] or 0) == 2 else 0
        fan_ports[p]["speed"].append(spd or 0)
    active_fan = {p: d for p, d in fan_ports.items() if any(s > 0 for s in d["speed"])}

    panels = [k for k in ("temp", "humidity", "vpd", "co2", "light", "water_temp", "ph", "ec") if buckets[k]]
    n_panels = len(panels) + (1 if active_fan else 0)
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 3.5 * n_panels), sharex=True)
    if n_panels == 1:
        axes = [axes]

    total_rows = sum(len(d["ts"]) for d in port_data.values())
    hours_span = (end_ts - start_ts) / 3600
    from_str   = datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M")
    fig.suptitle(
        f"AC Infinity ACI_V3.5_CTRLER — {label}   ({total_rows:,} readings, {hours_span:.1f}h)",
        fontsize=13, fontweight="bold"
    )

    fmt = mticker.FuncFormatter(lambda x, _: f"{x:.1f}h")

    def pad_ylim(mn, mx, pct=0.3, lo=None, hi=None):
        pad = max((mx - mn) * pct, 0.05)
        return (max(lo, mn - pad) if lo is not None else mn - pad,
                min(hi, mx + pad) if hi is not None else mx + pad)

    panel_cfg = {
        "temp":       ("Air Temperature",  "Temperature (°F)",  1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=40))),
        "humidity":   ("Humidity",         "Humidity (%RH)",    1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0, hi=100))),
        "vpd":        ("VPD",              "VPD (kPa)",         1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0))),
        "co2":        ("CO₂",             "CO₂ (ppm)",         100.0, lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0))),
        "light":      ("Light",            "Light (×100)",      100.0, lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0))),
        "water_temp": ("Water Temperature","Water Temp (°F)",   1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=32, hi=100))),
        "ph":         ("pH",               "pH",                1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0, hi=14))),
        "ec":         ("EC / TDS",         "EC (mS/cm)",        1.0,   lambda ax, mn, mx: ax.set_ylim(*pad_ylim(mn, mx, lo=0))),
    }

    for ax_idx, role in enumerate(panels):
        title, ylabel, scale, ylim_fn = panel_cfg[role]
        draw_panel(axes[ax_idx], buckets[role], port_data, t0, title, ylabel, fmt,
                   ylim_fn=ylim_fn, scale=scale)

    if active_fan:
        ax = axes[len(panels)]
        for i, (port_id, data) in enumerate(sorted(active_fan.items())):
            ax.step(data["ts"], data["speed"], where="post",
                    linewidth=1.5, color=PORT_COLORS[i % len(PORT_COLORS)],
                    label=f"Port {port_id}")
        ax.set_ylabel("Fan Speed (0–10)", fontsize=10)
        ax.set_ylim(-0.5, 11)
        ax.legend(loc="upper right", fontsize=8)
        ax.set_title("Fan Speed")
        ax.grid(True, alpha=0.25)
        ax.xaxis.set_major_formatter(fmt)

    axes[-1].set_xlabel(f"Hours since {from_str}", fontsize=10)
    plt.tight_layout()

    if out_path:
        save_path = Path(out_path)
    elif all_data:
        save_path = Path(__file__).parent.parent / "graph_all.png"
    elif hours:
        save_path = Path(__file__).parent.parent / f"graph_last{hours:.0f}h.png"
    else:
        save_path = Path(__file__).parent.parent / f"graph_{label}.png"

    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Graph saved: {save_path}")
    import os
    os.startfile(save_path)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Generate graph from logged controller data")
    p.add_argument("--date",  default="today", help="Date to graph (YYYY-MM-DD or 'today')")
    p.add_argument("--all",   action="store_true", help="Graph all data in the DB")
    p.add_argument("--ports", type=int, nargs="*", default=None,
                   help="Limit fan speed panel to these ports (default: active ports only)")
    p.add_argument("--out",   default=None, help="Output PNG path")
    p.add_argument("--hours", type=float, default=0, help="Graph last N hours (e.g. --hours 1)")
    args = p.parse_args()

    init_schema()
    plot(args.date, args.ports, args.out, all_data=args.all, hours=args.hours)
