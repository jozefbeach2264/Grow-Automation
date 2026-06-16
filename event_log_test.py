#!/usr/bin/env python3
"""
Self-tests for the event ledger (event_log.py). No hardware: the JSONL path is
redirected to a temp dir and records are read back. Run: python3 event_log_test.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import runtime_state

_TMP = Path(tempfile.mkdtemp(prefix="evlog_test_"))
runtime_state._EVENT_LOG = _TMP / "events.jsonl"

import event_log

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


def reset():
    try:
        runtime_state._EVENT_LOG.unlink()
    except FileNotFoundError:
        pass


def read():
    return event_log._read_events()


def by_type(t):
    return [e for e in read() if e.get("type") == t]


def snap(**kw):
    base = {
        "grow_week": 2, "grow_stage": "veg", "water_level_source": "FLOAT",
        "res_health": {"state": "IDEAL", "water_trend": "FALLING",
                       "ec_trend": "STATIC", "co2_gate": "ADVANCE",
                       "dose_gate": "NORMAL", "ph_gate": "ALLOW"},
        "schedule_deltas": [{"kind": "light"}],
        "devices": [{"name": "4 x 4", "sensors": {"temp_f_tent": 78.0, "co2_ppm": 900}}],
    }
    base.update(kw)
    return base


# --- cycle -------------------------------------------------------------------

def test_start_cycle():
    reset()
    cid = event_log.start_cycle(snap(), mode="live")
    check("start_cycle returns an id", isinstance(cid, str) and len(cid) >= 8)
    recs = by_type("cycle")
    check("one cycle record written", len(recs) == 1)
    r = recs[0]
    check("cycle_id threaded", r["cycle_id"] == cid)
    check("mode recorded", r["mode"] == "live")
    check("res-health gates captured", r["dose_gate"] == "NORMAL" and r["ph_gate"] == "ALLOW")
    check("flat sensors captured", r["sensors"]["temp_f_tent"] == 78.0 and r["sensors"]["co2_ppm"] == 900)
    check("schedule delta count", r["n_schedule_deltas"] == 1)
    check("emergency flags default false", r["temp_emergency"] is False and r["res_burst"] is False)


def test_start_cycle_emergencies():
    reset()
    s = snap(temp_emergency={"active": True}, co2_emergency={"active": False},
             res_burst={"active": True}, leak={"wet": True})
    event_log.start_cycle(s)
    r = by_type("cycle")[0]
    check("temp_emergency active reflected", r["temp_emergency"] is True)
    check("co2_emergency inactive reflected", r["co2_emergency"] is False)
    check("res_burst active reflected", r["res_burst"] is True)
    check("leak_wet reflected", r["leak_wet"] is True)


def test_start_cycle_sparse_snapshot():
    # Missing res_health / devices must not raise.
    reset()
    cid = event_log.start_cycle({"grow_week": 1})
    r = by_type("cycle")[0]
    check("sparse snapshot still logs", isinstance(cid, str))
    check("missing gates -> None", r["dose_gate"] is None and r["res_health"] is None)
    check("missing sensors -> empty", r["sensors"] == {})


# --- ai decision -------------------------------------------------------------

def test_ai_decision():
    reset()
    res = {"assessment": "all nominal", "actions": [{"a": 1}, {"a": 2}],
           "next_check_seconds": 120, "notify_user": False}
    event_log.log_ai_decision("C1", res, latency_sec=3.456)
    r = by_type("ai_decision")[0]
    check("parsed_ok true with result", r["parsed_ok"] is True)
    check("action count", r["n_actions"] == 2)
    check("latency rounded", r["latency_sec"] == 3.46)
    check("next_check passed", r["next_check_seconds"] == 120)


def test_ai_decision_failure():
    reset()
    event_log.log_ai_decision("C1", None)
    r = by_type("ai_decision")[0]
    check("parsed_ok false on None result", r["parsed_ok"] is False)
    check("n_actions zero on None", r["n_actions"] == 0)


def test_assessment_clipped():
    reset()
    event_log.log_ai_decision("C1", {"assessment": "x" * 500})
    r = by_type("ai_decision")[0]
    check("long assessment clipped", len(r["assessment"]) <= 283 and r["assessment"].endswith("..."))


# --- action lifecycle --------------------------------------------------------

def test_action_request():
    reset()
    aid = event_log.log_action_request("C1", {
        "device": "4 x 4", "port": 2, "action": "set_speed", "value": 10,
        "reason": "high-temp guardrail"}, source="deterministic_emergency")
    r = by_type("action_request")[0]
    check("action_request returns id", isinstance(aid, str) and len(aid) >= 8)
    check("request fields captured",
          r["device"] == "4 x 4" and r["port"] == 2 and r["action_type"] == "set_speed")
    check("source captured", r["source"] == "deterministic_emergency")
    check("value jsonsafe", r["value"] == 10)


def test_validation_and_execution():
    reset()
    aid = "A1"
    event_log.log_action_validation("C1", aid, False, reason="blocked_by_safety_gate", stage="safety_gate")
    event_log.log_action_execution("C1", "A2", executed=True, success=True,
                                   verified=True, device="4 x 4", port=2,
                                   command_type="set_speed", value_sent=10)
    v = by_type("action_validation")[0]
    check("validation reason captured", v["valid"] is False and v["reason"] == "blocked_by_safety_gate")
    check("validation stage captured", v["stage"] == "safety_gate")
    e = by_type("action_execution")[0]
    check("execution success+verified", e["success"] is True and e["verified"] is True)
    check("execution fields", e["value_sent"] == 10 and e["command_type"] == "set_speed")


def test_outcome():
    reset()
    event_log.log_action_outcome("C1", "A1", True, status="matched_expected_model",
                                 delta_ph=-0.3)
    r = by_type("action_outcome")[0]
    check("outcome success", r["success"] is True)
    check("outcome extra fields", r["status"] == "matched_expected_model" and r["delta_ph"] == -0.3)


# --- recent_actions ----------------------------------------------------------

def _append_exec(wall_ts, device, port, success=True):
    """Append a raw action_execution event with a custom wall_ts (for windowing)."""
    rec = {"wall_time_utc": "x", "wall_ts": wall_ts, "monotonic": 0.0, "pid": 1,
           "type": "action_execution", "device": device, "port": port,
           "command_type": "set_speed", "value_sent": 5, "success": success,
           "executed": True}
    with runtime_state._EVENT_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def test_recent_actions():
    reset()
    now = time.time()
    _append_exec(now - 10 * 3600, "old_dev", 1)      # 10h ago
    _append_exec(now - 60, "recent_dev", 2)          # 1m ago
    _append_exec(now - 30 * 3600, "stale_dev", 3)    # 30h ago (outside 24h)
    out = event_log.recent_actions(limit=10, window_hours=24)
    devs = [a["device"] for a in out]
    check("excludes events older than window", "stale_dev" not in devs)
    check("includes in-window events", "recent_dev" in devs and "old_dev" in devs)
    check("newest first", devs[0] == "recent_dev")
    check("age_minutes computed", out[0]["age_minutes"] < 2)


def test_recent_actions_limit():
    reset()
    now = time.time()
    for i in range(5):
        _append_exec(now - i * 60, f"dev{i}", i)
    out = event_log.recent_actions(limit=3, window_hours=24)
    check("limit honored", len(out) == 3)
    check("limit keeps newest", out[0]["device"] == "dev0")


def test_recent_actions_empty():
    reset()
    check("no log -> empty list", event_log.recent_actions() == [])


# --- robustness --------------------------------------------------------------

def test_corrupt_lines_skipped():
    reset()
    with runtime_state._EVENT_LOG.open("a") as f:
        f.write("not json\n")
        f.write('{"type": "action_execution", "wall_ts": ' + str(time.time()) + ', "device": "ok", "port": 1, "success": true}\n')
        f.write("\n")
    out = event_log.recent_actions()
    check("corrupt lines skipped, valid kept", len(out) == 1 and out[0]["device"] == "ok")


def main():
    print("Event ledger self-tests")
    print("=" * 44)
    for fn in (
        test_start_cycle,
        test_start_cycle_emergencies,
        test_start_cycle_sparse_snapshot,
        test_ai_decision,
        test_ai_decision_failure,
        test_assessment_clipped,
        test_action_request,
        test_validation_and_execution,
        test_outcome,
        test_recent_actions,
        test_recent_actions_limit,
        test_recent_actions_empty,
        test_corrupt_lines_skipped,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
