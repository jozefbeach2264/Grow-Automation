#!/usr/bin/env python3
"""
AI-driven hardware cycle test.

Walks every variable-speed port across the CTR89Q controllers. For each port:
  1. Prompts the configured Ollama model to set the port to speed 10
  2. Executes whatever JSON action the model returns (with bounds-checking)
  3. Polls device state every 2s so you see the ramp climb live
  4. Holds 30 seconds at speed 10
  5. Prompts the model to set the port back to speed 0
  6. Polls during ramp-down
  7. Pauses, moves to the next port

Bypasses ai_advisor.filter_actions() -- this is a hardware test, not grow
management. The AI is in the decision loop, but the prompt is bounded to
one specific port at a time so there's no ambiguity about what should happen.

WARNING: cycles include the 4 dosers on RDWC Control. If their intake lines
are sitting in nutrient or pH solution bottles, fluid WILL be pumped into
the reservoir at full speed for ~50 seconds per port.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from ac_infinity_client import (
    ACInfinityAuthError,
    fetch_all_devices,
    get_or_refresh_token,
    parse_device,
    set_port_speed,
)

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

EMAIL        = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD     = os.getenv("AC_INFINITY_PASSWORD", "")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")

TARGET_SPEED       = 10
HOLD_SECONDS       = 30
POLL_SECONDS       = 2
HOLD_POLL_SECONDS  = 6
RAMP_TIMEOUT       = 25
INTER_PORT_PAUSE   = 5
COUNTDOWN_SECONDS  = 10

# CTR89Q only (variable-speed). 21 = ADA4 outlets, skipped.
VARIABLE_SPEED_DEV_TYPES = {20}


# ---------------------------------------------------------------------------
# AI plumbing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """Pull the first valid JSON object out of `text`, stripping <think> blocks."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    depth, start = 0, -1
    for i, c in enumerate(cleaned):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def ai_action(prompt: str) -> tuple[dict | None, float, str]:
    """Ask the model for one JSON action. Returns (action, latency_sec, raw_text)."""
    t0 = time.time()
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/generate", json={
            "model":  OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }, timeout=90)
        r.raise_for_status()
        raw = r.json().get("response", "")
    except Exception as e:
        return None, time.time() - t0, f"<request failed: {e}>"

    return _extract_json(raw), time.time() - t0, raw


# ---------------------------------------------------------------------------
# Device polling
# ---------------------------------------------------------------------------

def get_port_state(token: str, dev_id: str, port: int) -> dict | None:
    """Return current port speed/target/online from a fresh device fetch."""
    try:
        devs = fetch_all_devices(token)
    except Exception as e:
        print(f"  [POLL] fetch failed: {e}")
        return None
    for raw in devs:
        if raw.get("devId") != dev_id:
            continue
        for p in (raw.get("deviceInfo") or {}).get("ports") or []:
            if p.get("port") == port:
                return {
                    "speed_actual": p.get("speak", 0),
                    "speed_target": p.get("onSpead", 0),
                    "online":       p.get("online") == 1,
                    "mode":         p.get("curMode"),
                }
    return None


def watch_to_target(token: str, dev_id: str, port: int, target: int,
                    timeout_sec: int, label: str) -> int:
    """Poll until speed_actual hits target or timeout. Returns last observed speed."""
    deadline = time.time() + timeout_sec
    last_shown = -1
    last_seen  = -1
    while time.time() < deadline:
        st = get_port_state(token, dev_id, port)
        if st is None:
            time.sleep(POLL_SECONDS)
            continue
        actual = st["speed_actual"]
        last_seen = actual
        if actual != last_shown:
            print(f"    {label}  speed: {actual}/10   (target {target}, mode={st['mode']})")
            last_shown = actual
        if actual == target:
            return actual
        time.sleep(POLL_SECONDS)
    return last_seen


def hold_with_polls(token: str, dev_id: str, port: int, hold_sec: int, label: str):
    """Hold at target while showing periodic confirmation of steady state."""
    print(f"    {label}  holding {hold_sec}s at speed {TARGET_SPEED}...")
    start = time.time()
    while time.time() - start < hold_sec:
        time.sleep(HOLD_POLL_SECONDS)
        st = get_port_state(token, dev_id, port)
        elapsed = int(time.time() - start)
        if st:
            print(f"    {label}  +{elapsed:02d}s  speed: {st['speed_actual']}/10")
        else:
            print(f"    {label}  +{elapsed:02d}s  (no readback)")


# ---------------------------------------------------------------------------
# Cycle one port
# ---------------------------------------------------------------------------

def _validate_action(action: dict | None, expected_value: int) -> tuple[bool, str]:
    if not isinstance(action, dict):
        return False, "no JSON returned"
    if action.get("action") != "set_speed":
        return False, f"action != set_speed ({action.get('action')})"
    val = action.get("value")
    if not isinstance(val, int) or val < 0 or val > 10:
        return False, f"value out of range: {val!r}"
    if val != expected_value:
        return False, f"value {val} != expected {expected_value}"
    return True, "ok"


def cycle_port(token: str, dev: dict, port: dict) -> dict:
    dev_id   = dev["dev_id"]
    dev_type = dev["type"]
    port_num = port["port"]
    label    = f"[{dev['name'][:14]:<14} p{port_num}]"
    pretty   = f"{dev['name']} port {port_num} ({port['name']})"

    print(f"\n{'-' * 72}")
    print(f"  CYCLE: {pretty}")
    print(f"         online={port['online']}  speed_actual={port['speed_actual']}"
          f"  speed_target={port['speed_target']}  mode={port['mode']}")

    if not port["online"]:
        print(f"  SKIP -- offline")
        return {"port": port_num, "device": dev["name"], "ok": False, "reason": "offline"}

    # ----- ramp up -----
    prompt_up = (
        "You are commanding hardware in a grow-tent test rig. "
        f"Set device \"{dev['name']}\" port {port_num} (\"{port['name']}\") to speed {TARGET_SPEED}. "
        "Respond with this JSON object only and no commentary:\n"
        f'{{"action": "set_speed", "device": "{dev["name"]}", '
        f'"port": {port_num}, "value": {TARGET_SPEED}}}'
    )
    print(f"\n  {label} AI: requesting ramp-up to {TARGET_SPEED}...")
    action, latency, raw = ai_action(prompt_up)
    print(f"  {label} AI: {latency:.1f}s  ->  {action}")
    ok, reason = _validate_action(action, TARGET_SPEED)
    if not ok:
        print(f"  {label} AI returned bad action ({reason}). Raw: {raw[:200]!r}")
        print(f"  {label} SKIP -- not executing")
        return {"port": port_num, "device": dev["name"], "ok": False, "reason": f"ai_bad_up:{reason}"}

    print(f"  {label} HW: set_port_speed(speed={TARGET_SPEED})")
    try:
        set_port_speed(token, dev_id, port_num, TARGET_SPEED, dev_type)
    except Exception as e:
        print(f"  {label} HW write failed: {e}")
        return {"port": port_num, "device": dev["name"], "ok": False, "reason": f"write_up:{e}"}

    final_up = watch_to_target(token, dev_id, port_num, TARGET_SPEED, RAMP_TIMEOUT, label)
    if final_up != TARGET_SPEED:
        print(f"  {label} WARN: ramp-up reached {final_up}, not {TARGET_SPEED}")

    # ----- hold -----
    hold_with_polls(token, dev_id, port_num, HOLD_SECONDS, label)

    # ----- ramp down -----
    prompt_down = (
        "Hardware test continues. "
        f"Set device \"{dev['name']}\" port {port_num} (\"{port['name']}\") back to speed 0 (stop). "
        "Respond with this JSON object only:\n"
        f'{{"action": "set_speed", "device": "{dev["name"]}", '
        f'"port": {port_num}, "value": 0}}'
    )
    print(f"\n  {label} AI: requesting ramp-down to 0...")
    action, latency, raw = ai_action(prompt_down)
    print(f"  {label} AI: {latency:.1f}s  ->  {action}")
    ok, reason = _validate_action(action, 0)

    # Always force stop even if AI misbehaved -- safety first
    if not ok:
        print(f"  {label} AI returned bad ramp-down ({reason}). Forcing stop anyway.")
    try:
        set_port_speed(token, dev_id, port_num, 0, dev_type)
    except Exception as e:
        print(f"  {label} HW stop failed: {e}")
        return {"port": port_num, "device": dev["name"], "ok": False, "reason": f"write_down:{e}"}

    final_down = watch_to_target(token, dev_id, port_num, 0, RAMP_TIMEOUT, label)
    if final_down != 0:
        print(f"  {label} WARN: ramp-down left port at {final_down}. Re-sending stop.")
        try:
            set_port_speed(token, dev_id, port_num, 0, dev_type)
        except Exception as e:
            print(f"  {label} second stop failed: {e}")
        # one more watch
        final_down = watch_to_target(token, dev_id, port_num, 0, RAMP_TIMEOUT, label)

    print(f"  {label} DONE  (ramp-up reached {final_up}, ramp-down ended at {final_down})")
    return {
        "port":   port_num,
        "device": dev["name"],
        "ok":     final_down == 0,
        "reason": f"final_down={final_down}",
        "ai_ok_down": ok,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def emergency_stop_all(token: str, devices: list[dict]):
    print("\n[EMERGENCY STOP] commanding speed 0 on every variable-speed port...")
    for d in devices:
        if d["type"] not in VARIABLE_SPEED_DEV_TYPES:
            continue
        for p in d["ports"]:
            if p["is_outlet"] or not p["online"]:
                continue
            try:
                set_port_speed(token, d["dev_id"], p["port"], 0, d["type"])
                print(f"  stopped {d['name']} port {p['port']}")
            except Exception as e:
                print(f"  FAILED to stop {d['name']} port {p['port']}: {e}")


def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: set AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    print(f"AI cycle test  |  model: {OLLAMA_MODEL}")
    print(f"Target speed: {TARGET_SPEED}   Hold: {HOLD_SECONDS}s   Poll: {POLL_SECONDS}s")
    print()
    print("WARNING:")
    print(f"  Will ramp every variable-speed port on every CTR89Q to {TARGET_SPEED}")
    print( "  for ~50s including the 4 dosers on RDWC Control. If doser lines are")
    print( "  in nutrient/pH solution, fluid WILL be pumped into the reservoir.")
    print()
    print(f"Starting in {COUNTDOWN_SECONDS}s -- Ctrl-C to abort.")
    for i in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"  {i:2d}...", end="\r", flush=True)
        time.sleep(1)
    print()

    print("Authenticating...")
    token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
    print("Fetching device list...")
    raw_devs = fetch_all_devices(token)
    devices  = [parse_device(r) for r in raw_devs]

    targets = [d for d in devices
               if d["type"] in VARIABLE_SPEED_DEV_TYPES and d["online"]]
    if not targets:
        print("No variable-speed controllers online. Nothing to do.")
        sys.exit(1)

    print(f"\nControllers to cycle ({len(targets)}):")
    for d in targets:
        active = [p for p in d["ports"] if p["online"] and not p["is_outlet"]]
        print(f"  - {d['name']}  ({len(active)} online variable-speed port(s))")
        for p in active:
            print(f"      port {p['port']}: {p['name']}")

    results = []
    interrupted = False
    try:
        for dev in targets:
            for port in dev["ports"]:
                if port["is_outlet"] or not port["online"]:
                    continue
                results.append(cycle_port(token, dev, port))
                time.sleep(INTER_PORT_PAUSE)
    except KeyboardInterrupt:
        interrupted = True
        emergency_stop_all(token, devices)
    except ACInfinityAuthError as e:
        print(f"\n[AUTH] token rejected mid-run: {e}")
        emergency_stop_all(token, devices)
    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}")
        emergency_stop_all(token, devices)
        raise

    print(f"\n{'=' * 72}")
    print(f"  SUMMARY  ({'INTERRUPTED' if interrupted else 'COMPLETE'})")
    print(f"{'=' * 72}")
    ok    = [r for r in results if r["ok"]]
    fail  = [r for r in results if not r["ok"]]
    print(f"  cycled successfully: {len(ok)}")
    for r in ok:
        print(f"    OK   {r['device']:<16}  port {r['port']}  ({r['reason']})")
    print(f"  failed / skipped: {len(fail)}")
    for r in fail:
        print(f"    FAIL {r['device']:<16}  port {r['port']}  -- {r['reason']}")
    print()


if __name__ == "__main__":
    main()
