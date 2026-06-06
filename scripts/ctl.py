#!/usr/bin/env python3
"""
ctl.py — Send control commands to the running logger via the DB command queue.

The logger pops one pending command per tick (~1s) and issues set_level over
the existing BLE connection — no reconnect needed.

Usage:
  python scripts/ctl.py --port 1 --speed 5       # turn port 1 ON at speed 5
  python scripts/ctl.py --port 1 --off            # turn port 1 OFF
  python scripts/ctl.py --port 1 --speed 0        # also turns off

work_type: 2 = ON (uses speed), 1 = OFF
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from aci_ble_lab.db import _conn, enqueue_command


def send(port: int, work_type: int, speed: int):
    cmd_id = enqueue_command(port, work_type, speed, source="ctl")
    label = "ON" if work_type == 2 else "OFF"
    print(f"Queued: port={port}  {label}  speed={speed}  (id={cmd_id})")

    deadline = time.time() + 5.0
    while time.time() < deadline:
        with _conn() as c:
            row = c.execute(
                "SELECT status FROM command_queue WHERE id=?", (cmd_id,)
            ).fetchone()
        if row and row["status"] != "pending":
            print(f"Command {row['status']}.")
            return
        time.sleep(0.1)

    print("Warning: logger did not pick up command within 5s (is it running?)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Send control command to running logger")
    p.add_argument("--port",  type=int, required=True, help="Port 1-8")
    p.add_argument("--speed", type=int, default=5,     help="Speed 0-10 (default 5)")
    p.add_argument("--off",   action="store_true",     help="Turn off")
    args = p.parse_args()

    work_type = 1 if (args.off or args.speed == 0) else 2
    speed     = 0 if work_type == 1 else args.speed
    send(args.port, work_type, speed)
