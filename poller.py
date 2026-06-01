#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grow Automation -- AC Infinity device poller and aggregator."""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os
import time
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH    = Path(__file__).parent / ".env"
LABELS_PATH = Path(__file__).parent / "labels.env"
load_dotenv(ENV_PATH)
load_dotenv(LABELS_PATH)

from ac_infinity_client import (
    ACInfinityAuthError,
    get_or_refresh_token,
    fetch_all_devices,
    parse_device,
    set_outlet,
    set_port_speed,
)
from utils import name_slug

EMAIL           = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD        = os.getenv("AC_INFINITY_PASSWORD", "")
INTERVAL        = int(os.getenv("POLL_INTERVAL",        "30"))
STABLE_INTERVAL = int(os.getenv("POLL_INTERVAL_STABLE", "900"))
ACTIVE_INTERVAL = int(os.getenv("POLL_INTERVAL_ACTIVE", "60"))
AI_ENABLED      = os.getenv("AI_ENABLED", "false").lower() == "true"
ADVISORY_MODE   = os.getenv("ADVISORY_MODE", "true").lower() != "false"

START_TIME     = time.time()
LAST_AI_TIME   = None


def elapsed(since: float) -> str:
    s = int(time.time() - since)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02}m {s:02}s"
    if m:
        return f"{m}m {s:02}s"
    return f"{s}s"


def get_port_label(dev_name: str, port: int, fallback: str) -> str:
    key = f"PORT_{name_slug(dev_name)}_{port}"
    label = os.getenv(key, "").strip()
    return label if label else fallback


def is_doser_port(dev_name: str, port: int) -> bool:
    ports_str = os.getenv(f"DOSER_PORTS_{name_slug(dev_name)}", "")
    if not ports_str:
        return False
    return str(port) in [p.strip() for p in ports_str.split(",")]


def print_device(dev: dict):
    status = "ONLINE" if dev["online"] else "OFFLINE"
    print(f"\n  [{dev['type_label']}]  {dev['name']}  --  {status}")
    def fmt(val, unit=""):
        return f"{val}{unit}" if val is not None else None

    if dev["is_ai"]:
        slug      = name_slug(dev["name"])
        hide_air  = os.getenv(f"HIDE_AIR_{slug}", "").lower() == "true"
        air_label  = os.getenv(f"AIR_LABEL_{slug}",  "Air")
        air2_label = os.getenv(f"AIR2_LABEL_{slug}", "Air(2)")
        if not hide_air:
            hide_builtin = os.getenv(f"HIDE_AIR_BUILTIN_{slug}", "").lower() == "true"
            hide_ext     = os.getenv(f"HIDE_AIR_EXT_{slug}",     "").lower() == "true"
            # Built-in sensor hub
            if not hide_builtin and dev["temp_f"] is not None:
                print(f"    {air_label:<8}Temp: {fmt(dev['temp_f'], 'F')}  "
                      f"Humidity: {fmt(dev['humidity_pct'], '%')}  "
                      f"VPD: {fmt(dev['vpd_kpa'], ' kPa')}")
            # External air probe
            if not hide_ext and dev["temp_f_ext"] is not None:
                print(f"    {air2_label:<8}Temp: {fmt(dev['temp_f_ext'], 'F')}  "
                      f"Humidity: {fmt(dev['humidity_ext'], '%')}  "
                      f"VPD: {fmt(dev['vpd_ext'], ' kPa')}")
        # Environment sensors
        env_parts = []
        if dev["co2_ppm"] is not None:
            env_parts.append(f"CO2: {dev['co2_ppm']} ppm")
        if dev["light"] is not None:
            env_parts.append(f"Light: {dev['light']}")
        if dev["water_level"] is not None:
            env_parts.append(f"Water Level: {dev['water_level']}")
        if env_parts:
            print(f"    Env     {' | '.join(env_parts)}")
        # HDS3 hydro probe
        hydro_parts = []
        if dev["ph"] is not None:
            hydro_parts.append(f"pH: {dev['ph']}")
        if dev["tds_ppm"] is not None:
            hydro_parts.append(f"TDS: {dev['tds_ppm']} ppm")
        if dev["ec_us"] is not None:
            hydro_parts.append(f"EC: {dev['ec_us']} uS/cm")
        if dev["water_temp_f"] is not None:
            hydro_parts.append(f"H2O: {dev['water_temp_f']}F")
        if hydro_parts:
            print(f"    Hydro   {' | '.join(hydro_parts)}")
    for p in dev["ports"]:
        conn = "online" if p["online"] else "offline"
        mode_map = {1: "OFF", 2: "MANUAL", 3: "AUTO", 8: "VPD"}
        mode = mode_map.get(p["mode"], f"mode={p['mode']}")
        name = get_port_label(dev["name"], p["port"], p["name"])
        if p.get("is_outlet"):
            power = "ON" if p.get("powered") else "OFF"
            print(f"    Outlet {p['port']:>2}  {name:<18}  {power:<6}  [{conn}]")
        elif is_doser_port(dev["name"], p["port"]):
            spd = p.get("speed_actual") or 0
            ml_min = spd * 21
            dose_str = f"{ml_min} mL/min  (spd {spd}/10)" if ml_min > 0 else "idle"
            print(f"    Doser {p['port']:>2}  {name:<20}  {dose_str:<26}  [{conn}]")
        else:
            speed_str = f"speed {p['speed_actual']}/10" if p.get("speed_actual") is not None else ""
            print(f"    Port {p['port']:>2}  {name:<20}  {mode:<8}  {speed_str}  [{conn}]")


def poll_once(token: str, debug: bool = False) -> list[dict]:
    raw_devices = fetch_all_devices(token)
    if debug:
        import json
        print("\n=== RAW API RESPONSE ===")
        print(json.dumps(raw_devices, indent=2))
        print("=== END RAW ===\n")
    return [parse_device(r) for r in raw_devices]


def enforce_co2_emergency(snapshot: dict, devices: list, token: str) -> list:
    """
    Fire the CO2 emergency dump actions deterministically. Highest priority --
    runs before the AI cycle. Returns the actions actually executed (empty list
    when no emergency is active).
    """
    em = snapshot.get("co2_emergency")
    if not em or not em.get("active"):
        return []

    print(f"  [!!! CO2 EMERGENCY !!!] {em['co2_ppm']} ppm "
          f"(trigger={em['trigger']}, clear={em['clear']}) -- forcing dump")
    dev_map = {d["name"]: d for d in devices}
    fired   = []
    for a in em["actions"]:
        dev = dev_map.get(a["device"])
        if not dev:
            print(f"  [CO2-EM] Unknown device '{a['device']}' -- cannot enforce")
            continue
        try:
            if a["action"] == "set_outlet":
                from ac_infinity_client import set_outlet
                set_outlet(token, dev["dev_id"], a["port"], bool(a["value"]),
                           dev["type"])
            elif a["action"] == "set_speed":
                set_port_speed(token, dev["dev_id"], a["port"],
                               int(a["value"]), dev["type"])
            else:
                print(f"  [CO2-EM] Unknown action '{a['action']}' -- skipping")
                continue
            print(f"  [CO2-EM] {a['device']} port {a['port']} -> "
                  f"{a['action']}={a['value']}  ({a['reason']})")
            fired.append(a)
        except Exception as e:
            print(f"  [CO2-EM] FAILED {a['device']} port {a['port']}: {e}")
    return fired


def enforce_schedule_fallback(snapshot: dict, executed_actions: list,
                              devices: list, token: str) -> list:
    """
    Deterministic safety net for schedule corrections the AI didn't fire.

    Handles both set_speed (fans/light) and set_outlet (CO2 pulse) deltas.
    The AI is in the loop for nuance but cannot skip schedule enforcement --
    either it issues the correction or this does.
    """
    deltas = snapshot.get("schedule_deltas", []) or []
    if not deltas:
        return []

    # Index what the AI already executed, keyed by (device, port, action)
    done = {(a["device"], a["port"], a.get("action")): a.get("value")
            for a in executed_actions}

    dev_map = {d["name"]: d for d in devices}
    fired   = []

    for d in deltas:
        action = d.get("action", "set_speed")
        expected = d["expected_value"]
        key = (d["device"], d["port"], action)
        if done.get(key) == expected:
            continue
        dev = dev_map.get(d["device"])
        if not dev:
            print(f"  [SCHED] Unknown device '{d['device']}' for {d['kind']} -- skipping")
            continue
        try:
            if action == "set_speed":
                set_port_speed(token, dev["dev_id"], d["port"],
                               int(expected), dev["type"])
            elif action == "set_outlet":
                set_outlet(token, dev["dev_id"], d["port"],
                           bool(expected), dev["type"])
            else:
                print(f"  [SCHED] Unknown action '{action}' for {d['kind']} -- skipping")
                continue
            print(f"  [SCHED] FALLBACK {d['device']} port {d['port']} -> "
                  f"{action}={expected}  ({d['kind']}: AI missed)")
            fired.append({
                "device": d["device"], "port": d["port"],
                "action": action, "value": expected,
                "reason": f"schedule fallback ({d['kind']})",
            })
        except Exception as e:
            print(f"  [SCHED] FAIL {d['device']} port {d['port']}: {e}")

    return fired


def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: Set AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    print("Authenticating with AC Infinity cloud...")
    token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
    print(f"Token acquired. Polling every {INTERVAL}s. Ctrl-C to stop.\n")

    if AI_ENABLED:
        from ai_advisor import ask_ai, build_snapshot, print_advice, execute_actions, warmup
        from profile_manager import (
            active_profile_label, log_cycle,
            track_actions, record_outcomes, has_pending_outcomes,
        )
        mode = "ADVISORY" if ADVISORY_MODE else "LIVE CONTROL"
        print(f"AI advisor enabled ({mode} mode).  Profile: {active_profile_label()}")
        warmup()
        print()

    ai_failure_count = 0
    try:
        while True:
            try:
                global LAST_AI_TIME
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                devices = poll_once(token)
                devices.sort(key=lambda d: int(os.getenv(
                    f"DISPLAY_ORDER_{name_slug(d['name'])}", "99")))
                print(f"{'='*60}")
                uptime    = elapsed(START_TIME)
                cycle_str = f"last AI: {elapsed(LAST_AI_TIME)} ago" if LAST_AI_TIME else "first cycle"
                print(f"  Poll at {ts}  |  up: {uptime}  |  {cycle_str}  |  {len(devices)} device(s)")
                for dev in devices:
                    print_device(dev)
                print()

                if AI_ENABLED:
                    snapshot = build_snapshot(devices)
                    record_outcomes(snapshot)   # settle any pending action outcomes
                    active = has_pending_outcomes()

                    wl_source = snapshot.get("water_level_source", "SENSOR")
                    if wl_source == "MANUAL":
                        wl_trend = snapshot.get("trends", {}).get("water_level", "?")
                        print(f"  [MANUAL] Water level: {wl_trend}  "
                              f"(set WATER_LEVEL_TREND= in .env to change)")
                    elif wl_source == "MISSING":
                        print("  [WARN] Water level: no sensor and no WATER_LEVEL_TREND set -- "
                              "res health gate will HOLD all parameters")

                    rh = snapshot.get("res_health", {})
                    if rh:
                        print(f"  [RES] {rh['state']}  "
                              f"water:{rh['water_trend']}  ec:{rh['ec_trend']}  "
                              f"co2:{rh['co2_gate']}  dose:{rh['dose_gate']}  ph:{rh['ph_gate']}")

                    # Schedule status (always shown so deltas are visible at a glance)
                    exp = snapshot.get("expected", {})
                    lt  = exp.get("light", {})
                    if lt:
                        print(f"  [SCHED] light: "
                              f"{'ON' if lt.get('on') else 'OFF'} "
                              f"@ speed {lt.get('speed', 0)}  ({lt.get('reason', '')})")
                    deltas = snapshot.get("schedule_deltas", [])
                    if deltas:
                        for d in deltas:
                            print(f"  [SCHED] DELTA {d['device']} port {d['port']} "
                                  f"({d['kind']}): expected {d['expected_value']}, "
                                  f"actual {d['actual_value']}  -- {d['reason']}")
                    else:
                        print("  [SCHED] all schedule outputs in sync")

                    # CO2 emergency dump -- highest priority, runs BEFORE the AI
                    # cycle. If CO2 exceeded the trigger, force valve OFF and
                    # exhaust to max regardless of what the AI wants.
                    em_fired = []
                    if not ADVISORY_MODE:
                        em_fired = enforce_co2_emergency(snapshot, devices, token)

                    print("  [AI] Thinking...", flush=True)
                    ai_start = time.time()
                    result = ask_ai(snapshot)
                    if result:
                        ai_failure_count = 0
                        ai_elapsed = elapsed(ai_start)
                        LAST_AI_TIME = time.time()
                        print(f"  [AI] Response in {ai_elapsed}")
                        print_advice(result)
                        executed_actions = []
                        if not ADVISORY_MODE and result.get("actions"):
                            execute_actions(result, devices, token, snapshot=snapshot)
                            executed_actions = result.get("actions", [])
                            if executed_actions:
                                track_actions(executed_actions, snapshot)
                                active = True

                        # Deterministic schedule fallback -- fires any deltas
                        # the AI failed to correct. Only in LIVE mode.
                        if not ADVISORY_MODE:
                            fired = enforce_schedule_fallback(
                                snapshot, executed_actions, devices, token)
                            if fired:
                                executed_actions = list(executed_actions) + fired
                                active = True

                        # Re-enforce CO2 emergency AFTER the AI cycle -- if the
                        # AI somehow re-enabled the valve or dropped the exhaust,
                        # we slam it back to safe state in the same cycle.
                        if not ADVISORY_MODE and snapshot.get("co2_emergency"):
                            re_fired = enforce_co2_emergency(snapshot, devices, token)
                            if re_fired:
                                executed_actions = list(executed_actions) + re_fired
                                active = True

                        # Merge any pre-AI emergency actions into the logged set
                        if em_fired:
                            executed_actions = list(em_fired) + list(executed_actions)
                            active = True

                        log_cycle(snapshot, executed_actions)

                        # Adaptive sleep: active (adjusting) vs stable
                        ai_next = result.get("next_check_seconds")
                        if active or executed_actions:
                            sleep_for = ACTIVE_INTERVAL
                            mode_str  = "ACTIVE"
                        else:
                            sleep_for = STABLE_INTERVAL
                            mode_str  = "STABLE"
                        # AI can request a shorter check, never longer
                        if ai_next and ai_next < sleep_for:
                            sleep_for = max(ai_next, 30)
                        print(f"  [--] Mode: {mode_str}  next poll in {sleep_for}s")
                        time.sleep(sleep_for)
                        continue
                    else:
                        # AI returned None -- schedule + CO2 emergency must
                        # still be enforced before the backoff sleep.
                        if not ADVISORY_MODE:
                            # Emergency was already enforced pre-AI; re-check
                            # in case CO2 climbed during the failed AI call
                            re_em = enforce_co2_emergency(snapshot, devices, token)
                            fired = enforce_schedule_fallback(
                                snapshot, list(em_fired) + list(re_em),
                                devices, token)
                            if fired:
                                print(f"  [SCHED] {len(fired)} fallback action(s) "
                                      "fired despite AI failure")

                        # Exponential backoff so a broken Ollama doesn't get
                        # hammered every 30s.
                        ai_failure_count += 1
                        backoff = min(INTERVAL * (2 ** min(ai_failure_count - 1, 5)), 1800)
                        print(f"  [AI] No result -- backing off {backoff}s "
                              f"(failure #{ai_failure_count})")
                        time.sleep(backoff)
                        continue

            except ACInfinityAuthError:
                print("Token expired -- re-authenticating...")
                os.environ["AC_INFINITY_TOKEN"] = ""
                token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
            except Exception as e:
                print(f"Poll error: {e}")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
