#!/usr/bin/env python3
"""Show when each (port, sensor_type) combo first appeared in the DB."""
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")
from aci_ble_lab.db import _conn

with _conn() as c:
    rows = c.execute("""
        SELECT port, sensor_type,
               MIN(ts) as first_ts, MAX(ts) as last_ts, COUNT(*) as cnt,
               MIN(value) as mn, AVG(value) as avg, MAX(value) as mx
        FROM sensor_readings
        GROUP BY port, sensor_type
        ORDER BY first_ts
    """).fetchall()

print(f"{'Port':>4}  {'Type':>5}  {'First Seen':>19}  {'Last Seen':>19}  {'Count':>6}  {'Min':>7}  {'Avg':>7}  {'Max':>7}")
print("-" * 100)
for r in rows:
    first = datetime.fromtimestamp(r["first_ts"]).strftime("%Y-%m-%d %H:%M:%S")
    last  = datetime.fromtimestamp(r["last_ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  {r['port']:2d}  0x{r['sensor_type']:02X}  {first:>19}  {last:>19}  {r['cnt']:6d}  {r['mn']:7.2f}  {r['avg']:7.2f}  {r['mx']:7.2f}")
