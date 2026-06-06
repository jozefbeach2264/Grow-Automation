#!/usr/bin/env python3
import sys
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from aci_ble_lab.db import _conn

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
cutoff = time.time() - hours * 3600

with _conn() as c:
    rows = c.execute("""
        SELECT port, sensor_type, COUNT(*) as cnt,
               MIN(value) as mn, AVG(value) as avg, MAX(value) as mx,
               MAX(ts) as last_ts
        FROM sensor_readings
        WHERE ts > ?
        GROUP BY port, sensor_type
        ORDER BY port, sensor_type
    """, (cutoff,)).fetchall()

print(f"Port  Type   Count    Min     Avg     Max     LastSeen")
print("-" * 65)
for r in rows:
    last = datetime.fromtimestamp(r["last_ts"]).strftime("%H:%M:%S")
    print(f"  {r['port']:2d}  0x{r['sensor_type']:02X}  {r['cnt']:5d}  {r['mn']:7.2f}  {r['avg']:7.2f}  {r['mx']:7.2f}  {last}")
