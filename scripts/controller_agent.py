#!/usr/bin/env python3
"""
controller_agent.py — Hybrid rules/ML/LLM grow tent climate controller.

Three-layer control stack:
  Layer 1 (Rules, every 30s):   Hysteresis on current readings — immediate response.
  Layer 2 (ML, every 30s):      Ridge regression trained on last 30min, predicts 5min
                                 ahead — pre-empts threshold crossings before they happen.
  Layer 3 (LLM, every 30min):   Claude reviews trends and tunes live setpoints.

Device ports — update when plugged in:
  PORTS["ac"]           = None   → e.g. 1
  PORTS["humidifier"]   = None   → e.g. 2
  PORTS["dehumidifier"] = None   → e.g. 3

Usage:
  python scripts/controller_agent.py
  ANTHROPIC_API_KEY=sk-... python scripts/controller_agent.py
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

import numpy as np
from sklearn.linear_model import Ridge

from aci_ble_lab.db import _conn, build_unified_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("controller")

# ── Ports (update when devices are plugged in) ────────────────────────────────

PORTS = {
    "ac":           None,   # controller port 1-8
    "humidifier":   None,
    "dehumidifier": None,
}

# ── Setpoints (LLM may adjust these at runtime) ───────────────────────────────

TARGETS = {
    "temp_lo":     70.0,   # °F  — AC off below this
    "temp_hi":     74.0,   # °F  — AC on above this  (current ~75°F → will trigger)
    "hum_lo":      58.0,   # %RH — humidifier on below this
    "hum_hi":      65.0,   # %RH — dehumidifier on above this  (current ~66% → will trigger)
}

# ── Tuning ────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 30     # seconds between control ticks
ML_HORIZON    = 300    # seconds ahead to predict (5 min)
ML_HISTORY    = 1800   # seconds of history for ML training (30 min)
LLM_INTERVAL  = 1800   # seconds between LLM reviews (30 min)

DEADBAND = {"temp": 0.5, "hum": 1.5}   # prevents rapid on/off cycling

AC_SPEED    = 7   # fan speed when AC is on (1-10)
FAN_SPEED   = 4   # speed for humidifier / dehumidifier

CMD_FILE = Path(__file__).resolve().parent.parent / "aci_control.json"

# ── Device state (tracks last commanded state) ────────────────────────────────

_state = {"ac": False, "humidifier": False, "dehumidifier": False}

# ── Sensor port / type mapping ────────────────────────────────────────────────
# All T+H-style sensors use type codes 0x60-0x7F (rotate +8 as sensors are added).
# We average across all matching type codes in the window.

SENSOR = {
    "temp":     {"port": 0,  "type_lo": 0x60, "type_hi": 0x7F},
    "humidity": {"port": 2,  "type_lo": 0x60, "type_hi": 0x7F},
    "vpd":      {"port": 3,  "type_lo": 0x60, "type_hi": 0x7F},
    "co2":      {"port": 11, "type_lo": 0x21, "type_hi": 0x21},
    "ph":       {"port": 13, "type_lo": 0x61, "type_hi": 0x69},
    "water_temp":{"port": 18,"type_lo": 0x61, "type_hi": 0x69},
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def fetch_series(sensor: str, window_s: int) -> tuple[list[float], list[float]]:
    """Return (timestamps, values) for a named sensor over the last window_s seconds."""
    cfg = SENSOR[sensor]
    cutoff = time.time() - window_s
    with _conn() as c:
        rows = c.execute("""
            SELECT ts, value FROM sensor_readings
            WHERE ts > ? AND port = ? AND sensor_type BETWEEN ? AND ?
            ORDER BY ts
        """, (cutoff, cfg["port"], cfg["type_lo"], cfg["type_hi"])).fetchall()
    return [r["ts"] for r in rows], [r["value"] for r in rows]


def current_avg(sensor: str, window_s: int = 120) -> float | None:
    _, vals = fetch_series(sensor, window_s)
    return sum(vals) / len(vals) if vals else None


def current_from_snapshot(snapshot: dict, sensor: str) -> float | None:
    """Pull a named sensor's value from a unified snapshot dict."""
    sensor_id = SENSOR[sensor]["port"]
    entry = snapshot.get(sensor_id)
    return entry["value"] if entry else None


def summary_30min() -> dict:
    """Build a 30-min stats summary using BLE time-series + current unified snapshot."""
    out = {}
    # BLE time-series stats (high-frequency)
    for name in SENSOR:
        _, vals = fetch_series(name, 1800)
        if vals:
            out[name] = {
                "avg": round(sum(vals) / len(vals), 2),
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "source": "ble",
                "n": len(vals),
            }
    # Cloud-only sensors (anything in unified snapshot not covered by BLE above)
    snap = build_unified_snapshot(ble_max_age=60, cloud_max_age=300)
    for sensor_id, entry in snap.items():
        if entry["source"] == "cloud":
            # Find a human name if we have one
            label = next(
                (n for n, cfg in SENSOR.items() if cfg["port"] == sensor_id), f"sensor_{sensor_id}"
            )
            if label not in out:
                out[label] = {
                    "avg": entry["value"], "min": entry["value"], "max": entry["value"],
                    "source": "cloud", "n": 1, "dev": entry.get("dev_name"),
                }
    return out


# ── Actuator control ──────────────────────────────────────────────────────────

def _send(device: str, on: bool, speed: int) -> bool:
    port = PORTS.get(device)
    if port is None:
        return False

    deadline = time.time() + 5.0
    while CMD_FILE.exists() and time.time() < deadline:
        time.sleep(0.1)
    if CMD_FILE.exists():
        log.warning("Drop-file busy, skipping %s command", device)
        return False

    payload = {"port": port, "work_type": 2 if on else 1, "speed": speed if on else 0}
    CMD_FILE.write_text(json.dumps(payload))

    deadline = time.time() + 3.0
    while CMD_FILE.exists() and time.time() < deadline:
        time.sleep(0.1)

    _state[device] = on
    log.info("%-13s → %-3s  port=%d  speed=%d", device, "ON" if on else "OFF", port, speed if on else 0)
    return True


def set_device(device: str, on: bool, speed: int = FAN_SPEED):
    if _state[device] != on:
        _send(device, on, speed)


# ── Layer 1: Rules ────────────────────────────────────────────────────────────

def layer_rules(temp: float | None, hum: float | None):
    if temp is not None:
        if temp > TARGETS["temp_hi"] + DEADBAND["temp"]:
            set_device("ac", True, AC_SPEED)
        elif temp < TARGETS["temp_lo"] - DEADBAND["temp"]:
            set_device("ac", False)

    if hum is not None:
        if hum > TARGETS["hum_hi"] + DEADBAND["hum"]:
            set_device("dehumidifier", True)
            set_device("humidifier", False)
        elif hum < TARGETS["hum_lo"] - DEADBAND["hum"]:
            set_device("humidifier", True)
            set_device("dehumidifier", False)
        elif TARGETS["hum_lo"] <= hum <= TARGETS["hum_hi"]:
            set_device("humidifier", False)
            set_device("dehumidifier", False)


# ── Layer 2: ML predictor ─────────────────────────────────────────────────────

def predict_ahead(ts: list[float], vals: list[float], horizon_s: int) -> float | None:
    if len(ts) < 20:
        return None
    X = np.array(ts).reshape(-1, 1)
    y = np.array(vals)
    model = Ridge(alpha=1.0)
    model.fit(X, y)
    return float(model.predict([[ts[-1] + horizon_s]])[0])


def layer_ml():
    ts, vals = fetch_series("temp", ML_HISTORY)
    pred_temp = predict_ahead(ts, vals, ML_HORIZON)

    ts, vals = fetch_series("humidity", ML_HISTORY)
    pred_hum = predict_ahead(ts, vals, ML_HORIZON)

    acted = False
    if pred_temp is not None:
        log.info("ML  temp  now=%.1f°F  predicted 5min=%.1f°F", vals[-1] if vals else 0, pred_temp)
        if pred_temp > TARGETS["temp_hi"] and not _state["ac"]:
            log.info("ML pre-empting AC ON  (forecast %.1f°F > %.1f°F)", pred_temp, TARGETS["temp_hi"])
            set_device("ac", True, AC_SPEED)
            acted = True

    if pred_hum is not None:
        log.info("ML  hum   now=%.1f%%  predicted 5min=%.1f%%", vals[-1] if vals else 0, pred_hum)
        if pred_hum > TARGETS["hum_hi"] and not _state["dehumidifier"]:
            log.info("ML pre-empting dehumidifier ON  (forecast %.1f%% > %.1f%%)", pred_hum, TARGETS["hum_hi"])
            set_device("dehumidifier", True)
            acted = True
        elif pred_hum < TARGETS["hum_lo"] and not _state["humidifier"]:
            log.info("ML pre-empting humidifier ON  (forecast %.1f%% < %.1f%%)", pred_hum, TARGETS["hum_lo"])
            set_device("humidifier", True)
            acted = True

    return acted


# ── Layer 3: LLM setpoint advisor ────────────────────────────────────────────

_llm_available = None   # None = not checked, True/False = result


def _check_llm() -> bool:
    global _llm_available
    if _llm_available is None:
        try:
            import anthropic   # noqa: F401
            _llm_available = bool(os.environ.get("ANTHROPIC_API_KEY"))
            if not _llm_available:
                log.info("ANTHROPIC_API_KEY not set — LLM layer disabled")
        except ImportError:
            _llm_available = False
            log.info("anthropic package missing — LLM layer disabled")
    return _llm_available


def layer_llm():
    if not _check_llm():
        return

    import anthropic

    stats = summary_30min()
    context_lines = [
        f"Current setpoints: temp {TARGETS['temp_lo']}-{TARGETS['temp_hi']}°F, "
        f"humidity {TARGETS['hum_lo']}-{TARGETS['hum_hi']}%RH",
        f"Device state: AC={'ON' if _state['ac'] else 'OFF'}, "
        f"humidifier={'ON' if _state['humidifier'] else 'OFF'}, "
        f"dehumidifier={'ON' if _state['dehumidifier'] else 'OFF'}",
        "Sensor 30-min stats (avg / min / max):",
    ]
    for name, s in stats.items():
        src = s.get("source", "")
        dev = f" ({s['dev']})" if s.get("dev") else ""
        context_lines.append(
            f"  {name:14}: {s['avg']:.2f}  [{s['min']:.2f} – {s['max']:.2f}]"
            f"  [{src}{dev}]"
        )
    context = "\n".join(context_lines)

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "You are a grow-tent climate controller. "
                "Review the 30-minute sensor summary and decide if setpoints need adjustment. "
                "Respond ONLY with valid JSON — no markdown, no explanation outside the JSON:\n"
                '{"temp_lo": float, "temp_hi": float, "hum_lo": float, "hum_hi": float, '
                '"reason": "one short sentence"}\n'
                "Constraints: temp range 65-85°F, humidity range 40-80%RH, "
                "max change ±2°F or ±3%RH per review. "
                "If no adjustment is needed respond with unchanged values and reason 'holding'."
            ),
            messages=[{"role": "user", "content": context}],
        )

        suggestion = json.loads(resp.content[0].text.strip())
        old = {k: TARGETS[k] for k in ("temp_lo", "temp_hi", "hum_lo", "hum_hi")}
        for k in old:
            if k in suggestion:
                TARGETS[k] = float(suggestion[k])
        log.info("LLM  reason='%s'  setpoints %s → %s",
                 suggestion.get("reason", "?"),
                 {k: old[k] for k in old},
                 {k: TARGETS[k] for k in old})

    except json.JSONDecodeError as e:
        log.warning("LLM response not valid JSON: %s", e)
    except Exception as e:
        log.warning("LLM review error: %s", e)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Controller started.")
    log.info("Ports     : %s", PORTS)
    log.info("Targets   : temp %.0f-%.0f°F  humidity %.0f-%.0f%%RH",
             TARGETS["temp_lo"], TARGETS["temp_hi"], TARGETS["hum_lo"], TARGETS["hum_hi"])
    log.info("LLM layer : %s", "enabled" if _check_llm() else "disabled (set ANTHROPIC_API_KEY)")

    t_last_llm = 0.0

    while True:
        now = time.time()

        # Unified snapshot: BLE preferred, cloud fills gaps
        snap = build_unified_snapshot(ble_max_age=90, cloud_max_age=300)

        temp = current_from_snapshot(snap, "temp")
        hum  = current_from_snapshot(snap, "humidity")
        vpd  = current_from_snapshot(snap, "vpd")

        # Show source tags on readings line
        def _src(sensor):
            e = snap.get(SENSOR[sensor]["port"])
            return e["source"][0].upper() if e else "?"   # B=ble, C=cloud

        if temp is not None:
            log.info("Readings  : temp=%.1f°F[%s]  hum=%.1f%%[%s]  vpd=%.2f[%s]",
                     temp, _src("temp"), hum or 0.0, _src("humidity"),
                     vpd or 0.0, _src("vpd"))
        else:
            log.warning("No recent readings — logger and cloud_ingest may both be down")

        layer_rules(temp, hum)
        layer_ml()   # ML still uses BLE time-series (fetch_series) — needs dense history

        if (now - t_last_llm) >= LLM_INTERVAL:
            t_last_llm = now
            layer_llm()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Controller stopped.")
