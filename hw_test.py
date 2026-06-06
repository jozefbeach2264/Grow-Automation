#!/usr/bin/env python3
"""
hw_test.py -- Hardware verification test.

Activates all device outputs to confirm wiring and firmware response before
the reservoir is filled.  Both port types run simultaneously so the whole
test completes in 60 seconds:

  Speed/variable ports  -- ramp to max (10), hold 60s, return to 0.
  Outlet (on/off) ports -- cycle ON/OFF x2 over the first 20s, then stay OFF.

Usage:
    python3 hw_test.py          test all devices, all ports
    python3 hw_test.py --dry    print what would happen, make no API calls

WARNING: runs all doser pumps at full speed.  Do not run with pH chemicals
loaded unless you intend to flush the lines.  Peristaltic pumps tolerate
brief dry runs without damage.
"""

import sys
import os
import time
import re
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH    = Path(__file__).parent / ".env"
LABELS_PATH = Path(__file__).parent / "labels.env"
load_dotenv(ENV_PATH)
load_dotenv(LABELS_PATH)

from ac_infinity_client import (
    get_or_refresh_token,
    fetch_all_devices,
    parse_device,
    set_port_speed,
    set_outlet,
)

DRY_RUN        = "--dry" in sys.argv
SPEED_HOLD_SEC = 60    # total hold time for speed ports at max
OUTLET_CYCLES  = 2     # on/off repetitions for outlet ports
OUTLET_HALF_S  = 5     # seconds per half-cycle (on phase and off phase)
# Total outlet time = OUTLET_CYCLES * OUTLET_HALF_S * 2 = 20s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name_slug(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _port_label(dev_name: str, port: int, fallback: str) -> str:
    label = os.getenv(f"PORT_{_name_slug(dev_name)}_{port}", "").strip()
    return label if label else fallback


def _wait(seconds: float):
    if DRY_RUN or seconds <= 0:
        return
    time.sleep(seconds)


def _set_speed(token, dev, port, speed):
    ml_min = speed * 21
    rate   = f"{ml_min} mL/min" if ml_min > 0 else "stopped"
    label  = _port_label(dev["name"], port, f"Port {port}")
    print(f"  set_speed   {dev['name']:<22}  {label:<18}  port {port}  "
          f"speed={speed}/10  ({rate})", end="  ", flush=True)
    if not DRY_RUN:
        try:
            set_port_speed(token, dev["dev_id"], port, speed, dev["type"])
            print("OK")
        except Exception as e:
            print(f"SKIP ({e})")
            return False
    else:
        print()
    return True


def _set_outlet_state(token, dev, port, on):
    state = "ON " if on else "OFF"
    label = _port_label(dev["name"], port, f"Port {port}")
    print(f"  set_outlet  {dev['name']:<22}  {label:<18}  port {port}  -> {state}",
          end="  ", flush=True)
    if not DRY_RUN:
        try:
            set_outlet(token, dev["dev_id"], port, on)
            print("OK")
        except Exception as e:
            print(f"SKIP ({e})")
            return False
    else:
        print()
    return True


def _zero_all(token, speed_ports, outlet_ports):
    """Return everything to off/0 -- called on normal exit and on Ctrl-C."""
    for dev, port in speed_ports:
        _set_speed(token, dev, port, 0)
    for dev, port in outlet_ports:
        _set_outlet_state(token, dev, port, False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    email    = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")

    if not email or not password:
        print("ERROR: AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD must be set in .env")
        sys.exit(1)

    print("Authenticating with AC Infinity cloud (fresh login)...")
    os.environ["AC_INFINITY_TOKEN"] = ""   # force re-auth, don't use cached token
    token = get_or_refresh_token(email, password, str(ENV_PATH))

    print("Fetching device list...")
    devices = [parse_device(r) for r in fetch_all_devices(token)]

    # Separate online ports by type
    speed_ports  = []
    outlet_ports = []
    for dev in devices:
        for p in dev["ports"]:
            if not p["online"]:
                continue
            if p.get("is_outlet"):
                outlet_ports.append((dev, p["port"]))
            else:
                speed_ports.append((dev, p["port"]))

    # Show test plan
    print(f"\n{'='*60}")
    print(f"  HW TEST PLAN")
    print(f"{'='*60}")
    print(f"\n  Speed ports -- max (10) for {SPEED_HOLD_SEC}s then back to 0:")
    for dev, port in speed_ports:
        label = _port_label(dev["name"], port, f"Port {port}")
        print(f"    {dev['name']:<24}  port {port}  {label}")

    print(f"\n  Outlet ports -- ON/OFF x{OUTLET_CYCLES} over "
          f"{OUTLET_CYCLES * OUTLET_HALF_S * 2}s:")
    for dev, port in outlet_ports:
        label = _port_label(dev["name"], port, f"Port {port}")
        print(f"    {dev['name']:<24}  port {port}  {label}")

    print(f"\n  Total test time: ~{SPEED_HOLD_SEC}s")

    if DRY_RUN:
        print("\n  [DRY RUN] No API calls will be made.\n")
    else:
        print()
        print("  WARNING: all doser pumps will run at full speed.")
        print("  Do not run with pH chemicals loaded unless flushing lines.")
        print()
        ans = input("  Type YES to start the test: ").strip()
        if ans.upper() != "YES":
            print("  Aborted.")
            sys.exit(0)

    print(f"\n{'='*60}")
    print("  RUNNING TEST")
    print(f"{'='*60}\n")

    start = time.time()

    try:
        # --- Step 1: set all speed ports to max ---
        print(f"[{0:>4.0f}s]  Ramping speed ports to max...")
        for dev, port in speed_ports:
            _set_speed(token, dev, port, 10)

        # --- Step 2: cycle outlets while speed ports hold ---
        for cycle in range(1, OUTLET_CYCLES + 1):
            elapsed = time.time() - start
            print(f"\n[{elapsed:>4.0f}s]  Outlet cycle {cycle}/{OUTLET_CYCLES}: ON")
            for dev, port in outlet_ports:
                _set_outlet_state(token, dev, port, True)

            _wait(OUTLET_HALF_S)

            elapsed = time.time() - start
            print(f"[{elapsed:>4.0f}s]  Outlet cycle {cycle}/{OUTLET_CYCLES}: OFF")
            for dev, port in outlet_ports:
                _set_outlet_state(token, dev, port, False)

            if cycle < OUTLET_CYCLES:
                _wait(OUTLET_HALF_S)

        # --- Step 3: hold speed ports for remainder of 60s ---
        elapsed  = time.time() - start
        remaining = SPEED_HOLD_SEC - elapsed
        if remaining > 0:
            print(f"\n[{elapsed:>4.0f}s]  Speed ports holding at max "
                  f"({remaining:.0f}s remaining)...")
            _wait(remaining)

        # --- Step 4: return speed ports to 0 ---
        elapsed = time.time() - start
        print(f"\n[{elapsed:>4.0f}s]  Returning speed ports to 0...")
        for dev, port in speed_ports:
            _set_speed(token, dev, port, 0)

        elapsed = time.time() - start
        print(f"\n[{elapsed:>4.0f}s]  Test complete.  All outputs at 0.\n")

    except KeyboardInterrupt:
        elapsed = time.time() - start
        print(f"\n[{elapsed:>4.0f}s]  Interrupted -- returning all outputs to safe state...")
        _zero_all(token, speed_ports, outlet_ports)
        print("  Cleanup complete.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
