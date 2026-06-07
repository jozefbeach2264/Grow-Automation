#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supervised single-pump dose-response test (bucket / diluted-stock calibration).

Drives ONE doser at a time, lets the reservoir mix and stabilize, re-reads the HDS3
probe, and prints the measured pH/EC/TDS delta + the response per mL. You stay at the
keyboard: every dose is confirmed before it fires. This is the Layer 2 "supervised
diluted live test" -- not autonomous.

  baseline read -> pick port + mL -> confirm -> (optional prime) -> timed_dose ->
  wait for the probe to stabilize -> show delta + response/mL -> adjust -> repeat

Each result is appended to profiles/bucket_test_log.jsonl.

DO NOT run this while poller.py is running in LIVE mode -- the doser watchdog there
would fight a dose this script starts. Stop the poller first.

Run:  python3 bucket_dose_test.py
"""

import json
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

from ac_infinity_client import (
    get_or_refresh_token, fetch_all_devices, parse_device,
    set_port_speed, ramp_seconds,
)
import dosing
import safety_state
import runtime_state

DEVICE_NAME = os.getenv("BUCKET_TEST_DEVICE", "Hydroponics Control")
LOG_FILE = Path(__file__).parent / "profiles" / "bucket_test_log.jsonl"

PORTS = {
    1: ("Floraflex V1", "nute", 2),
    2: ("Floraflex V2", "nute", 2),
    3: ("PH UP",        "ph",   1),
    4: ("PH DOWN",      "ph",   1),
}

# Display cadence during the HARD settle wait (the wait length itself is the canonical
# doser settle in dosing.dose_settle_seconds(), default 5 min -- no early exit).
STABLE_POLL_SEC = 15


def read_hydro(token, dev_id):
    """Return current HDS3 readings for the device, or None if not found."""
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
    """HDS3 returns -327.68 when not submerged; 0/None when not reading."""
    if not r or r.get("ph") in (None, -327.68) or r.get("tds_ppm") in (None, -327.68):
        return False
    return True


def wait_for_stable(token, dev_id):
    """HARD settle: wait the full doser settle window (dosing.dose_settle_seconds, default
    5 min) before trusting the reading -- NO early exit. Chemistry, especially pH, keeps
    drifting well past the apparent quick-settle (a 15s 'settled' read drifted for 5 min in
    testing). Interim reads are shown only for visibility."""
    total = int(dosing.dose_settle_seconds())
    print(f"  HARD settle {total // 60}m{total % 60:02d}s (no early exit -- doser doses lag)...")
    elapsed = 0
    while elapsed < total:
        step = min(STABLE_POLL_SEC, total - elapsed)
        time.sleep(step); elapsed += step
        cur = read_hydro(token, dev_id)
        print(f"    t+{elapsed:>4}s  {fmt(cur)}")
    return read_hydro(token, dev_id)


def log_result(rec):
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  [WARN] could not write log: {e}")


def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else default


def prime_line(token, dev, port, label):
    sec = ask(f"  Prime {label} line -- run seconds to fill tubing (blank = skip): ")
    if not sec:
        return
    try:
        secs = float(sec)
    except ValueError:
        print("  bad number -- skipping prime")
        return
    spd = 3
    print(f"  Priming p{port} at speed {spd} for {secs}s ...")
    try:
        set_port_speed(token, dev["dev_id"], port, spd, dev["type"])
        time.sleep(secs)
    finally:
        ok = dosing._force_stop(token, dev, port)
    print(f"  Prime done (stop {'verified' if ok else 'NOT verified'}). "
          "Run again if solution hasn't reached the outlet yet.")


def main():
    email = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")
    if not email or not password:
        print("Set AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    if safety_state.is_dosing_disabled():
        disabled, reason = safety_state.dosing_disable_status()
        print(f"!! Dosing is FROZEN: {reason}")
        if ask("   Clear it to run the test? (y/N): ", "n").lower() == "y":
            os.environ.pop("DOSING_DISABLED", None)
            safety_state.clear_dosing_disable()
        else:
            print("   Leaving frozen -- doses would be blocked. Exiting.")
            sys.exit(0)

    token = get_or_refresh_token(email, password, str(ENV))
    dev = next((parse_device(r) for r in fetch_all_devices(token)
                if parse_device(r)["name"] == DEVICE_NAME), None)
    if not dev:
        print(f"Device '{DEVICE_NAME}' not found.")
        sys.exit(1)

    vol = os.getenv("RESERVOIR_VOLUME_GAL", "?")
    print(f"\n=== Bucket dose test -- {DEVICE_NAME}  (RESERVOIR_VOLUME_GAL={vol}) ===")
    base = read_hydro(token, dev["dev_id"])
    print(f"Baseline: {fmt(base)}")
    if not probe_sane(base):
        print("!! Probe not reading sanely (not submerged / still acclimating). "
              "Let it acclimate before trusting deltas.")
        if ask("   Continue anyway? (y/N): ", "n").lower() != "y":
            sys.exit(0)

    while True:
        print("\nPorts: " + "  ".join(
            f"{p}={lbl}(x{dosing.strength_factor(DEVICE_NAME, p)})"
            for p, (lbl, _, _) in PORTS.items()))
        sel = ask("Port to dose (1-4, or q to quit): ")
        if not sel or sel.lower() == "q":
            break
        try:
            port = int(sel)
            label, kind, def_spd = PORTS[port]
        except (ValueError, KeyError):
            print("  pick 1-4")
            continue

        def_ml = (dosing.dose_ml("PH_MICRODOSE_ML") if kind == "ph"
                  else dosing.dose_ml("NUTE_MICRODOSE_ML_EACH"))
        ml = ask(f"  {label}: target mL [default {def_ml}]: ", str(def_ml))
        spd = ask(f"  speed [default {def_spd}]: ", str(def_spd))
        try:
            ml = float(ml); spd = int(spd)
        except ValueError:
            print("  bad number")
            continue

        plan = dosing.calculate_timed_dose(spd, ml,
                                           flow_ml_min=dosing._flow_ml_min(DEVICE_NAME, port),
                                           ramp_rate=dosing._ramp_rate())
        if not plan["deliverable"]:
            print(f"  {plan['reason']}")
            continue
        sf = dosing.strength_factor(DEVICE_NAME, port)
        print(f"  -> pump on ~{plan['on_ms']/1000:.1f}s, ~{plan['estimated_actual_ml']} mL "
              f"actual = {round(plan['estimated_actual_ml']*sf, 3)} mL full-strength-eq")

        prime_line(token, dev, port, label)

        if ask(f"  FIRE {label} now? (y/N): ", "n").lower() != "y":
            print("  skipped.")
            continue

        before = read_hydro(token, dev["dev_id"])
        print(f"  before: {fmt(before)}")
        res = dosing.timed_dose(token, dev, port, spd, ml, solution=label)
        if not res.get("ok"):
            print(f"  !! dose did not complete cleanly: {res.get('reason', res)}")
            if safety_state.is_dosing_disabled():
                print("  !! dosing is now FROZEN (stop unverified). Inspect before continuing.")
                break

        after = wait_for_stable(token, dev["dev_id"])

        d_ph = round((after["ph"] or 0) - (before["ph"] or 0), 2)
        d_ec = round((after["ec_us"] or 0) - (before["ec_us"] or 0), 1)
        d_tds = round((after["tds_ppm"] or 0) - (before["tds_ppm"] or 0), 1)
        actual_ml = res.get("estimated_actual_ml", plan["estimated_actual_ml"])
        fse = res.get("full_strength_equivalent_ml", round(actual_ml * sf, 3))
        print(f"\n  RESPONSE  {label}:  dpH {d_ph:+}   dEC {d_ec:+} uS/cm   dTDS {d_tds:+} ppm")
        if actual_ml:
            print(f"            per mL actual: dpH {d_ph/actual_ml:+.3f}  dTDS {d_tds/actual_ml:+.2f}")
        if fse:
            print(f"            per mL full-strength-eq: dTDS {d_tds/fse:+.2f}")

        log_result({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": port, "solution": label,
            "speed": spd, "target_ml": ml, "actual_ml": actual_ml,
            "strength_factor": sf, "full_strength_equiv_ml": fse,
            "reservoir_gal": vol, "stop_verified": res.get("stop_verified"),
            "before": before, "after": after,
            "d_ph": d_ph, "d_ec_us": d_ec, "d_tds_ppm": d_tds,
        })
        print(f"  logged -> {LOG_FILE.name}")

    print("\nDone. Review profiles/bucket_test_log.jsonl for the dose-response record.")


if __name__ == "__main__":
    main()
