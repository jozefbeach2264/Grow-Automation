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
    stop_and_verify,
)
from utils import name_slug
import runtime_state
import event_log

EMAIL           = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD        = os.getenv("AC_INFINITY_PASSWORD", "")
INTERVAL        = int(os.getenv("POLL_INTERVAL",        "30"))
STABLE_INTERVAL = int(os.getenv("POLL_INTERVAL_STABLE", "900"))
ACTIVE_INTERVAL = int(os.getenv("POLL_INTERVAL_ACTIVE", "60"))
AI_ENABLED      = os.getenv("AI_ENABLED", "false").lower() == "true"
ADVISORY_MODE   = os.getenv("ADVISORY_MODE", "true").lower() != "false"
HEARTBEAT_ENABLED      = os.getenv("HEARTBEAT_ENABLED", "true").strip().lower() != "false"
DOSER_WATCHDOG_ENABLED = os.getenv("DOSER_WATCHDOG_ENABLED", "true").strip().lower() != "false"
VERIFY_WRITES          = os.getenv("VERIFY_WRITES", "true").strip().lower() != "false"

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


def is_ph_port(dev_name: str, port: int) -> bool:
    ports_str = os.getenv(f"PH_PORTS_{name_slug(dev_name)}", "")
    if not ports_str:
        return False
    return str(port) in [p.strip() for p in ports_str.split(",")]


def is_chem_port(dev_name: str, port: int) -> bool:
    """Any port that delivers chemicals -- doser or pH. These should sit at speed 0
    unless an active timed dose says otherwise; the watchdog enforces that."""
    return is_doser_port(dev_name, port) or is_ph_port(dev_name, port)


def heartbeat(phase: str, **kwargs) -> None:
    """Thin wrapper so heartbeat writes are a no-op when disabled, and a heartbeat
    failure can never take down the poll loop."""
    if not HEARTBEAT_ENABLED:
        return
    try:
        runtime_state.write_heartbeat(phase, **kwargs)
    except Exception as e:
        print(f"  [HB] heartbeat write failed ({phase}): {e}")


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


def enforce_temp_emergency(snapshot: dict, devices: list, token: str) -> list:
    """
    Fire the high-temperature exhaust guardrail deterministically. Climate-only
    (ramps ROLE_EXHAUST to max) -- never touches chemicals or the CO2 valve, so
    it runs independently of the reservoir/CO2 emergencies. Returns the actions
    actually executed (empty list when the guardrail is not active).
    """
    te = snapshot.get("temp_emergency")
    if not te or not te.get("active"):
        return []

    print(f"  [!!! HIGH TEMP !!!] {te['sensor']}={te['temp_f']} "
          f"(trigger={te['trigger']}, clear={te['clear']}) -- forcing exhaust to max")
    dev_map = {d["name"]: d for d in devices}
    fired   = []
    for a in te["actions"]:
        dev = dev_map.get(a["device"])
        if not dev:
            print(f"  [TEMP-EM] Unknown device '{a['device']}' -- cannot enforce")
            continue
        try:
            set_port_speed(token, dev["dev_id"], a["port"], int(a["value"]), dev["type"])
            print(f"  [TEMP-EM] {a['device']} port {a['port']} -> "
                  f"{a['action']}={a['value']}  ({a['reason']})")
            fired.append(a)
        except Exception as e:
            print(f"  [TEMP-EM] FAILED {a['device']} port {a['port']}: {e}")
    return fired


def enforce_res_burst(snapshot: dict, devices: list, token: str) -> list:
    """
    Fire the reservoir-burst shutdown deterministically. HIGHEST priority -- runs
    before the CO2 emergency and the AI cycle. Scope is WATER/CHEMICAL ONLY: stop
    dosers + close the CO2 valve. Lights, exhaust, and fans are never commanded
    here (cutting ventilation/lighting is never acceptable, burst or not). Also
    trips the persistent dosing freeze so chemicals stay off until manually cleared.
    Returns the actions actually executed (empty when no burst is active).
    """
    rb = snapshot.get("res_burst")
    if not rb or not rb.get("active"):
        return []

    print(f"  [!!! RES BURST !!!] {rb['reason']}")
    dev_map = {d["name"]: d for d in devices}
    fired   = []
    for a in rb.get("actions", []):
        dev = dev_map.get(a["device"])
        if not dev:
            print(f"  [RES-BURST] Unknown device '{a['device']}' -- cannot enforce")
            continue
        try:
            if a["action"] == "set_outlet":
                set_outlet(token, dev["dev_id"], a["port"], bool(a["value"]), dev["type"])
            elif a["action"] == "set_speed":
                set_port_speed(token, dev["dev_id"], a["port"], int(a["value"]), dev["type"])
            else:
                print(f"  [RES-BURST] Unknown action '{a['action']}' -- skipping")
                continue
            print(f"  [RES-BURST] {a['device']} port {a['port']} -> "
                  f"{a['action']}={a['value']}  ({a['reason']})")
            fired.append(a)
        except Exception as e:
            print(f"  [RES-BURST] FAILED {a['device']} port {a['port']}: {e}")

    # Read-after-write: re-confirm each doser actually reached 0 via the shared stop
    # primitive (re-issues the stop + verifies + retries). These are the most critical
    # stops in the system (active leak), so the extra confirmation is deliberate.
    if VERIFY_WRITES:
        for a in fired:
            if a["action"] != "set_speed" or int(a.get("value") or 0) != 0:
                continue
            dev = dev_map.get(a["device"])
            if dev and not _verified_doser_stop(token, dev, a["port"], "RES-BURST"):
                print(f"  [!!! RES-BURST !!!] {a['device']} port {a['port']} could not be "
                      f"confirmed stopped -- dosing freeze (below) stays in force")

    # Persist a chemical freeze so dosing stays off across cycles/restarts until the
    # user inspects and clears it. Climate is unaffected.
    try:
        from safety_state import disable_dosing
        disable_dosing(f"reservoir burst (water_leak={rb.get('water_leak')})")
    except Exception as e:
        print(f"  [RES-BURST] could not persist dosing freeze: {e}")
    return fired


def _verified_doser_stop(token: str, dev: dict, port: int, tag: str) -> bool:
    """Stop a doser/pH port and confirm it via the shared stop primitive; one retry if
    the first stop will not verify. Returns True only when the port is confirmed at 0.
    Caller owns the freeze/alert policy on a False return."""
    res = stop_and_verify(token, dev, port, retries=1, verify=VERIFY_WRITES)
    if res["ok"]:
        if res["reason"] != "verify skipped":
            print(f"  [{tag}] verified stop {dev['name']} port {port} ({res['elapsed_sec']}s)")
        return True
    obs = (res.get("observed") or {}).get("speed_actual")
    print(f"  [{tag}] stop UNVERIFIED {dev['name']} port {port} "
          f"(observed speed {obs}) -- {res['reason']}")
    return False


def _doser_watchdog_debounce() -> int:
    """Consecutive out-of-window nonzero reads (that we successfully stop) before the
    PERSISTENT dosing freeze. A failed stop, or a startup orphan, still freezes
    immediately. Min 1 (=freeze on the first stopped orphan, the pre-debounce behavior)."""
    try:
        return max(1, int(os.getenv("DOSER_WATCHDOG_DEBOUNCE", "2")))
    except ValueError:
        return 2


def doser_watchdog(devices: list, token: str, startup: bool = False) -> list:
    """Stop any doser/pH port found running outside a legitimate active-dose window.

    A chemical pump should be at speed 0 unless timed dosing (#7) has an in-window
    active_dose record vouching for it. Anything else -- a pump left running by a
    crash, a glitch, or a manual change -- is an orphan and a chemical hazard.

    Same contract as the reservoir-burst path: detect (alert + event) in EVERY mode,
    but only actuate in LIVE. On any orphan it actuates in LIVE, it freezes dosing and
    opens a high-alert reservoir-polling window. Returns the stop actions executed."""
    if not DOSER_WATCHDOG_ENABLED:
        return []

    allowed_ports = runtime_state.active_dose_window_ports()  # in-window timed dose(s)
    orphans = []
    for dev in devices:
        for p in dev["ports"]:
            if p.get("is_outlet"):
                continue
            port = p["port"]
            if not is_chem_port(dev["name"], port):
                continue
            key = f"{dev['name']}:{port}"
            spd = p.get("speed_actual") or 0
            if spd > 0 and port not in allowed_ports:
                orphans.append((dev, port, spd, key))
            else:
                # At 0 or legitimately in an active-dose window -> clear any prior streak.
                runtime_state.watchdog_streak_reset(key)

    if not orphans:
        return []

    debounce = _doser_watchdog_debounce()
    tag = "STARTUP-RECOVERY" if startup else "DOSER-WATCHDOG"
    fired = []
    freeze_reason = None
    for dev, port, spd, key in orphans:
        label = get_port_label(dev["name"], port, "")
        print(f"  [!!! {tag} !!!] {dev['name']} port {port} ({label}) running at speed "
              f"{spd} outside any dose window -- orphan chemical pump")
        runtime_state.record_event(
            "active_dose_recovered" if startup else "hardware_watchdog_nonzero_doser",
            device=dev["name"], port=port, observed_speed=spd, startup=startup)
        if ADVISORY_MODE:
            print(f"  [{tag}] ADVISORY -- not actuating (would stop {dev['name']} port {port})")
            continue
        ok = _verified_doser_stop(token, dev, port, tag)
        runtime_state.record_event("stop_recovery_verified" if ok else "stop_recovery_failed",
                                   device=dev["name"], port=port)
        fired.append({"device": dev["name"], "port": port,
                      "action": "set_speed", "value": 0,
                      "reason": f"{tag.lower()} orphan pump"})
        streak = runtime_state.watchdog_streak_bump(key)
        # When to FREEZE (persistent, manual-clear): immediately if the stop would not
        # verify (a pump that won't stop is the critical case) or on startup recovery (a
        # crash orphan is unknowable); otherwise only once the orphan persists across
        # DOSER_WATCHDOG_DEBOUNCE cycles, so one stale readback we successfully stop does
        # not freeze all dosing. The immediate STOP + high-alert fire every cycle regardless.
        if not ok:
            freeze_reason = f"{tag.lower()}: orphan pump {key} would not stop"
        elif startup:
            freeze_reason = f"{tag.lower()}: orphan chemical pump at startup ({key})"
        elif streak >= debounce:
            freeze_reason = (f"{tag.lower()}: orphan chemical pump {key} persisted "
                             f"{streak}/{debounce} cycles")

    if fired:  # LIVE only -- we actuated, so watch the res; freeze if warranted (above)
        runtime_state.start_high_alert(f"{tag.lower()} stopped an orphan chemical pump")
        if freeze_reason:
            try:
                from safety_state import disable_dosing
                disable_dosing(freeze_reason)
            except Exception as e:
                print(f"  [{tag}] could not persist dosing freeze: {e}")
    return fired


def recover_on_startup(token: str) -> None:
    """Pre-loop crash-recovery check (rollout step 3 of docs/done/WATCHDOG_HEARTBEAT_PLAN.md).
    Diagnose how the previous run ended, estimate any interrupted dose, then poll the
    hardware and stop any chemical pump still running. Runs before AI / normal
    polling so a pump left on by a crash is dealt with first."""
    diag = runtime_state.diagnose_restart()
    if diag["fresh"]:
        runtime_state.record_event("process_started", fresh=True)
        print("  [RECOVERY] fresh start -- no prior runtime state.")
    elif diag["clean"]:
        runtime_state.record_event("process_started", clean=True)
        print("  [RECOVERY] previous run exited cleanly.")
    else:
        print(f"  [!!! RECOVERY !!!] previous run did NOT exit cleanly -- "
              f"last_phase={diag['last_phase']} rebooted={diag['rebooted']} "
              f"disconnect={diag['disconnect_sec']}s had_active_dose={diag['had_active_dose']}")
        runtime_state.record_event("process_restarted", **diag)

    ad = runtime_state.get_active_dose()
    if ad and ad.get("status") == "pump_running":
        est = runtime_state.estimate_interrupted_dose(ad, time.time())
        print(f"  [RECOVERY] interrupted dose on {ad.get('device')} port {ad.get('port')}: "
              f"est {est['estimated_actual_ml_min']}-{est['estimated_actual_ml_max']} mL "
              f"actual (best {est['estimated_actual_ml_best']})")
        runtime_state.record_event("estimated_overdose_window",
                                   device=ad.get("device"), port=ad.get("port"), **est)

    # Poll and stop any running chemical pump.
    try:
        devices = poll_once(token)
    except Exception as e:
        print(f"  [RECOVERY] could not poll for recovery scan: {e} -- main loop will retry")
        return
    fired = doser_watchdog(devices, token, startup=True)

    # If the previous run died mid-dose we cannot prove what happened during the gap,
    # so freeze + high-alert even if nothing is currently running.
    if not ADVISORY_MODE and not diag["clean"] and diag["had_active_dose"] and not fired:
        try:
            from safety_state import disable_dosing
            disable_dosing("crash recovery -- dose state unknown across restart")
        except Exception as e:
            print(f"  [RECOVERY] could not persist dosing freeze: {e}")
        runtime_state.start_high_alert("crash recovery -- interrupted dose, reservoir unverified")

    if ad:  # the record has now been handled; clear it so it can't re-trigger
        runtime_state.mark_active_dose_stopped(verified=bool(fired), recovered=True)
        runtime_state.clear_active_dose()


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

    # Index what the AI already executed, keyed by (device, port, action). Use .get so
    # chemical `dose` actions (no single port/value) don't raise here -- they never
    # match a schedule delta anyway (those are set_speed/set_outlet on a specific port).
    done = {(a.get("device"), a.get("port"), a.get("action")): a.get("value")
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

    # build_snapshot is needed for deterministic enforcement even when AI is off,
    # so it imports unconditionally. The AI-specific helpers stay gated below.
    from ai_advisor import build_snapshot

    if AI_ENABLED:
        from ai_advisor import ask_ai, print_advice, execute_actions, warmup
        from profile_manager import (
            active_profile_label, log_cycle,
            track_actions, record_outcomes, has_pending_outcomes,
        )
        mode = "ADVISORY" if ADVISORY_MODE else "LIVE CONTROL"
        print(f"AI advisor enabled ({mode} mode).  Profile: {active_profile_label()}")
        warmup()
        print()
    else:
        mode = "ADVISORY" if ADVISORY_MODE else "DETERMINISTIC LIVE"
        print(f"AI advisor DISABLED ({mode} mode).")
        print(f"  - Schedule enforcement (lights/fans/CO2 pulse) "
              f"{'ACTIVE' if not ADVISORY_MODE else 'inactive (advisory)'}")
        print(f"  - CO2 emergency dump "
              f"{'ACTIVE' if not ADVISORY_MODE else 'inactive (advisory)'}")
        print(f"  - High-temp exhaust guardrail "
              f"{'ACTIVE' if not ADVISORY_MODE else 'inactive (advisory)'}")
        print(f"  - Sensor monitoring active either way")
        print()

    # --- Crash recovery: deal with any pump left running by a previous crash
    #     BEFORE normal polling / AI (rollout step 3 of the watchdog plan). ---
    heartbeat("starting")
    try:
        recover_on_startup(token)
    except ACInfinityAuthError:
        print("  [RECOVERY] token expired during recovery -- re-authenticating...")
        os.environ["AC_INFINITY_TOKEN"] = ""
        token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
        recover_on_startup(token)
    except Exception as e:
        print(f"  [RECOVERY] recovery check error: {e}")
    print()

    ai_failure_count = 0
    try:
        while True:
            try:
                global LAST_AI_TIME
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                heartbeat("polling_devices")
                devices = poll_once(token)
                devices.sort(key=lambda d: int(os.getenv(
                    f"DISPLAY_ORDER_{name_slug(d['name'])}", "99")))
                print(f"{'='*60}")
                uptime    = elapsed(START_TIME)
                if AI_ENABLED:
                    cycle_str = (f"last AI: {elapsed(LAST_AI_TIME)} ago"
                                 if LAST_AI_TIME else "first cycle")
                else:
                    cycle_str = "no AI"
                print(f"  Poll at {ts}  |  up: {uptime}  |  {cycle_str}  "
                      f"|  {len(devices)} device(s)")
                for dev in devices:
                    print_device(dev)
                print()

                # --- Deterministic foundation -- runs regardless of AI ---
                heartbeat("building_snapshot", poll_ok=True, api_ok=True)
                snapshot = build_snapshot(devices)

                # Open the cycle ledger record (one cycle_id per poll). Threads
                # through execute_actions so every action ties back to the
                # conditions that produced it. Logging never raises.
                cycle_id = event_log.start_cycle(
                    snapshot, mode="advisory" if ADVISORY_MODE else "live")
                event_log.log_stressors(cycle_id, snapshot.get("diagnostics"))

                active = False
                if AI_ENABLED:
                    record_outcomes(snapshot)
                    active = has_pending_outcomes()

                wl_source = snapshot.get("water_level_source", "SENSOR")
                wl_trend = snapshot.get("trends", {}).get("water_level", "?")
                if wl_source == "FLOAT":
                    print(f"  [FLOAT] Water level: {wl_trend}  "
                          f"(magnetic float -- reposition daily to the drawdown line)")
                elif wl_source == "MANUAL":
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

                diag = snapshot.get("diagnostics", {})
                stressors = diag.get("stressors", [])
                if stressors:
                    print(f"  [DIAG] {diag.get('count')} stressor(s), "
                          f"worst={diag.get('worst_severity')}")
                    for s in stressors:
                        pbs = ",".join(s.get("allowed_playbooks", []))
                        print(f"         [{s['severity']:<8}] {s['name']}: "
                              f"{s['evidence']}  -> {pbs}")
                else:
                    print("  [DIAG] no stressors detected")

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

                # --- Reservoir leak / burst (highest priority; water/chemical only) ---
                # Alert always, even in ADVISORY mode, so a leak is never silent.
                leak = snapshot.get("leak", {})
                if leak.get("wet"):
                    print(f"  [LEAK] water_leak WET (raw={leak.get('raw')}, "
                          f"streak={leak.get('streak')}, confirmed={leak.get('confirmed')})")
                rb = snapshot.get("res_burst")
                if rb and rb.get("active"):
                    print(f"  [!!! RES BURST ALERT !!!] {rb['reason']}")
                te = snapshot.get("temp_emergency")
                if te and te.get("active"):
                    print(f"  [!!! HIGH TEMP ALERT !!!] {te['sensor']}={te['temp_f']}F "
                          f">= {te['trigger']}F -- exhaust to max until < {te['clear']}F")

                executed_actions: list[dict] = []

                # Orphan-pump watchdog: a chemical pump should sit at speed 0 unless
                # an active timed dose vouches for it. Detect/alert in any mode,
                # actuate (stop + freeze + high-alert) only in LIVE. Runs first so a
                # runaway pump is dealt with ahead of everything else.
                wd_fired = doser_watchdog(devices, token)
                if wd_fired:
                    executed_actions.extend(wd_fired)
                    active = True

                if not ADVISORY_MODE:
                    # Burst shutdown runs before everything else (stops dosers,
                    # closes CO2; lights/ventilation untouched).
                    burst_fired = enforce_res_burst(snapshot, devices, token)
                    if burst_fired:
                        executed_actions.extend(burst_fired)
                        active = True

                    # Evac pump tracks the leak sensor (ON when confirmed wet, OFF when
                    # dry). Independent of RES_BURST_ENABLED; gated by EVAC_PUMP config.
                    ev = snapshot.get("evac_pump")
                    if ev:
                        evdev = {d["name"]: d for d in devices}.get(ev["device"])
                        if not evdev:
                            print(f"  [EVAC] Unknown device '{ev['device']}' -- cannot run evac pump")
                        else:
                            try:
                                set_outlet(token, evdev["dev_id"], ev["port"],
                                           bool(ev["value"]), evdev["type"])
                                print(f"  [EVAC] {ev['device']} port {ev['port']} -> "
                                      f"set_outlet={ev['value']}  ({ev['reason']})")
                                executed_actions.append(ev)
                                active = True
                            except Exception as e:
                                print(f"  [EVAC] FAILED {ev['device']} port {ev['port']}: {e}")

                    # --- Pre-AI CO2 emergency dump ---
                    em_fired = enforce_co2_emergency(snapshot, devices, token)
                    executed_actions.extend(em_fired)

                    # --- Pre-AI high-temp exhaust guardrail (climate-only) ---
                    temp_fired = enforce_temp_emergency(snapshot, devices, token)
                    if temp_fired:
                        executed_actions.extend(temp_fired)
                        active = True

                # --- Optional AI cycle ---
                ai_result = None
                ai_next   = None
                if AI_ENABLED:
                    print("  [AI] Thinking...", flush=True)
                    ai_start  = time.time()
                    ai_result = ask_ai(snapshot)
                    ai_latency = time.time() - ai_start
                    event_log.log_ai_decision(cycle_id, ai_result, ai_latency)
                    if ai_result:
                        ai_failure_count = 0
                        LAST_AI_TIME = time.time()
                        print(f"  [AI] Response in {elapsed(ai_start)}")
                        print_advice(ai_result)
                        if not ADVISORY_MODE and ai_result.get("actions"):
                            # execute_actions returns ONLY what actually executed (dose
                            # actions are enriched with playbook + resolved ports); track
                            # those, not the raw proposals.
                            executed = execute_actions(ai_result, devices, token,
                                                       snapshot=snapshot,
                                                       cycle_id=cycle_id)
                            if executed:
                                track_actions(executed, snapshot)
                                executed_actions.extend(executed)
                                active = True
                        ai_next = ai_result.get("next_check_seconds")
                    else:
                        ai_failure_count += 1
                        print(f"  [AI] No result (failure #{ai_failure_count})")

                # --- Schedule fallback + emergency re-check (always in LIVE) ---
                if not ADVISORY_MODE:
                    fired = enforce_schedule_fallback(
                        snapshot, executed_actions, devices, token)
                    if fired:
                        executed_actions.extend(fired)
                        active = True

                    if snapshot.get("co2_emergency"):
                        re_em = enforce_co2_emergency(snapshot, devices, token)
                        if re_em:
                            executed_actions.extend(re_em)
                            active = True

                    if snapshot.get("temp_emergency"):
                        re_temp = enforce_temp_emergency(snapshot, devices, token)
                        if re_temp:
                            executed_actions.extend(re_temp)
                            active = True

                # --- Log cycle (when AI ran successfully) ---
                if AI_ENABLED and ai_result:
                    log_cycle(snapshot, executed_actions)

                # --- Sleep decision ---
                if AI_ENABLED and ai_result:
                    sleep_for = ACTIVE_INTERVAL if (active or executed_actions) else STABLE_INTERVAL
                    mode_str  = "ACTIVE" if (active or executed_actions) else "STABLE"
                    if ai_next and ai_next < sleep_for:
                        sleep_for = max(ai_next, 30)
                    print(f"  [--] Mode: {mode_str}  next poll in {sleep_for}s")
                elif AI_ENABLED and not ai_result:
                    sleep_for = min(INTERVAL * (2 ** min(ai_failure_count - 1, 5)), 1800)
                    print(f"  [AI] backing off {sleep_for}s after failure #{ai_failure_count}")
                else:
                    # Deterministic-only mode (no AI). Faster cadence when actions
                    # fired this cycle, regular interval otherwise.
                    sleep_for = ACTIVE_INTERVAL if executed_actions else INTERVAL
                    mode_str  = "DET-ACTIVE" if executed_actions else "DET-IDLE"
                    print(f"  [--] Mode: {mode_str}  next poll in {sleep_for}s  (no AI)")

                # High-alert reservoir polling: after a recovery/scare, poll faster
                # for a bounded, persisted window. Never lengthens the chosen sleep.
                ha_active, ha_remaining, ha_reason = runtime_state.high_alert_status()
                if ha_active:
                    ha_iv = runtime_state.high_alert_poll_interval()
                    if ha_iv < sleep_for:
                        sleep_for = ha_iv
                    print(f"  [HIGH-ALERT] {ha_reason} -- {ha_remaining}s left; "
                          f"polling every {sleep_for}s")

                heartbeat("sleeping")
                time.sleep(sleep_for)

            except ACInfinityAuthError:
                print("Token expired -- re-authenticating...")
                heartbeat("error_backoff", api_ok=False)
                os.environ["AC_INFINITY_TOKEN"] = ""
                token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
                time.sleep(INTERVAL)
            except Exception as e:
                print(f"Poll error: {e}")
                heartbeat("error_backoff", poll_ok=False)
                time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if HEARTBEAT_ENABLED:
            try:
                runtime_state.mark_clean_shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
