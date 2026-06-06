#!/usr/bin/env python3
"""
ctl.py — Send control commands to the running logger via drop-file.

The logger checks for aci_control.json every second and executes it
over the existing BLE connection — no reconnect needed.

Usage:
  python scripts/ctl.py --port 1 --speed 5       # turn port 1 ON at speed 5
  python scripts/ctl.py --port 1 --off            # turn port 1 OFF
  python scripts/ctl.py --port 1 --speed 0        # also turns off

work_type: 2 = ON (uses speed), 1 = OFF
"""

import argparse
import json
import sys
import time
from pathlib import Path

CMD_FILE = Path(__file__).resolve().parent.parent / "aci_control.json"


def send(port: int, work_type: int, speed: int):
    payload = {"port": port, "work_type": work_type, "speed": speed}
    CMD_FILE.write_text(json.dumps(payload))
    print(f"Sent: port={port}  {'ON' if work_type == 2 else 'OFF'}  speed={speed}")
    # Wait briefly for logger to pick it up
    deadline = time.time() + 3.0
    while CMD_FILE.exists() and time.time() < deadline:
        time.sleep(0.1)
    if CMD_FILE.exists():
        CMD_FILE.unlink()
        print("Warning: logger did not pick up command within 3s (is it running?)")
    else:
        print("Command delivered.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Send control command to running logger")
    p.add_argument("--port",  type=int, required=True, help="Port 1-8")
    p.add_argument("--speed", type=int, default=5,     help="Speed 0-10 (default 5)")
    p.add_argument("--off",   action="store_true",     help="Turn off")
    args = p.parse_args()

    work_type = 1 if (args.off or args.speed == 0) else 2
    speed     = 0 if work_type == 1 else args.speed
    send(args.port, work_type, speed)
