"""
event_log.py -- structured cycle + action-lifecycle ledger.

A thin layer over runtime_state.record_event (append-only profiles/events.jsonl).
This is deliberately NOT a new store: per the architect review in
UPGRADE_PRIORITY_TREE.md, JSONL stays until it becomes genuinely painful to
query; the SQLite design in EVENT_LOGGING_PLAN.md is deferred to v1.1.

Every poll cycle gets one `cycle_id`; every requested action gets one
`action_id`. Threading those through the records lets the full lifecycle
(requested -> validated -> executed -> verified -> outcome) be reconstructed
from the single log, alongside the watchdog/recovery events already written
there by runtime_state.

All helpers swallow errors (record_event never raises) -- logging must never be
able to take down the control loop.

Event types emitted here:
  cycle             one per poll: grow week/stage, res-health gates, sensor snapshot, mode
  ai_decision       one per AI response: assessment, action count, latency, next_check
  action_request    one per requested action (ai / deterministic / manual / scheduled)
  action_validation one per action: validated/blocked at a named stage, with reason
  action_execution  one per action actually attempted: sent? success? verified? error
  action_outcome    one per measured result after the wait window
"""

import uuid

import runtime_state


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #
def new_cycle_id() -> str:
    return uuid.uuid4().hex[:12]


def new_action_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clip(s, limit: int = 280):
    """Trim long free-text so the log stays line-readable. Non-strings pass through."""
    if isinstance(s, str) and len(s) > limit:
        return s[:limit] + "..."
    return s


def _jsonsafe(v):
    """Keep only plainly-serializable scalars; stringify anything else."""
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return str(v)


def _flat_sensors(snapshot: dict) -> dict:
    """Merge every device's sensor dict into one flat {name: value} map."""
    out: dict = {}
    for dev in snapshot.get("devices", []) or []:
        out.update(dev.get("sensors", {}) or {})
    return out


def _active(snapshot: dict, key: str) -> bool:
    block = snapshot.get(key)
    return bool(block and block.get("active"))


# --------------------------------------------------------------------------- #
# cycle + AI decision
# --------------------------------------------------------------------------- #
def start_cycle(snapshot: dict, mode: str = "advisory") -> str:
    """Open a poll-cycle record and return its cycle_id. Captures the snapshot
    context (gates, sensors, active emergencies) so later action records can be
    correlated to the conditions that produced them."""
    cid = new_cycle_id()
    rh = snapshot.get("res_health") or {}
    runtime_state.record_event(
        "cycle",
        cycle_id=cid,
        mode=mode,
        grow_week=snapshot.get("grow_week"),
        grow_stage=snapshot.get("grow_stage"),
        water_level_source=snapshot.get("water_level_source"),
        res_health=rh.get("state"),
        water_trend=rh.get("water_trend"),
        ec_trend=rh.get("ec_trend"),
        co2_gate=rh.get("co2_gate"),
        dose_gate=rh.get("dose_gate"),
        ph_gate=rh.get("ph_gate"),
        n_schedule_deltas=len(snapshot.get("schedule_deltas") or []),
        temp_emergency=_active(snapshot, "temp_emergency"),
        co2_emergency=_active(snapshot, "co2_emergency"),
        res_burst=_active(snapshot, "res_burst"),
        leak_wet=bool((snapshot.get("leak") or {}).get("wet")),
        sensors=_flat_sensors(snapshot),
    )
    return cid


def log_stressors(cycle_id: str | None, diag: dict | None) -> None:
    """Record the deterministic stressor list for a cycle (away-mode triage
    context). `diag` is the snapshot's diagnostics block. No-op when empty."""
    diag = diag or {}
    stressors = diag.get("stressors") or []
    if not stressors:
        return
    runtime_state.record_event(
        "stressors",
        cycle_id=cycle_id,
        count=diag.get("count", len(stressors)),
        worst_severity=diag.get("worst_severity"),
        names=[s.get("name") for s in stressors],
        items=[{"name": s.get("name"), "severity": s.get("severity"),
                "evidence": _clip(s.get("evidence"), 160)} for s in stressors],
    )


def log_ai_decision(cycle_id: str | None, result: dict | None,
                    latency_sec: float | None = None) -> None:
    """Record the AI's parsed response summary. `result` None/empty means the
    model failed to return usable JSON (parsed_ok=False)."""
    result = result or {}
    runtime_state.record_event(
        "ai_decision",
        cycle_id=cycle_id,
        parsed_ok=bool(result),
        assessment=_clip(result.get("assessment")),
        n_actions=len(result.get("actions") or []),
        next_check_seconds=result.get("next_check_seconds"),
        notify_user=result.get("notify_user"),
        latency_sec=round(latency_sec, 2) if latency_sec is not None else None,
    )


# --------------------------------------------------------------------------- #
# action lifecycle
# --------------------------------------------------------------------------- #
def log_action_request(cycle_id: str | None, action: dict,
                       source: str = "ai") -> str:
    """Record a requested action and return its action_id. `source` is one of
    ai / deterministic_emergency / manual_user / scheduled."""
    aid = new_action_id()
    action = action or {}
    runtime_state.record_event(
        "action_request",
        cycle_id=cycle_id,
        action_id=aid,
        source=source,
        device=action.get("device"),
        port=action.get("port"),
        action_type=action.get("action"),
        value=_jsonsafe(action.get("value")),
        playbook=action.get("playbook"),
        reason=_clip(action.get("reason")),
    )
    return aid


def log_action_validation(cycle_id: str | None, action_id: str, valid: bool,
                          reason: str | None = None,
                          stage: str = "schema") -> None:
    """Record whether an action passed a named gate (stage: schema / preflight /
    safety_gate). `reason` carries the rejection cause when valid is False."""
    runtime_state.record_event(
        "action_validation",
        cycle_id=cycle_id,
        action_id=action_id,
        stage=stage,
        valid=bool(valid),
        reason=reason,
    )


def log_action_execution(cycle_id: str | None, action_id: str, *,
                         executed: bool, success: bool, **fields) -> None:
    """Record a hardware command attempt. Extra fields (device, port,
    command_type, value_sent, verified, error, target_ml, ...) pass through."""
    runtime_state.record_event(
        "action_execution",
        cycle_id=cycle_id,
        action_id=action_id,
        executed=bool(executed),
        success=bool(success),
        **{k: _jsonsafe(v) for k, v in fields.items()},
    )


def log_action_outcome(cycle_id: str | None, action_id: str | None,
                       success: bool | None, **fields) -> None:
    """Record a measured outcome after the wait window (status, deltas, etc.)."""
    runtime_state.record_event(
        "action_outcome",
        cycle_id=cycle_id,
        action_id=action_id,
        success=success,
        **{k: _jsonsafe(v) for k, v in fields.items()},
    )


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def _read_events() -> list[dict]:
    """Load every event from the JSONL log (best-effort; skips corrupt lines).
    Reads runtime_state._EVENT_LOG dynamically so tests can redirect it."""
    import json
    path = runtime_state._EVENT_LOG
    out: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return out


def recent_actions(limit: int = 10, window_hours: float = 24.0) -> list[dict]:
    """Compact summary of the most recent executed actions, newest first, for
    the AI prompt / HUD so corrections aren't repeated blindly. Pulls from
    action_execution records within the window; falls back to wall_ts ordering."""
    import time
    cutoff = time.time() - window_hours * 3600.0
    rows = [e for e in _read_events()
            if e.get("type") == "action_execution"
            and isinstance(e.get("wall_ts"), (int, float))
            and e["wall_ts"] >= cutoff]
    rows.sort(key=lambda e: e["wall_ts"], reverse=True)
    now = time.time()
    out: list[dict] = []
    for e in rows[:limit]:
        out.append({
            "age_minutes": round((now - e["wall_ts"]) / 60.0, 1),
            "device": e.get("device"),
            "port": e.get("port"),
            "action": e.get("command_type") or e.get("playbook"),
            "value": e.get("value_sent"),
            "success": e.get("success"),
            "verified": e.get("verified"),
        })
    return out
