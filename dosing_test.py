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
_op_log = []          # ordered ("write", port, speed) / ("verify", port, None)
_start_raise_on = set()   # ports where the START write (speed>0) should raise
_verify_ok = True
_precheck_speed = 0       # what read_port_state reports before dosing


def fake_set_port_speed(token, dev_id, port, speed, dev_type):
    if speed > 0 and port in _start_raise_on:
        raise RuntimeError(f"simulated start failure on port {port}")
    _writes.append((port, speed))
    _op_log.append(("write", port, speed))


def fake_read_port_state(token, dev_id, port):
    return {"port": port, "speed_actual": _precheck_speed, "online": True,
            "is_outlet": False, "powered": _precheck_speed > 0}


def fake_verify(token, dev_id, port, expected, timeout_sec=0):
    _op_log.append(("verify", port, None))
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


print("\n== timed_dose: exception mid-dose + unverified stop STILL freezes (regression #1) ==")
reset(); _writes.clear()
# Start write raises AND the stop cannot be verified -- the exact token-expiry/network
# fault the freeze exists for. Previously the freeze sat outside the try/finally and was
# skipped when the body raised; it must now fire from inside the finally.
_start_raise_on = {4}; _verify_ok = False; _precheck_speed = 0
raised = False
try:
    dosing.timed_dose("TEST", DEV, 4, 1, 0.5)
except RuntimeError:
    raised = True
check("exception still propagates", raised is True)
check("stop attempted in finally", len(stops_for(4)) >= 1)
check("unverified stop on EXCEPTION path freezes dosing", safety_state.is_dosing_disabled() is True)
check("active dose torn down even on exception", runtime_state.get_active_dose() is None)
_start_raise_on.clear(); _verify_ok = True
safety_state.clear_dosing_disable()


print("\n== timed_dose: refuses to start when dosing already frozen (regression #11) ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0
safety_state.disable_dosing("test freeze")
r = dosing.timed_dose("TEST", DEV, 4, 1, 0.5)
check("frozen single dose returns not ok", r["ok"] is False and "frozen" in r.get("reason", ""))
check("frozen single dose issues NO pump start", _writes == [])
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0)
check("frozen pair returns not ok", r["ok"] is False and "frozen" in r.get("reason", ""))
check("frozen pair issues NO pump start", _writes == [])
safety_state.clear_dosing_disable()


print("\n== timed_dose_pair: stop COMMANDS decoupled from verify (regression #5) ==")
reset(); _writes.clear(); _op_log.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
check("pair ok", r["ok"] is True)
# Both ports' stop COMMANDS must be issued before the FIRST verify -- otherwise port 2's
# stop would block on port 1's verify latency and over-run the faster pump.
first_verify = next((i for i, op in enumerate(_op_log) if op[0] == "verify"), None)
stops_before_verify = [op for op in _op_log[:first_verify] if op[0] == "write" and op[2] == 0]
check("both stop commands fire before the first verify",
      first_verify is not None and len({op[1] for op in stops_before_verify}) == 2)


print("\n== calculate_timed_dose: rejects non-positive flow/ramp (regression #16) ==")
threw = False
try:
    dosing.calculate_timed_dose(2, 5.0, flow_ml_min=0)
except ValueError:
    threw = True
check("flow=0 raises ValueError (not ZeroDivisionError)", threw is True)
threw = False
try:
    dosing.calculate_timed_dose(2, 5.0, ramp_rate=0)
except ValueError:
    threw = True
check("ramp_rate=0 raises ValueError", threw is True)
os.environ["RAMP_SPEED_PER_SEC"] = "0"
check("env ramp=0 clamps back to default",
      dosing._ramp_rate() == dosing.DEFAULT_RAMP_SPEED_PER_SEC)
os.environ.pop("RAMP_SPEED_PER_SEC", None)


print("\n== _flow_ml_min: explicit non-positive override aborts LOUDLY (F8) ==")
# A set-but-bad FLOW_ML_MIN must never silently substitute the 21 spec default: if the
# pump's true calibrated flow is ~40 mL/min, dosing at "21" runs ~1.9x long -- a silent
# overdose. The abort must land BEFORE any pump start (no client writes).
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
os.environ["FLOW_ML_MIN_RDWC_CONTROL_3"] = "0"
threw = False
try:
    dosing.timed_dose("TEST", DEV, 3, 1, 0.5, solution="ph_up")
except ValueError:
    threw = True
check("override=0 raises ValueError before dosing", threw is True)
check("override=0 issues NO client writes", _writes == [])
os.environ["FLOW_ML_MIN_RDWC_CONTROL_3"] = "-1"
threw = False
try:
    dosing._flow_ml_min("RDWC Control", 3)
except ValueError:
    threw = True
check("override=-1 raises ValueError", threw is True)
for _bad in ("nan", "inf", "40,5"):
    os.environ["FLOW_ML_MIN_RDWC_CONTROL_3"] = _bad
    threw = False
    try:
        dosing._flow_ml_min("RDWC Control", 3)
    except ValueError:
        threw = True
    check(f"override={_bad!r} raises ValueError (no silent 21 fallback)", threw is True)
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_3", None)
check("unset override still uses the spec default",
      dosing._flow_ml_min("RDWC Control", 3) == dosing.DEFAULT_FLOW_ML_MIN)
_writes.clear()
os.environ["FLOW_ML_MIN_RDWC_CONTROL_2"] = "0"
threw = False
try:
    dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0)
except ValueError:
    threw = True
check("pair with bad override on one port raises before dosing", threw is True)
check("pair with bad override issues NO client writes", _writes == [])
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_2", None)


print("\n== timed_dose_pair: exception mid-pair -> ALL stop writes before any verify (F5) ==")
# On Ctrl-C / any exception while both pumps run, the finally must fire the raw stop
# WRITES to BOTH ports first and only then start the per-port verify/retry loop --
# verifying port 1 first would leave port 2 pumping through the verify latency
# (~4-40s, i.e. 3-30 mL of concentrate over-run).
reset(); _writes.clear(); _op_log.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
_orig_sleep = dosing._sleep_ms
def _raising_sleep(ms):
    raise KeyboardInterrupt("simulated Ctrl-C mid-pair")
dosing._sleep_ms = _raising_sleep
raised = False
try:
    dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
except KeyboardInterrupt:
    raised = True
dosing._sleep_ms = _orig_sleep
check("exception propagates out of the pair", raised is True)
first_verify = next((i for i, op in enumerate(_op_log) if op[0] == "verify"), None)
stops_before_verify = {op[1] for op in _op_log[:first_verify]
                       if op[0] == "write" and op[2] == 0}
check("BOTH stop writes issued before the first verify on the exception path",
      first_verify is not None and stops_before_verify == {1, 2})
check("both stops still verified afterward (freeze semantics intact)",
      safety_state.is_dosing_disabled() is False)
# Same exception path with an UNVERIFIABLE stop must still freeze (unchanged semantics).
reset(); _writes.clear(); _op_log.clear()
_verify_ok = False
dosing._sleep_ms = _raising_sleep
try:
    dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
except KeyboardInterrupt:
    pass
dosing._sleep_ms = _orig_sleep
check("unverified stop on the exception path still freezes dosing",
      safety_state.is_dosing_disabled() is True)
_verify_ok = True
safety_state.clear_dosing_disable()


print("\n== timed_dose_pair: partial start exposes started ports + delivered est (F7) ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0
_start_raise_on = {2}
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
check("partial pair reports failure", r["ok"] is False and r["start_failed"] == 2)
check("partial pair exposes started=[1]", r.get("started") == [1])
check("partial pair delivered est: never-started port 2 is 0.0",
      r.get("delivered_ml_each", {}).get(2) == 0.0)
check("partial pair delivered est for port 1 is bounded by the plan",
      0.0 <= r["delivered_ml_each"][1] <= r["estimated_actual_ml_each"][1])
_start_raise_on.clear()
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
check("clean pair also carries delivered_ml_each for both ports",
      set(r.get("delivered_ml_each", {}).keys()) == {1, 2})
r = dosing.timed_dose("TEST", DEV, 4, 1, 0.5, solution="ph_down")
check("single dose exposes started=[port] + delivered est",
      r.get("started") == [4] and r.get("delivered_ml_each", {}).get(4) == r["estimated_actual_ml"])


print("\n== timed_dose_pair: active-dose record stamps per-port flows (F14) ==")
reset(); _writes.clear()
_verify_ok = True; _precheck_speed = 0; _start_raise_on.clear()
os.environ["FLOW_ML_MIN_RDWC_CONTROL_1"] = "21"
os.environ["FLOW_ML_MIN_RDWC_CONTROL_2"] = "42"
_captured_ad = {}
_orig_begin = runtime_state.begin_active_dose
def _capture_begin(record):
    _captured_ad.update(record)
    _orig_begin(record)
runtime_state.begin_active_dose = _capture_begin
r = dosing.timed_dose_pair("TEST", DEV, [1, 2], 2, 5.0, solution="nutrient")
runtime_state.begin_active_dose = _orig_begin
check("pair record carries flow_ml_min_by_port for BOTH pumps",
      _captured_ad.get("flow_ml_min_by_port") == {1: 21.0, 2: 42.0})
check("pair record keeps the scalar flow for old readers",
      _captured_ad.get("flow_ml_min") == 42.0)
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_1", None)
os.environ.pop("FLOW_ML_MIN_RDWC_CONTROL_2", None)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
