#!/usr/bin/env python3
"""
Self-tests for the watchdog / crash-recovery layer.

Covers runtime_state (heartbeat, restart diagnosis, active-dose record + window,
interrupted-dose estimate, high-alert window) and poller.doser_watchdog (orphan
pump detection + LIVE actuation + dosing freeze + high-alert). No hardware: the
AC Infinity writes/readbacks are monkeypatched, state files are redirected to a
temp dir, and the SIM token is used. Run: python3 watchdog_test.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# --- Redirect every persistent state file to a temp dir BEFORE the modules that
#     bind those paths get imported, so a test run never touches real grow state. ---
_TMP = Path(tempfile.mkdtemp(prefix="wd_test_"))

import runtime_state
runtime_state._STATE_FILE = _TMP / ".runtime_state.json"
runtime_state._EVENT_LOG = _TMP / "events.jsonl"

import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

# Env must be set before importing poller (it binds ADVISORY_MODE etc. at import).
# load_dotenv() does not override already-set vars, so these win.
os.environ["ADVISORY_MODE"] = "false"
os.environ["DOSER_WATCHDOG_ENABLED"] = "true"
os.environ["HEARTBEAT_ENABLED"] = "true"
os.environ["VERIFY_WRITES"] = "true"
os.environ["DOSER_PORTS_RDWC_CONTROL"] = "1,2,3,4"
os.environ["PH_PORTS_RDWC_CONTROL"] = "3,4"
os.environ["HIGH_ALERT_DURATION_MINUTES"] = "30"
os.environ["DOSER_WATCHDOG_DEBOUNCE"] = "1"   # default for existing tests: freeze on first stopped orphan

import poller
import ac_infinity_client

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


def reset_state():
    for f in (runtime_state._STATE_FILE, runtime_state._EVENT_LOG,
              safety_state._STATE_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


# --- mock hardware ---------------------------------------------------------- #
_writes = []


def fake_set_port_speed(token, dev_id, port, speed, dev_type):
    _writes.append((dev_id, port, speed))


def _make_verify(ok=True):
    def fake_verify(token, dev_id, port, expected, timeout_sec=0):
        obs = {"speed_actual": 0 if ok else 5}
        return {"ok": ok, "reason": "" if ok else "still_running",
                "observed": obs, "elapsed_sec": 1, "attempts": 1}
    return fake_verify


poller.set_port_speed = fake_set_port_speed
# Stops route through ac_infinity_client.stop_and_verify -> patch the primitives there.
ac_infinity_client.set_port_speed = fake_set_port_speed


def dev_with_doser(speed_port1=0, speed_port3=0):
    return {
        "name": "RDWC Control", "dev_id": "d-rdwc", "type": 20,
        "ports": [
            {"port": 1, "speed_actual": speed_port1, "is_outlet": False},
            {"port": 2, "speed_actual": 0, "is_outlet": False},
            {"port": 3, "speed_actual": speed_port3, "is_outlet": False},
            {"port": 4, "speed_actual": 0, "is_outlet": False},
        ],
    }


# =========================================================================== #
print("\n== runtime_state: heartbeat + restart diagnosis ==")
reset_state()

d = runtime_state.diagnose_restart()
check("fresh start detected when no heartbeat", d["fresh"] is True)

runtime_state.write_heartbeat("polling_devices", poll_ok=True, api_ok=True)
hb = runtime_state.last_heartbeat()
check("heartbeat persists phase", hb["phase"] == "polling_devices")
check("heartbeat carries pid + boot_id", hb["pid"] == os.getpid() and "boot_id" in hb)

runtime_state.write_heartbeat("sleeping")  # poll_ok=None must preserve prior True
hb = runtime_state.last_heartbeat()
check("None *_ok preserves previous value", hb["last_poll_ok"] is True)

d = runtime_state.diagnose_restart()
check("unclean restart flagged (phase != shutdown)", d["clean"] is False and d["fresh"] is False)
check("disconnect_sec computed", isinstance(d["disconnect_sec"], (int, float)))

runtime_state.mark_clean_shutdown()
d = runtime_state.diagnose_restart()
check("clean shutdown flagged", d["clean"] is True)


print("\n== runtime_state: active dose record + window ==")
reset_state()
check("no active dose initially", runtime_state.get_active_dose() is None)
check("no dose window port initially", runtime_state.active_dose_window_ports() == set())

runtime_state.begin_active_dose({
    "device": "RDWC Control", "dev_id": "d-rdwc", "port": 4, "speed": 2,
    "target_ml": 0.5, "strength_factor": 0.25, "start_verified": True,
    "started_wall_ts": time.time(),
    "planned_stop_wall_ts": time.time() + 60,
})
check("active dose stored as pump_running",
      (runtime_state.get_active_dose() or {}).get("status") == "pump_running")
check("in-window dose vouches for its port (no orphan)",
      runtime_state.active_dose_window_ports() == {4})

# Past the planned stop + grace -> no longer vouched.
ad = runtime_state.get_active_dose()
ad["planned_stop_wall_ts"] = time.time() - 100
runtime_state._save({"heartbeat": None, "active_dose": ad, "high_alert": None})
check("expired dose window no longer vouches", runtime_state.active_dose_window_ports() == set())

runtime_state.clear_active_dose()
check("active dose cleared", runtime_state.get_active_dose() is None)


print("\n== runtime_state: interrupted-dose estimate ==")
# Verified start, speed 2 (=42 mL/min), ran 30s -> max ~21 mL; min = planned target.
ad = {"speed": 2, "start_verified": True, "target_ml": 0.5,
      "strength_factor": 0.25, "started_wall_ts": 1000.0}
est = runtime_state.estimate_interrupted_dose(ad, stop_wall_ts=1030.0)
check("max ml = 42 mL/min * 0.5 min = 21", abs(est["estimated_actual_ml_max"] - 21.0) < 0.01)
check("min ml falls back to planned target when start verified",
      est["estimated_actual_ml_min"] == 0.5)
check("full-strength-equiv scales by strength_factor",
      abs(est["estimated_full_strength_equivalent_max"] - 21.0 * 0.25) < 0.01)

ad2 = {"speed": 2, "start_verified": False, "target_ml": 0.5,
       "started_wall_ts": 1000.0}
est2 = runtime_state.estimate_interrupted_dose(ad2, stop_wall_ts=1030.0)
check("unverified start -> min ml is 0", est2["estimated_actual_ml_min"] == 0.0)


print("\n== runtime_state: high-alert window ==")
reset_state()
active, remaining, reason = runtime_state.high_alert_status()
check("no high-alert initially", active is False)

runtime_state.start_high_alert("unit test", duration_minutes=1)
active, remaining, reason = runtime_state.high_alert_status()
check("high-alert active after start", active is True and remaining > 0)
check("high-alert reason preserved", reason == "unit test")

# Force expiry and confirm auto-clear on read.
st = runtime_state.read_state()
st["high_alert"]["until_ts"] = time.time() - 1
runtime_state._save(st)
active, _, _ = runtime_state.high_alert_status()
check("expired high-alert auto-clears on read", active is False)
check("state cleared after expiry", runtime_state.read_state()["high_alert"] is None)


print("\n== poller.doser_watchdog: orphan detection + actuation ==")
reset_state()
_writes.clear()
ac_infinity_client.verify_port_state = _make_verify(ok=True)
poller.ADVISORY_MODE = False

# Nothing running -> no action.
fired = poller.doser_watchdog([dev_with_doser(0, 0)], "TEST")
check("clean state -> watchdog fires nothing", fired == [])
check("clean state -> dosing not frozen", safety_state.is_dosing_disabled() is False)

# A doser running at speed 3 with no active dose -> orphan, stopped + frozen.
reset_state()
_writes.clear()
fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")
check("orphan pump detected + stopped (1 action)", len(fired) == 1 and fired[0]["port"] == 1)
check("stop command sent to port 1 at speed 0", (("d-rdwc", 1, 0) in _writes))
check("orphan triggers dosing freeze", safety_state.is_dosing_disabled() is True)
active, _, _ = runtime_state.high_alert_status()
check("orphan triggers high-alert window", active is True)

# An in-window active dose on port 4 must NOT be treated as an orphan.
reset_state()
_writes.clear()
runtime_state.begin_active_dose({
    "device": "RDWC Control", "port": 4, "speed": 2,
    "started_wall_ts": time.time(), "planned_stop_wall_ts": time.time() + 60,
})
# Port 4 (the vouched-for dose port) is the one actually running here.
dev_dosing = dev_with_doser(0, 0)
dev_dosing["ports"][3]["speed_actual"] = 5  # port 4 running, in its dose window
fired = poller.doser_watchdog([dev_dosing], "TEST")
check("in-window dose on port 4 is left alone", fired == [])
check("in-window dose -> no freeze", safety_state.is_dosing_disabled() is False)

# Advisory mode: detect but never actuate.
reset_state()
_writes.clear()
poller.ADVISORY_MODE = True
fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")
check("advisory mode -> no actuation", fired == [] and _writes == [])
check("advisory mode -> no freeze", safety_state.is_dosing_disabled() is False)
poller.ADVISORY_MODE = False

# Stop that will not verify -> still frozen (the whole point).
reset_state()
_writes.clear()
ac_infinity_client.verify_port_state = _make_verify(ok=False)
fired = poller.doser_watchdog([dev_with_doser(4, 0)], "TEST")
check("unverifiable stop still recorded as fired", len(fired) == 1)
check("unverifiable stop freezes dosing", safety_state.is_dosing_disabled() is True)


# --- Debounce: a successfully-stopped orphan must persist DOSER_WATCHDOG_DEBOUNCE
#     cycles before the PERSISTENT freeze; a startup orphan or a failed stop freezes now.
print("\n== poller.doser_watchdog: persistent-orphan debounce ==")
reset_state()
_writes.clear()
ac_infinity_client.verify_port_state = _make_verify(ok=True)
poller.ADVISORY_MODE = False
os.environ["DOSER_WATCHDOG_DEBOUNCE"] = "2"

fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")
check("debounce: 1st stopped orphan stops but does NOT freeze",
      len(fired) == 1 and safety_state.is_dosing_disabled() is False)
fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")
check("debounce: 2nd consecutive orphan freezes", safety_state.is_dosing_disabled() is True)

# A stopped orphan that does NOT recur clears its streak (no freeze).
reset_state()
_writes.clear()
poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")       # streak 1, no freeze
poller.doser_watchdog([dev_with_doser(0, 0)], "TEST")       # port clear -> streak reset
fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST")  # streak back to 1, still no freeze
check("debounce: a non-recurring orphan never freezes", safety_state.is_dosing_disabled() is False)

# Startup recovery freezes immediately regardless of debounce (crash orphan unknowable).
reset_state()
_writes.clear()
fired = poller.doser_watchdog([dev_with_doser(3, 0)], "TEST", startup=True)
check("startup orphan freezes immediately despite debounce", safety_state.is_dosing_disabled() is True)
os.environ["DOSER_WATCHDOG_DEBOUNCE"] = "1"


print("\n== runtime_state: a crash-surviving dose record does NOT shield an orphan (regression #2) ==")
reset_state()
_writes.clear()
ac_infinity_client.verify_port_state = _make_verify(ok=True)
poller.ADVISORY_MODE = False
# Write an active-dose record as if a PRIOR process started it (begin_active_dose stamps
# pid/boot_id), then simulate that process having crashed by changing the owning pid.
runtime_state.begin_active_dose({
    "device": "RDWC Control", "dev_id": "d-rdwc", "port": 4, "speed": 2,
    "started_wall_ts": time.time(), "planned_stop_wall_ts": time.time() + 60,
})
ad = runtime_state.get_active_dose()
ad["pid"] = os.getpid() + 99999          # a different (dead) process owned this dose
runtime_state._save({"heartbeat": None, "active_dose": ad, "high_alert": None})
check("foreign-pid record does NOT vouch for a port",
      runtime_state.active_dose_window_ports() == set())
# The pump is physically still spinning on port 4 after the crash -- the startup watchdog
# must now stop it instead of being shielded by the stale window.
dev_orphan = dev_with_doser(0, 0)
dev_orphan["ports"][3]["speed_actual"] = 5
fired = poller.doser_watchdog([dev_orphan], "TEST", startup=True)
check("crash-orphan pump is stopped (not shielded by stale dose window)",
      len(fired) == 1 and fired[0]["port"] == 4)
check("crash-orphan stop sent to port 4", ("d-rdwc", 4, 0) in _writes)
check("crash-orphan freezes dosing", safety_state.is_dosing_disabled() is True)


print("\n== runtime_state: estimate honors persisted per-port flow (regression #17) ==")
ad_fast = {"speed": 2, "start_verified": True, "target_ml": 5.0,
           "started_wall_ts": 1000.0, "flow_ml_min": 42.0}   # 2x the 21 spec, per level
est = runtime_state.estimate_interrupted_dose(ad_fast, stop_wall_ts=1030.0)
check("estimate uses FLOW override (84 mL/min total -> 42 mL in 30s)",
      abs(est["estimated_actual_ml_max"] - 42.0) < 0.01)
ad_def = {"speed": 2, "start_verified": True, "target_ml": 5.0, "started_wall_ts": 1000.0}
est_def = runtime_state.estimate_interrupted_dose(ad_def, stop_wall_ts=1030.0)
check("estimate falls back to the 21 spec without override",
      abs(est_def["estimated_actual_ml_max"] - 21.0) < 0.01)
check("interrupted-dose bracket never inverts (min <= max)",
      est["estimated_actual_ml_min"] <= est["estimated_actual_ml_max"])


print("\n== runtime_state: rebooted not falsely set when current boot_id unreadable (regression #18) ==")
reset_state()
runtime_state.write_heartbeat("polling_devices", poll_ok=True)
_orig_boot = runtime_state.boot_id
runtime_state.boot_id = lambda: ""        # simulate /proc boot_id unreadable right now
try:
    d = runtime_state.diagnose_restart()
    check("unreadable current boot_id -> rebooted False (no phantom reboot)",
          d["rebooted"] is False)
finally:
    runtime_state.boot_id = _orig_boot


print("\n== runtime_state: LIVE cross-process dose vouches; dead/rebooted does not (F9) ==")
# The supervised bucket-calibration harnesses dose from a DIFFERENT process while the
# poller's watchdog looks on -- a live foreign writer's in-window record must vouch for
# the pump (liveness, not identity), or the watchdog kills the calibration dose and
# trips the persistent freeze mid-observation.
reset_state()


def _pair_record(pid=None, boot=None):
    runtime_state.begin_active_dose({
        "device": "RDWC Control", "dev_id": "d-rdwc", "port": 4, "speed": 2,
        "started_wall_ts": time.time(), "planned_stop_wall_ts": time.time() + 60,
    })
    ad = runtime_state.get_active_dose()
    if pid is not None:
        ad["pid"] = pid
    if boot is not None:
        ad["boot_id"] = boot
    runtime_state._save({"heartbeat": None, "active_dose": ad, "high_alert": None})


# pid 1 (init) is always alive but not ours: os.kill(1, 0) -> EPERM -> alive.
_pair_record(pid=1)
check("LIVE foreign pid + same boot vouches for the dose window",
      runtime_state.active_dose_window_ports() == {4})

# A reaped child is a deterministic DEAD pid on this boot.
import subprocess
_proc = subprocess.Popen(["true"]); _proc.wait()
_pair_record(pid=_proc.pid)
check("DEAD writer pid does not vouch (crash orphan still caught)",
      runtime_state.active_dose_window_ports() == set())

# Live pid but a different boot_id (record survived a reboot; theoretical pid reuse).
_pair_record(pid=1, boot="not-this-boot")
check("different boot_id does not vouch even with a live pid",
      runtime_state.active_dose_window_ports() == set())

check("_pid_alive: own pid alive", runtime_state._pid_alive(os.getpid()) is True)
check("_pid_alive: None/garbage/nonpositive are dead",
      runtime_state._pid_alive(None) is False and
      runtime_state._pid_alive("x") is False and runtime_state._pid_alive(0) is False)
runtime_state.clear_active_dose()


print("\n== runtime_state: pair crash-estimate SUMS concurrent pumps (F14) ==")
# Both nutrient pumps run at once; a power loss 30s into the pair must estimate the
# TOTAL delivered (both pumps), not one pump's worth -- the old max()-flow record
# under-counted the overdose window ~2x and the operator under-reacts.
ad_pair = {"speed": 2, "start_verified": True, "started_wall_ts": 1000.0,
           "flow_ml_min": 21.0, "flow_ml_min_by_port": {1: 21.0, 2: 21.0}}
est_pair = runtime_state.estimate_interrupted_dose(ad_pair, stop_wall_ts=1030.0)
check("pair record sums per-port flows (42 mL, ~2x the single-pump 21)",
      abs(est_pair["estimated_actual_ml_max"] - 42.0) < 0.01)
ad_single = {"speed": 2, "start_verified": True, "started_wall_ts": 1000.0,
             "flow_ml_min": 21.0}
est_single = runtime_state.estimate_interrupted_dose(ad_single, stop_wall_ts=1030.0)
check("old single-flow record unchanged (21 mL)",
      abs(est_single["estimated_actual_ml_max"] - 21.0) < 0.01)
# After a crash the record round-trips through JSON -> dict keys become strings.
ad_json = {"speed": 2, "start_verified": True, "started_wall_ts": 1000.0,
           "flow_ml_min": 21.0, "flow_ml_min_by_port": {"1": 21.0, "2": 42.0}}
est_json = runtime_state.estimate_interrupted_dose(ad_json, stop_wall_ts=1030.0)
check("JSON string-keyed pair map still sums (63 * 2 * 0.5 = 63 mL)",
      abs(est_json["estimated_actual_ml_max"] - 63.0) < 0.01)
ad_bad = {"speed": 2, "start_verified": True, "started_wall_ts": 1000.0,
          "flow_ml_min": 21.0, "flow_ml_min_by_port": {"1": "garbage"}}
est_bad = runtime_state.estimate_interrupted_dose(ad_bad, stop_wall_ts=1030.0)
check("malformed pair map falls back to the scalar (21 mL)",
      abs(est_bad["estimated_actual_ml_max"] - 21.0) < 0.01)


# =========================================================================== #
print(f"\n{'='*48}")
print(f"  {_PASS} passed, {_FAIL} failed")
print(f"{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
