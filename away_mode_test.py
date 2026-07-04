#!/usr/bin/env python3
"""
Self-tests for the away-mode triage executor (away_mode.py). No hardware: the
AC Infinity writes are monkeypatched and the event log is redirected to a temp
dir. Covers selection (worst-first, actionable), the live/advisory/dry dispatch
policy, gating (AWAY_MODE + ADVISORY_MODE), and bounded exhaust stepping.
Run: python3 away_mode_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="away_test_"))
import runtime_state
runtime_state._EVENT_LOG = _TMP / "events.jsonl"

import away_mode
import diagnostics

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")


def reset(**env):
    for k in ("AWAY_MODE", "ADVISORY_MODE", "AWAY_EXHAUST_STEP", "AWAY_EXHAUST_MAX",
              "AWAY_LIGHT_FLOOR", "ROLE_EXHAUST", "ROLE_LIGHT", "CO2_VALVE"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


# Captured hardware writes.
_writes = []


def _install_fake_client():
    import ac_infinity_client as cli
    cli.set_port_speed = lambda token, dev_id, port, speed, dev_type: _writes.append(("speed", dev_id, port, speed))
    cli.set_outlet = lambda token, dev_id, port, val, dev_type: _writes.append(("outlet", dev_id, port, val))


_install_fake_client()
DEVS = [{"name": "4 x 4", "dev_id": "D1", "type": 20}]


def snap_with(stressors, **extra):
    """Build a snapshot whose diagnostics carries the given stressor names, plus
    the ports needed for planning. `extra` overrides port speeds etc."""
    exhaust_speed = extra.get("exhaust_speed", 3)
    light_speed = extra.get("light_speed", 10)
    ports = [{"port": 2, "speed": exhaust_speed}, {"port": 1, "speed": light_speed}]
    diag_stressors = []
    for name in stressors:
        diag_stressors.append({
            "name": name, "severity": extra.get("sev", "high"),
            "evidence": f"{name} evidence",
            "allowed_playbooks": diagnostics.allowed_playbooks(name),
        })
    s = {"devices": [{"name": "4 x 4", "ports": ports}],
         "diagnostics": {"stressors": diag_stressors,
                         "count": len(diag_stressors),
                         "worst_severity": diag_stressors[0]["severity"] if diag_stressors else "none"}}
    if extra.get("temp_emergency"):
        s["temp_emergency"] = {"active": True}
    if extra.get("co2_emergency"):
        s["co2_emergency"] = {"active": True}
    return s


def run(snapshot, cycle_actions=None):
    _writes.clear()
    return away_mode.run(snapshot, DEVS, "TOK", cycle_id="C1",
                         cycle_actions=cycle_actions)


# --- gating ------------------------------------------------------------------

def test_inert_when_disabled():
    reset()  # AWAY_MODE unset
    out = run(snap_with(["tent_temp_high"]))
    check("inert when AWAY_MODE unset", out == [] and _writes == [])


def test_no_stressors_no_action():
    reset(AWAY_MODE="true", ADVISORY_MODE="false")
    out = run(snap_with([]))
    check("no stressors -> nothing", out == [] and _writes == [])


# --- live exhaust dispatch ---------------------------------------------------

def test_exhaust_dispatch_live():
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=3))
    check("exhaust dispatched live", len(out) == 1 and out[0]["playbook"] == "increase_exhaust_one_step")
    check("exhaust stepped +1 (3->4)", _writes == [("speed", "D1", 2, 4)])
    check("dispatch tagged away_mode source", out[0]["source"] == "away_mode")


def test_exhaust_capped():
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2", AWAY_EXHAUST_MAX="6")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=6))
    # Exhaust already at cap -> no exhaust action; falls through to reduce_light (advisory) -> no write.
    check("no write when exhaust at cap", _writes == [])
    check("capped exhaust returns nothing actuated", out == [])


def test_exhaust_yields_to_guardrail():
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=3, temp_emergency=True))
    check("away-mode yields exhaust to active guardrail", _writes == [])


def test_exhaust_yields_to_co2_emergency():
    # A CO2 dump also forces the exhaust to max; away-mode must not step it (a stale
    # snapshot speed could otherwise drive the exhaust BELOW the forced maximum). Uses the
    # same tent_temp_high stressor as the guardrail test so it WOULD step exhaust but for
    # the yield.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=3, co2_emergency=True))
    check("away-mode yields exhaust to active CO2 dump", _writes == [] and out == [])


def test_custom_step():
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="3", AWAY_EXHAUST_MAX="10")
    run(snap_with(["tent_temp_high"], exhaust_speed=4))
    check("custom step 4->7", _writes == [("speed", "D1", 2, 7)])


# --- same-cycle writes: never downgrade a raise that already went out ---------

def test_same_cycle_raise_not_downgraded():
    # AI raised exhaust 4->8 earlier this cycle; the snapshot still says 4.
    # Away-mode must plan from the effective 8, never write below it.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    acts = [{"device": "4 x 4", "port": 2, "action": "set_speed", "value": 8}]
    run(snap_with(["tent_temp_high"], exhaust_speed=4), cycle_actions=acts)
    check("same-cycle raise: steps from 8 not 4", _writes == [("speed", "D1", 2, 9)])
    check("same-cycle raise: nothing written below 8",
          all(w[3] >= 8 for w in _writes if w[0] == "speed" and w[2] == 2))


def test_same_cycle_raise_at_cap_no_op():
    # Same-cycle write already at the cap -> exhaust step is a no-op (falls
    # through to reduce_light, which is advisory) -> no write at all.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_MAX="10")
    acts = [{"device": "4 x 4", "port": 2, "action": "set_speed", "value": 10}]
    out = run(snap_with(["tent_temp_high"], exhaust_speed=4), cycle_actions=acts)
    check("same-cycle write at cap: no exhaust write", _writes == [] and out == [])


def test_same_cycle_write_other_port_ignored():
    # A raise on a DIFFERENT port must not affect exhaust planning.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    acts = [{"device": "4 x 4", "port": 1, "action": "set_speed", "value": 8}]
    run(snap_with(["tent_temp_high"], exhaust_speed=4), cycle_actions=acts)
    check("other-port write ignored (4->5)", _writes == [("speed", "D1", 2, 5)])


def test_same_cycle_lower_write_uses_snapshot():
    # Effective speed = max(snapshot, same-cycle write): a same-cycle STOP/lower
    # write never pulls the planning base below the snapshot.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    acts = [{"device": "4 x 4", "port": 2, "action": "set_speed", "value": 0}]
    run(snap_with(["tent_temp_high"], exhaust_speed=4), cycle_actions=acts)
    check("same-cycle lower write: plans from snapshot (4->5)",
          _writes == [("speed", "D1", 2, 5)])


def test_same_cycle_junk_entries_tolerated():
    # Non-set_speed / malformed entries (dose, set_outlet, bad value) in the
    # cycle-actions list must be skipped, not crash planning.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          AWAY_EXHAUST_STEP="1", AWAY_EXHAUST_MAX="10")
    acts = [
        {"device": "Hydroponics Control", "action": "dose", "playbook": "timed_ph_down_microdose"},
        {"device": "4 x 4", "port": 2, "action": "set_outlet", "value": True},
        {"device": "4 x 4", "port": 2, "action": "set_speed", "value": "junk"},
        {"device": "4 x 4", "port": 2, "action": "set_speed", "value": 7},
    ]
    run(snap_with(["tent_temp_high"], exhaust_speed=4), cycle_actions=acts)
    check("junk entries skipped, valid raise honored (7->8)",
          _writes == [("speed", "D1", 2, 8)])


# --- advisory / dry: log intent, never actuate -------------------------------

def test_advisory_mode_no_actuation():
    reset(AWAY_MODE="true", ADVISORY_MODE="true", ROLE_EXHAUST="4 x 4:2")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=3))
    check("ADVISORY mode: exhaust NOT actuated", _writes == [] and out == [])


def test_reduce_light_is_advisory():
    # Exhaust maxed -> next allowed playbook is reduce_light (advisory) -> no actuation.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2",
          ROLE_LIGHT="4 x 4:1", AWAY_EXHAUST_MAX="10", AWAY_LIGHT_FLOOR="1")
    out = run(snap_with(["tent_temp_high"], exhaust_speed=10, light_speed=8))
    check("reduce_light never actuates (advisory)", _writes == [] and out == [])


def test_chemical_playbook_dry_run():
    # ph_high allows timed_ph_down_microdose (dry) -> logged intent, no actuation.
    reset(AWAY_MODE="true", ADVISORY_MODE="false")
    out = run(snap_with(["ph_high"]))
    check("chemical playbook dry-run, no actuation", _writes == [] and out == [])


def test_alert_only_stressor():
    # water_temp_high allows only alert_only -> alert, no dispatch.
    reset(AWAY_MODE="true", ADVISORY_MODE="false")
    out = run(snap_with(["water_temp_high"], sev="medium"))
    check("alert-only stressor: no actuation", _writes == [] and out == [])


# --- selection ordering ------------------------------------------------------

def test_worst_first_actionable():
    # water_temp_high (alert-only) listed first, but tent_temp_high is actionable.
    # Severity tie here; selection should still find the actionable exhaust dispatch.
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2")
    s = snap_with(["water_temp_high", "tent_temp_high"], exhaust_speed=2)
    out = run(s)
    check("skips alert-only stressor to actionable one", _writes == [("speed", "D1", 2, 3)])
    check("dispatch is exhaust", out and out[0]["playbook"] == "increase_exhaust_one_step")


# --- ledger ------------------------------------------------------------------

def test_alert_logged():
    reset(AWAY_MODE="true", ADVISORY_MODE="false", ROLE_EXHAUST="4 x 4:2")
    run(snap_with(["tent_temp_high"], exhaust_speed=3))
    import event_log
    alerts = [e for e in event_log._read_events() if e.get("type") == "alert"]
    check("alert recorded for worst stressor", any("tent_temp_high" in (a.get("title") or "") for a in alerts))
    execs = [e for e in event_log._read_events()
             if e.get("type") == "action_execution" and e.get("playbook") == "increase_exhaust_one_step"]
    check("execution recorded with playbook + success", execs and execs[-1]["success"] is True)


def main():
    print("Away-mode executor self-tests")
    print("=" * 44)
    for fn in (
        test_inert_when_disabled,
        test_no_stressors_no_action,
        test_exhaust_dispatch_live,
        test_exhaust_capped,
        test_exhaust_yields_to_guardrail,
        test_exhaust_yields_to_co2_emergency,
        test_custom_step,
        test_same_cycle_raise_not_downgraded,
        test_same_cycle_raise_at_cap_no_op,
        test_same_cycle_write_other_port_ignored,
        test_same_cycle_lower_write_uses_snapshot,
        test_same_cycle_junk_entries_tolerated,
        test_advisory_mode_no_actuation,
        test_reduce_light_is_advisory,
        test_chemical_playbook_dry_run,
        test_alert_only_stressor,
        test_worst_first_actionable,
        test_alert_logged,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
