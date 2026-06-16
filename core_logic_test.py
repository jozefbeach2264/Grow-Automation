#!/usr/bin/env python3
"""
Self-tests for the deterministic core decision logic in ai_advisor:
res_health_check (the reservoir gate table) and _trend (rate-normalized trend
classification). Pure functions; no hardware. Run: python3 core_logic_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Redirect persisted state before importing ai_advisor (it touches state files).
_TMP = Path(tempfile.mkdtemp(prefix="core_test_"))
import runtime_state
runtime_state._STATE_FILE = _TMP / ".runtime_state.json"
runtime_state._EVENT_LOG = _TMP / "events.jsonl"
import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

import ai_advisor

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


# --- res_health_check: the gate table --------------------------------------- #

def rh(water, ec, ph="STATIC"):
    return ai_advisor.res_health_check({"water_level": water, "ec_ms": ec, "ph": ph})


def test_res_ideal():
    r = rh("FALLING", "STATIC")
    check("IDEAL state", r["state"] == "IDEAL")
    check("IDEAL co2 ADVANCE", r["co2_gate"] == "ADVANCE")
    check("IDEAL dose NORMAL", r["dose_gate"] == "NORMAL")
    check("IDEAL ph ALLOW", r["ph_gate"] == "ALLOW")
    r2 = rh("FALLING", "FALLING")
    check("FALLING+FALLING also IDEAL", r2["state"] == "IDEAL")


def test_res_watch_drinking():
    r = rh("FALLING", "RISING")
    check("WATCH (drinking>eating) state", r["state"] == "WATCH")
    check("WATCH holds CO2", r["co2_gate"] == "HOLD")
    check("WATCH holds dose", r["dose_gate"] == "HOLD")
    check("WATCH still allows pH", r["ph_gate"] == "ALLOW")


def test_res_watch_eating():
    r = rh("STATIC", "FALLING")
    check("WATCH (eating not drinking) state", r["state"] == "WATCH")
    check("eating-not-drinking allows dose", r["dose_gate"] == "NORMAL")


def test_res_stall():
    r = rh("STATIC", "STATIC")
    check("STALL state", r["state"] == "STALL")
    check("STALL holds everything", r["co2_gate"] == "HOLD" and r["dose_gate"] == "HOLD" and r["ph_gate"] == "HOLD")


def test_res_stress():
    r = rh("STATIC", "RISING")
    check("STRESS state", r["state"] == "STRESS")
    check("STRESS reduces CO2", r["co2_gate"] == "REDUCE")
    check("STRESS no nutrients", r["dose_gate"] == "NONE")
    check("STRESS holds pH", r["ph_gate"] == "HOLD")


def test_res_problem():
    r = rh("RISING", "STATIC")
    check("PROBLEM on rising water", r["state"] == "PROBLEM")
    check("PROBLEM no nutrients", r["dose_gate"] == "NONE")
    r2 = rh("RISING", "RISING")
    check("PROBLEM regardless of EC", r2["state"] == "PROBLEM")


def test_res_unknown():
    r = ai_advisor.res_health_check({})
    check("UNKNOWN with no trends", r["state"] == "UNKNOWN")
    check("UNKNOWN holds CO2+dose", r["co2_gate"] == "HOLD" and r["dose_gate"] == "HOLD")
    check("UNKNOWN allows pH (conservative)", r["ph_gate"] == "ALLOW")


def test_res_ec_source_priority():
    # ec_ms missing -> falls back to ec_us, then tds_ppm.
    r = ai_advisor.res_health_check({"water_level": "FALLING", "ec_us": "RISING"})
    check("ec_us used when ec_ms absent", r["state"] == "WATCH")
    r2 = ai_advisor.res_health_check({"water_level": "STATIC", "tds_ppm": "RISING"})
    check("tds_ppm used when ec_ms/ec_us absent", r2["state"] == "STRESS")


# --- _trend: rate-normalized classification --------------------------------- #

def set_prev(label, value):
    ai_advisor._prev_sensors[label] = value


def test_trend_first_cycle_unknown():
    ai_advisor._prev_sensors.clear()
    check("no prior -> UNKNOWN", ai_advisor._trend("ph", 6.0) == "UNKNOWN")


def test_trend_basic():
    ai_advisor._prev_sensors.clear()
    set_prev("ph", 6.0)
    check("above threshold -> RISING", ai_advisor._trend("ph", 6.2) == "RISING")
    set_prev("ph", 6.0)
    check("below threshold -> FALLING", ai_advisor._trend("ph", 5.8) == "FALLING")
    set_prev("ph", 6.0)
    check("within threshold -> STATIC", ai_advisor._trend("ph", 6.02) == "STATIC")


def test_trend_threshold_edge():
    ai_advisor._prev_sensors.clear()
    set_prev("tds_ppm", 800)
    # threshold is 10.0; exactly +10 is NOT > thresh -> STATIC
    check("exactly at threshold -> STATIC", ai_advisor._trend("tds_ppm", 810) == "STATIC")
    set_prev("tds_ppm", 800)
    check("just over threshold -> RISING", ai_advisor._trend("tds_ppm", 811) == "RISING")


def test_trend_dt_normalization_reduces_sensitivity():
    # A 0.06 pH rise (above the 0.05 thresh at nominal dt) seen over a long gap
    # should be scaled DOWN below threshold -> STATIC, not RISING.
    os.environ["POLL_INTERVAL_ACTIVE"] = "60"
    ai_advisor._prev_sensors.clear()
    set_prev("ph", 6.0)
    check("long-gap drift scaled to STATIC",
          ai_advisor._trend("ph", 6.06, dt=600) == "STATIC")


def test_trend_dt_never_amplifies():
    # A short gap must NOT amplify a sub-threshold delta into RISING (scale<=1).
    os.environ["POLL_INTERVAL_ACTIVE"] = "60"
    ai_advisor._prev_sensors.clear()
    set_prev("ph", 6.0)
    check("short gap doesn't amplify noise",
          ai_advisor._trend("ph", 6.03, dt=5) == "STATIC")


def main():
    print("Core decision-logic self-tests (res_health + trend)")
    print("=" * 44)
    for fn in (
        test_res_ideal,
        test_res_watch_drinking,
        test_res_watch_eating,
        test_res_stall,
        test_res_stress,
        test_res_problem,
        test_res_unknown,
        test_res_ec_source_priority,
        test_trend_first_cycle_unknown,
        test_trend_basic,
        test_trend_threshold_edge,
        test_trend_dt_normalization_reduces_sensitivity,
        test_trend_dt_never_amplifies,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
