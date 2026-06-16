#!/usr/bin/env python3
"""
Self-tests for the deterministic high-temperature exhaust guardrail
(schedule.compute_temp_emergency) and its poller enforcement
(poller.enforce_temp_emergency). No hardware: the guardrail is a pure function
of the snapshot, and the enforce path's AC Infinity write is monkeypatched.

Run: python3 schedule_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import schedule

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


def snap(temp=None, sensor="temp_f_tent", devname="4 x 4"):
    """Minimal snapshot carrying one air-temp reading (or none)."""
    sensors = {}
    if temp is not None:
        sensors[sensor] = temp
    return {"devices": [{"name": devname, "sensors": sensors, "ports": []}]}


def reset(**env):
    """Clear guardrail state + relevant env, then apply the given overrides."""
    schedule._temp_emergency_active = False
    for k in ("AIR_TEMP_EMERGENCY_F", "AIR_TEMP_CLEAR_F",
              "HIGH_TEMP_SENSOR", "ROLE_EXHAUST"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


# --- compute_temp_emergency ------------------------------------------------- #

def test_disabled_by_default():
    reset()
    check("disabled when AIR_TEMP_EMERGENCY_F unset", schedule.compute_temp_emergency(snap(120)) is None)
    reset(AIR_TEMP_EMERGENCY_F="0")
    check("disabled when AIR_TEMP_EMERGENCY_F=0", schedule.compute_temp_emergency(snap(120)) is None)
    reset(AIR_TEMP_EMERGENCY_F="garbage")
    check("disabled when AIR_TEMP_EMERGENCY_F non-numeric", schedule.compute_temp_emergency(snap(120)) is None)


def test_below_trigger_inert():
    reset(AIR_TEMP_EMERGENCY_F="95")
    check("below trigger -> None", schedule.compute_temp_emergency(snap(80)) is None)
    check("below trigger leaves state inactive", schedule._temp_emergency_active is False)


def test_trips_at_trigger():
    reset(AIR_TEMP_EMERGENCY_F="95")
    em = schedule.compute_temp_emergency(snap(95))
    check("trips at exactly trigger", em is not None and em["active"] is True)
    check("reports the reading", em and em["temp_f"] == 95)
    check("default clear = trigger-7", em and em["clear"] == 88)
    acts = em["actions"] if em else []
    check("exactly one action (exhaust only)", len(acts) == 1)
    a = acts[0] if acts else {}
    check("action is set_speed", a.get("action") == "set_speed")
    check("action ramps exhaust to 10", a.get("value") == 10)
    check("targets default ROLE_EXHAUST 4 x 4:2",
          a.get("device") == "4 x 4" and a.get("port") == 2)


def test_hysteresis_holds_in_band():
    reset(AIR_TEMP_EMERGENCY_F="95", AIR_TEMP_CLEAR_F="88")
    schedule.compute_temp_emergency(snap(96))           # trip
    em = schedule.compute_temp_emergency(snap(90))      # in band (88<=90<95)
    check("stays active in hysteresis band", em is not None and em["active"])


def test_clears_below_clear():
    reset(AIR_TEMP_EMERGENCY_F="95", AIR_TEMP_CLEAR_F="88")
    schedule.compute_temp_emergency(snap(96))           # trip
    em = schedule.compute_temp_emergency(snap(87))      # below clear
    check("clears below clear threshold", em is None)
    check("state reset to inactive", schedule._temp_emergency_active is False)


def test_clear_gap_enforced():
    # clear >= trigger is nonsense; code forces a real gap (trigger-7).
    reset(AIR_TEMP_EMERGENCY_F="95", AIR_TEMP_CLEAR_F="99")
    em = schedule.compute_temp_emergency(snap(96))
    check("clear>=trigger corrected to trigger-7", em and em["clear"] == 88)


def test_missing_reading():
    # No reading + not active -> stay inert.
    reset(AIR_TEMP_EMERGENCY_F="95")
    check("no reading while inactive -> None", schedule.compute_temp_emergency(snap(None)) is None)
    # No reading + already active -> keep forcing exhaust (fail-safe).
    reset(AIR_TEMP_EMERGENCY_F="95")
    schedule.compute_temp_emergency(snap(96))           # trip
    em = schedule.compute_temp_emergency(snap(None))    # sensor dropped out
    check("no reading while active -> stays active", em is not None and em["active"])
    check("no-reading block reports temp_f None", em and em["temp_f"] is None)


def test_custom_sensor_key():
    reset(AIR_TEMP_EMERGENCY_F="95", HIGH_TEMP_SENSOR="temp_f_outside")
    check("ignores other sensors", schedule.compute_temp_emergency(snap(120, sensor="temp_f_tent")) is None)
    em = schedule.compute_temp_emergency(snap(120, sensor="temp_f_outside"))
    check("trips off configured sensor key", em is not None and em["active"])
    check("block records watched sensor", em and em["sensor"] == "temp_f_outside")


def test_custom_exhaust_role():
    reset(AIR_TEMP_EMERGENCY_F="95", ROLE_EXHAUST="Auxiliary Outputs:3")
    em = schedule.compute_temp_emergency(snap(96))
    a = em["actions"][0]
    check("honors custom ROLE_EXHAUST device", a["device"] == "Auxiliary Outputs")
    check("honors custom ROLE_EXHAUST port", a["port"] == 3)


def test_independent_of_chem_emergencies():
    # The guardrail reads only its temp sensor; res_burst/co2 keys are irrelevant.
    reset(AIR_TEMP_EMERGENCY_F="95")
    s = snap(96)
    s["res_burst"] = {"active": True}
    s["co2_emergency"] = {"active": True}
    em = schedule.compute_temp_emergency(s)
    check("fires regardless of chemical-side emergencies", em is not None and em["active"])


# --- poller.enforce_temp_emergency ------------------------------------------ #

def test_enforce():
    import poller

    calls = []

    def fake_set_port_speed(token, dev_id, port, speed, dev_type):
        calls.append((dev_id, port, speed, dev_type))

    orig = poller.set_port_speed
    poller.set_port_speed = fake_set_port_speed
    try:
        devices = [{"name": "4 x 4", "dev_id": "DEV1", "type": 20}]

        # Inactive -> nothing fired.
        fired = poller.enforce_temp_emergency({"temp_emergency": None}, devices, "TOKEN")
        check("enforce: inactive fires nothing", fired == [] and calls == [])

        # Active -> exhaust write issued.
        reset(AIR_TEMP_EMERGENCY_F="95")
        em = schedule.compute_temp_emergency(snap(97))
        fired = poller.enforce_temp_emergency({"temp_emergency": em}, devices, "TOKEN")
        check("enforce: active fires one action", len(fired) == 1)
        check("enforce: issued exhaust speed-10 write",
              calls == [("DEV1", 2, 10, 20)])

        # Unknown device -> skipped, no crash, no write.
        calls.clear()
        fired = poller.enforce_temp_emergency({"temp_emergency": em}, [], "TOKEN")
        check("enforce: unknown device skipped safely", fired == [] and calls == [])
    finally:
        poller.set_port_speed = orig


# --- compute_co2_emergency (CO2 dump) --------------------------------------- #

def co2_snap(co2=None, valve_powered=None, valve=("Auxiliary Outputs", 2)):
    sensors = {} if co2 is None else {"co2_ppm": co2}
    ports = []
    if valve_powered is not None:
        ports = [{"port": valve[1], "powered": valve_powered}]
    return {"devices": [{"name": valve[0], "sensors": sensors, "ports": ports}]}


def reset_co2(**env):
    schedule._co2_emergency_active = False
    schedule._co2_pulse_state = None
    for k in ("CO2_EMERGENCY_PPM", "CO2_DUMP_CLEAR_PPM", "CO2_VALVE",
              "ROLE_EXHAUST", "CO2_PULSE_BAND_PPM"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


def test_co2_emergency_disabled():
    reset_co2()
    check("co2 dump disabled when PPM unset", schedule.compute_co2_emergency(co2_snap(5000)) is None)
    reset_co2(CO2_EMERGENCY_PPM="0")
    check("co2 dump disabled when PPM=0", schedule.compute_co2_emergency(co2_snap(5000)) is None)


def test_co2_emergency_trips_and_clears():
    reset_co2(CO2_EMERGENCY_PPM="3000", CO2_DUMP_CLEAR_PPM="1800",
              CO2_VALVE="Auxiliary Outputs:2", ROLE_EXHAUST="4 x 4:2")
    em = schedule.compute_co2_emergency(co2_snap(3200))
    check("co2 dump trips above trigger", em is not None and em["active"])
    acts = {(a["action"], a.get("value")) for a in em["actions"]}
    check("co2 dump forces valve OFF", ("set_outlet", False) in acts)
    check("co2 dump ramps exhaust to 10", ("set_speed", 10) in acts)
    # Hysteresis: holds active between clear and trigger.
    em = schedule.compute_co2_emergency(co2_snap(2500))
    check("co2 dump holds in hysteresis band", em is not None and em["active"])
    # Clears below clear threshold.
    em = schedule.compute_co2_emergency(co2_snap(1700))
    check("co2 dump clears below clear", em is None)


def test_co2_emergency_clear_gap_enforced():
    reset_co2(CO2_EMERGENCY_PPM="3000", CO2_DUMP_CLEAR_PPM="3000")
    em = schedule.compute_co2_emergency(co2_snap(3100))
    check("co2 clear>=trigger gets a real gap", em and em["clear"] < em["trigger"])


# --- compute_co2_pulse (valve modulation) ----------------------------------- #

def pulse_snap(co2, valve_powered, target=1000, gate="ADVANCE"):
    s = co2_snap(co2, valve_powered=valve_powered)
    s["co2_target"] = target
    s["res_health"] = {"co2_gate": gate}
    return s


def test_co2_pulse_on_when_low():
    reset_co2(CO2_VALVE="Auxiliary Outputs:2", CO2_PULSE_BAND_PPM="250")
    d = schedule.compute_co2_pulse(pulse_snap(co2=600, valve_powered=False, target=1000))
    check("pulse turns valve ON below band", d is not None and d["expected_value"] is True)


def test_co2_pulse_off_when_high():
    reset_co2(CO2_VALVE="Auxiliary Outputs:2", CO2_PULSE_BAND_PPM="250")
    d = schedule.compute_co2_pulse(pulse_snap(co2=1400, valve_powered=True, target=1000))
    check("pulse turns valve OFF above band", d is not None and d["expected_value"] is False)


def test_co2_pulse_gate_forces_off():
    reset_co2(CO2_VALVE="Auxiliary Outputs:2", CO2_PULSE_BAND_PPM="250")
    d = schedule.compute_co2_pulse(pulse_snap(co2=600, valve_powered=True, target=1000, gate="HOLD"))
    check("pulse forces OFF when gate=HOLD even if CO2 low",
          d is not None and d["expected_value"] is False)


def test_co2_pulse_in_sync_no_delta():
    reset_co2(CO2_VALVE="Auxiliary Outputs:2", CO2_PULSE_BAND_PPM="250")
    # Low CO2 -> wants ON; valve already ON -> no delta.
    d = schedule.compute_co2_pulse(pulse_snap(co2=600, valve_powered=True, target=1000))
    check("pulse emits no delta when already correct", d is None)


def test_co2_pulse_disabled_without_valve():
    reset_co2(CO2_PULSE_BAND_PPM="250")
    check("pulse inert without CO2_VALVE", schedule.compute_co2_pulse(pulse_snap(600, False)) is None)


# --- compute_schedule_deltas (light / osc fans) ----------------------------- #

def deltas_reset(**env):
    for k in ("LIGHT_HOURS_ON", "LIGHT_HOURS_OFF", "LIGHT_INTENSITY",
              "ROLE_LIGHT", "OSC_FAN_SPEED", "ROLE_OSC_FANS"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


def test_schedule_delta_light_mismatch():
    deltas_reset(LIGHT_HOURS_ON="24", LIGHT_INTENSITY="10", ROLE_LIGHT="4 x 4:1",
                 ROLE_OSC_FANS="")
    snapshot = {"devices": [{"name": "4 x 4", "ports": [{"port": 1, "speed": 3}]}]}
    d = schedule.compute_schedule_deltas(snapshot)
    light = next((x for x in d if x["kind"] == "light"), None)
    check("light delta detected", light is not None)
    check("light delta wants intensity 10", light and light["expected_value"] == 10)
    check("light delta sees actual 3", light and light["actual_value"] == 3)


def test_schedule_delta_in_sync():
    deltas_reset(LIGHT_HOURS_ON="24", LIGHT_INTENSITY="10", ROLE_LIGHT="4 x 4:1",
                 ROLE_OSC_FANS="4 x 4:3", OSC_FAN_SPEED="5")
    snapshot = {"devices": [{"name": "4 x 4", "ports": [
        {"port": 1, "speed": 10}, {"port": 3, "speed": 5}]}]}
    check("no deltas when everything matches", schedule.compute_schedule_deltas(snapshot) == [])


def test_schedule_delta_fan_mismatch():
    deltas_reset(LIGHT_HOURS_ON="24", LIGHT_INTENSITY="10", ROLE_LIGHT="4 x 4:1",
                 ROLE_OSC_FANS="4 x 4:3", OSC_FAN_SPEED="5")
    snapshot = {"devices": [{"name": "4 x 4", "ports": [
        {"port": 1, "speed": 10}, {"port": 3, "speed": 0}]}]}
    d = schedule.compute_schedule_deltas(snapshot)
    fan = next((x for x in d if x["kind"] == "osc_fan"), None)
    check("fan delta detected", fan is not None and fan["expected_value"] == 5)


# --- PPFD control wired into expected_light_state --------------------------- #

def test_ppfd_controlled_light():
    import json, tempfile
    import ppfd
    # Synthetic map: level L -> 100*L avg PPFD @18in. veg target 25.92 DLI @18h -> level 4.
    tmp = Path(tempfile.mkdtemp()) / "ppfd_map.json"
    mp = {"footprint_in": [48, 48], "grid_spacing_in": 6, "heights_in": {
        "18": {str(l): {"grid": [[100 * l] * 3 for _ in range(3)]} for l in range(1, 11)}}}
    tmp.write_text(json.dumps(mp))
    ppfd._MAP_PATH = tmp

    for k in ("PPFD_CONTROL", "PPFD_METRIC", "CANOPY_DISTANCE_IN", "LIGHT_HOURS_ON",
              "LIGHT_INTENSITY", "LIGHT_HOURS_OFF", "GROW_START_DATE", "GROW_STAGE",
              "DLI_TARGET_VEG", "LIGHT_SUNRISE_MIN", "LIGHT_SUNSET_MIN"):
        os.environ.pop(k, None)
    os.environ.update({"LIGHT_INTENSITY": "10", "LIGHT_HOURS_ON": "18", "LIGHT_HOURS_OFF": "6",
                       "CANOPY_DISTANCE_IN": "18", "PPFD_METRIC": "avg",
                       "GROW_STAGE": "veg", "DLI_TARGET_VEG": "25.92"})

    # Disarmed -> uses LIGHT_INTENSITY (10) at plateau (no fades, mid-photoperiod).
    from datetime import datetime
    noon = datetime(2026, 6, 16, 12, 0)
    os.environ["PPFD_CONTROL"] = "false"
    st = schedule.expected_light_state(noon)
    check("disarmed: plateau uses LIGHT_INTENSITY", st["on"] and st["speed"] == 10)

    # Armed -> plateau intensity becomes the PPFD-recommended level (4).
    os.environ["PPFD_CONTROL"] = "true"
    st = schedule.expected_light_state(noon)
    check("armed: plateau driven to PPFD level", st["speed"] == 4)
    check("armed: reason notes PPFD control", "PPFD ctrl" in st["reason"])

    # Map missing a usable rec -> falls back to LIGHT_INTENSITY (lighting never breaks).
    ppfd._MAP_PATH = tmp.parent / "gone.json"
    st = schedule.expected_light_state(noon)
    check("armed but no map: falls back to LIGHT_INTENSITY", st["speed"] == 10)


def main():
    print("Schedule / emergency deterministic self-tests")
    print("=" * 44)
    for fn in (
        test_disabled_by_default,
        test_below_trigger_inert,
        test_trips_at_trigger,
        test_hysteresis_holds_in_band,
        test_clears_below_clear,
        test_clear_gap_enforced,
        test_missing_reading,
        test_custom_sensor_key,
        test_custom_exhaust_role,
        test_independent_of_chem_emergencies,
        test_enforce,
        test_co2_emergency_disabled,
        test_co2_emergency_trips_and_clears,
        test_co2_emergency_clear_gap_enforced,
        test_co2_pulse_on_when_low,
        test_co2_pulse_off_when_high,
        test_co2_pulse_gate_forces_off,
        test_co2_pulse_in_sync_no_delta,
        test_co2_pulse_disabled_without_valve,
        test_schedule_delta_light_mismatch,
        test_schedule_delta_in_sync,
        test_schedule_delta_fan_mismatch,
        test_ppfd_controlled_light,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
