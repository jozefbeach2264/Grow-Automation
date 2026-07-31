#!/usr/bin/env python3
"""
Self-tests for the reservoir-burst shutdown -- the documented HIGHEST-priority safety
path, previously with ZERO direct coverage (regression #13). No hardware: AC Infinity
writes are monkeypatched and state files redirected. Covers compute_res_burst's action
construction (stop every doser/pH port + close CO2; NEVER enumerate lights/vent) and
poller.enforce_res_burst's actuation + persistent dosing freeze. Run: python3 res_burst_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="rb_test_"))
import runtime_state
runtime_state._STATE_FILE = _TMP / ".runtime_state.json"
runtime_state._EVENT_LOG = _TMP / "events.jsonl"
import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

os.environ["RES_BURST_ENABLED"] = "true"
os.environ["DOSER_PORTS_HYDROPONICS_CONTROL"] = "1,2,3,4"
os.environ["PH_PORTS_HYDROPONICS_CONTROL"] = "3,4"
os.environ["CO2_VALVE"] = "Auxiliary Outputs:2"
os.environ.pop("DOSING_DISABLED", None)
os.environ["VERIFY_WRITES"] = "false"          # poller binds this at import

import ai_advisor
import poller

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
    safety_state.clear_dosing_disable()
    for f in (runtime_state._STATE_FILE, runtime_state._EVENT_LOG, safety_state._STATE_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def snapshot_with_leak(confirmed=True):
    return {
        "leak": {"confirmed": confirmed, "raw": 1 if confirmed else 0,
                 "streak": 2 if confirmed else 0},
        "devices": [
            {"name": "Hydroponics Control", "dev_id": "d-hydro", "type": 20,
             "ports": [{"port": p} for p in (1, 2, 3, 4)]},
            {"name": "Auxiliary Outputs", "dev_id": "d-aux", "type": 21,
             "ports": [{"port": 1}, {"port": 2}]},     # port 2 = CO2 valve
            {"name": "4 x 4", "dev_id": "d-tent", "type": 20,
             "ports": [{"port": 1}, {"port": 2}]},      # light, exhaust -- must NOT be touched
        ],
    }


# =========================================================================== #
print("\n== compute_res_burst: action-list construction ==")
reset()
rb = ai_advisor.compute_res_burst(snapshot_with_leak())
acts = rb["actions"]
doser_stops = [a for a in acts if a["action"] == "set_speed" and a["value"] == 0
               and a["device"] == "Hydroponics Control"]
co2_close = [a for a in acts if a["action"] == "set_outlet" and a["value"] is False
             and a["device"] == "Auxiliary Outputs" and a["port"] == 2]
touched = {a["device"] for a in acts}
check("active on a confirmed leak", rb["active"] is True)
check("stops ALL 4 doser/pH ports (incl pH 3 & 4)",
      sorted(a["port"] for a in doser_stops) == [1, 2, 3, 4])
check("closes the CO2 valve (set_outlet False)", len(co2_close) == 1)
check("NEVER enumerates lights/exhaust (4 x 4 untouched)", "4 x 4" not in touched)
check("non-valve Aux port 1 not touched",
      all(not (a["device"] == "Auxiliary Outputs" and a["port"] == 1) for a in acts))


print("\n== compute_res_burst: inert gating ==")
reset()
check("unconfirmed leak -> None",
      ai_advisor.compute_res_burst(snapshot_with_leak(confirmed=False)) is None)
os.environ["RES_BURST_ENABLED"] = "false"
check("RES_BURST_ENABLED=false -> None", ai_advisor.compute_res_burst(snapshot_with_leak()) is None)
os.environ["RES_BURST_ENABLED"] = "true"


print("\n== enforce_res_burst: actuation + persistent freeze ==")
reset()
_writes = []
poller.set_port_speed = lambda token, dev_id, port, speed, dev_type: _writes.append(("speed", dev_id, port, speed))
poller.set_outlet = lambda token, dev_id, port, val, dev_type: _writes.append(("outlet", dev_id, port, val))
poller.VERIFY_WRITES = False
snap = snapshot_with_leak()
snap["res_burst"] = ai_advisor.compute_res_burst(snap)
fired = poller.enforce_res_burst(snap, snap["devices"], "TOKEN")
check("fired all 5 actions (4 doser stops + CO2 close)", len(fired) == 5)
check("4 doser stops issued to the reservoir device",
      len([w for w in _writes if w[0] == "speed" and w[1] == "d-hydro" and w[3] == 0]) == 4)
check("CO2 valve closed", ("outlet", "d-aux", 2, False) in _writes)
check("NO write to the tent device (lights/exhaust never cut)", all(w[1] != "d-tent" for w in _writes))
check("res burst persists the dosing freeze", safety_state.is_dosing_disabled() is True)


print("\n== enforce_res_burst: freeze holds even when a stop re-verify fails ==")
reset()
_writes.clear()
poller.VERIFY_WRITES = True
poller._verified_doser_stop = lambda token, dev, port, tag: False   # pretend re-verify fails
snap = snapshot_with_leak()
snap["res_burst"] = ai_advisor.compute_res_burst(snap)
poller.enforce_res_burst(snap, snap["devices"], "TOKEN")
check("freeze stays in force even if a stop won't re-verify", safety_state.is_dosing_disabled() is True)


print("\n== enforce_res_burst: no-op when no burst is active ==")
reset()
fired = poller.enforce_res_burst({"res_burst": None}, [], "TOKEN")
check("no burst -> nothing fired", fired == [])
check("no burst -> no freeze", safety_state.is_dosing_disabled() is False)


print("\n== evac pump: IMMEDIATE on the first wet read ==")
# Operator requirement 2026-07-31: "if there is detection on the floor sensor i want
# IMMEDIATE evac". The RES_BURST_DEBOUNCE exists to stop a noisy read tripping the
# persistent dosing FREEZE; it must not also delay getting water off the floor.
os.environ["EVAC_PUMP"] = "Auxiliary Outputs:3"
os.environ.pop("EVAC_IMMEDIATE", None)


def evac_snap(streak, confirmed, powered=False):
    return {
        "leak": {"raw": 1 if streak else 0, "wet": streak > 0,
                 "confirmed": confirmed, "streak": streak},
        "devices": [{"name": "Auxiliary Outputs",
                     "ports": [{"port": 3, "powered": powered}]}],
    }


ev = ai_advisor.compute_evac_pump(evac_snap(1, False))
check("ONE wet read turns the evac pump ON (no debounce wait)",
      ev is not None and ev["value"] is True)
check("evac targets the configured outlet",
      ev["device"] == "Auxiliary Outputs" and ev["port"] == 3)
check("reason records it fired immediately", "immediate" in ev["reason"])

ev = ai_advisor.compute_evac_pump(evac_snap(1, False, powered=True))
check("no redundant write when the pump is already running", ev is None)

ev = ai_advisor.compute_evac_pump(evac_snap(0, False, powered=True))
check("first DRY read turns it OFF (never runs dry)",
      ev is not None and ev["value"] is False)

os.environ["EVAC_IMMEDIATE"] = "false"
ev = ai_advisor.compute_evac_pump(evac_snap(1, False))
check("EVAC_IMMEDIATE=false restores the debounced behavior", ev is None)
ev = ai_advisor.compute_evac_pump(evac_snap(2, True))
check("debounced mode still fires once confirmed",
      ev is not None and ev["value"] is True)
os.environ.pop("EVAC_IMMEDIATE", None)

ev = ai_advisor.compute_evac_pump({"leak": {"raw": None}, "devices": []})
check("no leak reading -> never commands the pump blind", ev is None)

os.environ.pop("EVAC_PUMP", None)
ev = ai_advisor.compute_evac_pump(evac_snap(1, False))
check("unconfigured evac pump stays inert", ev is None)


print("\n== enforce_evac_pump: the write is read back, not assumed ==")
os.environ["EVAC_PUMP"] = "Auxiliary Outputs:3"
_outlet_writes = []
_verify_ok = True


def fake_set_outlet(token, dev_id, port, powered, dev_type):
    _outlet_writes.append((port, powered))


def fake_verify(token, dev_id, port, expected, timeout_sec=15.0, **kw):
    return {"ok": _verify_ok, "observed": {"powered": not expected["powered"]},
            "elapsed_sec": 1, "attempts": 1, "reason": ""}


poller.set_outlet = fake_set_outlet
poller.verify_port_state = fake_verify
poller.VERIFY_WRITES = True
EVDEV = {"name": "Auxiliary Outputs", "dev_id": "d-aux", "type": 21}
EV_ON = {"device": "Auxiliary Outputs", "port": 3, "action": "set_outlet",
         "value": True, "reason": "leak WET (read 1, immediate) -- evac pump ON"}

reset(); _outlet_writes.clear(); _verify_ok = True
sent = poller.enforce_evac_pump(EV_ON, EVDEV, "TOKEN")
check("evac ON is written and verified", sent is True and _outlet_writes == [(3, True)])
active, _, _ = runtime_state.high_alert_status()
check("a verified evac raises no alarm", active is False)

reset(); _outlet_writes.clear(); _verify_ok = False
sent = poller.enforce_evac_pump(EV_ON, EVDEV, "TOKEN")
check("an unconfirmed evac re-issues the write", len(_outlet_writes) == 2)
active, _, reason = runtime_state.high_alert_status()
check("an unconfirmed evac opens high-alert", active is True)
check("high-alert names the evac pump", "evac" in (reason or ""))
check("but does NOT freeze dosing (evac is not a doser)",
      safety_state.is_dosing_disabled() is False)

reset(); _outlet_writes.clear()


def raising_set_outlet(token, dev_id, port, powered, dev_type):
    raise RuntimeError("simulated cloud outage")


poller.set_outlet = raising_set_outlet
sent = poller.enforce_evac_pump(EV_ON, EVDEV, "TOKEN")
check("a failed evac write reports not-sent", sent is False)
active, _, _ = runtime_state.high_alert_status()
check("a failed evac write opens high-alert", active is True)
poller.set_outlet = fake_set_outlet
os.environ.pop("EVAC_PUMP", None)
reset()


print("\n== clamp_safety_sleep: the leak debounce bounds the poll cadence ==")
# 2026-07-31 review P1-6: the sleep is chosen by AI/idle logic that knows nothing
# about the leak debounce. At POLL_INTERVAL_STABLE=900 with RES_BURST_DEBOUNCE=2 that
# is ~15 min to CONFIRM a leak on top of up to ~15 min to first see it, and the
# AI-failure backoff can reach 1800s.
_armed = os.environ.get("RES_BURST_ENABLED")
os.environ.pop("EVAC_PUMP", None)
os.environ.pop("LEAK_CONFIRM_POLL_SEC", None)
os.environ.pop("SAFETY_POLL_MAX_SEC", None)


def leak_snap(streak, confirmed):
    return {"leak": {"raw": 1 if streak else 0, "wet": streak > 0,
                     "confirmed": confirmed, "streak": streak}}


os.environ["RES_BURST_ENABLED"] = "false"          # nothing armed -> ceiling off
s, note = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("dry + unarmed leaves the idle cadence alone", s == 900 and note is None)

s, note = poller.clamp_safety_sleep(900, leak_snap(1, False))
check("ONE wet read collapses a 900s sleep to the confirm cadence", s == 30)
check("the clamp explains itself", note is not None and "unconfirmed" in note)

s, _ = poller.clamp_safety_sleep(1800, leak_snap(1, False))
check("the 1800s AI-failure backoff is bounded too", s == 30)

s, note = poller.clamp_safety_sleep(900, leak_snap(2, True))
check("a CONFIRMED leak does not clamp (res-burst already fired)", s == 900)

s, _ = poller.clamp_safety_sleep(10, leak_snap(1, False))
check("never LENGTHENS an already-fast sleep", s == 10)

os.environ["RES_BURST_ENABLED"] = "true"           # armed -> detection ceiling on
s, note = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("armed responder caps the idle sleep at the safety ceiling", s == 300)
check("ceiling explains itself", note is not None and "armed" in note)

os.environ["RES_BURST_ENABLED"] = "false"
os.environ["EVAC_PUMP"] = "Auxiliary Outputs:3"
s, _ = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("a configured evac pump arms a ceiling on its own -- the TIGHT one, "
      "since immediate evac makes poll interval = response time", s == 30)
os.environ.pop("EVAC_PUMP", None)

os.environ["RES_BURST_ENABLED"] = "true"
os.environ["SAFETY_POLL_MAX_SEC"] = "120"
os.environ["LEAK_CONFIRM_POLL_SEC"] = "10"
s, _ = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("SAFETY_POLL_MAX_SEC is honored", s == 120)
s, _ = poller.clamp_safety_sleep(900, leak_snap(1, False))
check("an unconfirmed streak beats the ceiling (tighter wins)", s == 10)
os.environ.pop("SAFETY_POLL_MAX_SEC", None)
os.environ.pop("LEAK_CONFIRM_POLL_SEC", None)

s, note = poller.clamp_safety_sleep(900, {})
check("a snapshot with no leak block is handled", s == 300 and "armed" in (note or ""))

# An armed evac pump is the tightest bound -- it fires on the first wet read, so the
# poll interval IS the response time.
os.environ["EVAC_PUMP"] = "Auxiliary Outputs:3"
s, note = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("an armed evac pump caps the poll at 30s, not 300s", s == 30)
check("evac ceiling explains itself", "evac pump armed" in (note or ""))
os.environ["EVAC_POLL_MAX_SEC"] = "15"
s, _ = poller.clamp_safety_sleep(900, leak_snap(0, False))
check("EVAC_POLL_MAX_SEC is honored", s == 15)
os.environ.pop("EVAC_POLL_MAX_SEC", None)
os.environ.pop("EVAC_PUMP", None)
if _armed is not None:
    os.environ["RES_BURST_ENABLED"] = _armed


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
