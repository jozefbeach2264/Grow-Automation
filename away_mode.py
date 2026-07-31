"""
away_mode.py -- deterministic away-mode triage executor (Layer 3).

Code-driven dispatch: given the deterministic stressor list that `diagnostics.py`
already attached to the snapshot, this picks the WORST stressor that has an
actionable playbook and dispatches that playbook's bounded action. The AI action
contract is deliberately UNCHANGED -- per the architect review, the
raw-action -> selected_playbook prompt refactor is a separate, riskier step.
Here the AI stays advisory and CODE owns the triage decision.

Safety scope (current hardware reality -- reservoir + CO2 physically disconnected):
  LIVE   increase_exhaust_one_step   (bounded, capped; exhaust is not schedule-pinned)
  ADVISORY reduce_light_one_step     (the schedule enforcer pins light intensity; going
                                       live needs a schedule-aware light override -- deferred)
  DRY    disable_co2                 (CO2 valve disconnected)
  DRY    timed_*_microdose           (Tier 3 chemical; gated until live dosing is proven)
  ALERT  alert_only                  (log + notify, no hardware)

Gating:
  AWAY_MODE=true            master enable (default false -> this module is inert)
  ADVISORY_MODE             when true, even LIVE playbooks are logged as "would dispatch"
                            (detect-always / actuate-in-LIVE, same contract as the
                            CO2 dump / high-temp guardrail)

One dispatch + one alert per cycle: the executor acts on the worst ACTIONABLE
stressor and always alerts on the worst stressor overall.
"""

import os

import event_log
from diagnostics import ALERT_ONLY, PLAYBOOKS
from schedule import _parse_role, _find_port, _co2_outlet


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    return os.getenv("AWAY_MODE", "false").strip().lower() == "true"


def _advisory() -> bool:
    return os.getenv("ADVISORY_MODE", "true").strip().lower() != "false"


def _exhaust_step() -> int:
    try:
        return max(1, int(os.getenv("AWAY_EXHAUST_STEP", "1")))
    except ValueError:
        return 1


def _exhaust_max() -> int:
    try:
        return max(0, min(10, int(os.getenv("AWAY_EXHAUST_MAX", "10"))))
    except ValueError:
        return 10


def _light_floor() -> int:
    try:
        return max(0, min(10, int(os.getenv("AWAY_LIGHT_FLOOR", "1"))))
    except ValueError:
        return 1


# --------------------------------------------------------------------------- #
# playbook planners -- each returns a bounded action dict, or None when the
# playbook can't help right now (role missing/offline, or already at the cap so
# the action would be a no-op). A None means "try the next playbook".
# --------------------------------------------------------------------------- #
def _same_cycle_speed(cycle_actions: list | None, dev: str, port: int) -> int | None:
    """Highest set_speed already WRITTEN to (dev, port) earlier this cycle (the
    AI / schedule fallback / emergency layers all run before away-mode), or None.
    The snapshot speed predates those writes, so planning from it alone could
    command a speed BELOW a stronger correction that just went out."""
    best = None
    for a in cycle_actions or []:
        if not isinstance(a, dict) or a.get("action") != "set_speed":
            continue
        if a.get("device") != dev or a.get("port") != port:
            continue
        try:
            v = int(a.get("value"))
        except (TypeError, ValueError):
            continue
        if best is None or v > best:
            best = v
    return best


def _plan_increase_exhaust(snapshot: dict, cycle_actions: list | None = None) -> dict | None:
    # The high-temp guardrail AND the CO2 dump both force the exhaust to max during an
    # active emergency -- don't fight either with a +1 step. The snapshot speed read
    # below predates this cycle's emergency enforcement, so a stale value could otherwise
    # drive the exhaust BELOW the forced maximum.
    if (snapshot.get("temp_emergency") or {}).get("active") \
            or (snapshot.get("co2_emergency") or {}).get("active"):
        return None
    dev, port = _parse_role("ROLE_EXHAUST", ("4 x 4", 2))
    p = _find_port(snapshot, dev, port)
    if p is None:
        return None
    cur = int(p.get("speed") or 0)
    # Effective current speed = max(snapshot, same-cycle write): if the AI or the
    # schedule already raised this port this cycle, step up FROM that raise --
    # never overwrite it with a lower speed planned off the stale snapshot.
    written = _same_cycle_speed(cycle_actions, dev, port)
    if written is not None and written > cur:
        cur = written
    mx = _exhaust_max()
    if cur >= mx:
        return None  # already at cap -> no-op
    target = min(cur + _exhaust_step(), mx)
    return {"device": dev, "port": port, "action": "set_speed", "value": target,
            "reason": f"exhaust {cur}->{target} to shed heat/VPD/CO2 load"}


def _plan_reduce_light(snapshot: dict, cycle_actions: list | None = None) -> dict | None:
    dev, port = _parse_role("ROLE_LIGHT", ("4 x 4", 1))
    p = _find_port(snapshot, dev, port)
    if p is None:
        return None
    cur = int(p.get("speed") or 0)
    floor = _light_floor()
    if cur <= floor:
        return None
    return {"device": dev, "port": port, "action": "set_speed", "value": cur - 1,
            "reason": f"light {cur}->{cur - 1} to ease heat/VPD (advisory: schedule pins intensity)"}


def _plan_disable_co2(snapshot: dict, cycle_actions: list | None = None) -> dict | None:
    valve = _co2_outlet()
    if valve is None:
        return None
    dev, port = valve
    p = _find_port(snapshot, dev, port)
    if p is not None and p.get("powered") is False:
        return None  # already off -> no-op
    return {"device": dev, "port": port, "action": "set_outlet", "value": False,
            "reason": "close CO2 valve until CO2 / res health recovers"}


# Dispatch policy per playbook: (mode, planner). mode is one of:
#   "live"     -> actuate when AWAY_MODE and not ADVISORY_MODE
#   "advisory" -> never actuate yet (conflicts with another controller); log intent
#   "dry"      -> hardware disconnected / chemical-gated; log intent only
_DISPATCH: dict[str, tuple[str, object]] = {
    "increase_exhaust_one_step": ("live", _plan_increase_exhaust),
    "reduce_light_one_step":     ("advisory", _plan_reduce_light),
    "disable_co2":               ("dry", _plan_disable_co2),
    "timed_nutrient_microdose":  ("dry", None),
    "timed_ph_up_microdose":     ("dry", None),
    "timed_ph_down_microdose":   ("dry", None),
}


def _plan_for(playbook: str, snapshot: dict,
              cycle_actions: list | None = None) -> dict | None:
    """Return a bounded action for `playbook`, or None if not applicable. Chemical
    playbooks (no planner) return an intent stub -- they are dry-run/logged only."""
    mode, planner = _DISPATCH.get(playbook, (None, None))
    if planner is None:
        if mode == "dry":  # chemical intent -- selectable, but never actuated here
            return {"playbook": playbook, "intent": True,
                    "reason": f"{playbook} would be considered (chemical, gated/dry-run)"}
        return None
    return planner(snapshot, cycle_actions)


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def select(snapshot: dict, cycle_actions: list | None = None) -> dict | None:
    """Pick the dispatch for this cycle from snapshot.diagnostics. Returns
    {worst, dispatch} or None when there are no stressors. `dispatch` is the
    chosen {stressor, severity, playbook, mode, plan} or None (alert-only).
    `cycle_actions` = actions already executed this cycle, so planners see
    same-cycle writes the snapshot predates."""
    stressors = (snapshot.get("diagnostics") or {}).get("stressors") or []
    if not stressors:
        return None

    chosen = None
    for s in stressors:
        for pb in s.get("allowed_playbooks", []):
            if pb == ALERT_ONLY:
                continue
            plan = _plan_for(pb, snapshot, cycle_actions)
            if plan is not None:
                mode, _ = _DISPATCH.get(pb, ("alert", None))
                chosen = {"stressor": s["name"], "severity": s["severity"],
                          "playbook": pb, "mode": mode, "plan": plan}
                break
        if chosen:
            break

    return {"worst": stressors[0], "dispatch": chosen}


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(snapshot: dict, devices: list, token: str,
        cycle_id: str | None = None,
        cycle_actions: list | None = None) -> list:
    """Evaluate + (when live) actuate away-mode triage. Inert unless AWAY_MODE.
    Always alerts on the worst stressor and logs the chosen dispatch; actuates a
    LIVE climate playbook only when not ADVISORY_MODE. `cycle_actions` = the
    actions already executed earlier this cycle (poller's running list), so
    step-up planning never downgrades a same-cycle raise. Returns executed
    actions."""
    if not _enabled():
        return []

    sel = select(snapshot, cycle_actions)
    if sel is None:
        return []

    worst = sel["worst"]
    dispatch = sel["dispatch"]
    advisory = _advisory()

    # Always alert on the worst stressor so an away operator is never blind.
    event_log.log_alert(cycle_id, worst["severity"], f"stressor: {worst['name']}",
                        worst.get("evidence"), worst_severity=worst["severity"])
    print(f"  [AWAY] worst: [{worst['severity']}] {worst['name']} -- {worst.get('evidence')}")

    if dispatch is None:
        print("  [AWAY] no actionable playbook -- alert only")
        return []

    pb = dispatch["playbook"]
    mode = dispatch["mode"]
    plan = dispatch["plan"]

    # Dry/advisory/chemical or advisory-mode global: log intent, don't actuate.
    if mode != "live" or advisory or plan.get("intent"):
        why = ("ADVISORY mode" if advisory and mode == "live"
               else f"{mode} playbook")
        print(f"  [AWAY] would dispatch {pb} for {dispatch['stressor']} "
              f"({why}): {plan.get('reason')}")
        aid = event_log.log_action_request(cycle_id, {**plan, "playbook": pb},
                                           source="away_mode")
        event_log.log_action_validation(cycle_id, aid, True, stage="away_dispatch")
        event_log.log_action_execution(cycle_id, aid, executed=False, success=False,
                                       reason=f"not actuated ({why})",
                                       playbook=pb, device=plan.get("device"),
                                       port=plan.get("port"))
        return []

    # LIVE climate dispatch -- through the SAME gated, read-after-write path as every
    # AI action, never a private one.
    #
    # This used to call set_port_speed/set_outlet directly, which skipped
    # validate_actions + filter_actions (schema, speed caps, the chemical interlock) AND
    # the read-after-write verification -- so a dispatch was logged success=True on
    # nothing more than an HTTP call that did not raise. Two independent reviews flagged
    # it, and it is the clearest instance of the pattern they both found: the safety net
    # is solid on the main path and thin on every path that bypasses execute_actions.
    # Routing through it makes the gate structural rather than a convention this module
    # happened not to follow. execute_actions tags the ledger with source="away_mode",
    # so provenance survives the move.
    from ai_advisor import execute_actions
    action = {**plan, "playbook": pb, "source": "away_mode"}
    try:
        executed = execute_actions({"actions": [action]}, devices, token,
                                   snapshot=snapshot, cycle_id=cycle_id,
                                   source="away_mode")
    except Exception as e:
        print(f"  [AWAY] FAILED {pb} on {plan['device']} port {plan['port']}: {e}")
        return []
    if not executed:
        print(f"  [AWAY] {pb} NOT dispatched -- rejected by the safety gate or the write "
              f"failed (see the [VALIDATE]/[SAFETY]/[EXEC] lines above)")
        return []
    print(f"  [AWAY] DISPATCH {pb}: {plan['device']} port {plan['port']} -> "
          f"{plan['action']}={plan['value']}  ({plan['reason']})")
    return [{**a, "playbook": pb, "source": "away_mode"} for a in executed]
