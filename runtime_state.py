"""
Runtime / heartbeat state + crash-recovery primitives for the grow controller.

Deliberately separate from safety_state.py. That module owns the persistent
*chemical-freeze* flag (dosing_disabled). This module owns the *liveness* signals
the watchdog needs to notice a crash and recover safely:

  - a frequently-written heartbeat (phase, pid, boot_id, wall + monotonic clocks)
  - an optional active-dose record (written by timed dosing #7; read by recovery)
  - a persisted high-alert window (faster reservoir polling after a scare)
  - an append-only event log (events.jsonl) -- also the Layer 2 ledger seed

Clock model (per docs/done/WATCHDOG_HEARTBEAT_PLAN.md):
  - wall clock (time.time / UTC) for timestamps, cross-restart math, logs
  - monotonic clock (time.monotonic) for in-process durations / heartbeat age
  Never time a dose with the wall clock -- NTP can jump it.

Recovery rule of thumb: a doser port should be at speed 0 unless a live, in-window
active dose says otherwise. Anything else found nonzero -- especially across a
restart -- is an orphan to be stopped, verified, frozen, and alerted.

State file: profiles/.runtime_state.json  (atomic tmp+replace, corrupt-tolerant)
Event log:  profiles/events.jsonl          (append-only, one JSON object per line)
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_STATE_FILE = Path(__file__).parent / "profiles" / ".runtime_state.json"
_EVENT_LOG  = Path(__file__).parent / "profiles" / "events.jsonl"

# Grace added to a planned dose window before the watchdog treats a running pump as
# an orphan -- covers ramp-down time so we don't fight a dose that is legitimately
# still settling. Seconds.
_DOSE_WINDOW_GRACE_SEC = 10.0

_DEFAULT = {
    "heartbeat": None,         # see write_heartbeat()
    "active_dose": None,       # see begin_active_dose()
    "high_alert": None,        # see start_high_alert()
    "watchdog_streaks": {},    # {"device:port": consecutive out-of-window nonzero reads}
    "leak_streak": 0,          # consecutive wet reads of the boolean leak sensor
}


# --------------------------------------------------------------------------- #
# clocks / ids
# --------------------------------------------------------------------------- #
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def boot_id() -> str:
    """Linux boot id -- changes on every reboot, so comparing it across a restart
    distinguishes 'process died' from 'whole machine rebooted'. Empty if unreadable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# persistence (atomic, corrupt-tolerant -- mirrors safety_state.py)
# --------------------------------------------------------------------------- #
def _load() -> dict:
    if not _STATE_FILE.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(_STATE_FILE.read_text())
        if not isinstance(data, dict):
            return dict(_DEFAULT)
    except Exception as e:
        print(f"[RUNTIME] Could not read {_STATE_FILE.name} ({e}) -- starting fresh")
        return dict(_DEFAULT)
    merged = dict(_DEFAULT)
    merged.update({k: data[k] for k in _DEFAULT if k in data})
    return merged


def _save(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(_STATE_FILE)
    except Exception as e:
        print(f"[RUNTIME] Could not write {_STATE_FILE.name}: {e}")


def read_state() -> dict:
    """Full runtime state (heartbeat + active_dose + high_alert)."""
    return _load()


# --------------------------------------------------------------------------- #
# event log -- append-only JSONL (Layer 2 ledger seed)
# --------------------------------------------------------------------------- #
def record_event(event_type: str, **fields) -> None:
    """Append one event to events.jsonl. Never raises -- logging must not be able
    to take down the control loop."""
    rec = {
        "wall_time_utc": _utc_now_iso(),
        "wall_ts": round(time.time(), 3),
        "monotonic": round(time.monotonic(), 3),
        "pid": os.getpid(),
        "type": event_type,
    }
    rec.update(fields)
    try:
        _EVENT_LOG.parent.mkdir(exist_ok=True)
        with _EVENT_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[RUNTIME] Could not append event {event_type}: {e}")


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def write_heartbeat(phase: str, *, poll_ok: bool | None = None,
                    api_ok: bool | None = None,
                    readback_ok: bool | None = None) -> None:
    """Persist the current loop phase + liveness flags. Passing None for an *_ok
    flag preserves its previous value (so a phase tick doesn't clobber a known
    failure). Cheap enough to call several times per cycle."""
    state = _load()
    prev = state.get("heartbeat") or {}
    hb = {
        "wall_time_utc": _utc_now_iso(),
        "wall_ts": round(time.time(), 3),
        "monotonic": round(time.monotonic(), 3),
        "pid": os.getpid(),
        "boot_id": boot_id(),
        "phase": phase,
        "last_poll_ok": prev.get("last_poll_ok") if poll_ok is None else poll_ok,
        "last_api_ok": prev.get("last_api_ok") if api_ok is None else api_ok,
        "last_readback_ok": prev.get("last_readback_ok") if readback_ok is None else readback_ok,
    }
    state["heartbeat"] = hb
    _save(state)


def last_heartbeat() -> dict | None:
    return _load().get("heartbeat")


def mark_clean_shutdown() -> None:
    """Phase -> shutdown. A heartbeat with any other phase on next startup means
    the previous run did not exit cleanly (crash / kill / power loss)."""
    write_heartbeat("shutdown")
    record_event("clean_shutdown")


# --------------------------------------------------------------------------- #
# active dose (written by timed dosing #7; structure wired now so recovery works)
# --------------------------------------------------------------------------- #
def begin_active_dose(record: dict) -> None:
    """Persist an active-dose record BEFORE the pump starts, so a crash mid-dose is
    recoverable. `record` should carry at least: device, dev_id, port, speed, and
    (when known) target_ml, strength_factor, started_wall_ts, planned_stop_wall_ts,
    start_verified. status is forced to 'pump_running'."""
    state = _load()
    rec = dict(record)
    # Accept either a single `port` or a list of `ports` (nutrient pair). Keep both
    # populated so the watchdog window and the human-readable messages both work.
    if "ports" not in rec and rec.get("port") is not None:
        rec["ports"] = [rec["port"]]
    if "port" not in rec and rec.get("ports"):
        rec["port"] = rec["ports"][0]
    rec.setdefault("started_wall_time_utc", _utc_now_iso())
    rec.setdefault("started_wall_ts", time.time())
    rec.setdefault("started_monotonic", time.monotonic())
    rec["status"] = "pump_running"
    state["active_dose"] = rec
    _save(state)
    record_event("active_dose_started", **{k: rec.get(k) for k in
                 ("device", "port", "speed", "target_ml", "planned_stop_wall_ts")})


def mark_active_dose_running(last_confirmed_wall_ts: float | None = None) -> None:
    """Mark the active dose as readback-confirmed running. Tightens the crash estimate:
    a verified start means min delivered > 0 (vs 0 when start was never confirmed)."""
    state = _load()
    ad = state.get("active_dose")
    if not ad:
        return
    ad["start_verified"] = True
    ad["last_confirmed_running_wall_ts"] = last_confirmed_wall_ts or time.time()
    state["active_dose"] = ad
    _save(state)


def mark_active_dose_stopped(verified: bool, **extra) -> None:
    state = _load()
    ad = state.get("active_dose")
    if not ad:
        return
    ad["status"] = "stopped_verified" if verified else "stopped_unverified"
    ad["stopped_wall_time_utc"] = _utc_now_iso()
    ad["stopped_wall_ts"] = time.time()
    ad["stop_verified"] = bool(verified)
    ad.update(extra)
    state["active_dose"] = ad
    _save(state)


def clear_active_dose() -> None:
    state = _load()
    if state.get("active_dose") is not None:
        state["active_dose"] = None
        _save(state)


def get_active_dose() -> dict | None:
    return _load().get("active_dose")


def active_dose_window_ports() -> set:
    """Ports the watchdog should leave alone because an active dose genuinely still
    vouches for them (planned stop + ramp grace not yet passed). Returns a set so a
    nutrient pair (ports 1+2) is covered. Empty when no dose is in-window -- then any
    running doser is an orphan."""
    ad = get_active_dose()
    if not ad or ad.get("status") != "pump_running":
        return set()
    planned_stop = ad.get("planned_stop_wall_ts")
    if planned_stop is None:
        # Running with no planned stop recorded -> cannot vouch for it; treat as orphan.
        return set()
    if time.time() <= float(planned_stop) + _DOSE_WINDOW_GRACE_SEC:
        ports = ad.get("ports")
        if ports:
            return set(ports)
        p = ad.get("port")
        return {p} if p is not None else set()
    return set()


# --------------------------------------------------------------------------- #
# interrupted-dose estimate (conservative; refined when #7 lands)
# --------------------------------------------------------------------------- #
def estimate_interrupted_dose(active_dose: dict, stop_wall_ts: float) -> dict:
    """Worst-case bracket of how much was delivered by an interrupted dose.

    Flow model: AC Infinity peristaltic pumps move 21 mL/min per speed level.
      max_ml -- pump ran from start until verified stop at commanded speed.
      min_ml -- 0 if the pump start was never readback-verified; otherwise the
                planned target (or flow to last-confirmed-running, if recorded).
      best_ml -- planned target when known, else the max bracket.
    Strength factor scales actual mL to full-strength-equivalent mL."""
    speed = int(active_dose.get("speed") or 0)
    flow_ml_min = speed * 21.0
    started_ts = active_dose.get("started_wall_ts")
    target_ml = active_dose.get("target_ml")
    strength = float(active_dose.get("strength_factor") or 1.0)
    start_verified = bool(active_dose.get("start_verified"))
    last_confirmed = active_dose.get("last_confirmed_running_wall_ts")

    if started_ts is not None:
        max_elapsed = max(0.0, float(stop_wall_ts) - float(started_ts))
    else:
        max_elapsed = 0.0
    max_ml = round(flow_ml_min * max_elapsed / 60.0, 3)

    if not start_verified:
        min_ml = 0.0
    elif last_confirmed is not None and started_ts is not None:
        min_ml = round(flow_ml_min * max(0.0, float(last_confirmed) - float(started_ts)) / 60.0, 3)
    elif target_ml is not None:
        min_ml = float(target_ml)
    else:
        min_ml = 0.0

    best_ml = float(target_ml) if target_ml is not None else max_ml

    return {
        "estimated_actual_ml_min": min_ml,
        "estimated_actual_ml_max": max_ml,
        "estimated_actual_ml_best": best_ml,
        "estimated_full_strength_equivalent_min": round(min_ml * strength, 3),
        "estimated_full_strength_equivalent_max": round(max_ml * strength, 3),
        "commanded_speed": speed,
        "flow_ml_min": flow_ml_min,
        "elapsed_sec_max": round(max_elapsed, 1),
        "start_verified": start_verified,
    }


# --------------------------------------------------------------------------- #
# high-alert reservoir polling window (persisted, survives restart)
# --------------------------------------------------------------------------- #
def _high_alert_duration_sec() -> int:
    try:
        return max(0, int(float(os.getenv("HIGH_ALERT_DURATION_MINUTES", "30")) * 60))
    except ValueError:
        return 1800


def high_alert_poll_interval() -> int:
    try:
        return max(5, int(os.getenv("HIGH_ALERT_POLL_INTERVAL", "30")))
    except ValueError:
        return 30


def start_high_alert(reason: str, duration_minutes: float | None = None) -> None:
    """Enter (or extend) high-alert monitoring: faster reservoir polling for a
    bounded window. Persisted so it survives another restart. Dosing stays frozen
    independently via safety_state -- high-alert does not itself gate chemicals."""
    dur = (int(duration_minutes * 60) if duration_minutes is not None
           else _high_alert_duration_sec())
    now = time.time()
    state = _load()
    existing = state.get("high_alert") or {}
    new_until = now + dur
    # Extend rather than shorten if we are already in a (longer) window.
    until = max(new_until, float(existing.get("until_ts", 0) or 0))
    state["high_alert"] = {
        "until_ts": until,
        "until_utc": datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_utc": existing.get("started_utc") or _utc_now_iso(),
        "reason": reason,
    }
    _save(state)
    record_event("high_alert_started", reason=reason,
                 until_utc=state["high_alert"]["until_utc"])
    print(f"[RUNTIME] HIGH ALERT -- {reason}. Faster reservoir polling for "
          f"~{dur // 60} min (until {state['high_alert']['until_utc']}).")


def high_alert_status() -> tuple[bool, int, str | None]:
    """(active, remaining_sec, reason). Expired windows auto-clear on read and log
    high_alert_ended -- so the caller does not need a separate sweep."""
    state = _load()
    ha = state.get("high_alert")
    if not ha:
        return False, 0, None
    until = float(ha.get("until_ts", 0) or 0)
    remaining = int(until - time.time())
    if remaining <= 0:
        state["high_alert"] = None
        _save(state)
        record_event("high_alert_ended", reason=ha.get("reason"))
        return False, 0, None
    return True, remaining, ha.get("reason")


def clear_high_alert() -> None:
    state = _load()
    if state.get("high_alert") is not None:
        reason = (state.get("high_alert") or {}).get("reason")
        state["high_alert"] = None
        _save(state)
        record_event("high_alert_ended", reason=reason, cleared="manual")


# --------------------------------------------------------------------------- #
# debounce streaks (persisted so a restart does NOT reset a leak / orphan count)
# --------------------------------------------------------------------------- #
def leak_streak_get() -> int:
    """Consecutive wet reads of the boolean leak sensor (persisted across restarts)."""
    return int(_load().get("leak_streak", 0) or 0)


def leak_streak_bump() -> int:
    """Increment + persist the leak wet-streak; return the new value."""
    state = _load()
    val = int(state.get("leak_streak", 0) or 0) + 1
    state["leak_streak"] = val
    _save(state)
    return val


def leak_streak_reset() -> None:
    """Reset the leak wet-streak to 0 (writes only if it was nonzero)."""
    state = _load()
    if int(state.get("leak_streak", 0) or 0):
        state["leak_streak"] = 0
        _save(state)


def watchdog_streak_bump(key: str) -> int:
    """Increment + persist the out-of-window nonzero-read streak for one doser/pH port
    (key 'device:port'); return the new value. Survives a restart so the PERSISTENT
    dosing freeze waits for real confirmation rather than one stale readback."""
    state = _load()
    streaks = dict(state.get("watchdog_streaks") or {})
    streaks[key] = int(streaks.get(key, 0)) + 1
    state["watchdog_streaks"] = streaks
    _save(state)
    return streaks[key]


def watchdog_streak_reset(key: str) -> None:
    """Clear one port's watchdog streak (writes only if it was set)."""
    state = _load()
    streaks = dict(state.get("watchdog_streaks") or {})
    if key in streaks:
        del streaks[key]
        state["watchdog_streaks"] = streaks
        _save(state)


# --------------------------------------------------------------------------- #
# restart diagnosis (pure analysis; no hardware IO)
# --------------------------------------------------------------------------- #
def diagnose_restart() -> dict:
    """Inspect the heartbeat left by the previous run and classify how this process
    came to be. No IO beyond the state file. Fields:
      fresh           -- no prior heartbeat (first run / cleared state)
      clean           -- previous run wrote phase=shutdown
      rebooted        -- boot_id changed since last heartbeat (machine restarted)
      had_active_dose -- an active_dose record was left as pump_running
      last_phase, last_heartbeat_utc, disconnect_sec (now - last heartbeat wall ts)
    """
    state = _load()
    hb = state.get("heartbeat")
    ad = state.get("active_dose")
    had_active_dose = bool(ad and ad.get("status") == "pump_running")
    if not hb:
        return {"fresh": True, "clean": False, "rebooted": False,
                "had_active_dose": had_active_dose, "last_phase": None,
                "last_heartbeat_utc": None, "disconnect_sec": None}
    last_ts = hb.get("wall_ts")
    disconnect = round(time.time() - float(last_ts), 1) if last_ts else None
    return {
        "fresh": False,
        "clean": hb.get("phase") == "shutdown",
        "rebooted": bool(hb.get("boot_id")) and hb.get("boot_id") != boot_id(),
        "had_active_dose": had_active_dose,
        "last_phase": hb.get("phase"),
        "last_heartbeat_utc": hb.get("wall_time_utc"),
        "disconnect_sec": disconnect,
    }
