#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Supervised CO2 shot calibration -- learn the valve-open time for a fixed-ppm "shot".

The CO2 analog of the nutrient-K feedforward. A solenoid at constant tank pressure flows
~constant, so co2_ppm rise is ~linear in valve-open time. So: characterize the rate
(ppm per second of open valve) from a known seed pulse, then size every later pulse to the
target shot (`CO2_SHOT_PPM`, default 100 ppm) and refine the rate each time -- until a shot
reliably lands within tolerance. The measured rise is the NET rise after the equalize window
(after exhaust/uptake), which is the usable shot size. The equalize MUST cover the tent's
mixing/sensor lag (measured ~4 min on this rig -- a short window reads mid-climb and overshoots
badly: a fill targeting 1500 peaked at 1835), hence the 5-min default. Fire ONE shot at a time
and let it fully settle before the next.

Airflow: on start the test turns the oscillating fans ON (mixing, so the sensor reads the
true tent average) and makes sure the EXHAUST is OFF (so CO2 isn't vented during the shots
or the decay watch). It does NOT restore them on exit -- airflow is left as-is so the
natural decay can be watched / captured in a CSV export.

Each pulse: read CO2 -> open the valve for the computed seconds -> FORCE closed + verify
(retry once) -> equalize -> read CO2 -> log to profiles/co2_pulse_log.jsonl.

SAFETY -- a stuck-open CO2 valve is an asphyxiation hazard:
  - every pulse closes the valve in a `finally`, retries once, and VERIFIES it shut;
  - aborts if co2 is already at/over `CO2_EMERGENCY_PPM`;
  - halts the run if a close can't be confirmed.

DO NOT run while poller.py is live -- the schedule CO2 modulator there would fight it.

Run:  python3 co2_pulse_test.py
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

from ac_infinity_client import (get_or_refresh_token, fetch_all_devices, parse_device,
                                set_outlet, set_port_speed, verify_port_state)

LOG_FILE = Path(__file__).parent / "profiles" / "co2_pulse_log.jsonl"
SHOT_PPM = float(os.getenv("CO2_SHOT_PPM", "100"))            # target ppm per calibrated shot
SHOT_TOL = float(os.getenv("CO2_SHOT_TOL", "25"))            # +/- ppm to call a shot calibrated
CHAR_PULSE_SEC = float(os.getenv("CO2_CHAR_PULSE_SEC", "3"))  # seed pulse when no rate learned yet
MIN_PULSE_SEC = float(os.getenv("CO2_MIN_PULSE_SEC", "1"))
MAX_PULSE_SEC = float(os.getenv("CO2_MAX_PULSE_SEC", "90"))   # hard ceiling on a single open (allows the 85% fast pull at a slow flow)
EQUALIZE_SEC = float(os.getenv("CO2_EQUALIZE_SEC", "300"))   # mix/settle before reading -- must cover the ~4 min mixing/sensor lag
EMERGENCY_PPM = float(os.getenv("CO2_EMERGENCY_PPM", "3000") or 3000)
MIN_EFFECTIVE_PPM = 5.0                                       # rise below this = ineffective pulse
OSC_FAN_SPEED = int(float(os.getenv("OSC_FAN_SPEED", "10")))  # mixing speed for the test


def co2_outlet():
    raw = os.getenv("CO2_VALVE", "").strip()
    if not raw or ":" not in raw:
        return None
    dev, _, port = raw.rpartition(":")
    try:
        return dev.strip(), int(port)
    except ValueError:
        return None


def _parse_roles(env_key):
    """Parse ROLE_* = '<device>:<port>,<device>:<port>' into [(device, port), ...]."""
    out = []
    for chunk in os.getenv(env_key, "").split(","):
        chunk = chunk.strip()
        if ":" in chunk:
            d, _, p = chunk.rpartition(":")
            try:
                out.append((d.strip(), int(p)))
            except ValueError:
                pass
    return out


def setup_airflow(token):
    """Osc fans ON (mixing), exhaust OFF (no venting). Returns prior speeds for reference;
    does NOT auto-restore -- airflow is left running for the decay watch."""
    devs = {d["name"]: d for d in (parse_device(r) for r in fetch_all_devices(token))}

    def cur_speed(d, port):
        p = next((x for x in d.get("ports", []) if x.get("port") == port), None)
        return p.get("speed_actual") if p else None

    prior = []
    print("  [air] osc fans -> ON, exhaust -> OFF (so CO2 mixes but isn't vented)")
    for devname, port in _parse_roles("ROLE_EXHAUST"):
        d = devs.get(devname)
        if not d:
            continue
        was = cur_speed(d, port)
        prior.append(("exhaust", devname, port, was))
        if was:
            set_port_speed(token, d["dev_id"], port, 0, d["type"])
        print(f"     exhaust {devname}:{port}  was {was} -> 0")
    for devname, port in _parse_roles("ROLE_OSC_FANS"):
        d = devs.get(devname)
        if not d:
            continue
        was = cur_speed(d, port)
        prior.append(("fan", devname, port, was))
        set_port_speed(token, d["dev_id"], port, OSC_FAN_SPEED, d["type"])
        print(f"     osc fan {devname}:{port}  was {was} -> {OSC_FAN_SPEED}")
    return prior


def read_co2(token, dev_name):
    for raw in fetch_all_devices(token):
        d = parse_device(raw)
        if d["name"] == dev_name:
            return d.get("co2_ppm"), d
    return None, None


def load_rate():
    """Running-mean ppm/s of valve-open from prior EFFECTIVE pulses (positive rise only)."""
    if not LOG_FILE.exists():
        return None, 0
    total, n = 0.0, 0
    for line in LOG_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        rate, d = r.get("ppm_per_sec"), r.get("d_co2_ppm")
        if rate is not None and d is not None and d >= MIN_EFFECTIVE_PPM:
            total += rate
            n += 1
    return (total / n, n) if n else (None, 0)


def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else default


def pulse_valve(token, dev, port, secs):
    """Open the valve for `secs`, then ALWAYS close + verify (retry once). Returns True only
    when the valve is confirmed shut -- the caller halts on False."""
    dev_id, dev_type = dev["dev_id"], dev["type"]
    try:
        set_outlet(token, dev_id, port, True, dev_type)
        print(f"  [CO2] valve OPEN (port {port}) for {secs:.1f}s ...")
        time.sleep(secs)
    finally:
        ok = False
        for attempt in (1, 2):
            try:
                set_outlet(token, dev_id, port, False, dev_type)
            except Exception as e:
                print(f"  [CO2] close command failed (try {attempt}): {e}")
            v = verify_port_state(token, dev_id, port, {"powered": False})
            ok = bool(v.get("ok"))
            print(f"  [CO2] valve CLOSE try {attempt}: verified={ok} ({v.get('elapsed_sec')}s)")
            if ok:
                break
        if not ok:
            print("  !!! VALVE NOT CONFIRMED CLOSED -- physically check / unplug it now (CO2 hazard).")
    return ok


def log_result(rec):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    email = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")
    if not email or not password:
        print("Set AC_INFINITY_EMAIL / AC_INFINITY_PASSWORD in .env")
        sys.exit(1)

    valve = co2_outlet()
    if not valve:
        print("CO2_VALVE not configured in .env as <device>:<port>")
        sys.exit(1)
    dev_name, port = valve

    token = get_or_refresh_token(email, password, str(ENV))
    prior_air = setup_airflow(token)
    co2, dev = read_co2(token, dev_name)
    if dev is None:
        print(f"Device '{dev_name}' not found.")
        sys.exit(1)
    outlet = next((p for p in dev.get("ports", []) if p.get("port") == port), None)

    rate, n = load_rate()
    print(f"\n=== CO2 shot calibration -- aim {SHOT_PPM:.0f} ppm/shot (+/-{SHOT_TOL:.0f}) ===")
    print(f"    valve {dev_name} port {port} ('{outlet.get('name') if outlet else '?'}'); "
          f"equalize {EQUALIZE_SEC:.0f}s; emergency {EMERGENCY_PPM:.0f} ppm")
    print(f"    learned rate: {f'{rate:.1f} ppm/s open (n={n})' if rate else 'NONE yet -- first pulse characterizes it'}")
    print(f"    CO2 now: {co2} ppm")
    test_id = ask(f"Test id [default {time.strftime('%Y%m%d')}]: ", time.strftime("%Y%m%d"))
    tgt = ask("Fill to CO2 target ppm (blank = single supervised shots): ", None)
    target = float(tgt) if tgt else None

    while True:
        co2, dev = read_co2(token, dev_name)
        print(f"\nNow: CO2 {co2} ppm")
        if co2 is None:
            if ask("  No CO2 reading. Continue anyway? (y/N): ", "n").lower() != "y":
                break
        elif co2 >= EMERGENCY_PPM:
            print(f"  !! CO2 {co2} >= emergency {EMERGENCY_PPM:.0f} -- NOT pulsing. Vent first.")
            break

        if target is not None and co2 is not None:
            gap = target - co2
            if gap <= MIN_EFFECTIVE_PPM:
                print(f"  -> within {MIN_EFFECTIVE_PPM:.0f} ppm of target {target:.0f}. Done.")
                break
            far = gap > 0.15 * target
            aim = (0.85 * gap) if far else (0.9 * gap)   # 85% fast, then creep the last 15% -- like the res
            mode = "fast" if far else "creep"
        else:
            gap, aim, mode = None, SHOT_PPM, "shot"

        if rate and rate > 0:
            want = aim / rate
            pulse_sec = min(max(want, MIN_PULSE_SEC), MAX_PULSE_SEC)
            clamped = abs(want - pulse_sec) > 1e-6
            tail = f"  toward {target:.0f} (gap {gap:.0f})" if gap is not None else ""
            print(f"  [{mode}] aim {aim:.0f} ppm = {pulse_sec:.1f}s open "
                  f"(rate {rate:.1f} ppm/s, n={n}){' [CLAMPED]' if clamped else ''}{tail}")
        else:
            pulse_sec, mode, aim = CHAR_PULSE_SEC, "characterize", SHOT_PPM
            print(f"  characterization pulse {pulse_sec:.0f}s (no rate yet)")

        if ask(f"  PULSE valve {pulse_sec:.1f}s now? (y/N): ", "n").lower() != "y":
            if ask("  Quit? (y/N): ", "n").lower() == "y":
                break
            continue

        before = co2
        closed_ok = pulse_valve(token, dev, port, pulse_sec)
        print(f"  equalizing {EQUALIZE_SEC:.0f}s ...")
        time.sleep(EQUALIZE_SEC)
        after, _ = read_co2(token, dev_name)

        d_co2 = (round(after - before, 1)
                 if (after is not None and before is not None) else None)
        obs_rate = (round(d_co2 / pulse_sec, 2)
                    if (d_co2 is not None and d_co2 >= MIN_EFFECTIVE_PPM and pulse_sec) else None)
        log_result({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "test_id": test_id, "mode": mode,
            "valve": f"{dev_name}:{port}", "aim_ppm": aim, "pulse_sec": round(pulse_sec, 2),
            "equalize_sec": EQUALIZE_SEC, "co2_before": before, "co2_after": after,
            "d_co2_ppm": d_co2, "ppm_per_sec": obs_rate, "valve_closed_verified": closed_ok,
        })

        if d_co2 is None:
            print("  RESPONSE: no reading.")
        elif d_co2 < MIN_EFFECTIVE_PPM:
            print(f"  RESPONSE: {before} -> {after} ppm (d {d_co2:+}) -- INEFFECTIVE "
                  "(empty tank / valve didn't open / venting it). Not updating rate.")
        else:
            rate, n = load_rate()      # refine from the log (this pulse included)
            if mode != "characterize":
                off = d_co2 - aim
                tag = "ON TARGET" if abs(off) <= SHOT_TOL else f"off {off:+.0f}"
                print(f"  RESPONSE: {before} -> {after} ppm  (d {d_co2:+}, ~{obs_rate} ppm/s) -- "
                      f"aimed {aim:.0f} -> {tag}.  Rate now {rate:.1f} ppm/s (n={n}); "
                      f"{SHOT_PPM:.0f}-ppm shot ~= {SHOT_PPM/rate:.1f}s")
            else:
                print(f"  RESPONSE: {before} -> {after} ppm  (d {d_co2:+} over {pulse_sec:.0f}s) -- "
                      f"rate {obs_rate} ppm/s.  A {SHOT_PPM:.0f}-ppm shot ~= {SHOT_PPM/rate:.1f}s open.")

        if not closed_ok:
            print("  HALT: valve close could not be verified -- stopping for safety.")
            break

    print("\nDone. profiles/co2_pulse_log.jsonl holds the run (rate self-updates).")
    print("Airflow LEFT running for the decay watch -- fans ON, exhaust OFF. Prior speeds:")
    for role, devname, p, was in prior_air:
        print(f"  {role} {devname}:{p} = {was}")
    print("Restore them (or restart poller.py) when you're done watching the decay.")


if __name__ == "__main__":
    main()
