"""
Strain profile manager.

Three things live here:
  1. Per-cycle sensor + action logging (keyed by strain / run / week / stage)
  2. Outcome tracking — measures what each action actually did N cycles later
  3. Calibration context — summarises dose-response data for AI prompt injection

The model can't update its own weights at runtime, but injecting calibration
data lets it compute instead of guess: "I need pH +0.3; speed-2 gives +0.28 ->
use speed 2."  Every observed outcome makes the next decision more accurate.
"""

import json
import os
import time
from pathlib import Path

from grow_state import current_grow_week_and_stage

PROFILES_DIR = Path(__file__).parent / "profiles"
_PENDING_FILE = PROFILES_DIR / ".pending_outcomes.json"

MAX_CYCLES_PER_WEEK  = 96   # ~48 h of history at 30-min poll
MAX_CAL_OBSERVATIONS = 30   # rolling window per action type
MIN_CAL_OBSERVATIONS = 2    # require this many before trusting an average

# Sensors we track dose-response for
_TRACKED_SENSORS = ["ph", "ec_ms", "ec_us", "tds_ppm", "water_temp_f"]


# --- Dynamic config readers (re-read .env every call) ---

def _strain_name() -> str:
    return os.getenv("STRAIN_NAME", "").strip()

def _run_id() -> str:
    return os.getenv("RUN_ID", "run_1").strip()

def _outcome_wait_sec() -> int:
    """Wait window before reading an action's outcome.
    Calibrated against ACTIVE polling interval since actions trigger ACTIVE mode."""
    return (int(os.getenv("OUTCOME_WAIT_CYCLES", "2"))
            * int(os.getenv("POLL_INTERVAL_ACTIVE", "60")))


def _is_doser_action(action: dict) -> bool:
    """True if the action targets a doser/pH port -- including the `dose` playbook verb
    (chemicals), which must get the hard doser settle before its outcome is read."""
    if action.get("action") == "dose":
        return True
    from utils import name_slug
    port = action.get("port")
    if port is None:
        return False
    slug = name_slug(action.get("device", ""))
    for key in (f"DOSER_PORTS_{slug}", f"PH_PORTS_{slug}"):
        ports = os.getenv(key, "")
        if ports and str(port) in [x.strip() for x in ports.split(",")]:
            return True
    return False


def _wait_for(action: dict) -> float:
    """Outcome-read wait for one action. Doser/pH doses get a HARD 5-min minimum settle
    (chemistry, esp. pH, keeps drifting past the apparent quick-settle); everything else
    uses the normal ACTIVE-interval window."""
    base = _outcome_wait_sec()
    if _is_doser_action(action):
        from dosing import dose_settle_seconds
        return max(base, dose_settle_seconds())
    return base


# --- Persistent pending-outcome queue (survives restarts) ---

def _load_pending() -> list[dict]:
    if _PENDING_FILE.exists():
        try:
            return json.loads(_PENDING_FILE.read_text())
        except Exception:
            return []
    return []


def _save_pending():
    PROFILES_DIR.mkdir(exist_ok=True)
    tmp = _PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_pending, indent=2))
    tmp.replace(_PENDING_FILE)


_pending: list[dict] = _load_pending()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _profile_path(strain: str) -> Path:
    PROFILES_DIR.mkdir(exist_ok=True)
    slug = strain.lower().replace(" ", "_")
    return PROFILES_DIR / f"{slug}.json"


def _load(strain: str) -> dict:
    p = _profile_path(strain)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"strain": strain, "runs": {}, "calibration": {}}


def _save(strain: str, data: dict):
    """Atomic write: tmp + replace so a kill mid-write can't corrupt the profile."""
    p   = _profile_path(strain)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def _week_key() -> str:
    week, stage = current_grow_week_and_stage()
    return f"week_{week}_{stage}"


def _extract_sensors(snapshot: dict) -> dict:
    sensors = {}
    for dev in snapshot.get("devices", []):
        for k, v in dev.get("sensors", {}).items():
            if isinstance(v, (int, float)):
                sensors[k] = v
    return sensors


def _cal_key(action: dict) -> str:
    """Unique key for an action's dose-response bucket. `dose` actions key on the
    playbook (code owns port/speed/volume), so a microdose and a small dose at the same
    pump speed don't collide into one bucket; everything else keys on device/port/action/
    value."""
    if action.get("action") == "dose":
        return f"{action.get('device')}:dose:{action.get('playbook')}"
    return (
        f"{action['device']}:port{action['port']}"
        f":{action['action']}:{action['value']}"
    )


# ---------------------------------------------------------------------------
# Cycle logging
# ---------------------------------------------------------------------------

def log_cycle(snapshot: dict, actions: list):
    """Append the current cycle's readings and executed actions to the profile."""
    strain = _strain_name()
    if not strain:
        return
    profile = _load(strain)
    run  = profile["runs"].setdefault(_run_id(), {"weeks": {}})
    week = run["weeks"].setdefault(_week_key(), {"cycles": []})

    week["cycles"].append({
        "ts":      snapshot.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S")),
        "sensors": _extract_sensors(snapshot),
        "actions": [
            {"device": a.get("device"), "port": a.get("port"),
             "action": a.get("action"), "value": a.get("value")}
            for a in actions
        ],
    })

    if len(week["cycles"]) > MAX_CYCLES_PER_WEEK:
        week["cycles"] = week["cycles"][-MAX_CYCLES_PER_WEEK:]

    _save(strain, profile)


# ---------------------------------------------------------------------------
# Outcome / calibration tracking
# ---------------------------------------------------------------------------

def has_pending_outcomes() -> bool:
    """True when actions have fired but their outcomes haven't been read yet."""
    return bool(_pending)


def track_actions(actions: list, before_snapshot: dict):
    """
    Call immediately after actions are executed.
    Queues each action for outcome measurement N cycles later.
    """
    # record_outcomes (the only drain path) returns early without a strain, so queuing
    # here when STRAIN_NAME is unset (a supported climate-only config) would grow the
    # pending file unbounded AND pin the poller to ACTIVE polling forever. Mirror the
    # strain guard every other function here already has.
    if not _strain_name():
        return
    before_sensors = _extract_sensors(before_snapshot)
    fired_at = time.time()
    for a in actions:
        if a.get("action") == "dose":
            rec = {"device": a.get("device"), "action": "dose",
                   "playbook": a.get("playbook"), "kind": a.get("kind"),
                   "ports": a.get("ports") or []}
        else:
            rec = {"device": a.get("device"), "port": a.get("port"),
                   "action": a.get("action"), "value": a.get("value")}
        _pending.append({"action": rec, "before": before_sensors, "fired_at": fired_at})
    _save_pending()


def _relevant_sensors(action: dict) -> set:
    """Which tracked sensors a CHEMICAL action can plausibly move, so two doses that
    settle in the same window don't cross-contaminate each other's dose-response buckets
    (a nutrient dose otherwise learns a spurious pH drop from a co-occurring pH dose, and
    vice-versa). Returns an empty set for non-chemical actions -> no restriction, current
    behavior preserved."""
    if action.get("action") == "dose":
        return {"ph"} if action.get("kind") == "ph" else {"ec_ms", "ec_us", "tds_ppm"}
    port = action.get("port")
    if port is not None:
        from utils import name_slug
        slug = name_slug(action.get("device", ""))
        ph_ports    = [x.strip() for x in os.getenv(f"PH_PORTS_{slug}", "").split(",") if x.strip()]
        doser_ports = [x.strip() for x in os.getenv(f"DOSER_PORTS_{slug}", "").split(",") if x.strip()]
        if str(port) in ph_ports:
            return {"ph"}
        if str(port) in doser_ports:
            return {"ec_ms", "ec_us", "tds_ppm"}
    return set()


def record_outcomes(current_snapshot: dict):
    """
    Call at the start of each poll cycle (before asking AI).
    Settles any pending actions whose wait window has elapsed and
    records the measured delta into the calibration table.
    """
    strain = _strain_name()
    if not strain or not _pending:
        return

    now    = time.time()
    after  = _extract_sensors(current_snapshot)
    settle = []
    keep   = []

    for p in _pending:
        if now - p["fired_at"] >= _wait_for(p["action"]):
            settle.append(p)
        else:
            keep.append(p)

    if not settle:
        return

    profile = _load(strain)
    cal     = profile.setdefault("calibration", {})

    for p in settle:
        before   = p["before"]
        relevant = _relevant_sensors(p["action"])   # empty -> no restriction (non-chem)
        deltas = {}
        for sensor in _TRACKED_SENSORS:
            # For a chemical action, fold in ONLY the sensors it can plausibly move so a
            # co-settling pH + nutrient dose don't pollute each other's buckets.
            if relevant and sensor not in relevant:
                continue
            if sensor in before and sensor in after:
                delta = round(after[sensor] - before[sensor], 3)
                if abs(delta) >= 0.01:   # filter measurement noise
                    deltas[sensor] = delta

        if not deltas:
            continue

        key   = _cal_key(p["action"])
        entry = cal.setdefault(key, {"observations": [], "averages": {}, "count": 0})

        entry["observations"].append({
            "ts":       time.strftime("%Y-%m-%d %H:%M:%S"),
            "deltas":   deltas,
            "wait_sec": int(now - p["fired_at"]),
        })
        if len(entry["observations"]) > MAX_CAL_OBSERVATIONS:
            entry["observations"] = entry["observations"][-MAX_CAL_OBSERVATIONS:]

        # Recompute rolling averages
        totals, counts = {}, {}
        for obs in entry["observations"]:
            for sensor, delta in obs["deltas"].items():
                totals[sensor] = totals.get(sensor, 0.0) + delta
                counts[sensor] = counts.get(sensor, 0) + 1
        entry["averages"] = {
            s: round(totals[s] / counts[s], 3) for s in totals
        }
        entry["count"] = len(entry["observations"])

        print(f"  [CAL] {key}  ->  {entry['averages']}  ({entry['count']} obs)")

    # Drain the settled entries from the IN-MEMORY queue FIRST. The disk writes below
    # can fail (unwritable profiles dir), and if the batch stayed queued in memory it
    # would re-settle and re-raise identically on EVERY poll cycle -- record_outcomes
    # runs ahead of the poller's deterministic safety enforcement (doser watchdog,
    # res-burst, CO2/temp emergencies, schedule), so a persistent raise here would
    # starve all of it indefinitely.
    _pending.clear()
    _pending.extend(keep)

    # Persist calibration BEFORE dropping the settled entries from the ON-DISK queue. A
    # crash between the two writes then re-settles those entries after restart (a second,
    # slightly later observation for a noise-averaged table) instead of losing them
    # entirely. A disk FAILURE is contained -- logged loudly, never raised -- so one bad
    # disk costs at most this batch's calibration, never the poll cycle's safety checks.
    try:
        _save(strain, profile)
        _save_pending()
    except Exception as e:
        print(f"  [CAL] WARNING: could not persist calibration/pending queue: {e}")
        from runtime_state import record_event
        record_event("calibration_save_failed", strain=strain,
                     settled=len(settle), error=str(e))


# ---------------------------------------------------------------------------
# Context builders for AI prompt injection
# ---------------------------------------------------------------------------

SENSOR_LABELS = {
    "ph":           "pH",
    "tds_ppm":      "TDS ppm",
    "ec_ms":        "EC mS/cm",
    "ec_us":        "EC uS/cm",
    "water_temp_f": "H2O temp F",
    "air_temp_f":   "air temp F",
    "humidity_pct": "humidity %",
    "vpd_kpa":      "VPD kPa",
    "co2_ppm":      "CO2 ppm",
}


def get_calibration_context() -> str:
    """
    Dose-response table for this system, built from observed outcomes.
    Injected into the AI prompt so the model can size actions correctly
    instead of guessing.  Only entries with >= MIN_CAL_OBSERVATIONS are included.
    """
    strain = _strain_name()
    if not strain:
        return ""

    profile = _load(strain)
    cal     = profile.get("calibration", {})
    if not cal:
        return ""

    lines = [
        "System calibration (observed dose-response — use to size actions accurately):"
    ]
    found = False

    for key, data in sorted(cal.items()):
        if data.get("count", 0) < MIN_CAL_OBSERVATIONS:
            continue
        parts = key.split(":")
        delta_parts = []
        for sensor, avg in data["averages"].items():
            sign = "+" if avg > 0 else ""
            delta_parts.append(f"{sensor} {sign}{avg}")
        if not delta_parts:
            continue
        if len(parts) == 3 and parts[1] == "dose":          # device:dose:playbook
            label = f"  {parts[0]} dose {parts[2]}"
        elif len(parts) == 4:                                # device:portN:action:value
            device, port_s, action_type, value = parts
            label = f"  {device} port {port_s.replace('port', '')}  {action_type}={value}"
        else:
            continue
        found = True
        lines.append(f"{label}  ({data['count']} obs):  {', '.join(delta_parts)}")

    if not found:
        return ""

    lines.append(
        "\nTo hit a target delta, pick the speed whose observed delta is closest. "
        "Prefer undershooting — you can always dose again next cycle."
    )
    return "\n".join(lines)


def get_historical_context() -> str:
    """
    Week-averaged sensor summary from past runs of the same strain/week/stage.
    Injected into the AI prompt as a baseline for what worked before.
    """
    strain = _strain_name()
    if not strain:
        return ""

    profile = _load(strain)
    if not profile["runs"]:
        return ""

    wk      = _week_key()
    cur_run = _run_id()
    lines   = [f"Strain history: {strain} | {wk.replace('_', ' ')}"]
    found   = False

    for run_id, run in profile["runs"].items():
        if run_id == cur_run:
            continue
        if wk not in run["weeks"] or not run["weeks"][wk]["cycles"]:
            continue

        cycles = run["weeks"][wk]["cycles"]
        totals, counts, action_counts = {}, {}, {}
        for cycle in cycles:
            for k, v in cycle.get("sensors", {}).items():
                totals[k]  = totals.get(k, 0.0) + v
                counts[k]  = counts.get(k, 0) + 1
            for a in cycle.get("actions", []):
                ak = f"{a.get('device')}:port{a.get('port')}:{a.get('action')}"
                action_counts[ak] = action_counts.get(ak, 0) + 1

        found = True
        lines.append(f"\n  Run '{run_id}' ({len(cycles)} cycles):")
        readings = []
        for key in SENSOR_LABELS:
            if key in totals:
                readings.append(
                    f"{SENSOR_LABELS[key]} {round(totals[key]/counts[key], 2)}"
                )
        if readings:
            lines.append("    Avg: " + ", ".join(readings))
        if action_counts:
            lines.append("    Interventions: " + ", ".join(
                f"{k} x{v}" for k, v in action_counts.items()
            ))

    if not found:
        return ""

    lines.append(
        "\nNudge toward these proven values while staying within target ranges."
    )
    return "\n".join(lines)


def active_profile_label() -> str:
    """Short status string for the poll header display."""
    from grow_state import days_into_current_stage
    strain = _strain_name()
    week, stage = current_grow_week_and_stage()
    day, planned = days_into_current_stage()
    day_str = f" (day {day}/{planned})" if planned else ""

    if not strain:
        return f"no strain set | wk{week} {stage}{day_str}"

    profile = _load(strain)
    n_cal   = sum(
        1 for e in profile.get("calibration", {}).values()
        if e.get("count", 0) >= MIN_CAL_OBSERVATIONS
    )
    cal_str = f"  {n_cal} calibrated action(s)" if n_cal else "  calibrating..."
    return f"{strain} | wk{week} {stage}{day_str} | run {_run_id()} |{cal_str}"
