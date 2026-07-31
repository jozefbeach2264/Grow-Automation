#!/usr/bin/env python3
"""
Self-tests for the safety gate + dose verb (ai_advisor). No hardware:
validate_actions / filter_actions are pure given a snapshot; the execute_actions dose
path is exercised with the SIM token and dosing.timed_dose* monkeypatched, and the
AUTONOMOUS_DOSING gate is toggled. State files are redirected to a temp dir.
Run: python3 safety_gate_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="gate_test_"))

import runtime_state
runtime_state._STATE_FILE = _TMP / ".runtime_state.json"
runtime_state._EVENT_LOG = _TMP / "events.jsonl"
import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

# Reservoir device port roles for the synthetic "TestRes" device.
os.environ["DOSER_PORTS_TESTRES"] = "1,2,3,4"
os.environ["PH_PORTS_TESTRES"] = "3,4"
os.environ.pop("DOSING_DISABLED", None)
os.environ["RES_CHANGE_MODE"] = "false"
os.environ["MAX_DOSE_ML_CYCLE"] = "50"
os.environ.pop("AUTONOMOUS_DOSING", None)        # default off
os.environ.pop("GROW_START_DATE", None)          # force manual week/stage

import ai_advisor
import dosing
ai_advisor._LOCKOUT_FILE = _TMP / ".lockouts.json"   # redirect lockout persistence

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
    ai_advisor._last_dose_time.clear()
    ai_advisor._last_ph_time = 0.0
    safety_state.clear_dosing_disable()
    for f in (runtime_state._STATE_FILE, safety_state._STATE_FILE, ai_advisor._LOCKOUT_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def snap(ph=6.0, tds=400, dose_gate="NORMAL", ph_gate="ALLOW", co2_gate="ADVANCE"):
    sensors = {}
    if ph is not None:
        sensors["ph"] = ph
    if tds is not None:
        sensors["tds_ppm"] = tds
    ports = [{"port": n, "speed": 0, "mode": 2} for n in (1, 2, 3, 4)]
    return {
        "res_health": {"dose_gate": dose_gate, "ph_gate": ph_gate, "co2_gate": co2_gate},
        "devices": [{"name": "TestRes", "ports": ports, "sensors": sensors}],
    }


def dose(playbook):
    return {"device": "TestRes", "action": "dose", "playbook": playbook, "reason": "t"}


def has_dose(actions, playbook):
    return any(a.get("action") == "dose" and a.get("playbook") == playbook for a in actions)


# =========================================================================== #
print("\n== validate_actions: dose verb ==")
reset()
v = ai_advisor.validate_actions([dose("timed_ph_down_microdose")], snapshot=snap())
check("valid dose accepted", len(v) == 1 and v[0].get("_resolved", {}).get("ports") == [4])
check("ph_down resolves to port 4", v[0]["_resolved"]["ports"] == [4])

v = ai_advisor.validate_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("nutrient resolves to pair [1,2]", v and v[0]["_resolved"]["ports"] == [1, 2])

v = ai_advisor.validate_actions([dose("not_a_real_playbook")], snapshot=snap())
check("unknown playbook rejected", v == [])

v = ai_advisor.validate_actions([{"device": "TestRes", "action": "dose"}], snapshot=snap())
check("dose missing playbook rejected", v == [])


print("\n== filter_actions: chemical interlock ==")
reset()
out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 1}], snapshot=snap())
check("raw chem set_speed>0 BLOCKED by interlock", out == [])

out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 0}], snapshot=snap())
check("chem stop (value 0) ALLOWED", len(out) == 1)

out = ai_advisor.filter_actions(
    [{"device": "TestTent", "port": 1, "action": "set_speed", "value": 8}], snapshot=snap())
check("climate set_speed on non-chem device ALLOWED", len(out) == 1 and out[0]["value"] == 8)


print("\n== filter_actions: dose gating ==")
reset()
out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap(dose_gate="NORMAL"))
check("nutrient dose passes when dose_gate NORMAL", has_dose(out, "timed_nutrient_microdose"))

out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap(dose_gate="HOLD"))
check("nutrient dose blocked when dose_gate HOLD", out == [])

out = ai_advisor.filter_actions([dose("timed_ph_down_microdose")], snapshot=snap(ph_gate="ALLOW"))
check("pH dose passes when ph_gate ALLOW", has_dose(out, "timed_ph_down_microdose"))

out = ai_advisor.filter_actions([dose("timed_ph_down_microdose")], snapshot=snap(ph_gate="HOLD"))
check("pH dose blocked when ph_gate HOLD", out == [])

out = ai_advisor.filter_actions([dose("timed_ph_down_microdose")], snapshot=snap(ph=None))
check("pH dose blocked with no pH reading", out == [])

reset()
safety_state.disable_dosing("unit test freeze")
out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("dose blocked while dosing frozen", out == [])
safety_state.clear_dosing_disable()

print("\n== filter_actions: one-pH-per-cycle + lockout + mL ceiling ==")
reset()
out = ai_advisor.filter_actions(
    [dose("timed_ph_down_microdose"), dose("timed_ph_up_microdose")], snapshot=snap())
check("only one pH dose per cycle", len(out) == 1)

reset()
ai_advisor.record_actions([{"device": "TestRes", "action": "dose",
                            "playbook": "timed_nutrient_microdose", "ports": [1, 2]}])
out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("dose blocked by per-port lockout after a recent dose", out == [])

reset()
os.environ["MAX_DOSE_ML_CYCLE"] = "1"     # 5 mL nutrient microdose exceeds this
out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("dose blocked when target_ml exceeds MAX_DOSE_ML_CYCLE", out == [])
os.environ["MAX_DOSE_ML_CYCLE"] = "50"


print("\n== filter_actions: nutrient per-cycle dedup (regression #4) ==")
reset()
out = ai_advisor.filter_actions(
    [dose("timed_nutrient_microdose"), dose("timed_nutrient_small")], snapshot=snap())
check("two nutrient doses in one cycle -> only ONE passes (no 2x over-dose)", len(out) == 1)
reset()
out = ai_advisor.filter_actions(
    [dose("timed_nutrient_microdose"), dose("timed_nutrient_microdose")], snapshot=snap())
check("duplicate nutrient dose deduped to one", len(out) == 1)
reset()
# A nutrient (ports 1+2) and a pH (port 4) dose touch different ports -> both allowed.
out = ai_advisor.filter_actions(
    [dose("timed_nutrient_microdose"), dose("timed_ph_down_microdose")], snapshot=snap())
check("nutrient + pH in one cycle both pass (no false collision)", len(out) == 2)


print("\n== filter_actions: STOP always allowed, even inside lockout ==")
reset()
# Drive ports 1-4 into an active dose/pH lockout, then try to STOP them.
ai_advisor.record_actions([
    {"device": "TestRes", "action": "dose", "playbook": "timed_nutrient_microdose", "ports": [1, 2]},
    {"device": "TestRes", "action": "dose", "playbook": "timed_ph_down_microdose", "ports": [4]},
])
out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 0}], snapshot=snap())
check("doser STOP passes despite active lockout", len(out) == 1)
out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 4, "action": "set_speed", "value": 0}], snapshot=snap())
check("pH STOP passes despite active pH lockout", len(out) == 1)
# A pH STOP must NOT consume the one-pH-per-cycle budget -> a real pH dose still allowed.
reset()
out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 4, "action": "set_speed", "value": 0},
     dose("timed_ph_up_microdose")], snapshot=snap())
check("pH STOP does not consume the one-pH-per-cycle budget",
      has_dose(out, "timed_ph_up_microdose"))


print("\n== execute_actions: AUTONOMOUS_DOSING gate ==")
reset()
DEVS = [{"name": "TestRes", "dev_id": "d-test", "type": 20}]
_dose_calls = []
dosing.timed_dose = lambda *a, **k: (_dose_calls.append(("single", a, k)) or {"ok": True})
dosing.timed_dose_pair = lambda *a, **k: (_dose_calls.append(("pair", a, k)) or {"ok": True})

os.environ.pop("AUTONOMOUS_DOSING", None)   # off
_dose_calls.clear()
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_nutrient_microdose")]}, DEVS, "SIM", snapshot=snap())
check("AUTONOMOUS_DOSING off -> dose NOT executed (advisory)", executed == [])
check("AUTONOMOUS_DOSING off -> timed_dose never called", _dose_calls == [])

os.environ["AUTONOMOUS_DOSING"] = "true"
_dose_calls.clear()
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_nutrient_microdose")]}, DEVS, "SIM", snapshot=snap())
check("AUTONOMOUS_DOSING on -> dose executed", len(executed) == 1 and executed[0]["action"] == "dose")
check("AUTONOMOUS_DOSING on -> timed_dose_pair routed", _dose_calls and _dose_calls[0][0] == "pair")
os.environ.pop("AUTONOMOUS_DOSING", None)


print("\n== _verify_executed_action: failed chem STOP freezes dosing (regression #14) ==")
import ac_infinity_client as acic
_orig_v = (acic.set_port_speed, acic.verify_port_state, acic.stop_and_verify)
acic.set_port_speed = lambda *a, **k: None
acic.verify_port_state = lambda *a, **k: {"ok": False, "reason": "still_running",
                                          "observed": {"speed_actual": 5}, "elapsed_sec": 1, "attempts": 1}
acic.stop_and_verify = lambda *a, **k: {"ok": False, "reason": "still_running",
                                        "observed": {"speed_actual": 5}, "elapsed_sec": 1, "attempts": 2}
os.environ["VERIFY_WRITES"] = "true"
reset()
# A raw chemical STOP (value 0 on pH port 4) passes the gate (stops always allowed),
# executes, then fails verification + retry -> the chemical freeze fires. Uses a NON-SIM
# token so verification actually runs (SIM skips it). This is the safety link the freeze
# was built for and was previously untested.
ai_advisor.execute_actions(
    {"actions": [{"device": "TestRes", "port": 4, "action": "set_speed", "value": 0}]},
    DEVS, "TOKEN", snapshot=snap())
check("unverified chem STOP freezes dosing", safety_state.is_dosing_disabled() is True)
# Happy path: a verified stop must NOT freeze.
acic.verify_port_state = lambda *a, **k: {"ok": True, "reason": "", "observed": {"speed_actual": 0},
                                          "elapsed_sec": 1, "attempts": 1}
reset()
ai_advisor.execute_actions(
    {"actions": [{"device": "TestRes", "port": 4, "action": "set_speed", "value": 0}]},
    DEVS, "TOKEN", snapshot=snap())
check("verified chem STOP does NOT freeze", safety_state.is_dosing_disabled() is False)
acic.set_port_speed, acic.verify_port_state, acic.stop_and_verify = _orig_v


print("\n== reason collectors: precise ledger reasons ==")
reset()
# validate_actions populates the reasons collector with (action, code) per rejection.
r = []
ai_advisor.validate_actions([
    {"device": "Ghost", "port": 9, "action": "set_speed", "value": 3},
    {"device": "TestRes", "port": 1, "action": "set_speed", "value": 99},
    {"device": "TestRes", "action": "dose", "playbook": "not_real"},
], snapshot=snap(), reasons=r)
codes = {c for _, c in r}
check("validate collector: unknown_device", "unknown_device" in codes)
check("validate collector: value_range", "value_range" in codes)
check("validate collector: invalid_dose", "invalid_dose" in codes)
check("validate collector size matches rejections", len(r) == 3)

# filter_actions collector carries the safety-gate block code.
reset()
rf = []
ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 1}],
    snapshot=snap(), reasons=rf)
check("filter collector: raw_chem_not_permitted", rf and rf[0][1] == "raw_chem_not_permitted")

reset()
rf = []
ai_advisor.filter_actions([dose("timed_ph_down_microdose")],
                          snapshot=snap(ph_gate="HOLD"), reasons=rf)
check("filter collector: ph_gate_hold reason on blocked pH dose",
      rf and "ph_gate" in rf[0][1].lower())

# Collector is opt-in: omitting it leaves behavior identical (no crash, same filtering).
reset()
out = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 0}], snapshot=snap())
check("collector omitted -> filtering unchanged (stop allowed)", len(out) == 1)


print("\n== schedule clamp: bloom week > 8 holds the flush ==")
os.environ["GROW_STAGE"] = "bloom"
os.environ["GROW_WEEK"] = "9"
os.environ.pop("PPM_BLOOM_WK9", None)
os.environ["PPM_SCALE"] = "500"
check("bloom wk9 PPM clamps to wk8 flush (0)", ai_advisor._get_ppm_target() == 0)
check("bloom wk9 CO2 clamps to wk8 (400)", ai_advisor._get_co2_target() == 400)
check("bloom wk9 pH clamps to wk8 (6.0-6.5)", ai_advisor._get_ph_range() == (6.0, 6.5))


print("\n== record_actions: executed STOPS never re-stamp lockout clocks (F6) ==")
import time
reset()
# Drive nutrient + pH ports into lockout, note the clocks, then record executed stops.
ai_advisor.record_actions([
    {"device": "TestRes", "action": "dose", "playbook": "timed_nutrient_microdose", "ports": [1, 2]},
    {"device": "TestRes", "action": "dose", "playbook": "timed_ph_down_microdose", "ports": [4]},
])
dose_clock_before = dict(ai_advisor._last_dose_time)
ph_clock_before = ai_advisor._last_ph_time
time.sleep(0.05)
ai_advisor.record_actions([
    {"device": "TestRes", "port": 1, "action": "set_speed", "value": 0},
    {"device": "TestRes", "port": 4, "action": "set_speed", "value": 0},
    {"device": "TestRes", "port": 2, "action": "set_outlet", "value": False},
])
check("executed stop leaves per-port dose clocks unchanged",
      ai_advisor._last_dose_time == dose_clock_before)
check("executed stop leaves the global pH clock unchanged",
      ai_advisor._last_ph_time == ph_clock_before)
ai_advisor.record_actions([{"device": "TestRes", "port": 1, "action": "set_speed", "value": 3}])
check("a chemical-moving action still stamps its clock",
      ai_advisor._last_dose_time["TestRes:1"] > dose_clock_before["TestRes:1"])

# Full path: an executed stop DURING an active lockout must not restart it.
reset()
_orig_sps = acic.set_port_speed
acic.set_port_speed = lambda *a, **k: None
ai_advisor.record_actions([{"device": "TestRes", "action": "dose",
                            "playbook": "timed_ph_down_microdose", "ports": [4]}])
ph_before = ai_advisor._last_ph_time
dose_before = dict(ai_advisor._last_dose_time)
time.sleep(0.05)
executed = ai_advisor.execute_actions(
    {"actions": [{"device": "TestRes", "port": 4, "action": "set_speed", "value": 0}]},
    DEVS, "SIM", snapshot=snap())
check("stop executes during the active lockout (stops always allowed)", len(executed) == 1)
check("in-lockout executed stop leaves the pH expiry unchanged",
      ai_advisor._last_ph_time == ph_before)
check("in-lockout executed stop leaves the dose clocks unchanged",
      ai_advisor._last_dose_time == dose_before)
acic.set_port_speed = _orig_sps


print("\n== _execute_dose: PARTIAL pair delivery is tracked + stamps lockout (F7) ==")
reset()
os.environ["AUTONOMOUS_DOSING"] = "true"
# Port 1 started and ran, port 2's start write failed -> ok=False but chem moved.
dosing.timed_dose_pair = lambda *a, **k: {
    "ok": False, "started": [1], "start_failed": 2,
    "delivered_ml_each": {1: 3.2, 2: 0.0}, "stop_results": {1: True, 2: True}}
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_nutrient_microdose")]}, DEVS, "SIM", snapshot=snap())
check("partial dose IS tracked (returned as executed)",
      len(executed) == 1 and executed[0].get("partial") is True)
check("partial tracked under its ':partial' playbook (own calibration bucket)",
      executed[0]["playbook"] == "timed_nutrient_microdose:partial")
check("partial tracks only the STARTED port(s)", executed[0]["ports"] == [1])
check("partial carries the best delivered estimate",
      executed[0]["delivered_ml_each"] == {1: 3.2, 2: 0.0})
check("partial stamps the started port's dose lockout",
      "TestRes:1" in ai_advisor._last_dose_time)
check("never-started port is NOT locked out",
      "TestRes:2" not in ai_advisor._last_dose_time)
out = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("next-cycle full re-dose blocked by the partial's lockout", out == [])

# ok=False with NOTHING started (pre-check abort / frozen) stays untracked.
reset()
dosing.timed_dose_pair = lambda *a, **k: {"ok": False,
                                          "reason": "port 2 not at 0 before pair dose"}
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_nutrient_microdose")]}, DEVS, "SIM", snapshot=snap())
check("no-start failure stays untracked (no phantom lockout)",
      executed == [] and ai_advisor._last_dose_time == {})

# pH single-port partial (unverified stop) stamps the global pH clock too.
reset()
dosing.timed_dose = lambda *a, **k: {"ok": False, "started": [4],
                                     "delivered_ml_each": {4: 0.5}, "stop_verified": False}
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_ph_down_microdose")]}, DEVS, "SIM", snapshot=snap())
check("pH partial stamps the global pH lockout",
      len(executed) == 1 and ai_advisor._last_ph_time > 0)

# A dose that RAISES leaves chem state unknown -> conservatively stamps resolved ports.
reset()
def _raise_dose(*a, **k):
    raise RuntimeError("boom mid-dose")
dosing.timed_dose_pair = _raise_dose
executed = ai_advisor.execute_actions(
    {"actions": [dose("timed_nutrient_microdose")]}, DEVS, "SIM", snapshot=snap())
check("raised dose is not in executed", executed == [])
check("raised dose still stamps lockout for its resolved ports (conservative)",
      "TestRes:1" in ai_advisor._last_dose_time and "TestRes:2" in ai_advisor._last_dose_time)
os.environ.pop("AUTONOMOUS_DOSING", None)


# =========================================================================== #
# 2026-07-31 review, P0-3: a corrupt lockout file must not hand every port a free
# immediate re-dose. Kept last -- it deliberately leaves a fail-closed floor set.
# =========================================================================== #
print("\n== corrupt lockout persistence fails CLOSED ==")
import json
reset()
ai_advisor._last_dose_time = {}
ai_advisor._last_ph_time = 0.0
ai_advisor._lockout_floor = 0.0

ai_advisor._LOCKOUT_FILE.write_text(json.dumps(
    {"last_dose_time": {"TestRes:1": time.time() - 10}, "last_ph_time": 0}))
ai_advisor._load_lockouts()
check("a VALID lockout file loads with no fail-closed floor",
      ai_advisor._lockout_floor == 0.0)
check("valid file restores the real per-port clock",
      "TestRes:1" in ai_advisor._last_dose_time)

ai_advisor._last_dose_time = {}
ai_advisor._last_ph_time = 0.0
ai_advisor._LOCKOUT_FILE.write_text('{"last_dose_time": {"TestRes:1": 17')  # truncated
_before = time.time()
ai_advisor._load_lockouts()
check("corrupt lockout file sets a fail-closed floor",
      ai_advisor._lockout_floor >= _before)
check("a port with NO restored record is locked out anyway",
      ai_advisor._last_dose_ts("TestRes:1") == ai_advisor._lockout_floor)
check("the global pH clock restarts too",
      ai_advisor._last_ph_time == ai_advisor._lockout_floor)
check("the corrupt lockout file is preserved for inspection",
      any(p.name.startswith(".lockouts.json.corrupt.") for p in _TMP.iterdir()))

# The real proof: the dose verb (which is how chemicals actually move) is gated by
# the floor even though no per-port record survived the corrupt file.
blocked = ai_advisor.filter_actions([dose("timed_nutrient_microdose")], snapshot=snap())
check("the dose verb is blocked by the fail-closed floor", blocked == [])
blocked_ph = ai_advisor.filter_actions([dose("timed_ph_down_microdose")], snapshot=snap())
check("pH doses are blocked by the fail-closed floor too", blocked_ph == [])
stop = ai_advisor.filter_actions(
    [{"device": "TestRes", "port": 1, "action": "set_speed", "value": 0}], snapshot=snap())
check("a STOP still passes under the fail-closed floor", len(stop) == 1)

ai_advisor._lockout_floor = 0.0     # do not leak the floor into any later case


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
