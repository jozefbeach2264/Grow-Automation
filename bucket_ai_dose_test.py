#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Closed-loop AI-assisted bucket dose test -- FloraFlex feedforward + creep.

Evolves bucket_dose_test.py from "you pick the mL" to "code calculates the mL toward
a target you set, qwen rides shotgun, you confirm every dose." Two control laws:

  NUTRIENTS (EC/TDS) -- linear, calculable:
    fast dose to 85% of the gap (one calculated V1+V2 pair), then CREEP the last 15%
    with small calculated doses. Deliberate undershoot -- EC only comes down by dilution.

  pH -- non-linear, buffered by the nutrients:
    buffer capacity scales with EC, so size the acid/base dose from the EC-normalized
    buffer constant  K_pH = dpH * EC * gal / mL  (binned by pH region), undershoot 0.7,
    then creep. ALWAYS dose pH AFTER EC is in band -- the buffer (and thus the dose) is
    only valid at the final EC.

The mL is always CODE-OWNED (calculated from measured calibration), never AI-chosen --
qwen only assesses + picks the axis. Every dose is supervised (y/N) and fired through the
same bounded dosing.timed_dose / timed_dose_pair path (verify + forced stop + freeze).

Each result appends to profiles/bucket_test_log.jsonl in the SAME schema as the manual
harness (so both datasets merge for calibration), plus normalized fields: k_nute / k_ph,
ph_bin, ec_at_dose, target_*, res_id, mode, ai_assessment.

DO NOT run while poller.py is live -- the doser watchdog there would fight these doses.

Run:  python3 bucket_ai_dose_test.py
"""

import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv

ENV = Path(__file__).parent / ".env"
LABELS = Path(__file__).parent / "labels.env"
load_dotenv(ENV)
load_dotenv(LABELS)

from ac_infinity_client import get_or_refresh_token, fetch_all_devices, parse_device
import dosing
import safety_state
import ai_advisor
from utils import name_slug

DEVICE_NAME = os.getenv("BUCKET_TEST_DEVICE", "Hydroponics Control")
LOG_FILE = Path(__file__).parent / "profiles" / "bucket_test_log.jsonl"

# Display cadence during the HARD settle (the wait itself is dosing.dose_settle_seconds()).
STABLE_POLL_SEC = 15
# Control-law constants.
FAST_FRACTION = 0.85       # nutrient fast dose closes this much of the gap in one shot
PH_UNDERSHOOT = 0.70       # pH dose aims for this fraction of the needed move
PH_BIN_WIDTH = 0.25        # pH-region bin for the buffer constant
EC_TOL_US = 25.0           # EC within this of target counts as "in band" (gates pH)
PH_TOL = 0.05              # pH within this of the band edge counts as in range
# No-chemical fire test: pumps spin but nothing is dosed (lines purged). Quarantined
# log + short settle + effectiveness alarm; never touches the real calibration data.
DRYFIRE_LOG = Path(__file__).parent / "profiles" / "bucket_dryfire_log.jsonl"
NOCHEM_SETTLE_SEC = 30
INEFFECTIVE_RATIO = 0.25   # observed < this fraction of model prediction -> ineffective
INEFFECTIVE_HALT = 2       # consecutive ineffective doses on an axis -> halt + alert
PH_MIN_RESPONSE = 0.02     # pH learning creep: |dpH| >= this counts as a live (non-dead) dose

# --- Rework (2026-06-04): calculate-before-deadband, fast high-speed shot + slow creep,
#     bucket cap lift, pH fixed-creep learning. See dose_align findings. ---
FAST_DOSE_SPEED = int(os.getenv("FAST_DOSE_SPEED", "8"))     # the 85% feedforward shot fires fast
CREEP_DOSE_SPEED = int(os.getenv("CREEP_DOSE_SPEED", "2"))   # the last 15% creeps slow + precise
# Supervised bucket fill -> lift the autonomous 50 mL grow cap so the calculated 85% shot
# fires whole instead of being chopped. Still a sane ceiling against a fat-finger.
BUCKET_MAX_DOSE_ML = float(os.getenv("BUCKET_MAX_DOSE_ML", "250"))
# pH is non-linear / not yet calculable (K_pH not stationary) -> creep with a FIXED small
# dose, log each to build K_pH across bins. PH_DONE_TOL: within this of the band = done.
PH_CREEP_ML = float(os.getenv("PH_CREEP_ML", "4.0"))
PH_DONE_TOL = float(os.getenv("PH_DONE_TOL", "0.05"))


# --------------------------------------------------------------------------- #
# Calibration model -- normalized, stationary constants learned from the log
# --------------------------------------------------------------------------- #
def ph_bin(ph: float) -> str:
    """Region key for the pH buffer constant (buffering is locally linear in a bin)."""
    return f"{math.floor(ph / PH_BIN_WIDTH) * PH_BIN_WIDTH:.2f}"


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_calibration() -> dict:
    """
    Build normalized dose-response constants from profiles/bucket_test_log.jsonl.

      nutrients:  K_nute[port] = d_tds * gal / actual_ml         (ppm.gal per actual-mL)
      pH:         K_ph[dir][bin] = |d_ph| * ec_before * gal / actual_ml   (EC-normalized)

    Returns {"nute": {port: {"k": float, "n": int}},
             "ph":   {"up"/"down": {bin: {"k": float, "n": int}}}}.
    Both are running means -- thin now (n~1), they tighten as the grow sweeps EC.
    """
    cal = {"nute": {}, "nute_pair": {"k": 0.0, "n": 0}, "ph": {"up": {}, "down": {}}}
    if not LOG_FILE.exists():
        return cal
    for line in LOG_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        gal = _f(r.get("reservoir_gal"))
        ml = _f(r.get("actual_ml"))
        if not gal or not ml or gal <= 0 or ml <= 0:
            continue
        sol = (r.get("solution") or "").upper()
        before = r.get("before") or {}
        d_tds = _f(r.get("d_tds_ppm"))
        d_ph = _f(r.get("d_ph"))
        if "PH" in sol:  # pH record
            ec = _f(before.get("ec_us"))
            bph = _f(before.get("ph"))
            if ec and bph is not None and d_ph is not None and abs(d_ph) > 1e-6:
                direction = "up" if d_ph > 0 else "down"
                k = abs(d_ph) * ec * gal / ml
                _accumulate(cal["ph"][direction], ph_bin(bph), k)
        elif r.get("axis") == "pair":    # AI V1+V2 pair dose -> directly-measured pair K
            if d_tds is not None and abs(d_tds) > 1e-6:
                cell = cal["nute_pair"]
                cell["k"] = (cell["k"] * cell["n"] + d_tds * gal / ml) / (cell["n"] + 1)
                cell["n"] += 1
        else:            # single-pump characterization record (V1 or V2 alone)
            port = r.get("port")
            if port is not None and d_tds is not None and abs(d_tds) > 1e-6:
                _accumulate(cal["nute"], port, d_tds * gal / ml)
    return cal


def _accumulate(bucket: dict, key, k: float):
    cell = bucket.setdefault(key, {"k": 0.0, "n": 0})
    cell["k"] = (cell["k"] * cell["n"] + k) / (cell["n"] + 1)
    cell["n"] += 1


def k_nute_pair(cal: dict, nute_ports: list) -> tuple[float, int]:
    """Combined ppm.gal per mL-each for a V1+V2 pair. Prefer the directly-measured pair K
    (from V1+V2 doses); fall back to summing the per-port single-pump constants."""
    pair = cal.get("nute_pair") or {}
    if pair.get("n", 0) > 0:
        return pair["k"], pair["n"]
    total, n_min = 0.0, None
    for p in nute_ports:
        cell = cal["nute"].get(p) or cal["nute"].get(str(p))
        if not cell:
            return 0.0, 0          # no data for a port -> can't calculate the pair
        total += cell["k"]
        n_min = cell["n"] if n_min is None else min(n_min, cell["n"])
    return total, (n_min or 0)


def k_ph(cal: dict, direction: str, ph: float) -> tuple[float, int]:
    """EC-normalized buffer constant for a direction near this pH; fall back to the
    nearest populated bin, then to any bin in that direction."""
    bins = cal["ph"].get(direction, {})
    if not bins:
        return 0.0, 0
    want = ph_bin(ph)
    if want in bins:
        return bins[want]["k"], bins[want]["n"]
    # nearest bin by numeric distance
    nearest = min(bins, key=lambda b: abs(float(b) - float(want)))
    return bins[nearest]["k"], bins[nearest]["n"]


# --------------------------------------------------------------------------- #
# Dose sizing -- feedforward from the normalized constants
# --------------------------------------------------------------------------- #
def plan_nutrient(current_tds, target_tds, gal, k_pair) -> dict | None:
    """Calculated V1+V2 dose. >15% below target -> fast dose to 85% of the gap;
    inside the last 15% -> creep the remainder (×0.9). None if at/above target."""
    if current_tds is None:
        return None
    gap = target_tds - current_tds
    if gap <= 0:                                  # at/above target; resolution floor is the caller's
        return None
    if k_pair <= 0:
        return {"err": "no nutrient calibration yet -- run a manual characterization dose first"}
    far = gap > 0.15 * target_tds
    want_delta = (FAST_FRACTION * gap) if far else (0.9 * gap)
    ml_each = want_delta * gal / k_pair
    return {"axis": "nute", "mode": "fast" if far else "creep",
            "ml_each": ml_each, "want_delta": want_delta, "gap": gap}


def plan_ph(current_ph, ph_min, ph_max, ec, gal, cal) -> dict | None:
    """pH is non-linear and not yet calculable (K_pH not stationary -- see dose_align).
    Creep with a FIXED small dose toward the band; every logged dose builds K_pH across pH
    bins so it can be calculated later. None when already in range (within PH_DONE_TOL)."""
    if current_ph is None:
        return None
    if ph_min - PH_DONE_TOL <= current_ph <= ph_max + PH_DONE_TOL:
        return None
    if current_ph > ph_max:                        # too high -> pH DOWN
        direction, need = "down", current_ph - ph_max
    else:                                          # too low -> pH UP
        direction, need = "up", ph_min - current_ph
    return {"axis": "ph", "direction": direction, "ml": PH_CREEP_ML,
            "need": need, "mode": "creep-learn"}


def ppm_tol() -> float:
    return float(os.getenv("EC_TOLERANCE", "0.1")) * ai_advisor._ppm_scale()


def nute_ratio(device: str) -> tuple[float, float]:
    """V1/V2 VOLUME split from NUTE_RATIO_<SLUG> (e.g. '55/45'); default 50/50, clamped so
    each part stays 45-55% (warn outside). A manual knob the user owns -- never derived from
    potency/ppm/MW; the two-part formula is dosed by equal volume by default."""
    raw = os.getenv(f"NUTE_RATIO_{name_slug(device)}", "").strip()
    if not raw:
        return 0.5, 0.5
    try:
        a, b = (float(x) for x in raw.replace(",", "/").split("/"))
        f1 = a / (a + b)
    except (ValueError, ZeroDivisionError):
        print(f"  [ratio] bad NUTE_RATIO '{raw}' -- using 50/50")
        return 0.5, 0.5
    f1c = min(max(f1, 0.45), 0.55)
    if abs(f1c - f1) > 1e-9:
        print(f"  [ratio] NUTE_RATIO {raw} (V1 {f1:.0%}) outside 45-55% -- clamped to {f1c:.0%}")
    return f1c, 1.0 - f1c


# --------------------------------------------------------------------------- #
# Hydro read + hard settle (same contract as bucket_dose_test.py)
# --------------------------------------------------------------------------- #
def read_hydro(token, dev_id):
    for raw in fetch_all_devices(token):
        if raw.get("devId") != dev_id:
            continue
        d = parse_device(raw)
        return {"ph": d.get("ph"), "ec_us": d.get("ec_us"),
                "tds_ppm": d.get("tds_ppm"), "water_temp_f": d.get("water_temp_f")}
    return None


def fmt(r):
    if not r:
        return "(no reading)"
    return (f"pH {r['ph']}  EC {r['ec_us']} uS/cm  TDS {r['tds_ppm']} ppm  "
            f"H2O {r['water_temp_f']} F")


def probe_sane(r):
    if not r or r.get("ph") in (None, -327.68) or r.get("tds_ppm") in (None, -327.68):
        return False
    return True


def wait_for_stable(token, dev_id, settle_sec=None):
    """HARD settle the full doser window -- NO early exit (pH drifts ~5 min).
    settle_sec overrides the window (the short no-chemical-test settle)."""
    total = int(settle_sec if settle_sec is not None else dosing.dose_settle_seconds())
    print(f"  HARD settle {total // 60}m{total % 60:02d}s (no early exit)...")
    elapsed = 0
    while elapsed < total:
        step = min(STABLE_POLL_SEC, total - elapsed)
        time.sleep(step)
        elapsed += step
        print(f"    t+{elapsed:>4}s  {fmt(read_hydro(token, dev_id))}")
    return read_hydro(token, dev_id)


def log_result(rec, path=LOG_FILE):
    try:
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  [WARN] could not write log: {e}")


def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else default


# --------------------------------------------------------------------------- #
# AI oversight (advisory) -- qwen assesses + picks an axis; mL stays code-owned
# --------------------------------------------------------------------------- #
def ai_oversight(devices, target_ph, target_tds):
    """Inject the session targets the way the live system does (env overrides that
    _build_system_prompt reads), open the bucket res-health gate, and ask qwen."""
    week, stage = ai_advisor._effective_week_stage()
    os.environ[f"PPM_{stage.upper()}_WK{week}"] = str(int(target_tds))
    # Tight band around target pH so the prompt renders a real setpoint.
    os.environ["PH_MIN"] = f"{target_ph - 0.15:.2f}"
    os.environ["PH_MAX"] = f"{target_ph + 0.15:.2f}"
    try:
        snapshot = ai_advisor.build_snapshot(devices)
        # No plant / no trends in a bucket -> open the reservoir-health gate so qwen engages.
        # Every real gate (interlock, lockout, mL ceiling, freeze) and the human still apply.
        snapshot["res_health"] = {"state": "BUCKET", "dose_gate": "NORMAL",
                                  "ph_gate": "ALLOW", "co2_gate": "HOLD"}
        result = ai_advisor.ask_ai(snapshot)
        if result:
            ai_advisor.print_advice(result)
        return result
    except Exception as e:
        print(f"  [AI] oversight skipped ({e})")
        return None


# --------------------------------------------------------------------------- #
def main():
    email = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")
    if not email or not password:
        print("Set AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    if safety_state.is_dosing_disabled():
        _, reason = safety_state.dosing_disable_status()
        print(f"!! Dosing is FROZEN: {reason}")
        if ask("   Clear it to run the test? (y/N): ", "n").lower() == "y":
            os.environ.pop("DOSING_DISABLED", None)
            safety_state.clear_dosing_disable()
        else:
            print("   Leaving frozen -- doses would be blocked. Exiting.")
            sys.exit(0)

    gal = ai_advisor._reservoir_volume_gal()
    res_id = ask(f"Reservoir id/label [default {time.strftime('%Y%m%d')}]: ",
                 time.strftime("%Y%m%d"))
    nochem = ask("No-chemical FIRE test? pumps spin, nothing dosed, lines purged (y/N): ",
                 "n").lower() == "y"
    if nochem:
        dry, log_target, settle = False, DRYFIRE_LOG, NOCHEM_SETTLE_SEC
        print(f"  >> NO-CHEM mode: real pump fire, quarantined log ({DRYFIRE_LOG.name}), "
              f"{settle}s settle, K NOT updated. Expect every dose to read INEFFECTIVE.")
    else:
        dry = ask("Dry run -- compute doses but DON'T fire? (Y/n): ", "y").lower() != "n"
        log_target, settle = LOG_FILE, None
    target_ph = float(ask("Target pH [default 5.8]: ", "5.8"))
    target_tds = float(ask("Target TDS ppm [default 800]: ", "800"))

    mode_txt = ("NO-CHEM FIRE TEST" if nochem
                else "DRY RUN (no dosing)" if dry else "LIVE DOSING (supervised)")
    print(f"\n=== AI bucket test -- {DEVICE_NAME}  ({gal} gal, res {res_id}) ===")
    print(f"    targets: pH {target_ph}  TDS {target_tds} ppm   {mode_txt}")
    ineffective = {"nute": 0, "ph": 0}    # consecutive no-response doses per axis, this run

    cal = load_calibration()
    token = get_or_refresh_token(email, password, str(ENV))
    devices = [parse_device(r) for r in fetch_all_devices(token)]
    dev = next((d for d in devices if d["name"] == DEVICE_NAME), None)
    if not dev:
        print(f"Device '{DEVICE_NAME}' not found.")
        sys.exit(1)

    nute_ports = ai_advisor._nutrient_ports(DEVICE_NAME)
    up_port = ai_advisor._ph_up_port(DEVICE_NAME)
    down_port = ai_advisor._ph_down_port(DEVICE_NAME)
    kp, kp_n = k_nute_pair(cal, nute_ports)
    print(f"\nCalibration loaded: nutrient pair K={kp:.3f} ppm.gal/mL (n={kp_n}); "
          f"pH bins up={len(cal['ph']['up'])} down={len(cal['ph']['down'])}")
    if kp_n <= 1:
        print("  [!] thin nutrient data -- the 85% fast dose is a first estimate; "
              "watch the measured vs predicted delta and let K self-correct.")

    while True:
        cur = read_hydro(token, dev["dev_id"])
        print(f"\nNow: {fmt(cur)}")
        if not probe_sane(cur):
            if ask("  Probe not sane (not submerged?). Continue anyway? (y/N): ",
                   "n").lower() != "y":
                break

        ec = cur.get("ec_us")
        cap = BUCKET_MAX_DOSE_ML
        plan = None
        # Sequence: EC to target FIRST, then pH (the buffer is only valid at the final EC).
        # CALCULATE the dose, THEN decide done -- converged only when the calculated dose is
        # below the pump's minimum deliverable pulse (resolution), never a ppm deadband.
        nute = plan_nutrient(cur.get("tds_ppm"), target_tds, gal, kp)
        if nute and "err" in nute:
            print(f"  [nute] {nute['err']}")
            nute = None
        ec_done = False
        if nute:
            ml_each = nute["ml_each"]
            if ml_each > cap:
                print(f"  [!] calculated {ml_each:.1f} mL/pump > bucket cap {cap:.0f} -- clamping.")
                ml_each = cap
            speed = FAST_DOSE_SPEED if nute["mode"] == "fast" else CREEP_DOSE_SPEED
            chk = dosing.calculate_timed_dose(
                speed, ml_each, flow_ml_min=dosing._flow_ml_min(DEVICE_NAME, nute_ports[0]),
                ramp_rate=dosing._ramp_rate())
            if chk["deliverable"]:
                f1, f2 = nute_ratio(DEVICE_NAME)
                split = (f"  [V1/V2 {f1:.0%}/{f2:.0%}: p{nute_ports[0]} {2*ml_each*f1:.1f} / "
                         f"p{nute_ports[1]} {2*ml_each*f2:.1f} mL]" if abs(f1 - 0.5) > 1e-9 else "")
                print(f"  PLAN [{nute['mode']}] nutrients V1+V2 @ {ml_each:.2f} mL each @ spd {speed} "
                      f"(close {nute['want_delta']:.0f} of {nute['gap']:.0f} ppm gap){split}")
                plan = {"axis": "nute", "mode": nute["mode"]}
                ports, kind, label, target_ml = nute_ports, "pair", "nutrient V1+V2", ml_each
            else:
                ec_done = True
                print(f"  [EC] calc dose {ml_each:.2f} mL below min pulse -- within resolution of "
                      f"{target_tds:.0f} ppm. EC done.")
        else:
            ec_done = True
            if cur.get("tds_ppm") is not None and cur["tds_ppm"] > target_tds:
                print(f"  [nute] TDS {cur['tds_ppm']} ABOVE target {target_tds} -- cannot dose EC down.")

        # pH only once EC is at target (within resolution).
        if plan is None and ec_done:
            ph_plan = plan_ph(cur.get("ph"), target_ph - 0.15, target_ph + 0.15, ec, gal, cal)
            if ph_plan:
                d = ph_plan["direction"]
                port = up_port if d == "up" else down_port
                ml = min(ph_plan["ml"], cap)
                chk = dosing.calculate_timed_dose(
                    1, ml, flow_ml_min=dosing._flow_ml_min(DEVICE_NAME, port),
                    ramp_rate=dosing._ramp_rate())
                if chk["deliverable"]:
                    print(f"  PLAN pH {d.upper()} @ {ml:.2f} mL @ spd 1  (need {ph_plan['need']:+.2f} pH, "
                          f"fixed learning creep, EC={ec})")
                    plan = {"axis": "ph", "direction": d}
                    ports, speed, kind, label, target_ml = [port], 1, "single", f"PH {d.upper()}", ml
                else:
                    print(f"  [pH] creep dose {ml:.2f} mL below resolution -- pH done.")

        if plan is None:
            print("  -> EC and pH both at target (within resolution). Done.")
            break

        # qwen oversight (advisory) -- does its axis pick agree with the controller?
        ai_oversight(devices, target_ph, target_tds)
        print(f"       -> pump ~{chk['on_ms']/1000:.1f}s, ~{chk['estimated_actual_ml']} mL actual")

        if dry:
            print("  [DRY RUN] not firing. Re-reading without a dose to continue the walk-through.")
            if ask("  Continue dry run? (Y/n): ", "y").lower() == "n":
                break
            continue

        if ask(f"  FIRE {label} {target_ml:.2f} mL now? (y/N): ", "n").lower() != "y":
            print("  skipped.")
            if ask("  Quit? (y/N): ", "n").lower() == "y":
                break
            continue

        before = read_hydro(token, dev["dev_id"])
        if kind == "pair":
            f1, f2 = nute_ratio(DEVICE_NAME)
            total = 2 * target_ml
            dose_each = {ports[0]: round(total * f1, 3), ports[1]: round(total * f2, 3)}
            res = dosing.timed_dose_pair(token, dev, ports, speed, dose_each, solution=label)
        else:
            res = dosing.timed_dose(token, dev, ports[0], speed, target_ml, solution=label)
        if not res.get("ok"):
            print(f"  !! dose did not complete cleanly: {res.get('reason', res)}")
            if safety_state.is_dosing_disabled():
                print("  !! dosing FROZEN (stop unverified). Inspect before continuing.")
                break

        after = wait_for_stable(token, dev["dev_id"], settle)
        d_ph = round((after["ph"] or 0) - (before["ph"] or 0), 2)
        d_tds = round((after["tds_ppm"] or 0) - (before["tds_ppm"] or 0), 1)
        d_ec = round((after["ec_us"] or 0) - (before["ec_us"] or 0), 1)
        actual_ml = res.get("estimated_actual_ml", target_ml)

        # Effectiveness check -- the chemical analog of read-after-write. timed_dose proved
        # the PUMP moved + stopped; this proves the RESERVOIR responded. A purged line, empty
        # stock, or clog fires a clean pump with ~zero effect. Predicted = the K-model's
        # expectation for the mL ACTUALLY delivered (so the mL-ceiling clamp doesn't skew it).
        ec_b = before.get("ec_us") or ec
        if plan["axis"] == "nute":
            predicted = (kp * actual_ml / gal) if (kp and gal and actual_ml) else 0.0
            observed = d_tds
            ratio = observed / predicted if abs(predicted) > 1e-9 else 0.0
            effective = ratio >= INEFFECTIVE_RATIO
            print(f"\n  RESPONSE nute: predicted {predicted:+.2f}, observed {observed:+.2f}"
                  f"  ({ratio:.0%} of model)")
        else:
            # pH is a learning creep (no K prediction yet) -- just confirm the reservoir moved,
            # and record the observed buffer K for this bin.
            predicted = None
            observed = d_ph
            effective = abs(d_ph) >= PH_MIN_RESPONSE
            k_obs = (abs(d_ph) * ec_b * gal / actual_ml) if (ec_b and actual_ml) else 0.0
            print(f"\n  RESPONSE pH: observed {observed:+.2f} pH for {actual_ml:.2f} mL"
                  f"  (K_obs {k_obs:.0f} at EC {ec_b:.0f}, bin {ph_bin(before['ph'])})")
        if effective:
            ineffective[plan["axis"]] = 0
        else:
            ineffective[plan["axis"]] += 1
            print(f"  [!!! INEFFECTIVE DOSE !!!] reservoir barely moved -- likely dry line / "
                  f"no chemical / clog / pump fault. ({ineffective[plan['axis']]}/{INEFFECTIVE_HALT})")

        # Online calibration update -- ONLY on a genuine response, never in no-chem mode
        # (averaging a zero response would poison K).
        rec_k = {}
        if effective and not nochem and actual_ml:
            if kind == "pair":
                rec_k = {"k_nute_pair_obs": round(d_tds * gal / actual_ml, 4)}
            elif ec_b:
                rec_k = {"k_ph_obs": round(abs(d_ph) * ec_b * gal / actual_ml, 1),
                         "ph_bin": ph_bin(before["ph"])}

        log_result({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "harness": "ai",
            "res_id": res_id, "axis": kind, "solution": label, "nochem": nochem,
            "speed": speed, "target_ml": target_ml, "actual_ml": actual_ml,
            "reservoir_gal": str(gal), "target_ph": target_ph, "target_tds": target_tds,
            "mode": plan.get("mode", plan.get("direction")),
            "predicted": (round(predicted, 3) if predicted is not None else None), "effective": effective,
            "stop_verified": res.get("stop_verified"),
            "before": before, "after": after,
            "d_ph": d_ph, "d_ec_us": d_ec, "d_tds_ppm": d_tds, **rec_k,
        }, log_target)

        if not nochem and effective:
            cal = load_calibration()
            kp, kp_n = k_nute_pair(cal, nute_ports)
            print(f"  logged -> {log_target.name}   (nutrient K now {kp:.3f}, n={kp_n})")
        else:
            print(f"  logged -> {log_target.name}")

        if ineffective[plan["axis"]] >= INEFFECTIVE_HALT:
            print(f"  [HALT] {plan['axis']} axis: {INEFFECTIVE_HALT} ineffective doses in a row. "
                  "Stopping -- fix the line / chemical and re-run.")
            break

    print("\nDone. profiles/bucket_test_log.jsonl holds the run (raw + normalized K).")


if __name__ == "__main__":
    main()
