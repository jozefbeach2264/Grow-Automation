#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from aci_ble_lab.db import init_schema, build_unified_snapshot, add_cloud_readings
import time

init_schema()

# Inject a fake cloud reading to verify the merge logic
add_cloud_readings(time.time(), "fake-dev", "Test Device", {99: 42.0})

snap = build_unified_snapshot(ble_max_age=120, cloud_max_age=60)
print(f"Unified snapshot: {len(snap)} sensors")
for sid, e in sorted(snap.items()):
    dev = f" ({e['dev_name']})" if e.get("dev_name") else ""
    print(f"  sensor_id={sid:2d}  value={e['value']:7.2f}  source={e['source']}{dev}")
