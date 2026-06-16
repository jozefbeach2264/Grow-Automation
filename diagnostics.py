"""
diagnostics.py -- deterministic stressor list + code-owned playbook registry.

Layer 3 (away-mode triage) FOUNDATION. This module builds a machine-readable
list of what is wrong with the grow RIGHT NOW, straight from the snapshot, and
maps each stressor to the code-owned playbooks that would be allowed to address
it. It is READ-ONLY: it attaches `snapshot["diagnostics"]` for the HUD, the AI
prompt (situational awareness), and the event ledger. It does NOT actuate
anything and does NOT change the AI action contract -- per the architect review,
the raw-action -> selected_playbook contract change is a separate, riskier step
gated behind trusted live control. Playbooks here are reference data + the
allowed-list each stressor exposes; nothing dispatches them yet.

All thresholds are read from `.env` each call (dynamic, same as the rest of the
system). Stressors are emitted only for sensors that are actually present and
out of band, plus offline devices -- so the deliberately-disconnected reservoir
and CO2 hardware stay quiet instead of spamming false alarms.

Severity vocabulary: info / watch / medium / high / critical.
"""

import os


# --------------------------------------------------------------------------- #
# Playbook registry -- code-owned. Each entry documents what the playbook does,
# which tier it sits in, and (for chemical ones) that it is gated. The away-mode
# executor (future) will dispatch ONLY from this registry; for now it is the
# source of the `allowed_playbooks` lists and a reference for the AI/HUD.
# --------------------------------------------------------------------------- #
ALERT_ONLY = "alert_only"

PLAYBOOKS: dict[str, dict] = {
    ALERT_ONLY: {
        "tier": 0, "actuates": False,
        "summary": "Log + notify only; no hardware action.",
    },
    "increase_exhaust_one_step": {
        "tier": 2, "actuates": True, "role": "ROLE_EXHAUST",
        "summary": "Raise exhaust fan +1 speed (capped) to shed heat/VPD/CO2.",
    },
    "reduce_light_one_step": {
        "tier": 2, "actuates": True, "role": "ROLE_LIGHT",
        "summary": "Lower light intensity -1 step (floored) to ease heat/VPD.",
    },
    "disable_co2": {
        "tier": 1, "actuates": True, "role": "CO2_VALVE",
        "summary": "Force CO2 valve OFF until res health / CO2 recovers.",
    },
    # Chemical playbooks -- Tier 3, stricter gating; present for the registry but
    # never dispatched until timed dosing is proven live (AUTONOMOUS_DOSING).
    "timed_nutrient_microdose": {
        "tier": 3, "actuates": True, "chemical": True,
        "summary": "Small timed V1+V2 nutrient dose, forced stop, then verify.",
    },
    "timed_ph_up_microdose": {
        "tier": 3, "actuates": True, "chemical": True,
        "summary": "Small timed pH-up dose, forced stop, then verify.",
    },
    "timed_ph_down_microdose": {
        "tier": 3, "actuates": True, "chemical": True,
        "summary": "Small timed pH-down dose, forced stop, then verify.",
    },
}

# Stressor -> ordered allowed playbooks. alert_only is always appended as the
# safe fallback so there is always a valid choice.
_STRESSOR_PLAYBOOKS: dict[str, list[str]] = {
    "tent_temp_high":             ["increase_exhaust_one_step", "reduce_light_one_step"],
    "tent_temp_low":              [],
    "vpd_high":                   ["increase_exhaust_one_step", "reduce_light_one_step"],
    "vpd_low":                    [],
    "humidity_high":              ["increase_exhaust_one_step"],
    "humidity_low":               [],
    "co2_high":                   ["disable_co2", "increase_exhaust_one_step"],
    "co2_high_while_res_stalled": ["disable_co2"],
    "ph_high":                    ["timed_ph_down_microdose"],
    "ph_low":                     ["timed_ph_up_microdose"],
    "tds_high":                   [],
    "tds_low":                    ["timed_nutrient_microdose"],
    "water_temp_high":            [],
    "water_temp_low":             [],
    "water_level_static":         [],
    "water_level_rising":         [],
    "device_offline":             [],
}


def allowed_playbooks(stressor_name: str) -> list[str]:
    """Code-owned allow-list for a stressor (alert_only always included last)."""
    base = _STRESSOR_PLAYBOOKS.get(stressor_name, [])
    return [*base, ALERT_ONLY]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _flat_sensors(snapshot: dict) -> dict:
    out: dict = {}
    for dev in snapshot.get("devices", []) or []:
        out.update(dev.get("sensors", {}) or {})
    return out


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _stressor(name: str, severity: str, evidence: str, likely_effect: str) -> dict:
    return {
        "name": name,
        "severity": severity,
        "evidence": evidence,
        "likely_effect": likely_effect,
        "allowed_playbooks": allowed_playbooks(name),
    }


def _band_stressor(sensors: dict, key: str, base: str, lo_env: str, hi_env: str,
                   lo_default: float, hi_default: float, effect_high: str,
                   effect_low: str, *, critical_at: float | None = None,
                   high_severity: str = "high") -> dict | None:
    """Emit a high/low stressor if `key` is present and outside [lo, hi]. Skips
    silently when the sensor is absent (a deliberately-disconnected probe stays
    quiet). `critical_at` escalates a high reading to severity 'critical'."""
    v = sensors.get(key)
    if not isinstance(v, (int, float)):
        return None
    lo = _envf(lo_env, lo_default)
    hi = _envf(hi_env, hi_default)
    if v > hi:
        sev = "critical" if (critical_at is not None and v >= critical_at) else high_severity
        return _stressor(f"{base}_high", sev,
                         f"{key}={v:g}, target max={hi:g}", effect_high)
    if v < lo:
        return _stressor(f"{base}_low", high_severity,
                         f"{key}={v:g}, target min={lo:g}", effect_low)
    return None


# --------------------------------------------------------------------------- #
# stressor builder
# --------------------------------------------------------------------------- #
def build_stressors(snapshot: dict) -> list[dict]:
    """Return the deterministic stressor list for this snapshot, ordered by
    severity (critical first). Pure function; reads thresholds from .env."""
    sensors = _flat_sensors(snapshot)
    res = snapshot.get("res_health") or {}
    trends = snapshot.get("trends") or {}
    out: list[dict] = []

    # --- Air / canopy (the sensors that are live right now) -----------------
    tent_key = os.getenv("HIGH_TEMP_SENSOR", "temp_f_tent").strip() or "temp_f_tent"
    crit = _envf("AIR_TEMP_EMERGENCY_F", 0) or None
    s = _band_stressor(sensors, tent_key, "tent_temp", "AIR_TEMP_MIN", "AIR_TEMP_MAX",
                       70, 85, "heat stress; transpiration load up",
                       "cold stress; slowed growth", critical_at=crit)
    if s:
        out.append(s)

    hum_key = os.getenv("CANOPY_HUMIDITY_SENSOR", "humidity_tent").strip() or "humidity_tent"
    s = _band_stressor(sensors, hum_key, "humidity", "HUMIDITY_MIN", "HUMIDITY_MAX",
                       50, 70, "mold/mildew risk", "high VPD; transpiration stress")
    if s:
        out.append(s)

    vpd_key = os.getenv("CANOPY_VPD_SENSOR", "vpd_tent").strip() or "vpd_tent"
    s = _band_stressor(sensors, vpd_key, "vpd", "VPD_MIN", "VPD_MAX",
                       0.8, 1.5, "transpiration too fast; tip burn risk",
                       "transpiration too slow; weak uptake")
    if s:
        out.append(s)

    # --- Reservoir chemistry (quiet while the HDS3 is disconnected) ----------
    s = _band_stressor(sensors, "ph", "ph", "PH_MIN", "PH_MAX", 5.8, 6.2,
                       "nutrient lockout at high pH", "nutrient lockout at low pH")
    if s:
        out.append(s)

    s = _band_stressor(sensors, "tds_ppm", "tds", "TDS_MIN", "TDS_MAX", 800, 1600,
                       "over-fertilization; osmotic stress", "under-feeding")
    if s:
        out.append(s)

    # Water temp is reference-only (no chiller) -> cap at medium, alert-only.
    s = _band_stressor(sensors, "water_temp_f", "water_temp",
                       "WATER_TEMP_MIN", "WATER_TEMP_MAX", 65, 72,
                       "root-zone stress; DO drop / pathogen risk",
                       "slowed root metabolism", high_severity="medium")
    if s:
        out.append(s)

    # --- CO2 (quiet while the sensor is disconnected) -----------------------
    co2 = sensors.get("co2_ppm")
    target = snapshot.get("co2_target")
    if isinstance(co2, (int, float)) and isinstance(target, (int, float)) and target > 0:
        band = _envf("CO2_TOLERANCE", 100)
        if co2 > target + band:
            if res.get("state") in ("STALL", "STRESS", "PROBLEM"):
                out.append(_stressor(
                    "co2_high_while_res_stalled", "high",
                    f"co2_ppm={co2:g} > target {target:g}+{band:g}, res={res.get('state')}",
                    "enriching CO2 while the plant can't use it wastes gas + adds load"))
            else:
                out.append(_stressor(
                    "co2_high", "medium",
                    f"co2_ppm={co2:g} > target {target:g}+{band:g}",
                    "above enrichment target"))

    # --- Water level trend (FLOAT/manual/analog -> res_health) --------------
    wl = trends.get("water_level")
    if wl == "RISING":
        out.append(_stressor("water_level_rising", "high",
                             "water_level trend=RISING",
                             "plant not drinking; possible root problem / overfill"))
    elif wl == "STATIC" and (res.get("water_trend") == "STATIC"):
        out.append(_stressor("water_level_static", "watch",
                             "water_level trend=STATIC",
                             "reduced uptake; check feeding/health"))

    # --- Offline devices ----------------------------------------------------
    for dev in snapshot.get("devices", []) or []:
        if dev.get("online") is False:
            out.append(_stressor("device_offline", "high",
                                 f"device '{dev.get('name')}' reports offline",
                                 "lost control/telemetry for this device"))

    _SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "watch": 3, "info": 4}
    out.sort(key=lambda s: _SEV_ORDER.get(s["severity"], 5))
    return out


def build_diagnostics(snapshot: dict) -> dict:
    """Snapshot-attachable diagnostics block: the stressor list + a compact
    summary. Worst severity is surfaced for quick HUD/gate use."""
    stressors = build_stressors(snapshot)
    worst = stressors[0]["severity"] if stressors else "none"
    return {
        "stressors": stressors,
        "count": len(stressors),
        "worst_severity": worst,
    }
