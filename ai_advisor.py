"""
Grow Automation -- AI reasoning layer.
Reads aggregated sensor data, reasons about conditions, returns structured actions.
Runs in advisory mode by default -- logs decisions without executing them.
Set ADVISORY_MODE=false in .env to enable live control.
"""

import json
import os
import time
from pathlib import Path

import requests

from utils import name_slug
from grow_state import current_grow_week_and_stage, days_into_current_stage
from schedule import (compute_schedule_deltas, expected_light_state,
                      expected_osc_fan_state, compute_co2_emergency,
                      compute_co2_pulse)
from safety_state import dosing_disable_status, disable_dosing

OLLAMA_HOST   = os.getenv("OLLAMA_HOST",   "http://localhost:11434")
# Default chosen via head-to-head benchmark (model_benchmark.py): qwen2.5:3b-instruct
# scored 32/32 schema-valid at 1.9s median, fits in 4GB VRAM. Set OLLAMA_MODEL in
# .env to override. See project_rdwc_controller.md memory for full benchmark.
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL",  "qwen2.5:3b-instruct")

# Lockout state lives here so a poller restart doesn't clear cooldown clocks.
# Atomic tmp+replace writes; corrupt/missing file is silently treated as empty.
_LOCKOUT_FILE = Path(__file__).parent / "profiles" / ".lockouts.json"


# --- Dynamic config readers (re-read .env every call so edits take effect mid-run) ---

def _advisory_mode() -> bool:
    return os.getenv("ADVISORY_MODE", "true").lower() != "false"

def _dose_lockout_sec() -> int:
    return int(os.getenv("DOSE_LOCKOUT_MINUTES", "15")) * 60

def _ph_lockout_sec() -> int:
    return int(os.getenv("PH_LOCKOUT_MINUTES", "20")) * 60

def _max_doser_speed() -> int:
    return int(os.getenv("MAX_DOSER_SPEED", "5"))

def _max_dose_ml_cycle() -> float:
    return float(os.getenv("MAX_DOSE_ML_CYCLE", "50.0"))

def _reservoir_volume_gal() -> float:
    """Active reservoir volume in gallons. Dose-size math (mL delivered -> ppm /
    pH shift) is meaningless without it -- a 0.5 mL pH dose hits very differently
    in 20 gal vs 60 gal. Default 60; override with RESERVOIR_VOLUME_GAL. A bad/
    non-positive value falls back to the 60 gal default rather than poisoning math."""
    try:
        vol = float(os.getenv("RESERVOIR_VOLUME_GAL", "60"))
    except ValueError:
        return 60.0
    return vol if vol > 0 else 60.0


# Lockout state. Loaded from disk on module import; persisted on every update
# so a poller restart preserves the cooldown clock.
#
# File schema (profiles/.lockouts.json):
#   {
#     "last_dose_time": {"<device>:<port>": <unix_ts>, ...},
#     "last_ph_time":   <unix_ts>
#   }
_last_dose_time: dict[str, float] = {}
_last_ph_time: float = 0.0


def _load_lockouts():
    """Populate _last_dose_time and _last_ph_time from disk if the file exists.
    Silently tolerates missing or corrupt files -- worst case the lockouts reset,
    which matches the pre-persistence behaviour."""
    global _last_dose_time, _last_ph_time
    if not _LOCKOUT_FILE.exists():
        return
    try:
        data = json.loads(_LOCKOUT_FILE.read_text())
    except Exception as e:
        print(f"[LOCKOUTS] Could not read {_LOCKOUT_FILE.name} ({e}) -- starting fresh")
        return
    dt = data.get("last_dose_time") or {}
    if isinstance(dt, dict):
        _last_dose_time = {str(k): float(v) for k, v in dt.items()
                           if isinstance(v, (int, float))}
    pt = data.get("last_ph_time")
    if isinstance(pt, (int, float)):
        _last_ph_time = float(pt)

    # Report what we restored so a restart mid-lockout is visible in the log
    now = time.time()
    restored = []
    for key, ts in _last_dose_time.items():
        remaining = _dose_lockout_sec() - (now - ts)
        if remaining > 0:
            m, s = divmod(int(remaining), 60)
            restored.append(f"{key} ({m}m{s:02}s left)")
    if _last_ph_time:
        remaining = _ph_lockout_sec() - (now - _last_ph_time)
        if remaining > 0:
            m, s = divmod(int(remaining), 60)
            restored.append(f"pH-global ({m}m{s:02}s left)")
    if restored:
        print(f"[LOCKOUTS] Restored from disk: {', '.join(restored)}")


def _save_lockouts():
    """Atomic write of current lockout state. Failure here is logged but not
    fatal -- the in-memory state continues to function normally."""
    try:
        _LOCKOUT_FILE.parent.mkdir(exist_ok=True)
        tmp = _LOCKOUT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "last_dose_time": _last_dose_time,
            "last_ph_time":   _last_ph_time,
        }, indent=2))
        tmp.replace(_LOCKOUT_FILE)
    except Exception as e:
        print(f"[LOCKOUTS] Could not write {_LOCKOUT_FILE.name}: {e}")


_load_lockouts()


def _is_ph_port(device: str, port: int) -> bool:
    ports_str = os.getenv(f"PH_PORTS_{name_slug(device)}", "")
    return bool(ports_str) and str(port) in [p.strip() for p in ports_str.split(",")]


def _is_doser_port(device: str, port: int) -> bool:
    ports_str = os.getenv(f"DOSER_PORTS_{name_slug(device)}", "")
    return bool(ports_str) and str(port) in [p.strip() for p in ports_str.split(",")]


def _is_co2_valve(device: str, port: int) -> bool:
    """Match the CO2 enrichment valve from CO2_VALVE=<device>:<port> env config.
    Returns False if env unset -- gate enforcement is opt-in via .env."""
    raw = os.getenv("CO2_VALVE", "").strip()
    if not raw or ":" not in raw:
        return False
    try:
        d, p = raw.rsplit(":", 1)
        return d.strip() == device and int(p) == port
    except (ValueError, AttributeError):
        return False


def _port_max_speed(device: str, port: int) -> int:
    """Per-port speed cap from labels.env, falling back to global MAX_DOSER_SPEED.
    RES_CHANGE_MODE=true uncaps nutrient ports to 10; pH ports always keep their cap."""
    if (os.getenv("RES_CHANGE_MODE", "").lower() == "true"
            and not _is_ph_port(device, port)):
        return 10
    key = f"MAX_SPEED_{name_slug(device)}_{port}"
    per_port = os.getenv(key)
    if per_port is not None:
        return min(int(per_port), 10)
    return _max_doser_speed()


_VALID_ACTIONS = {"set_speed", "set_outlet"}


def validate_actions(actions: list, snapshot: dict | None = None) -> list:
    """
    Schema + preflight validation.

    Catches AI hallucinations BEFORE the safety gate runs. Each rejection is
    logged with a specific reason so failure modes are visible in the cycle
    output instead of being silently dropped at the executor.

    Per-action checks (short-circuits on first failure):
      1. Must be a dict
      2. Required keys present: device, port, action, value
      3. action must be one of: set_speed, set_outlet
      4. port must be an int
      5. value type matches action:
           set_speed  -> int in [0, 10]   (str "5" is rejected; bool is rejected)
           set_outlet -> real bool        (str "true"/"false" is rejected)
      6. device must exist in the current snapshot
      7. port must exist on that device
      8. port type must match action:
           set_speed  on outlet port      -> rejected
           set_outlet on speed port       -> rejected

    If snapshot is None or missing devices, the device/port existence and
    port-type checks are skipped (the structural checks still run). The
    executor's own "Unknown device" check is the last line of defense.
    """
    if not isinstance(actions, list):
        print(f"  [VALIDATE] actions field is not a list ({type(actions).__name__}) -- ignoring")
        return []

    # Build a name -> {port: port_dict} map from the snapshot
    dev_map: dict[str, dict[int, dict]] = {}
    if snapshot and isinstance(snapshot.get("devices"), list):
        for d in snapshot["devices"]:
            if not isinstance(d, dict) or "name" not in d:
                continue
            dev_map[d["name"]] = {
                p.get("port"): p for p in d.get("ports", []) or []
                if isinstance(p, dict) and isinstance(p.get("port"), int)
            }

    valid: list[dict] = []
    for i, a in enumerate(actions):
        # 1. Structural
        if not isinstance(a, dict):
            print(f"  [VALIDATE] reject action #{i}: not a dict ({type(a).__name__})")
            continue

        missing = [k for k in ("device", "port", "action", "value") if k not in a]
        if missing:
            print(f"  [VALIDATE] reject {a}: missing required keys {missing}")
            continue

        device = a["device"]
        port   = a["port"]
        verb   = a["action"]
        value  = a["value"]

        # 2. Verb whitelist
        if verb not in _VALID_ACTIONS:
            print(f"  [VALIDATE] reject {device} p{port}: unknown action {verb!r} "
                  f"(allowed: {sorted(_VALID_ACTIONS)})")
            continue

        # 3. Port is int
        if not isinstance(port, int) or isinstance(port, bool):
            print(f"  [VALIDATE] reject {device}: port must be int, got "
                  f"{type(port).__name__} ({port!r})")
            continue

        # 4. Value type per verb
        if verb == "set_speed":
            # bool is a subclass of int in Python -- exclude it explicitly
            if not isinstance(value, int) or isinstance(value, bool):
                print(f"  [VALIDATE] reject {device} p{port} set_speed: value must "
                      f"be int 0-10, got {type(value).__name__} ({value!r})")
                continue
            if value < 0 or value > 10:
                print(f"  [VALIDATE] reject {device} p{port} set_speed: value "
                      f"{value} out of range [0, 10]")
                continue
        elif verb == "set_outlet":
            if not isinstance(value, bool):
                print(f"  [VALIDATE] reject {device} p{port} set_outlet: value must "
                      f"be bool, got {type(value).__name__} ({value!r})")
                continue

        # 5. Device + port existence (skipped if no snapshot)
        if dev_map:
            if device not in dev_map:
                known = sorted(dev_map.keys())
                print(f"  [VALIDATE] reject p{port}: unknown device {device!r} "
                      f"(known: {known})")
                continue
            ports = dev_map[device]
            if port not in ports:
                print(f"  [VALIDATE] reject {device}: port {port} not present in "
                      f"snapshot (have {sorted(ports.keys())})")
                continue

            # 6. Port type vs verb. Outlet ports have a "powered" key; speed
            #    ports have a "speed" key. (See build_snapshot.)
            port_info = ports[port]
            is_outlet = "powered" in port_info
            if verb == "set_speed" and is_outlet:
                print(f"  [VALIDATE] reject {device} p{port} set_speed: target is "
                      "an outlet port (use set_outlet)")
                continue
            if verb == "set_outlet" and not is_outlet:
                print(f"  [VALIDATE] reject {device} p{port} set_outlet: target is "
                      "a variable-speed port (use set_speed)")
                continue

        valid.append(a)

    return valid


def filter_actions(actions: list, snapshot: dict | None = None) -> list:
    """Apply safety rules to AI-proposed actions before they touch the API."""
    global _last_ph_time
    now = time.time()
    safe = []
    ph_used_this_cycle = False

    # Build a flat sensor dict from the snapshot for validation checks
    _snapshot_sensors: dict = {}
    if snapshot:
        for dev in snapshot.get("devices", []):
            _snapshot_sensors.update(dev.get("sensors", {}))

    # Reservoir health gates -- enforced deterministically, NOT advisory.
    # Conservative defaults: missing snapshot -> treat as HOLD/HOLD/HOLD.
    res_health = (snapshot or {}).get("res_health", {}) or {}
    dose_gate  = res_health.get("dose_gate", "HOLD")
    ph_gate    = res_health.get("ph_gate",   "HOLD")
    co2_gate   = res_health.get("co2_gate",  "HOLD")

    # Chemical-only freeze (manual kill via DOSING_DISABLED=true, or a fail-safe
    # auto-trip after a failed pump-stop). Blocks doser/pH ports only -- climate
    # (lights, fans, exhaust, CO2) is intentionally left running, since killing
    # ventilation/lighting is its own hazard. Stops stay allowed (is_safety_dir).
    dosing_off, dosing_reason = dosing_disable_status()

    for a in actions:
        key      = f"{a['device']}:{a['port']}"
        is_ph    = _is_ph_port(a["device"], a["port"])
        is_doser = _is_doser_port(a["device"], a["port"])
        is_co2   = _is_co2_valve(a["device"], a["port"])
        value    = a.get("value", 0)

        # Helper: "dosing direction" means value>0 for set_speed, True for set_outlet.
        # Stops (value==0 / False) are always allowed -- safety-direction actions.
        action_type   = a.get("action")
        is_stop_speed = (action_type == "set_speed"  and int(value or 0) == 0)
        is_outlet_off = (action_type == "set_outlet" and not bool(value))
        is_safety_dir = is_stop_speed or is_outlet_off

        # 0-. Chemical freeze: block any dosing-direction action on doser/pH ports.
        #     Climate ports are unaffected. Stops fall through (is_safety_dir).
        if dosing_off and (is_doser or is_ph) and not is_safety_dir:
            print(f"  [SAFETY] Blocked {key}: dosing disabled -- {dosing_reason}")
            continue

        # 0. Hard block: never dose pH ports without a live pH reading
        if is_ph and not is_safety_dir and "ph" not in _snapshot_sensors:
            print(f"  [SAFETY] Blocked pH {key}: no pH sensor reading -- cannot dose blind")
            continue

        # 0a. Reservoir pH gate -- block pH dosing when res is stressed
        if is_ph and not is_safety_dir and ph_gate == "HOLD":
            print(f"  [SAFETY] Blocked pH {key}: res_health.ph_gate=HOLD "
                  "(resolve water/EC issue first -- pH chasing during stress causes more harm)")
            continue

        # 0b. Reservoir dose gate -- block nutrient dosing (non-pH doser ports)
        #     when the res isn't in a state to accept nutrients
        if is_doser and not is_ph and not is_safety_dir and dose_gate in ("HOLD", "NONE"):
            print(f"  [SAFETY] Blocked nutrient {key}: res_health.dose_gate={dose_gate} "
                  "(plant not consuming correctly -- adding nutrients would worsen imbalance)")
            continue

        # 0c. CO2 gate -- block turning the CO2 enrichment valve ON when gate
        #     says HOLD or REDUCE. OFF is always allowed (safety direction).
        if is_co2 and not is_safety_dir and co2_gate in ("HOLD", "REDUCE"):
            print(f"  [SAFETY] Blocked CO2 valve ON {key}: res_health.co2_gate={co2_gate}")
            continue

        # 1. Per-port dose lockout -- only applies to doser ports (not fans/lights/outlets)
        if is_doser and key in _last_dose_time:
            remaining = _dose_lockout_sec() - (now - _last_dose_time[key])
            if remaining > 0:
                m, s = divmod(int(remaining), 60)
                print(f"  [SAFETY] Blocked {key}: lockout {m}m{s:02}s remaining")
                continue

        # 2. pH-specific lockout
        if is_ph:
            remaining = _ph_lockout_sec() - (now - _last_ph_time)
            if remaining > 0:
                m, s = divmod(int(remaining), 60)
                print(f"  [SAFETY] Blocked pH {key}: pH lockout {m}m{s:02}s remaining")
                continue
            if ph_used_this_cycle:
                print(f"  [SAFETY] Blocked {key}: only one pH adjustment per cycle")
                continue
            ph_used_this_cycle = True

        # 3. Speed cap -- only applied to doser ports.
        #    Per-port limit wins, then global mL ceiling.
        #    mL ceiling is skipped in RES_CHANGE_MODE for non-pH ports.
        if a.get("action") == "set_speed" and is_doser:
            speed    = int(value)
            port_cap = _port_max_speed(a["device"], a["port"])
            res_mode = os.getenv("RES_CHANGE_MODE", "").lower() == "true"
            if res_mode and not is_ph:
                cap = port_cap   # mL ceiling suspended for nutrient ports
            else:
                ml_cap = int(_max_dose_ml_cycle() / 21)
                cap    = min(port_cap, ml_cap)
            if speed > cap:
                print(f"  [SAFETY] Capped {key} speed {speed}->{cap} "
                      f"({speed*21} mL/min -> {cap*21} mL/min)")
                a = {**a, "value": cap}

        safe.append(a)

    return _apply_nutrient_ratio(safe)


def _apply_nutrient_ratio(actions: list) -> list:
    """
    Scale port 1 and port 2 set_speed actions to reflect NUTRIENT_RATIO_1/NUTRIENT_RATIO_2.

    The AI always requests equal speeds for both ports.  This step adjusts those speeds
    to hit the configured ratio after the safety gate has already applied caps.

    Hard limits: neither side can be less than 45 or more than 55 (out of 100).
    At low speeds (1-2) integer rounding keeps both ports equal -- the ratio is only
    meaningfully visible at speed 5+, which happens during res changes.
    """
    r1_cfg = int(os.getenv("NUTRIENT_RATIO_1", "50"))
    r2_cfg = int(os.getenv("NUTRIENT_RATIO_2", "50"))

    # Clamp each side to 45-55, then re-normalise so they sum to 100
    r1 = max(45, min(55, r1_cfg))
    r2 = max(45, min(55, r2_cfg))
    total = r1 + r2
    r1 = round(r1 * 100 / total)
    r2 = 100 - r1

    if r1 != r1_cfg or r2 != r2_cfg:
        print(f"  [RATIO] Clamped {r1_cfg}/{r2_cfg} -> {r1}/{r2} (hard limit 45-55 per side)")

    if r1 == 50 and r2 == 50:
        return actions  # fast path -- no adjustment needed

    result = list(actions)
    seen_devices = dict.fromkeys(
        a["device"] for a in result if a.get("action") == "set_speed"
    )

    for dev in seen_devices:
        # Only apply nutrient ratio to devices where ports 1 AND 2 are both dosers.
        # Prevents distorting fan/light writes on non-doser devices (e.g. "4 x 4").
        if not (_is_doser_port(dev, 1) and _is_doser_port(dev, 2)):
            continue

        idx1 = next((i for i, a in enumerate(result)
                     if a["device"] == dev and a.get("port") == 1
                     and a.get("action") == "set_speed"), None)
        idx2 = next((i for i, a in enumerate(result)
                     if a["device"] == dev and a.get("port") == 2
                     and a.get("action") == "set_speed"), None)

        if idx1 is None or idx2 is None:
            continue  # only apply when both nutrient ports are being set together

        base = max(int(result[idx1]["value"]), int(result[idx2]["value"]))
        if base == 0:
            continue

        s1 = max(1, round(base * r1 / 50))
        s2 = max(1, round(base * r2 / 50))
        s1 = min(s1, _port_max_speed(dev, 1))
        s2 = min(s2, _port_max_speed(dev, 2))

        if s1 != int(result[idx1]["value"]) or s2 != int(result[idx2]["value"]):
            print(f"  [RATIO] {dev} ports 1+2  {r1}/{r2}  "
                  f"speed {base}/{base} -> {s1}/{s2}")

        result[idx1] = {**result[idx1], "value": s1}
        result[idx2] = {**result[idx2], "value": s2}

    return result


def record_actions(actions: list):
    """Mark executed actions in lockout state. Only doser ports get a lockout --
    fans, lights, and outlets are free to be re-issued at any cadence.
    Persists state to disk so a restart preserves cooldown clocks."""
    global _last_ph_time
    now = time.time()
    mutated = False
    for a in actions:
        if not _is_doser_port(a["device"], a["port"]):
            continue
        key = f"{a['device']}:{a['port']}"
        _last_dose_time[key] = now
        mutated = True
        if _is_ph_port(a["device"], a["port"]):
            _last_ph_time = now
    if mutated:
        _save_lockouts()


# FloraFlex Full Tilt EC defaults (mS/cm) -- internal source of truth
_FLORAFLEX_EC = {
    "veg":   {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.2, 5: 1.2, 6: 1.2, 7: 1.2, 8: 1.2},
    "bloom": {1: 1.4, 2: 1.4, 3: 1.4, 4: 1.4, 5: 1.4, 6: 1.0, 7: 0.4, 8: 0.0},
}

# Dr. Bruce Bugbee (Utah State) CO2 profile defaults (ppm)
# Veg: ramp 800->1200 as plant establishes
# Early-mid bloom: 1200-1500 -- peak period maximizes flower development
# Late bloom: taper to 800 -- reduce stress, allow natural ripening
# Flush: ambient (~400), no enrichment needed
_BUGBEE_CO2 = {
    "veg":   {1: 800,  2: 900,  3: 1000, 4: 1200, 5: 1200, 6: 1200, 7: 1200, 8: 1200},
    "bloom": {1: 1200, 2: 1500, 3: 1500, 4: 1500, 5: 1200, 6: 1000, 7: 800,  8: 400},
}

# Stage-aware pH defaults (RDWC hydroponic optimal ranges)
# Veg favors slightly more acidic for N uptake; bloom shifts up for P/K availability.
# Week 8 bloom (flush) widens to push final cation/anion swing.
_PH_DEFAULTS = {
    "veg":   {1: (5.5, 6.0), 2: (5.5, 6.0), 3: (5.5, 6.0), 4: (5.5, 6.0),
              5: (5.5, 6.0), 6: (5.5, 6.0), 7: (5.5, 6.0), 8: (5.5, 6.0)},
    "bloom": {1: (5.8, 6.2), 2: (5.8, 6.2), 3: (5.8, 6.2), 4: (5.8, 6.2),
              5: (5.8, 6.2), 6: (5.8, 6.2), 7: (5.8, 6.2), 8: (6.0, 6.5)},
}


def _ppm_scale() -> int:
    """500 (Hanna/Eutech) or 700 (Truncheon/Bluelab) conversion factor."""
    return int(os.getenv("PPM_SCALE", "500"))


def _effective_week_stage() -> tuple[int, str]:
    """Current (week, stage) with EXTEND_<STAGE>_WEEK cap applied to the week.
    Calendar comes from grow_state -- this just clamps to the extend setting."""
    week, stage = current_grow_week_and_stage()
    extend_at = os.getenv(f"EXTEND_{stage.upper()}_WEEK")
    if extend_at:
        try:
            week = min(week, int(extend_at))
        except ValueError:
            pass
    return week, stage


def _get_ppm_target() -> int:
    """
    Return PPM target for the current grow week and stage.

    Lookup order:
      1. PPM_<STAGE>_WK<N> explicit override in .env
      2. FloraFlex EC default * PPM_SCALE
    """
    week, stage = _effective_week_stage()
    override = os.getenv(f"PPM_{stage.upper()}_WK{week}")
    if override is not None:
        return int(float(override))
    ec = _FLORAFLEX_EC.get(stage, _FLORAFLEX_EC["veg"]).get(week, 1.2)
    return int(ec * _ppm_scale())


def _get_co2_target() -> int:
    """
    Return CO2 target (ppm) for the current grow week and stage.
    Checks CO2_<STAGE>_WK<N> in .env first; falls back to Bugbee defaults.
    """
    week, stage = _effective_week_stage()
    override = os.getenv(f"CO2_{stage.upper()}_WK{week}")
    if override is not None:
        return int(float(override))
    return _BUGBEE_CO2.get(stage, _BUGBEE_CO2["veg"]).get(week, 1200)


def _get_ph_range() -> tuple[float, float]:
    """
    Return (ph_min, ph_max) for the current grow week and stage.

    Lookup order (per side):
      1. PH_MIN_<STAGE>_WK<N> / PH_MAX_<STAGE>_WK<N> explicit override
      2. PH_MIN / PH_MAX legacy global override (only if set)
      3. _PH_DEFAULTS[stage][week] stage-driven default
    """
    week, stage = _effective_week_stage()
    d_min, d_max = _PH_DEFAULTS.get(stage, _PH_DEFAULTS["veg"]).get(week, (5.5, 6.5))

    def _resolve(side: str, default: float) -> float:
        wk_key = os.getenv(f"PH_{side}_{stage.upper()}_WK{week}", "").strip()
        if wk_key:
            try:
                return float(wk_key)
            except ValueError:
                pass
        legacy = os.getenv(f"PH_{side}", "").strip()
        if legacy:
            try:
                return float(legacy)
            except ValueError:
                pass
        return default

    return (_resolve("MIN", d_min), _resolve("MAX", d_max))


def _build_system_prompt() -> str:
    """Build the AI system prompt from .env target ranges — edit .env, not this function."""
    def _r(key, default): return os.getenv(key, default)
    week, stage = current_grow_week_and_stage()
    ph_min, ph_max = _get_ph_range()

    return f"""You are the reasoning engine for an automated RDWC (Recirculating Deep Water Culture) hydroponic grow system.

You receive a JSON snapshot of all sensor readings and device states every poll cycle.
Your job is to analyze conditions and decide what adjustments, if any, need to be made.

You must respond ONLY with a valid JSON object in this exact format:
{{
  "assessment": "one sentence summary of current conditions",
  "concerns": ["plain English description of each issue"],
  "actions": [
    {{
      "device": "device name as shown in snapshot",
      "port": <port number as integer>,
      "action": "set_speed" or "set_outlet",
      "value": <0-10 for set_speed, true/false for set_outlet>,
      "reason": "why"
    }}
  ],
  "next_check_seconds": <how long until you want another reading, 30-300>
}}

CRITICAL -- DEVICE vs PORT NAME:
The `device` field in your actions MUST be the value of `devices[].name` from the
snapshot (e.g. "4 x 4", "Hydroponics Control", "Auxiliary Outputs"). It is NEVER
the value of `devices[].ports[].name` (port labels like "Floraflex V/B1", "PH UP",
"Growcraft X6"). Port labels describe what is plugged into a port -- they are not
addressable. Always use the parent device's `name`. Wrong device names are silently
dropped by the executor.

If no action is needed, return an empty actions list.
Never recommend more than one dose of any nutrient per cycle.
Never recommend pH adjustment and nutrient dosing in the same cycle.
Always err on the side of doing less -- small adjustments only.

SCHEDULE ENFORCEMENT -- read snapshot.schedule_deltas FIRST.
The snapshot contains a `schedule_deltas` array listing every schedule-driven
output whose actual state does not match what it should be right now. For EACH
delta, you MUST include an action in your `actions` array matching the delta:
  - device           = delta.device
  - port             = delta.port
  - action           = delta.action       ("set_speed" or "set_outlet")
  - value            = delta.expected_value (int 0-10 for set_speed; bool for set_outlet)
These corrections are not optional and must come before any sensor-driven
recommendations.

If `schedule_deltas` is empty, all schedule outputs are already correct -- do
not issue redundant actions for the light, oscillating fans, or CO2 valve.
The snapshot also exposes `expected.light` / `expected.osc_fans` / `co2_target`
for context.

CO2 IS HANDLED DETERMINISTICALLY -- do not propose CO2 valve actions yourself.
A hysteresis-band pulse modulator runs every cycle and turns the valve on when
co2_ppm < (co2_target - band) and off when > (co2_target + band). It also
respects res_health.co2_gate (HOLD/REDUCE force OFF). If a CO2 valve correction
appears in schedule_deltas, copy it through as instructed above -- don't
override or omit it.

CO2 EMERGENCY -- if snapshot.co2_emergency is present, the system is in an
active CO2 dump: the valve has been forced OFF and the exhaust is at max,
deterministically. DO NOT propose actions that re-enable the CO2 valve, lower
the exhaust, or otherwise contradict the dump. The emergency clears when
co2_ppm falls below the configured threshold (also shown in the block).
Your only role during a CO2 emergency is to acknowledge it in your assessment.

RESERVOIR HEALTH GATES -- the res is the anchor for ALL decisions. Read snapshot.res_health first.
The gates override the schedule. Do not let the calendar run the plant -- let the plant run the calendar.

res_health.co2_gate rules:
  ADVANCE -- plant is actively eating and drinking; push CO2 toward the week's scheduled target
  HOLD    -- plant activity unclear or suboptimal; maintain current CO2, do not increase
  REDUCE  -- plant stressed or stalled; suggest reducing CO2 by 10-15% to ease transpiration load

res_health.dose_gate rules:
  NORMAL  -- dose nutrients to maintain PPM target as scheduled
  HOLD    -- do not dose; plant not consuming correctly, adding nutrients will worsen the imbalance
  NONE    -- absolutely no nutrients; plant is stressed, EC is already problematic

res_health.ph_gate rules:
  ALLOW   -- pH adjustment is permitted if pH is outside target range
  HOLD    -- do not adjust pH; resolve water/EC issue first, pH chasing during stress causes more harm

Week advancement note: only suggest advancing grow_week in your assessment if res_health.state
is IDEAL or GOOD for at least the past 2 cycles. If state is STALL, STRESS, or PROBLEM, explicitly
flag that week advancement should be delayed until res health recovers.

DWC diagnostic rules -- always evaluate WATER LEVEL + EC + PH together as a trend, not individually.
STATIC means unchanged since last cycle. RISING/FALLING means measurable movement in that direction.

WATER  EC       PH       DIAGNOSIS / ACTION
STATIC STATIC   STATIC   Plant not feeding. Usually lower EC slightly to stimulate uptake.
STATIC STATIC   RISING   pH buffers raising pH. Lower EC slightly or change res.
STATIC STATIC   FALLING  Media rinsed at low pH, or excess CO2. Change res, check air source.
STATIC RISING   STATIC   Plant leeching nutrition. Raise EC.
STATIC RISING   RISING   Plant leeching nutrition (unusual). Rising pH likely from alkaline nutes leeching back.
STATIC RISING   FALLING  As above -- watch for acid rain effect. Res change + raise EC.
STATIC FALLING  STATIC   Plant eating but not drinking. Lower EC or change res.
STATIC FALLING  RISING   Lower EC slightly. Rising pH is a good sign here.
STATIC FALLING  FALLING  pH and EC falling with no water drop -- likely acid rain effect. Lower EC after res change.
FALLING STATIC  STATIC   Perfect conditions. EC and pH are correct.
FALLING STATIC  RISING   Normal. Nothing to worry about unless other symptoms appear.
FALLING STATIC  FALLING  Change res. Lower EC if over 1.4, raise if under 1.0.
FALLING RISING  STATIC   Plant drinking more than eating. Lower EC.
FALLING RISING  RISING   Plant drinking more than eating. Lower EC.
FALLING RISING  FALLING  Plant drinking more than eating. Lower EC. Res change for possible acid rain.
FALLING FALLING STATIC   Hungry plant. Raise EC. Good situation -- nute buffers working.
FALLING FALLING RISING   Almost perfect. Raise EC slightly.
FALLING FALLING FALLING  Res change. Possible acid rain but plant still eating and drinking. Raise EC on new res.

Use these rules to inform your assessment and actions. When trends are unclear (only 1-2 cycles of data),
note it as a concern but do not act yet -- wait for the trend to confirm.

For next_check_seconds:
- Use {_r("POLL_INTERVAL_ACTIVE","60")} if you just acted or are watching an adjustment settle
- Use {_r("POLL_INTERVAL_STABLE","900")} if all readings are stable and no action needed
- You may request shorter than {_r("POLL_INTERVAL_ACTIVE","60")} only if a reading is critically out of range

Target ranges (keep all readings within these):
- pH:         {ph_min} - {ph_max}   (stage-driven default for {stage} wk{week})
- Water temp: {_r("WATER_TEMP_MIN","65")} - {_r("WATER_TEMP_MAX","72")} F
- Air temp:   {_r("AIR_TEMP_MIN","70")} - {_r("AIR_TEMP_MAX","85")} F
- Humidity:   {_r("HUMIDITY_MIN","50")} - {_r("HUMIDITY_MAX","70")} %
- VPD:        {_r("VPD_MIN","0.8")} - {_r("VPD_MAX","1.5")} kPa
- CO2:        target {_get_co2_target()} ppm  (+/-{_r("CO2_TOLERANCE","100")} ppm)  [Bugbee profile]
              Only reach this target if res_health.co2_gate == ADVANCE.
              If HOLD: maintain current CO2 reading.
              If REDUCE: target current reading minus 10-15%.

Nutrient system: FloraFlex Full Tilt (RDWC).
Doser ports: 1=nutrient A (V1/B1), 2=nutrient B (V2/B2), 3=pH UP, 4=pH DOWN.
ALWAYS request ports 1 and 2 at the same speed -- the system applies the configured
strain ratio automatically. Never request one without the other.

PPM scale in use: {_ppm_scale()} (500=Hanna/Eutech, 700=Truncheon/Bluelab)
Target PPM for grow week {week} {stage}: {_get_ppm_target()} PPM
PPM tolerance: +/-{int(float(_r("EC_TOLERANCE","0.1")) * _ppm_scale())} PPM

Use tds_ppm from the snapshot as the primary nutrient concentration reading.
TDS dosing rules:
- tds_ppm below (target - tolerance): dose ports 1 and 2 together at equal speed 1-2.
  Prefer speed 1 -- small adjustments prevent overshoot. Speed 2 only if well below target.
- tds_ppm above (target + tolerance): do NOT dose -- wait for plant uptake
- Bloom week 8: FLUSH only, do not dose nutrients under any circumstances
- Never dose nutrients and pH in the same cycle
- If TDS is within tolerance but pH is off, address pH only

pH correction rules -- pH takes priority over nutrients when outside hard limits:
- pH below {ph_min}: REQUIRED action -- dose port 3 (pH UP) at speed 1 only.
  Do not overshoot. One small dose per cycle; the lockout enforces the wait.
- pH above {ph_max}: REQUIRED action -- dose port 4 (pH DOWN) at speed 1 only.
  Do not overshoot. One small dose per cycle; the lockout enforces the wait.
- pH within {ph_min}-{ph_max} and drifting: do NOT dose -- let it swing naturally.
- Never recommend a pH action in the same cycle as a nutrient action.
- Only valid action types are "set_speed" (ports, speed 0-10) and "set_outlet" (outlets, true/false).
  Do not invent other action types.
"""


def warmup():
    """Load the model into VRAM before the first real inference call."""
    try:
        print(f"[AI] Loading {OLLAMA_MODEL} into VRAM...", flush=True)
        requests.post(f"{OLLAMA_HOST}/api/chat", json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "ready"}],
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 1, "num_ctx": 4096},
        }, timeout=240)
        print("[AI] Model ready.", flush=True)
    except Exception as e:
        print(f"[AI] Warmup failed: {e}")


# Last-cycle sensor readings for trend detection (keyed by sensor label)
_prev_sensors: dict[str, float] = {}

# Minimum delta to call a reading RISING or FALLING rather than STATIC
_TREND_THRESHOLDS = {
    "ph":          0.05,
    "ec_ms":       0.05,
    "ec_us":       5.0,
    "tds_ppm":     10.0,
    "water_level": 1.0,
}

def _trend(label: str, current: float) -> str:
    prev = _prev_sensors.get(label)
    if prev is None:
        return "UNKNOWN"
    thresh = _TREND_THRESHOLDS.get(label, 0.0)
    delta  = current - prev
    if delta > thresh:
        return "RISING"
    if delta < -thresh:
        return "FALLING"
    return "STATIC"


def res_health_check(trends: dict) -> dict:
    """
    Evaluate reservoir health from trend data and return a structured gate.

    Everything revolves around the res:
      water_level trend = is the plant transpiring/drinking?
      ec/tds trend      = is the plant eating?

    Returns:
      state      -- IDEAL / GOOD / WATCH / STALL / STRESS / UNKNOWN
      co2_gate   -- ADVANCE / HOLD / REDUCE
      dose_gate  -- NORMAL / HOLD / NONE
      ph_gate    -- ALLOW / HOLD  (never adjust pH when res is stressed)
      summary    -- one plain-English sentence for the AI to include in assessment
    """
    water = trends.get("water_level", "UNKNOWN")
    ec    = trends.get("ec_ms") or trends.get("ec_us") or trends.get("tds_ppm")
    ec    = ec if ec else "UNKNOWN"
    ph    = trends.get("ph", "UNKNOWN")

    # Map water + EC combination to health state
    if water == "FALLING" and ec in ("FALLING", "STATIC"):
        state    = "IDEAL"
        co2_gate = "ADVANCE"   # plant active -- OK to push CO2 toward week target
        dose_gate = "NORMAL"   # dose to maintain PPM target
        ph_gate  = "ALLOW"
        summary  = "Plant eating and drinking -- res healthy, advance parameters as scheduled."

    elif water == "FALLING" and ec == "RISING":
        state    = "WATCH"
        co2_gate = "HOLD"      # drinking more than eating -- don't stress further
        dose_gate = "HOLD"     # EC already rising, do NOT add nutrients
        ph_gate  = "ALLOW"
        summary  = "Plant drinking more than eating -- hold CO2, do not dose nutrients, lower EC if rising fast."

    elif water == "STATIC" and ec == "STATIC":
        state    = "STALL"
        co2_gate = "HOLD"      # plant not active -- no point enriching CO2
        dose_gate = "HOLD"     # don't add nutrients to a stalled plant
        ph_gate  = "HOLD"
        summary  = "Plant not eating or drinking -- hold all parameters, investigate environment (temp, VPD, light)."

    elif water == "STATIC" and ec == "FALLING":
        state    = "WATCH"
        co2_gate = "HOLD"      # eating but not drinking -- unusual
        dose_gate = "NORMAL"   # EC falling means it IS consuming, OK to top up
        ph_gate  = "ALLOW"
        summary  = "Plant eating but not drinking -- hold CO2, monitor closely, check water temp and root zone."

    elif water == "STATIC" and ec == "RISING":
        state    = "STRESS"
        co2_gate = "REDUCE"    # not drinking + EC building = stressed plant
        dose_gate = "NONE"     # absolutely no nutrients
        ph_gate  = "HOLD"
        summary  = "Stress state -- plant not drinking, EC building. Reduce CO2, do NOT dose, check roots and water temp."

    elif water == "RISING":
        state    = "PROBLEM"
        co2_gate = "REDUCE"
        dose_gate = "NONE"
        ph_gate  = "HOLD"
        summary  = "Water level rising -- plant not consuming. Do NOT dose. Check pumps, water temp, root health."

    else:
        state    = "UNKNOWN"
        co2_gate = "HOLD"      # no trend data yet (first cycle) -- conservative
        dose_gate = "HOLD"
        ph_gate  = "ALLOW"
        summary  = "Trend data not yet available -- holding parameters until baseline is established."

    return {
        "state":     state,
        "co2_gate":  co2_gate,
        "dose_gate": dose_gate,
        "ph_gate":   ph_gate,
        "water_trend": water,
        "ec_trend":    ec,
        "ph_trend":    ph,
        "summary":   summary,
    }


_leak_wet_streak = 0  # consecutive cycles the boolean leak sensor has read wet


def _res_burst_enabled() -> bool:
    return os.getenv("RES_BURST_ENABLED", "").strip().lower() == "true"


def _res_burst_debounce() -> int:
    """Consecutive WET reads before a leak is 'confirmed' (min 1). Guards against a
    single glitchy reading nuisance-tripping the response. Set 1 to act on first wet."""
    try:
        return max(1, int(os.getenv("RES_BURST_DEBOUNCE", "2")))
    except ValueError:
        return 2


def _assess_leak(all_sensors: dict) -> dict:
    """Debounced boolean leak assessment from `water_leak` (0=dry, nonzero=wet).
    Manages the cross-cycle wet streak in ONE place so every consumer (res-burst,
    evac pump) shares the same confirmed state. Returns {raw, wet, confirmed, streak}.
    Must be called exactly once per cycle -- build_snapshot does this."""
    global _leak_wet_streak
    raw = all_sensors.get("water_leak")
    if raw is None or float(raw) == 0.0:
        _leak_wet_streak = 0
        return {"raw": raw, "wet": False, "confirmed": False, "streak": 0}
    _leak_wet_streak += 1
    return {"raw": raw, "wet": True,
            "confirmed": _leak_wet_streak >= _res_burst_debounce(),
            "streak": _leak_wet_streak}


def compute_res_burst(snapshot: dict) -> dict | None:
    """When a leak is CONFIRMED wet (see _assess_leak) and RES_BURST_ENABLED, return a
    WATER/CHEMICAL-ONLY shutdown: stop every doser/pH port + close the CO2 valve.
    Lights and ventilation are NEVER cut -- there is deliberately no full-power kill.
    Reads snapshot['leak']; never touches water_level or the manual WATER_LEVEL_TREND.
    """
    if not _res_burst_enabled():
        return None
    leak = snapshot.get("leak") or {}
    if not leak.get("confirmed"):
        return None

    # Shutdown scope: WATER/CHEMICAL ONLY. Stop every doser/pH port and close the CO2
    # valve. Lights, exhaust, and fans are deliberately never enumerated or commanded.
    actions: list[dict] = []
    for dev in snapshot.get("devices", []):
        name = dev.get("name")
        for p in dev.get("ports", []):
            port = p.get("port")
            if _is_doser_port(name, port):
                actions.append({"device": name, "port": port, "action": "set_speed",
                                "value": 0, "reason": "res burst -- stop doser/pH pump"})
            elif _is_co2_valve(name, port):
                actions.append({"device": name, "port": port, "action": "set_outlet",
                                "value": False, "reason": "res burst -- close CO2 valve"})

    return {
        "active":     True,
        "water_leak": leak.get("raw"),
        "streak":     leak.get("streak"),
        "actions":    actions,
        "reason":     f"leak sensor wet ({leak.get('streak')} consecutive reads) "
                      "-- reservoir leak/burst; stop dosers + close CO2 "
                      "(lights/ventilation left running)",
    }


def _evac_pump_target() -> tuple[str, int] | None:
    """Parse EVAC_PUMP=<device>:<port>. Returns (device, port) or None if unset/blank."""
    raw = os.getenv("EVAC_PUMP", "").strip()
    if not raw or ":" not in raw:
        return None
    dev, _, port = raw.rpartition(":")
    try:
        return dev.strip(), int(port)
    except ValueError:
        return None


def compute_evac_pump(snapshot: dict) -> dict | None:
    """Evac pump tracks the leak sensor: ON once a leak is CONFIRMED wet, OFF when dry.
    Gated by EVAC_PUMP=<device>:<port> (no config -> no action). Returns a set_outlet
    delta ONLY when the desired state differs from the pump's current powered state, so
    it never spams writes or runs the pump dry. Not a doser -- not subject to the
    dosing freeze; water removal is always desirable during a leak."""
    tgt = _evac_pump_target()
    if not tgt:
        return None
    device, port = tgt
    leak = snapshot.get("leak") or {}
    if leak.get("raw") is None:
        return None  # no leak sensor reading -- do not command the pump blind
    desired_on = bool(leak.get("confirmed"))   # ON after debounced wet; OFF when dry
    current = None
    for dev in snapshot.get("devices", []):
        if dev.get("name") != device:
            continue
        for p in dev.get("ports", []):
            if p.get("port") == port:
                current = p.get("powered")
    if current is not None and bool(current) == desired_on:
        return None  # already in the desired state -- no redundant write
    return {"device": device, "port": port, "action": "set_outlet", "value": desired_on,
            "reason": ("leak confirmed -- evac pump ON" if desired_on
                       else "leak clear -- evac pump OFF")}


def build_snapshot(devices: list[dict]) -> dict:
    """Build a clean sensor + state snapshot to send to the model."""
    week, stage = current_grow_week_and_stage()
    snapshot = {
        "timestamp":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "grow_week":  week,
        "grow_stage": stage,
        "devices":    [],
    }
    for dev in devices:
        entry = {
            "name": dev["name"],
            "type": dev["type_label"],
            "online": dev["online"],
            "sensors": {},
            "ports": [],
        }
        # Air sensors — labels are device-specific (read from labels.env)
        slug = name_slug(dev["name"])
        hide_air     = os.getenv(f"HIDE_AIR_{slug}",         "").lower() == "true"
        hide_builtin = os.getenv(f"HIDE_AIR_BUILTIN_{slug}", "").lower() == "true"
        hide_ext     = os.getenv(f"HIDE_AIR_EXT_{slug}",     "").lower() == "true"
        if not hide_air:
            # Default labels include slug suffix to prevent collision across devices
            air_label  = os.getenv(f"AIR_LABEL_{slug}",  f"air_{slug}").lower().replace(" ", "_")
            air2_label = os.getenv(f"AIR2_LABEL_{slug}", f"air_z2_{slug}").lower().replace(" ", "_")
            builtin_keys = [
                ("temp_f",       f"temp_f_{air_label}"),
                ("humidity_pct", f"humidity_{air_label}"),
                ("vpd_kpa",      f"vpd_{air_label}"),
            ]
            ext_keys = [
                ("temp_f_ext",   f"temp_f_{air2_label}"),
                ("humidity_ext", f"humidity_{air2_label}"),
            ]
            for key, label in ([] if hide_builtin else builtin_keys) + ([] if hide_ext else ext_keys):
                if dev.get(key) is not None:
                    entry["sensors"][label] = dev[key]
        # Environment sensors — always included regardless of hide_air
        for key, label in [("co2_ppm", "co2_ppm"), ("light", "light")]:
            if dev.get(key) is not None:
                entry["sensors"][label] = dev[key]
        # Hydro sensors. The leak sensor and the reservoir-level sensor are BOTH
        # sensorType=20 (parsed as water_level) but live on different devices, so
        # they must be split or they collide on the cross-device merge. The device
        # named by LEAK_SENSOR exposes its water reading as the boolean `water_leak`
        # (feeds res-burst); any other device's water_level is the reservoir level
        # (feeds res_health). Default leak device: "Auxiliary Outputs".
        leak_device = os.getenv("LEAK_SENSOR", "Auxiliary Outputs").strip()
        for key in ("ph", "tds_ppm", "ec_us", "ec_ms", "water_temp_f", "water_level"):
            if dev.get(key) is None:
                continue
            if key == "water_level" and dev["name"] == leak_device:
                entry["sensors"]["water_leak"] = dev[key]
            else:
                entry["sensors"][key] = dev[key]
        # Ports
        for p in dev.get("ports", []):
            if not p["online"]:
                continue
            port_entry = {"port": p["port"], "name": p["name"]}
            if p.get("is_outlet"):
                port_entry["powered"] = p.get("powered")
            else:
                port_entry["speed"] = p.get("speed_actual")
                port_entry["mode"] = p.get("mode")
            entry["ports"].append(port_entry)
        snapshot["devices"].append(entry)

    # Collect all sensors and compute trends
    all_sensors: dict[str, float] = {}
    for dev in snapshot["devices"]:
        all_sensors.update(dev.get("sensors", {}))

    # Debounced leak assessment (single source of truth for res-burst + evac pump),
    # plus the evac-pump delta (ON when leak confirmed wet, OFF when dry).
    snapshot["leak"] = _assess_leak(all_sensors)
    evac = compute_evac_pump(snapshot)
    if evac:
        snapshot["evac_pump"] = evac

    trends = {}
    for label in ("ph", "ec_ms", "ec_us", "tds_ppm"):
        if label in all_sensors:
            trends[label] = _trend(label, all_sensors[label])

    # Water-level trend. Source priority:
    #   1. Boolean magnetic float (WATER_LEVEL_FLOAT=true) -- a real sensor, manually
    #      repositioned each day to the expected drawdown line. dry(0) = water has
    #      fallen below today's line -> FALLING; wet(nonzero) = still at/above it ->
    #      STATIC. A single float cannot see RISING; use the manual override for that.
    #      _trend is NOT used here -- a 0<->1 delta never clears the 1.0 threshold.
    #   2. Manual WATER_LEVEL_TREND override (FALLING/STATIC/RISING).
    #   3. Analog/depth sensor via _trend (future ultrasonic).
    float_mode = os.getenv("WATER_LEVEL_FLOAT", "").strip().lower() == "true"
    manual_wl  = os.getenv("WATER_LEVEL_TREND", "").strip().upper()
    if float_mode and "water_level" in all_sensors:
        trends["water_level"] = "STATIC" if float(all_sensors["water_level"]) != 0 else "FALLING"
        snapshot["water_level_source"] = "FLOAT"
    elif manual_wl in ("FALLING", "STATIC", "RISING"):
        trends["water_level"] = manual_wl
        snapshot["water_level_source"] = "MANUAL"
    elif "water_level" in all_sensors:
        trends["water_level"] = _trend("water_level", all_sensors["water_level"])
        snapshot["water_level_source"] = "SENSOR"
    else:
        # No sensor and no manual override -- gate will see UNKNOWN and hold
        snapshot["water_level_source"] = "MISSING"

    if trends:
        snapshot["trends"] = trends

    # Active reservoir volume -- anchors dose-size math (mL -> ppm / pH shift).
    # Surfaced in the snapshot so the AI computes dose impact against real volume.
    snapshot["reservoir_volume_gal"] = _reservoir_volume_gal()

    # Reservoir health gate -- anchor for all parameter decisions
    snapshot["res_health"] = res_health_check(trends)

    # Schedule enforcement -- what schedule-driven outputs should be doing
    # right now, and which ones are not matching. The AI is told to correct
    # any deltas; the poller has a deterministic fallback for misses.
    snapshot["expected"] = {
        "light":    expected_light_state(),
        "osc_fans": expected_osc_fan_state(),
    }
    # CO2 target -- per-week Bugbee value, used by the pulse modulator.
    snapshot["co2_target"] = _get_co2_target()

    snapshot["schedule_deltas"] = compute_schedule_deltas(snapshot)

    # Reservoir burst -- computed BEFORE CO2 modulation so the pulse can defer to it.
    # The only full-stop trigger, and even it is water/chemical only (lights +
    # ventilation never cut). Inert unless RES_BURST_ENABLED + a wet leak sensor.
    rb = compute_res_burst(snapshot)
    if rb:
        snapshot["res_burst"] = rb

    # CO2 emergency dump -- evaluated AFTER schedule deltas so AI sees both.
    # When active, deterministic enforcement in poller forces these actions
    # regardless of what the AI proposes.
    co2_em = compute_co2_emergency(snapshot)
    if co2_em:
        snapshot["co2_emergency"] = co2_em
    elif not snapshot.get("res_burst"):
        # CO2 pulse modulator -- runs only when no emergency AND no res burst is
        # active. During a burst the valve is force-closed; the pulse must not
        # reopen it in the same cycle (schedule fallback runs after the burst close).
        pulse_delta = compute_co2_pulse(snapshot)
        if pulse_delta:
            snapshot["schedule_deltas"].append(pulse_delta)

    # Update previous readings for next cycle
    _prev_sensors.update(all_sensors)

    return snapshot


def ask_ai(snapshot: dict) -> dict | None:
    """Send sensor snapshot to Ollama and return parsed action dict."""
    from profile_manager import get_historical_context, get_calibration_context
    history     = get_historical_context()
    calibration = get_calibration_context()

    context_block = ""
    if calibration:
        context_block += f"\n\n--- System Calibration ---\n{calibration}\n--- End Calibration ---"
    if history:
        context_block += f"\n\n--- Strain History ---\n{history}\n--- End History ---"

    prompt = (
        "Here is the current grow system snapshot:\n\n"
        + json.dumps(snapshot, indent=2)
        + context_block
        + "\n\nAnalyze conditions and respond with the JSON action object."
    )
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": _build_system_prompt()},
                    {"role": "user",   "content": prompt},
                ],
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "num_ctx":     4096,
                    "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "1200")),
                },
            },
            timeout=240,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]

        # Strip reasoning-model <think>...</think> blocks if present.
        # Qwen2.5 doesn't emit these; DeepSeek-R1 distills (and similar reasoning
        # models) do. Harmless when absent.
        if "<think>" in raw:
            raw = raw[raw.rfind("</think>") + len("</think>"):].strip()

        # Extract JSON from response
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            print(f"[AI] No JSON found in response:\n{raw}")
            return None
        return json.loads(raw[start:end])

    except json.JSONDecodeError as e:
        print(f"[AI] JSON parse error: {e}\nRaw: {raw}")
        return None
    except Exception as e:
        print(f"[AI] Error: {e}")
        return None


def print_advice(result: dict):
    """Print the AI's assessment and proposed actions."""
    print(f"\n  [AI] {result.get('assessment', '')}")
    concerns = result.get("concerns", [])
    if concerns:
        for c in concerns:
            print(f"  [!]  {c}")
    actions = result.get("actions", [])
    if not actions:
        print("  [AI] No actions needed.")
        return
    mode = "ADVISORY" if _advisory_mode() else "LIVE"
    for a in actions:
        print(f"  [{mode}] {a['device']} port {a['port']} -> "
              f"{a['action']}={a['value']}  ({a['reason']})")


def _verify_writes_enabled(token) -> bool:
    """Read-after-write verification is on by default. Skipped for the SIM sentinel
    and when VERIFY_WRITES=false (test rigs / when readback churn isn't wanted)."""
    if token == "SIM":
        return False
    return os.getenv("VERIFY_WRITES", "true").strip().lower() != "false"


def _verify_executed_action(token, dev: dict, a: dict) -> dict:
    """Poll readback to confirm a write physically took effect (Level 2 verification).
    For a doser/pH STOP that can't be verified, retry the stop once and -- if it still
    won't confirm -- FREEZE dosing: a pump that won't stop is a critical chemical
    hazard. Climate is untouched. Returns the verification result dict."""
    from ac_infinity_client import (verify_port_state, ramp_seconds,
                                     set_port_speed, ACInfinityAuthError)
    device, port, act, val = a["device"], a["port"], a["action"], a.get("value")
    is_chem = _is_doser_port(device, port) or _is_ph_port(device, port)

    if act == "set_outlet":
        expected, timeout = {"powered": bool(val)}, 15.0
    else:  # set_speed
        expected = {"speed_actual": int(val), "tolerance": 0 if is_chem else 1}
        timeout = ramp_seconds(int(val), 10) + 10.0

    try:
        res = verify_port_state(token, dev["dev_id"], port, expected, timeout_sec=timeout)
    except ACInfinityAuthError:
        print(f"  [VERIFY] auth failure verifying {device} port {port} -- poller will re-auth")
        return {"ok": False, "reason": "auth_failed"}

    if res["ok"]:
        print(f"  [VERIFY] {device} port {port} {act}={val} -> verified ({res['elapsed_sec']}s)")
        return res
    print(f"  [VERIFY] {device} port {port} {act}={val} -> UNVERIFIED: {res['reason']}")

    is_stop = (act == "set_speed" and int(val or 0) == 0) or (act == "set_outlet" and not bool(val))
    if is_chem and is_stop:
        print(f"  [VERIFY] CRITICAL: doser/pH stop unverified on {device} port {port} -- retrying stop")
        try:
            set_port_speed(token, dev["dev_id"], port, 0, dev["type"])
        except Exception as e:
            print(f"  [VERIFY] retry stop write failed: {e}")
        try:
            res2 = verify_port_state(token, dev["dev_id"], port,
                                     {"speed_actual": 0, "tolerance": 0},
                                     timeout_sec=ramp_seconds(0, 10) + 10.0)
        except ACInfinityAuthError:
            res2 = {"ok": False, "reason": "auth_failed", "observed": None}
        if res2["ok"]:
            print(f"  [VERIFY] retry stop verified ({res2['elapsed_sec']}s)")
            return res2
        obs = (res2.get("observed") or {}).get("speed_actual")
        disable_dosing(f"doser/pH stop NOT verified on {device} port {port} "
                       f"(observed speed {obs}) -- pump may still be running")
        print(f"  [!!! VERIFY CRITICAL !!!] {device} port {port} still not stopped -- DOSING FROZEN")
        return res2
    return res


def execute_actions(result: dict, devices: list[dict], token: str, snapshot: dict | None = None):
    """Validate -> safety-gate -> execute approved actions via the AC Infinity API.
    Each successful write is read-after-write verified (unless disabled / SIM)."""
    from ac_infinity_client import set_port_speed, set_outlet

    proposed  = result.get("actions", [])
    validated = validate_actions(proposed, snapshot=snapshot)
    if len(validated) != len(proposed):
        rejected = len(proposed) - len(validated)
        print(f"  [VALIDATE] {rejected} action(s) rejected by schema validation")

    safe_actions = filter_actions(validated, snapshot=snapshot)
    if len(validated) != len(safe_actions):
        blocked = len(validated) - len(safe_actions)
        print(f"  [SAFETY] {blocked} action(s) blocked by safety gate")

    dev_map = {d["name"]: d for d in devices}
    verify_enabled = _verify_writes_enabled(token)
    executed = []
    for a in safe_actions:
        dev = dev_map.get(a["device"])
        if not dev:
            print(f"  [EXEC] Unknown device '{a['device']}' -- skipping")
            continue
        try:
            if a["action"] == "set_outlet":
                set_outlet(token, dev["dev_id"], a["port"], bool(a["value"]), dev["type"])
            elif a["action"] == "set_speed":
                set_port_speed(token, dev["dev_id"], a["port"],
                               int(a["value"]), dev["type"])
            else:
                print(f"  [EXEC] Unknown action '{a['action']}' on {a['device']} "
                      f"port {a['port']} -- skipped")
                continue
            print(f"  [EXEC] {a['device']} port {a['port']} -> {a['action']}={a['value']}")
            executed.append(a)
        except Exception as e:
            print(f"  [EXEC] Failed {a['device']} port {a['port']}: {e}")
            continue
        if verify_enabled:
            _verify_executed_action(token, dev, a)

    if executed:
        record_actions(executed)
