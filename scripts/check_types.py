#!/usr/bin/env python3
import sys, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")
from aci_ble_lab.db import _conn

with _conn() as c:
    rows = c.execute("SELECT DISTINCT sensor_type FROM sensor_readings ORDER BY sensor_type").fetchall()
    print("All sensor_type values seen in DB:")
    for r in rows:
        print(f"  0x{r[0]:02X} ({r[0]})")

    total = c.execute("SELECT COUNT(*), MAX(ts) FROM sensor_readings").fetchone()
    last_dt = datetime.fromtimestamp(total[1]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nTotal rows: {total[0]}  Last reading: {last_dt}")

    cutoff = time.time() - 600
    recent = c.execute(
        "SELECT DISTINCT port, sensor_type FROM sensor_readings WHERE ts > ? ORDER BY port, sensor_type",
        (cutoff,)
    ).fetchall()
    print("\nLast 10 minutes - distinct port/type combos:")
    for r in recent:
        print(f"  port={r[0]}  type=0x{r[1]:02X}")
