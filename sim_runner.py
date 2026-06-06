#!/usr/bin/env python3
"""
sim_runner.py -- Run the AI advisor against simulated sensor data, bypassing the AC Infinity API.

Each scenario defines 3-4 cycles of sensor readings so the trend detector can see
RISING/FALLING/STATIC patterns develop over time (cycle 1 always shows UNKNOWN trends
since there is no previous data — that is expected and correct behaviour).

Usage:
    python3 sim_runner.py                # run all scenarios with AI
    python3 sim_runner.py --no-ai        # test gate logic only, skip Ollama calls
    python3 sim_runner.py <scenario>     # run one named scenario (with AI)
    python3 sim_runner.py <scenario> --no-ai

Available scenarios:
    ideal_low_tds           plant eating+drinking, TDS below target -- expect nutrients dosed
    ph_drift_low            healthy res, pH < 5.8 -- expect pH UP, no nutrients
    ph_drift_high           healthy res, pH > 6.2 -- expect pH DOWN, no nutrients
    watch_ec_rising         water falling, EC rising -- expect WATCH, hold nutrients
    stall                   nothing moving -- expect STALL, hold everything
    stress                  water static, EC building -- expect STRESS, no nutrients, reduce CO2
    high_water_temp         good res, water temp above 72F -- expect temp concern flagged
"""

import sys
import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv

# Load real config before importing ai_advisor (it reads env vars at module level)
_BASE = Path(__file__).parent
load_dotenv(_BASE / ".env")
load_dotenv(_BASE / "labels.env")

# Zero lockout times for simulation -- real lockouts are 15/20 min but back-to-back
# sim cycles would always block.  Set to 0 so every cycle can show actions.
os.environ["DOSE_LOCKOUT_MINUTES"] = "0"
os.environ["PH_LOCKOUT_MINUTES"]   = "0"

NO_AI = "--no-ai" in sys.argv

# Parse --delay N (seconds between cycles, default 0)
_DELAY = 0
for _i, _arg in enumerate(sys.argv[1:]):
    if _arg == "--delay" and _i + 1 < len(sys.argv) - 1:
        try:
            _DELAY = int(sys.argv[_i + 2])
        except ValueError:
            pass

import ac_infinity_client
import ai_advisor
import profile_manager  # noqa: F401 -- side-effect: registers STRAIN_NAME etc.

# ---------------------------------------------------------------------------
# Monkey-patch API calls so they print instead of hitting the cloud
# ---------------------------------------------------------------------------

def _sim_set_speed(token, dev_id, port, speed, dev_type):
    ml_min = speed * 21
    rate   = f"{ml_min} mL/min" if ml_min > 0 else "stopped"
    print(f"  [SIM]  set_speed  dev={dev_id}  port={port}  speed={speed}/10  ({rate})")

def _sim_set_outlet(token, dev_id, port, on):
    state = "ON" if on else "OFF"
    print(f"  [SIM]  set_outlet dev={dev_id}  port={port}  -> {state}")

ac_infinity_client.set_port_speed = _sim_set_speed
ac_infinity_client.set_outlet     = _sim_set_outlet


# ---------------------------------------------------------------------------
# State reset between scenarios
# ---------------------------------------------------------------------------

def _reset():
    """Clear trend memory and lockout state so each scenario starts clean."""
    ai_advisor._prev_sensors.clear()
    ai_advisor._last_dose_time.clear()
    ai_advisor._last_ph_time = 0.0


# ---------------------------------------------------------------------------
# Device builder
# ---------------------------------------------------------------------------

def make_devices(sensors: dict) -> list[dict]:
    """
    Build a minimal device list in parse_device() output format from a flat
    sensor dict.  Matches the real hardware layout: "4 x 4", "RDWC Control",
    "Auxiliary Outputs".  Only include keys the snapshot builder actually reads.
    """
    return [
        # Controller 1 -- Climate ("4 x 4")
        # HIDE_AIR_RDWC_CONTROL / HIDE_AIR_AUXILIARY_OUTPUTS are true in labels.env,
        # so air readings on those two devices are suppressed automatically.
        # AIR_LABEL_4_X_4 = Outside, AIR2_LABEL_4_X_4 = Tent (from labels.env)
        {
            "dev_id":     "SIM-CTRL1",
            "name":       "4 x 4",
            "type":       20,
            "type_label": "Controller AI+ (CTR89Q)",
            "online":     True,
            "is_ai":      True,
            "is_outlet":  False,
            "ports": [
                {"port": 1, "name": "Exhaust Fan",  "online": True, "mode": 2,
                 "speed_actual": 5, "speed_target": 5, "is_outlet": False},
                {"port": 2, "name": "Osc Fan 1",    "online": True, "mode": 2,
                 "speed_actual": 4, "speed_target": 4, "is_outlet": False},
                {"port": 3, "name": "Osc Fan 2",    "online": True, "mode": 2,
                 "speed_actual": 4, "speed_target": 4, "is_outlet": False},
                {"port": 4, "name": "CO2 Valve",    "online": True, "mode": 2,
                 "speed_actual": 0, "speed_target": 0, "is_outlet": False},
                {"port": 5, "name": "Light Dimmer", "online": True, "mode": 2,
                 "speed_actual": 7, "speed_target": 7, "is_outlet": False},
            ],
            # Built-in hub = outside air; external probe = tent air (per labels.env)
            "temp_f":       sensors.get("outside_temp_f", 72.0),
            "humidity_pct": sensors.get("outside_humidity", 55.0),
            "vpd_kpa":      sensors.get("outside_vpd", 1.0),
            "temp_f_ext":   sensors.get("tent_temp_f", 78.0),
            "humidity_ext": sensors.get("tent_humidity", 60.0),
            "vpd_ext":      sensors.get("tent_vpd", 1.2),
            "co2_ppm":      sensors.get("co2_ppm", 900),
            "light":        sensors.get("light", 450),
            "water_level":  None,
            "ph":           None, "tds_ppm": None, "ec_us": None,
            "ec_ms":        None, "water_temp_f": None, "water_temp_c": None,
            "temp_c": None, "temp_c_ext": None,
        },
        # Controller 2 -- Reservoir ("RDWC Control")
        {
            "dev_id":     "SIM-CTRL2",
            "name":       "RDWC Control",
            "type":       20,
            "type_label": "Controller AI+ (CTR89Q)",
            "online":     True,
            "is_ai":      True,
            "is_outlet":  False,
            "ports": [
                {"port": 1, "name": "Floraflex V1", "online": True, "mode": 1,
                 "speed_actual": 0, "speed_target": 0, "is_outlet": False},
                {"port": 2, "name": "Floraflex V2", "online": True, "mode": 1,
                 "speed_actual": 0, "speed_target": 0, "is_outlet": False},
                {"port": 3, "name": "PH UP",        "online": True, "mode": 1,
                 "speed_actual": 0, "speed_target": 0, "is_outlet": False},
                {"port": 4, "name": "PH DOWN",      "online": True, "mode": 1,
                 "speed_actual": 0, "speed_target": 0, "is_outlet": False},
            ],
            # Air readings suppressed via HIDE_AIR_RDWC_CONTROL=true in labels.env
            "temp_f": None, "humidity_pct": None, "vpd_kpa": None,
            "temp_f_ext": None, "humidity_ext": None, "vpd_ext": None,
            "co2_ppm": None, "light": None,
            "water_level":  sensors.get("water_level"),
            "ph":           sensors.get("ph", 6.0),
            "tds_ppm":      sensors.get("tds_ppm"),
            "ec_us":        sensors.get("ec_us"),
            "ec_ms":        sensors.get("ec_ms"),
            "water_temp_f": sensors.get("water_temp_f", 68.0),
            "water_temp_c": None, "temp_c": None, "temp_c_ext": None,
        },
        # ADA4 -- Auxiliary Outputs
        {
            "dev_id":     "SIM-ADA4",
            "name":       "Auxiliary Outputs",
            "type":       21,
            "type_label": "Outlet AI (ADA4)",
            "online":     True,
            "is_ai":      True,
            "is_outlet":  True,
            "ports": [
                {"port": 1, "name": "Grow Light",    "online": True, "mode": 2,
                 "load_state": 1, "powered": True,  "is_outlet": True},
                {"port": 2, "name": "Water Chiller", "online": True, "mode": 1,
                 "load_state": 0, "powered": False, "is_outlet": True},
                {"port": 3, "name": "CO2 System",    "online": True, "mode": 2,
                 "load_state": 1, "powered": True,  "is_outlet": True},
                {"port": 4, "name": "Spare",          "online": True, "mode": 1,
                 "load_state": 0, "powered": False, "is_outlet": True},
            ],
            "temp_f": None, "humidity_pct": None, "vpd_kpa": None,
            "temp_f_ext": None, "humidity_ext": None, "vpd_ext": None,
            "co2_ppm": None, "light": None, "water_level": None,
            "ph": None, "tds_ppm": None, "ec_us": None, "ec_ms": None,
            "water_temp_f": None, "water_temp_c": None,
            "temp_c": None, "temp_c_ext": None,
        },
    ]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
# Each scenario has:
#   description        -- printed as header
#   water_level_trend  -- FALLING / STATIC / RISING  (manual override)
#   cycles             -- list of sensor dicts, one per simulated poll cycle
#
# Cycle 1 always shows UNKNOWN trends (no previous data) -- that is correct.
# Meaningful trends appear from cycle 2 onward.
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, dict] = {

    "ideal_low_tds": {
        "description": (
            "IDEAL res state -- plant eating AND drinking, TDS below target.\n"
            "  Expect: IDEAL gates, nutrients dosed (ports 1+2 together)."
        ),
        "water_level_trend": "FALLING",
        "cycles": [
            # Cycle 1: baseline -- TDS starting slightly below wk1 veg target (500 PPM)
            {"ph": 6.0,  "tds_ppm": 495.0, "ec_ms": 0.99, "ec_us": 990.0,
             "water_temp_f": 68.0, "co2_ppm": 820, "tent_temp_f": 78.0,
             "tent_humidity": 60.0, "tent_vpd": 1.2},
            # Cycle 2: plant consumed -- TDS and EC dropping
            {"ph": 6.0,  "tds_ppm": 460.0, "ec_ms": 0.92, "ec_us": 920.0,
             "water_temp_f": 68.5, "co2_ppm": 840, "tent_temp_f": 78.5,
             "tent_humidity": 59.0, "tent_vpd": 1.25},
            # Cycle 3: TDS still falling -- trend confirmed, nutrient dose expected
            {"ph": 6.05, "tds_ppm": 425.0, "ec_ms": 0.85, "ec_us": 850.0,
             "water_temp_f": 68.0, "co2_ppm": 850, "tent_temp_f": 79.0,
             "tent_humidity": 58.0, "tent_vpd": 1.3},
        ],
    },

    "ph_drift_low": {
        "description": (
            "IDEAL res, pH on a natural downward swing past the 5.5 hard floor.\n"
            "  Expect: no action while swinging through 5.8-6.0 (still in range),\n"
            "  then pH UP dosed (port 3) once pH breaks below 5.5."
        ),
        "water_level_trend": "FALLING",
        "cycles": [
            # pH in normal swing zone -- no action expected
            {"ph": 5.9, "tds_ppm": 505.0, "ec_ms": 1.01, "ec_us": 1010.0,
             "water_temp_f": 68.0, "co2_ppm": 900},
            # Still within swing zone -- hold
            {"ph": 5.7, "tds_ppm": 494.0, "ec_ms": 0.99, "ec_us": 988.0,
             "water_temp_f": 68.0, "co2_ppm": 910},
            # Approaching hard floor
            {"ph": 5.55, "tds_ppm": 483.0, "ec_ms": 0.97, "ec_us": 966.0,
             "water_temp_f": 68.0, "co2_ppm": 915},
            # Breaks through 5.5 -- pH UP should fire
            {"ph": 5.42, "tds_ppm": 470.0, "ec_ms": 0.94, "ec_us": 940.0,
             "water_temp_f": 68.0, "co2_ppm": 920},
        ],
    },

    "ph_drift_high": {
        "description": (
            "IDEAL res, pH on a natural upward swing past the 6.5 hard ceiling.\n"
            "  Expect: no action while swinging through 6.0-6.3 (still in range),\n"
            "  then pH DOWN dosed (port 4) once pH breaks above 6.5."
        ),
        "water_level_trend": "FALLING",
        "cycles": [
            # pH in normal swing zone -- no action expected
            {"ph": 6.1, "tds_ppm": 502.0, "ec_ms": 1.0, "ec_us": 1004.0,
             "water_temp_f": 68.5, "co2_ppm": 900},
            # Drifting up -- still in range
            {"ph": 6.3, "tds_ppm": 491.0, "ec_ms": 0.98, "ec_us": 982.0,
             "water_temp_f": 68.5, "co2_ppm": 910},
            # Approaching hard ceiling
            {"ph": 6.48, "tds_ppm": 479.0, "ec_ms": 0.96, "ec_us": 958.0,
             "water_temp_f": 68.5, "co2_ppm": 915},
            # Breaks through 6.5 -- pH DOWN should fire
            {"ph": 6.62, "tds_ppm": 466.0, "ec_ms": 0.93, "ec_us": 932.0,
             "water_temp_f": 68.5, "co2_ppm": 920},
        ],
    },

    "watch_ec_rising": {
        "description": (
            "WATCH state -- plant drinking faster than eating (water FALLING, EC RISING).\n"
            "  Expect: WATCH gates, co2 HOLD, dose HOLD -- do NOT add nutrients."
        ),
        "water_level_trend": "FALLING",
        "cycles": [
            {"ph": 6.0,  "tds_ppm": 500.0, "ec_ms": 1.0, "ec_us": 1000.0,
             "water_temp_f": 68.0, "co2_ppm": 900},
            {"ph": 6.05, "tds_ppm": 560.0, "ec_ms": 1.12, "ec_us": 1120.0,
             "water_temp_f": 68.0, "co2_ppm": 905},
            {"ph": 6.1,  "tds_ppm": 620.0, "ec_ms": 1.24, "ec_us": 1240.0,
             "water_temp_f": 68.5, "co2_ppm": 910},
        ],
    },

    "stall": {
        "description": (
            "STALL -- plant not eating or drinking (all readings static).\n"
            "  Expect: STALL state, HOLD all gates, AI flags environment investigation."
        ),
        "water_level_trend": "STATIC",
        "cycles": [
            {"ph": 6.0, "tds_ppm": 500.0, "ec_ms": 1.0, "ec_us": 1000.0,
             "water_temp_f": 68.0, "co2_ppm": 900},
            {"ph": 6.0, "tds_ppm": 500.0, "ec_ms": 1.0, "ec_us": 1000.0,
             "water_temp_f": 68.0, "co2_ppm": 900},
            {"ph": 6.0, "tds_ppm": 500.0, "ec_ms": 1.0, "ec_us": 1000.0,
             "water_temp_f": 68.0, "co2_ppm": 900},
        ],
    },

    "stress": {
        "description": (
            "STRESS -- water static (plant not drinking), EC building (nutes concentrating).\n"
            "  Expect: STRESS gates, NO nutrients, REDUCE CO2, water temp concern."
        ),
        "water_level_trend": "STATIC",
        "cycles": [
            {"ph": 6.0,  "tds_ppm": 500.0, "ec_ms": 1.0,  "ec_us": 1000.0,
             "water_temp_f": 74.0, "co2_ppm": 900},
            {"ph": 5.98, "tds_ppm": 565.0, "ec_ms": 1.13, "ec_us": 1130.0,
             "water_temp_f": 75.5, "co2_ppm": 920},
            {"ph": 5.95, "tds_ppm": 630.0, "ec_ms": 1.26, "ec_us": 1260.0,
             "water_temp_f": 76.0, "co2_ppm": 940},
        ],
    },

    "high_water_temp": {
        "description": (
            "IDEAL res health but water temp rising above 72F limit.\n"
            "  Expect: IDEAL gates open, but water temp flagged as concern."
        ),
        "water_level_trend": "FALLING",
        "cycles": [
            {"ph": 6.0, "tds_ppm": 505.0, "ec_ms": 1.01, "ec_us": 1010.0,
             "water_temp_f": 71.0, "co2_ppm": 900},
            {"ph": 6.0, "tds_ppm": 480.0, "ec_ms": 0.96, "ec_us": 960.0,
             "water_temp_f": 73.5, "co2_ppm": 910},
            {"ph": 6.0, "tds_ppm": 455.0, "ec_ms": 0.91, "ec_us": 910.0,
             "water_temp_f": 75.2, "co2_ppm": 915},
        ],
    },
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_scenario(name: str, scenario: dict, use_ai: bool):
    _reset()

    print(f"\n{'#' * 72}")
    print(f"  SCENARIO: {name}")
    for line in scenario["description"].splitlines():
        print(f"  {line}")
    print(f"{'#' * 72}")

    wl_trend = scenario.get("water_level_trend", "FALLING")
    os.environ["WATER_LEVEL_TREND"] = wl_trend
    print(f"  WATER_LEVEL_TREND = {wl_trend}  (manual override)")

    n = len(scenario["cycles"])
    for i, sensor_values in enumerate(scenario["cycles"], 1):
        print(f"\n{'-' * 60}")
        print(f"  Cycle {i}/{n}")
        print(f"  Input:   {json.dumps(sensor_values)}")

        devices  = make_devices(sensor_values)
        snapshot = ai_advisor.build_snapshot(devices)

        rh     = snapshot.get("res_health", {})
        trends = snapshot.get("trends", {})

        print(f"  Trends:  {json.dumps(trends)}")
        print(f"  ResHlth: {rh.get('state','?')}  "
              f"co2_gate={rh.get('co2_gate','?')}  "
              f"dose_gate={rh.get('dose_gate','?')}  "
              f"ph_gate={rh.get('ph_gate','?')}")
        print(f"  Gate msg: {rh.get('summary','')}")

        if use_ai:
            print("  [AI] Thinking...", flush=True)
            t0     = time.time()
            result = ai_advisor.ask_ai(snapshot)
            secs   = f"{time.time() - t0:.1f}s"

            if result is None:
                print(f"  [AI] No response or parse error ({secs})")
                continue

            print(f"  [AI] ({secs})  {result.get('assessment', '')}")
            for c in result.get("concerns", []):
                print(f"  [!]  {c}")

            proposed = result.get("actions", [])
            if not proposed:
                print("  [AI] No actions recommended.")
            else:
                # Run the full execute path -- set_port_speed / set_outlet are
                # patched at the top of this file to print instead of hit the API.
                ai_advisor.execute_actions(result, devices, token="SIM")
        else:
            print("  (AI disabled -- gate logic only)")


def main():
    args   = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_ai = not NO_AI

    if args:
        name = args[0]
        if name not in SCENARIOS:
            print(f"Unknown scenario '{name}'.")
            print(f"Available: {', '.join(SCENARIOS)}")
            sys.exit(1)
        run_scenario(name, SCENARIOS[name], use_ai)
    else:
        for name, scenario in SCENARIOS.items():
            run_scenario(name, scenario, use_ai)

    print(f"\n{'=' * 72}")
    print("  Simulation complete.")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
