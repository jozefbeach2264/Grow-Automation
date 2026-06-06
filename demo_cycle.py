#!/usr/bin/env python3
"""
Deterministic demo runner.

Steps:
  1. Turn ON every online Aux outlet
  2. Sequentially cycle each variable-speed port on every CTR89Q:
       speed 0 -> 10 (ramp up, live polled)
       hold 30 seconds at 10
       speed 10 -> 0 (ramp down, live polled)
  3. Quiet pause between ports

No AI in the loop -- pure hardware control. Each command goes directly through
ac_infinity_client. Live readback every 2s so the viewer sees the ramp climb.

Safe: Ctrl-C triggers emergency stop on every variable-speed port.
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from ac_infinity_client import (
    ACInfinityAuthError,
    fetch_all_devices,
    get_or_refresh_token,
    parse_device,
    set_outlet,
    set_port_speed,
)

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

EMAIL    = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD = os.getenv("AC_INFINITY_PASSWORD", "")

TARGET_SPEED      = 10
HOLD_SECONDS      = 30
POLL_SECONDS      = 2
RAMP_TIMEOUT      = 25
HOLD_POLL_SECONDS = 6
INTER_PORT_PAUSE  = 4
COUNTDOWN_SECONDS = 5

VARIABLE_SPEED_DEV_TYPES = {20}   # CTR89Q


def get_port_speed(token: str, dev_id: str, port: int) -> int | None:
    try:
        devs = fetch_all_devices(token)
    except Exception:
        return None
    for raw in devs:
        if raw.get("devId") != dev_id:
            continue
        for p in (raw.get("deviceInfo") or {}).get("ports") or []:
            if p.get("port") == port:
                return p.get("speak", 0)
    return None


def watch_to(token: str, dev_id: str, port: int, target: int,
             timeout_sec: int, label: str) -> int:
    deadline = time.time() + timeout_sec
    last_shown = -1
    last_seen  = -1
    while time.time() < deadline:
        actual = get_port_speed(token, dev_id, port)
        if actual is None:
            time.sleep(POLL_SECONDS)
            continue
        last_seen = actual
        if actual != last_shown:
            print(f"    {label} speed: {actual:>2}/10   (target {target})")
            last_shown = actual
        if actual == target:
            return actual
        time.sleep(POLL_SECONDS)
    return last_seen


def hold_with_polls(token: str, dev_id: str, port: int, hold_sec: int, label: str):
    print(f"    {label} holding {hold_sec}s at speed {TARGET_SPEED}...")
    start = time.time()
    while time.time() - start < hold_sec:
        time.sleep(HOLD_POLL_SECONDS)
        s = get_port_speed(token, dev_id, port)
        elapsed = int(time.time() - start)
        if s is not None:
            print(f"    {label} +{elapsed:02d}s   speed: {s:>2}/10")


def cycle_port(token: str, dev: dict, port: dict):
    dev_id   = dev["dev_id"]
    dev_type = dev["type"]
    port_num = port["port"]
    label    = f"[{dev['name'][:14]:<14} p{port_num}]"
    pretty   = f"{dev['name']} port {port_num} ({port['name']})"

    print(f"\n{'-' * 72}")
    print(f"  CYCLE: {pretty}")

    print(f"  {label} -> speed {TARGET_SPEED}")
    try:
        set_port_speed(token, dev_id, port_num, TARGET_SPEED, dev_type)
    except Exception as e:
        print(f"  {label} ramp-up write FAILED: {e}")
        return
    watch_to(token, dev_id, port_num, TARGET_SPEED, RAMP_TIMEOUT, label)
    hold_with_polls(token, dev_id, port_num, HOLD_SECONDS, label)

    print(f"  {label} -> speed 0")
    try:
        set_port_speed(token, dev_id, port_num, 0, dev_type)
    except Exception as e:
        print(f"  {label} ramp-down write FAILED: {e}")
        return
    final = watch_to(token, dev_id, port_num, 0, RAMP_TIMEOUT, label)
    if final != 0:
        print(f"  {label} WARN: did not reach 0, re-sending stop")
        try:
            set_port_speed(token, dev_id, port_num, 0, dev_type)
        except Exception:
            pass
        watch_to(token, dev_id, port_num, 0, RAMP_TIMEOUT, label)

    print(f"  {label} DONE")


def emergency_stop(token: str, devices: list[dict]):
    print("\n[STOP] commanding speed 0 on every variable-speed port...")
    for d in devices:
        if d["type"] not in VARIABLE_SPEED_DEV_TYPES:
            continue
        for p in d["ports"]:
            if p["is_outlet"] or not p["online"]:
                continue
            try:
                set_port_speed(token, d["dev_id"], p["port"], 0, d["type"])
            except Exception:
                pass


def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: set AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    print("Authenticating...")
    token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
    print("Fetching device list...")
    devices = [parse_device(r) for r in fetch_all_devices(token)]

    # ------------------------------------------------------------------
    # Step 1: turn on every online Aux outlet
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("  STEP 1: turn ON every Aux outlet")
    print("=" * 72)
    any_outlets = False
    for d in devices:
        if not d["is_outlet"] or not d["online"]:
            continue
        any_outlets = True
        for p in d["ports"]:
            if not p["online"]:
                continue
            label = f"{d['name']} outlet {p['port']} ({p['name']})"
            if p.get("powered"):
                print(f"  --  {label}: already ON")
                continue
            try:
                set_outlet(token, d["dev_id"], p["port"], True, d["type"])
                print(f"  ON  {label}")
            except Exception as e:
                print(f"  FAIL {label}: {e}")
    if not any_outlets:
        print("  (no outlet-type devices online)")

    # ------------------------------------------------------------------
    # Step 2: ramp every variable-speed port 0 -> 10 -> hold -> 0
    # ------------------------------------------------------------------
    targets = [d for d in devices
               if d["type"] in VARIABLE_SPEED_DEV_TYPES and d["online"]]
    var_ports = [(d, p) for d in targets
                 for p in d["ports"]
                 if p["online"] and not p["is_outlet"]]

    print("\n" + "=" * 72)
    print(f"  STEP 2: cycle {len(var_ports)} variable-speed port(s) "
          f"0 -> {TARGET_SPEED} -> hold {HOLD_SECONDS}s -> 0")
    print("=" * 72)

    if not var_ports:
        print("  (no variable-speed ports online)")
    else:
        print(f"\nStarting in {COUNTDOWN_SECONDS}s -- Ctrl-C to abort.")
        for i in range(COUNTDOWN_SECONDS, 0, -1):
            print(f"  {i:2d}...", end="\r", flush=True)
            time.sleep(1)
        print()
        try:
            for dev, port in var_ports:
                cycle_port(token, dev, port)
                time.sleep(INTER_PORT_PAUSE)
        except KeyboardInterrupt:
            print("\n[INTERRUPT] aborting")
            emergency_stop(token, devices)
            return
        except ACInfinityAuthError as e:
            print(f"\n[AUTH] {e}")
            emergency_stop(token, devices)
            return
        except Exception as e:
            print(f"\n[FATAL] {type(e).__name__}: {e}")
            emergency_stop(token, devices)
            raise

    print("\n" + "=" * 72)
    print("  DEMO COMPLETE")
    print("=" * 72)


if __name__ == "__main__":
    main()
