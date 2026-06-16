#!/usr/bin/env python3
"""
Self-tests for the deterministic stressor list + playbook registry
(diagnostics.py). Pure functions of the snapshot; thresholds come from .env so
each test sets the env it needs. Run: python3 diagnostics_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    for k in ("AIR_TEMP_MIN", "AIR_TEMP_MAX", "AIR_TEMP_EMERGENCY_F",
              "HUMIDITY_MIN", "HUMIDITY_MAX", "VPD_MIN", "VPD_MAX",
              "PH_MIN", "PH_MAX", "TDS_MIN", "TDS_MAX",
              "WATER_TEMP_MIN", "WATER_TEMP_MAX", "CO2_TOLERANCE",
              "HIGH_TEMP_SENSOR", "CANOPY_HUMIDITY_SENSOR", "CANOPY_VPD_SENSOR"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


def snap(sensors=None, res_health=None, trends=None, co2_target=None, devices=None):
    devs = devices if devices is not None else [
        {"name": "4 x 4", "online": True, "sensors": sensors or {}}]
    s = {"devices": devs}
    if res_health is not None:
        s["res_health"] = res_health
    if trends is not None:
        s["trends"] = trends
    if co2_target is not None:
        s["co2_target"] = co2_target
    return s


def names(stressors):
    return [s["name"] for s in stressors]


def find(stressors, name):
    return next((s for s in stressors if s["name"] == name), None)


# --- absence = silence -------------------------------------------------------

def test_no_sensors_no_stressors():
    reset(AIR_TEMP_MAX="85")
    st = diagnostics.build_stressors(snap(sensors={}))
    check("empty sensors -> no stressors", st == [])


def test_in_band_quiet():
    reset(AIR_TEMP_MIN="70", AIR_TEMP_MAX="85")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 78}))
    check("in-band temp -> no stressor", st == [])


def test_disconnected_probe_silent():
    # ph/tds/co2 absent (HDS3 + CO2 disconnected) -> no res/co2 stressors.
    reset(AIR_TEMP_MAX="85")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 80}))
    check("disconnected res/co2 stays quiet", names(st) == [])


# --- air / canopy ------------------------------------------------------------

def test_tent_temp_high():
    reset(AIR_TEMP_MAX="85", AIR_TEMP_EMERGENCY_F="95")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 90}))
    s = find(st, "tent_temp_high")
    check("tent_temp_high emitted", s is not None)
    check("severity high below emergency", s and s["severity"] == "high")
    check("allows exhaust + light playbooks",
          s and "increase_exhaust_one_step" in s["allowed_playbooks"]
          and "reduce_light_one_step" in s["allowed_playbooks"])
    check("alert_only always last", s and s["allowed_playbooks"][-1] == "alert_only")


def test_tent_temp_critical():
    reset(AIR_TEMP_MAX="85", AIR_TEMP_EMERGENCY_F="95")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 96}))
    s = find(st, "tent_temp_high")
    check("at/above emergency -> critical", s and s["severity"] == "critical")


def test_tent_temp_low():
    reset(AIR_TEMP_MIN="70", AIR_TEMP_MAX="85")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 64}))
    check("tent_temp_low emitted", find(st, "tent_temp_low") is not None)


def test_custom_sensor_key():
    reset(AIR_TEMP_MAX="85", HIGH_TEMP_SENSOR="temp_f_canopy")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_canopy": 90, "temp_f_tent": 90}))
    # Only the configured key drives tent_temp; default key ignored.
    check("uses configured tent sensor key", find(st, "tent_temp_high") is not None)


def test_vpd_and_humidity():
    reset(VPD_MAX="1.5", HUMIDITY_MIN="50", HUMIDITY_MAX="70")
    st = diagnostics.build_stressors(snap(sensors={"vpd_tent": 1.8, "humidity_tent": 40}))
    check("vpd_high emitted", find(st, "vpd_high") is not None)
    check("humidity_low emitted", find(st, "humidity_low") is not None)


# --- reservoir / co2 ---------------------------------------------------------

def test_ph_and_tds():
    reset(PH_MIN="5.8", PH_MAX="6.2", TDS_MIN="800", TDS_MAX="1600")
    st = diagnostics.build_stressors(snap(sensors={"ph": 6.6, "tds_ppm": 500}))
    sph = find(st, "ph_high")
    check("ph_high emitted", sph is not None)
    check("ph_high allows ph-down playbook", sph and "timed_ph_down_microdose" in sph["allowed_playbooks"])
    std = find(st, "tds_low")
    check("tds_low emitted", std is not None)
    check("tds_low allows nutrient playbook", std and "timed_nutrient_microdose" in std["allowed_playbooks"])


def test_water_temp_medium_alert_only():
    reset(WATER_TEMP_MAX="72")
    st = diagnostics.build_stressors(snap(sensors={"water_temp_f": 78}))
    s = find(st, "water_temp_high")
    check("water_temp_high emitted", s is not None)
    check("water temp capped at medium severity", s and s["severity"] == "medium")
    check("water temp is alert-only (no chiller)", s and s["allowed_playbooks"] == ["alert_only"])


def test_co2_high_while_res_stalled():
    reset(CO2_TOLERANCE="100")
    st = diagnostics.build_stressors(snap(
        sensors={"co2_ppm": 1400}, co2_target=1000,
        res_health={"state": "STALL"}))
    check("co2_high_while_res_stalled emitted", find(st, "co2_high_while_res_stalled") is not None)


def test_co2_high_plain():
    reset(CO2_TOLERANCE="100")
    st = diagnostics.build_stressors(snap(
        sensors={"co2_ppm": 1400}, co2_target=1000,
        res_health={"state": "IDEAL"}))
    check("co2_high emitted when res healthy", find(st, "co2_high") is not None)


def test_co2_quiet_without_target():
    reset()
    st = diagnostics.build_stressors(snap(sensors={"co2_ppm": 5000}))
    check("no co2 target -> no co2 stressor", find(st, "co2_high") is None)


# --- water level + offline ---------------------------------------------------

def test_water_level_rising():
    reset()
    st = diagnostics.build_stressors(snap(trends={"water_level": "RISING"}))
    check("water_level_rising emitted", find(st, "water_level_rising") is not None)


def test_device_offline():
    reset()
    st = diagnostics.build_stressors(snap(devices=[
        {"name": "Hydroponics Control", "online": False, "sensors": {}}]))
    s = find(st, "device_offline")
    check("device_offline emitted", s is not None)
    check("device_offline names the device", s and "Hydroponics Control" in s["evidence"])


# --- ordering + registry -----------------------------------------------------

def test_severity_ordering():
    reset(AIR_TEMP_MAX="85", AIR_TEMP_EMERGENCY_F="95", WATER_TEMP_MAX="72")
    st = diagnostics.build_stressors(snap(sensors={"temp_f_tent": 97, "water_temp_f": 78}))
    check("critical sorted before medium", st[0]["severity"] == "critical")


def test_build_diagnostics_summary():
    reset(AIR_TEMP_MAX="85", AIR_TEMP_EMERGENCY_F="95")
    d = diagnostics.build_diagnostics(snap(sensors={"temp_f_tent": 97}))
    check("summary count", d["count"] == 1)
    check("summary worst severity", d["worst_severity"] == "critical")
    check("empty summary worst=none", diagnostics.build_diagnostics(snap(sensors={}))["worst_severity"] == "none")


def test_registry_integrity():
    # Every playbook referenced by any stressor must exist in PLAYBOOKS.
    referenced = {p for lst in diagnostics._STRESSOR_PLAYBOOKS.values() for p in lst}
    referenced.add(diagnostics.ALERT_ONLY)
    missing = referenced - set(diagnostics.PLAYBOOKS)
    check("all referenced playbooks defined in registry", missing == set())
    check("alert_only never actuates", diagnostics.PLAYBOOKS["alert_only"]["actuates"] is False)
    check("chemical playbooks flagged tier 3",
          all(diagnostics.PLAYBOOKS[p].get("tier") == 3
              for p in ("timed_nutrient_microdose", "timed_ph_up_microdose", "timed_ph_down_microdose")))


def main():
    print("Diagnostics / stressor-list self-tests")
    print("=" * 44)
    for fn in (
        test_no_sensors_no_stressors,
        test_in_band_quiet,
        test_disconnected_probe_silent,
        test_tent_temp_high,
        test_tent_temp_critical,
        test_tent_temp_low,
        test_custom_sensor_key,
        test_vpd_and_humidity,
        test_ph_and_tds,
        test_water_temp_medium_alert_only,
        test_co2_high_while_res_stalled,
        test_co2_high_plain,
        test_co2_quiet_without_target,
        test_water_level_rising,
        test_device_offline,
        test_severity_ordering,
        test_build_diagnostics_summary,
        test_registry_integrity,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
