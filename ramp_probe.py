#!/usr/bin/env python3
"""Ramp probe: set port to speed 10, poll every 2s to measure ramp curve."""
import os, time, sys
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

from ac_infinity_client import get_or_refresh_token, fetch_all_devices, set_port_speed

EMAIL    = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD = os.getenv("AC_INFINITY_PASSWORD", "")

TARGET_DEVICE  = "4 x 4"
TARGET_PORT    = 1
POLL_SECS      = 2
RAMP_UP_SECS   = 30
RAMP_DOWN_SECS = 25


def get_speak(token):
    for r in fetch_all_devices(token):
        if r.get("devName") == TARGET_DEVICE:
            for p in (r.get("deviceInfo") or {}).get("ports") or []:
                if p.get("port") == TARGET_PORT:
                    return p.get("speak", "?")
    return "?"


def main():
    print("Authenticating...")
    token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))

    dev_id = dev_type = None
    for r in fetch_all_devices(token):
        if r.get("devName") == TARGET_DEVICE:
            dev_id   = r["devId"]
            dev_type = r["devType"]
            break
    if not dev_id:
        print(f"ERROR: '{TARGET_DEVICE}' not found"); sys.exit(1)

    print(f"Device: {TARGET_DEVICE}  dev_id={dev_id}  dev_type={dev_type}")
    baseline = get_speak(token)
    print(f"\n  baseline  speak={baseline}\n")

    # --- Ramp UP ---
    print(f">>> SET port {TARGET_PORT} to speed 10")
    set_port_speed(token, dev_id, TARGET_PORT, 10, dev_type)
    t0 = time.time()
    print("\n  Ramp UP (to 10):")
    for _ in range(RAMP_UP_SECS // POLL_SECS):
        time.sleep(POLL_SECS)
        speak = get_speak(token)
        print(f"    t={time.time()-t0:>5.1f}s  speak={speak}")
        if speak == 10:
            print("    >>> Reached 10 -- stopping early")
            break

    # --- Ramp DOWN ---
    print(f"\n>>> SET port {TARGET_PORT} to speed 0")
    set_port_speed(token, dev_id, TARGET_PORT, 0, dev_type)
    t0 = time.time()
    print("\n  Ramp DOWN (to 0):")
    for _ in range(RAMP_DOWN_SECS // POLL_SECS):
        time.sleep(POLL_SECS)
        speak = get_speak(token)
        print(f"    t={time.time()-t0:>5.1f}s  speak={speak}")
        if speak == 0:
            print("    >>> Reached 0 -- stopping early")
            break

    print("\nDone.")


if __name__ == "__main__":
    main()
