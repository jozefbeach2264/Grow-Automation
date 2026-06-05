#!/usr/bin/env python3
"""
Self-tests for timed dosing (dosing.py). No hardware: AC Infinity writes/readbacks are
monkeypatched, state files redirected to a temp dir, and _sleep_ms is a no-op so doses
run instantly. Run: python3 dosing_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="dose_test_"))

import runtime_state
runtime_state._STATE_FILE = _TMP / ".runtime_state.json"
runtime_state._EVENT_LOG = _TMP / "events.jsonl"
import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

os.environ["VERIFY_WRITES"] = "true"
os.environ.pop("DOSING_DISABLED", None)

import dosing

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
    for f in (runtime_state._STATE_FILE, runtime_state._EVENT_LOG, safety_state._STATE_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


# --- mocks ------------------------------------------------------------------ #
_writes = []          # (port, speed)
_start_raise_on = set()   # ports where the START write (speed>0) should raise
_verify_ok = True
_precheck_speed = 0       # what read_port_state reports before dosing


def fake_set_port_speed(token, dev_id, port, speed, dev_type):
    if speed > 0 and port in _start_raise_on:
        raise RuntimeError(f"simulated start failure on port {port}")
    _writes.append((port, speed))


def fake_read_port_state(token, dev_id, port):
    return {"port": port, "speed_actual": _precheck_speed, "online": True,
            "is_outlet": False, "powered": _precheck_speed > 0}


def fake_verify(token, dev_id, port, expected, timeout_sec=0):
    return {"ok": _verify_ok, "reason": "" if _verify_ok else "still_running",
            "observed": {"speed_actual": 0 if _verify_ok else 5},
            "elapsed_sec": 1, "attempts": 1}


import ac_infinity_client
dosing.set_port_speed = fake_set_port_speed              # start writes (dosing's own ref)
dosing.read_port_state = fake_read_port_state            # pre-check / start-confirm
# Stops route through ac_infinity_client.stop_and_verify, which resolves set_port_speed /
# verify_port_state in the ac_infinity_client namespace -- patch them THERE so the shared
# stop primitive is mocked too.
ac_infinity_client.set_port_speed = fake_set_port_speed
ac_infinity_client.verify_port_state = fake_verify
dosing._sleep_ms = lambda ms: None          # don't actually sleep

DEV = {"name": "RDWC Control", "dev_id": "d-rdwc", "type": 20}


def stops_for(port):
    return [(p, s) for (p, s) in _writes if p == port and s == 0]


def starts_for(port):
    return [(p, s) for (p, s) in _writes if p == port and s > 0]


# =========================================================================== #
print("\n== calculate_timed_dose: pure math ==")
d = dosing.calculate_timed_dose(1, 0.5)        # pH microdose, speed 1
check("speed-1 0.5mL is deliverable", d["deliverable"] is True)
check("ramp_up_ml ~= 0.175 (0.175*S^2)", abs(d["ramp_up_ml"] - 0.175) < 1e-3)
check("ramp_down counted separately", abs(d["ramp_down_ml"] - 0.175) < 1e-3)
check("hold_ml = target - ramp_total = 0.15", abs(d["hold_ml"] - 0.15) < 1e-3)
check("estimated_actual_ml = target", abs(d["estimated_actual_ml"] - 0.5) < 1e-3)
check("on_ms = ramp_up + hold", d["on_ms"] == d["ramp_up_ms"] + d["hold_ms"])

d2 = dosing.calculate_timed_dose(5, 1.0)       # below minimum: ramp-only = 8.75 mL
check("speed-5 1mL below hardware resolution", d2["deliverable"] is False)
check("rejection reports min_ml", abs(d2["min_ml"] - 8.75) < 1e-3)

d3 = dosing.calculate_timed_dose(2, 5.0)       # nutrient microdose each
check("nutrient speed-2 5mL deliverable", d3["deliverable"] is True)
check("nutrient ramp_total = 1.4 mL", abs(d3["ramp_up_ml"] + d3["ramp_down_ml"] - 1.4) < 1e-3)
check("nutrient hold_ml = 3.6 mL", abs(d3["hold_ml"] - 3.6) < 1e-3)


print("\n== timed_dose: happy path ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
r = dosing.timed_dose("TEST", DEV, 4, 1, 0.5, solution="ph_down", strength=0.25)
check("dose ok", r["ok"] is True and r["stop_verified"] is True)
check("started port 4", len(starts_for(4)) == 1)
check("stopped port 4 in finally", len(stops_for(4)) >= 1)
check("full-strength-equiv = est * 0.25", abs(r["full_strength_equivalent_ml"] - 0.5 * 0.25) < 1e-3)
check("active dose cleared after", runtime_state.get_active_dose() is None)
check("dosing NOT frozen on success", safety_state.is_dosing_disabled() is False)


print("\n== timed_dose: stop verification fails -> freeze ==")
reset(); _writes.clear()
_verify_ok = False; _precheck_speed = 0
r = dosing.timed_dose("TEST", DEV, 4, 1, 0.5, solution="ph_down")
check("dose reports not ok", r["ok"] is False)
check("stop was attempted (in finally)", len(stops_for(4)) >= 1)
check("failed stop FREEZES dosing", safety_state.is_dosing_disabled() is True)
active, _, _ = runtime_state.high_alert_status()
check("failed stop opens high-alert", active is True)
_verify_ok = True


print("\n== timed_dose: aborts when port not already at 0 ==")
reset(); _writes.clear()
_precheck_speed = 5
r = dosing.timed_dose("TEST", DEV, 4, 1, 0.5)
check("dose aborted (port not at 0)", r["ok"] is False and "not at 0" in r["reason"])
check("no pump start issued on abort", _writes == [])
check("no active dose left after abort", runtime_state.get_active_dose() is None)
_precheck_speed = 0


print("\n== timed_dose: below-resolution dose never runs ==")
reset(); _writes.clear()
r = dosing.timed_dose("TEST", DEV, 4, 5, 1.0)   # ramp-only 8.75 mL > 1.0
check("below-resolution returns not deliverable", r.get("deliverable") is False)
check("below-resolution issues no writes", _writes == [])


print("\n== timed_dose: stop still runs in finally if start raises ==")
reset(); _writes.clear()
_start_raise_on = {4}
raised = False
try:
    dosing.timed_dose("TEST", DEV, 4, 1, 0.5)
except RuntimeError:
    raised = True
check("start failure propagates", raised is True)
check("finally still issued a stop on port 4", len(stops_for(4)) >= 1)
_start_raise_on.clear()


print("\n== timed_dose_pair: nutrient ports 1+2 ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
check("pair ok", r["ok"] is True)
check("both ports started", len(starts_for(1)) == 1 and len(starts_for(2)) == 1)
check("both ports stopped", len(stops_for(1)) >= 1 and len(stops_for(2)) >= 1)
check("per-port estimate ~5 mL each", abs(r["estimated_actual_ml_each"][1] - 5.0) < 1e-3)

print("\n== timed_dose_pair: one port fails to start -> both stop ==")
reset(); _writes.clear()
_start_raise_on = {2}
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0)
check("pair reports failure", r["ok"] is False and r["start_failed"] == 2)
check("both ports still got a stop", len(stops_for(1)) >= 1 and len(stops_for(2)) >= 1)
_start_raise_on.clear()


print("\n== timed_dose_pair: per-port flow -> per-port on_ms (accuracy) ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
os.environ["FLOW_ML_MIN_RDWC_CONTROL_1"] = "21"
os.environ["FLOW_ML_MIN_RDWC_CONTROL_2"] = "42"   # V2 twice as fast -> half the run time
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
check("pair ok with unequal flow", r["ok"] is True)
check("faster pump (p2) gets shorter on_ms", r["plans"][2]["on_ms"] < r["plans"][1]["on_ms"])
check("both ports stopped under unequal flow", len(stops_for(1)) >= 1 and len(stops_for(2)) >= 1)
check("equal target -> equal estimated mL each",
      abs(r["estimated_actual_ml_each"][1] - r["estimated_actual_ml_each"][2]) < 1e-6)
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_1", None)
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_2", None)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
