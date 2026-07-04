#!/usr/bin/env python3
"""
Self-tests for profile_manager calibration + the pending-outcome queue. No hardware:
snapshots are synthetic dicts, the profiles dir + pending file are redirected to a temp
dir, and the settle window is forced to 0 so outcomes settle on the same cycle.
Covers regressions #9 (strain guard), #20 (no bucket cross-contamination), #21
(calibration persisted before the queue is drained), and F4 (a persistently failing
_save must not make record_outcomes raise every cycle -- that would starve the poller's
safety enforcement forever). Run: python3 profile_manager_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="pm_test_"))
os.environ["DOSER_PORTS_TESTRES"] = "1,2,3,4"
os.environ["PH_PORTS_TESTRES"] = "3,4"
os.environ["OUTCOME_WAIT_CYCLES"] = "0"      # settle immediately
os.environ["DOSE_SETTLE_MINUTES"] = "0"
os.environ.pop("GROW_START_DATE", None)

import profile_manager as pm
pm.PROFILES_DIR = _TMP
pm._PENDING_FILE = _TMP / ".pending_outcomes.json"

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


def reset(strain="WhiteWidow"):
    os.environ["STRAIN_NAME"] = strain
    pm._pending.clear()
    try:
        pm._PENDING_FILE.unlink()
    except FileNotFoundError:
        pass
    try:
        pm._profile_path(strain).unlink()
    except FileNotFoundError:
        pass


def snap(**sensors):
    return {"devices": [{"name": "TestRes", "sensors": dict(sensors)}]}


DEV = "TestRes"
PH_KEY   = f"{DEV}:dose:timed_ph_down_microdose"
NUTE_KEY = f"{DEV}:dose:timed_nutrient_microdose"


# =========================================================================== #
print("\n== track_actions: no strain -> nothing queued (regression #9) ==")
reset()
os.environ["STRAIN_NAME"] = ""
pm._pending.clear()
pm.track_actions(
    [{"device": DEV, "action": "dose", "playbook": "timed_nutrient_microdose",
      "kind": "nute", "ports": [1, 2]}],
    snap(ph=6.0, tds_ppm=400))
check("no strain -> pending stays empty", pm.has_pending_outcomes() is False and len(pm._pending) == 0)
check("no strain -> pending file never written", not pm._PENDING_FILE.exists())


print("\n== record_outcomes: chemical buckets don't cross-contaminate (regression #20) ==")
reset()
before = snap(ph=6.0, tds_ppm=400)
# A pH-down dose AND a nutrient dose fire together; afterward BOTH pH dropped (pH dose)
# and TDS rose (nutrient dose) -- the combined effect. Each bucket must learn ONLY its
# own axis, not the other dose's effect.
pm.track_actions([
    {"device": DEV, "action": "dose", "playbook": "timed_ph_down_microdose", "kind": "ph", "ports": [4]},
    {"device": DEV, "action": "dose", "playbook": "timed_nutrient_microdose", "kind": "nute", "ports": [1, 2]},
], before)
pm.record_outcomes(snap(ph=5.6, tds_ppm=460))   # pH -0.4, TDS +60 combined
cal = pm._load("WhiteWidow")["calibration"]
check("pH bucket records its pH delta", "ph" in cal[PH_KEY]["averages"])
check("pH bucket does NOT absorb the nutrient TDS rise", "tds_ppm" not in cal[PH_KEY]["averages"])
check("nutrient bucket records its TDS delta", "tds_ppm" in cal[NUTE_KEY]["averages"])
check("nutrient bucket does NOT absorb the pH drop", "ph" not in cal[NUTE_KEY]["averages"])


print("\n== record_outcomes: calibration persisted + queue drained on settle (regression #21) ==")
check("pending queue drained after settle", pm.has_pending_outcomes() is False)
reloaded = pm._load("WhiteWidow")          # read back from disk, not memory
check("calibration persisted to disk before drain", NUTE_KEY in reloaded.get("calibration", {}))


print("\n== record_outcomes: broken disk must not starve the poll loop (F4) ==")
reset()
import runtime_state
runtime_state._EVENT_LOG = _TMP / "events.jsonl"
pm.track_actions(
    [{"device": DEV, "action": "dose", "playbook": "timed_ph_down_microdose",
      "kind": "ph", "ports": [4]}],
    snap(ph=6.0))
_orig_save = pm._save
def _broken_save(strain, data):
    raise OSError("profiles dir unwritable")
pm._save = _broken_save
try:
    raised = False
    try:
        pm.record_outcomes(snap(ph=5.8))
    except Exception:
        raised = True
    check("settle with a failing _save does not raise", not raised)
    check("in-memory queue drained despite the failed save",
          pm.has_pending_outcomes() is False)
    # Subsequent poll cycles (disk still broken) must be clean no-ops -- record_outcomes
    # runs ahead of doser_watchdog / res-burst / emergency enforcement in poller.main,
    # so an every-cycle re-raise would skip all deterministic safety enforcement.
    raised = False
    try:
        pm.record_outcomes(snap(ph=5.8))
        pm.record_outcomes(snap(ph=5.8))
    except Exception:
        raised = True
    check("subsequent cycles do not re-raise", not raised)
    # Crash-recovery intent (regression #21) preserved: _save_pending was skipped, so
    # the on-disk queue still holds the batch -- a restart re-settles it (delayed, not lost).
    check("settled batch retained on disk for restart re-settle",
          len(pm._load_pending()) == 1)
    check("failure logged loudly to the event ledger",
          runtime_state._EVENT_LOG.exists()
          and "calibration_save_failed" in runtime_state._EVENT_LOG.read_text())
finally:
    pm._save = _orig_save

# Disk recovers: the next batch must settle + persist normally (no lingering state).
pm._pending.clear()
pm.track_actions(
    [{"device": DEV, "action": "dose", "playbook": "timed_ph_down_microdose",
      "kind": "ph", "ports": [4]}],
    snap(ph=5.8))
pm.record_outcomes(snap(ph=5.6))
check("calibration resumes once the disk recovers",
      "ph" in pm._load("WhiteWidow")["calibration"].get(PH_KEY, {}).get("averages", {}))
check("pending queue drained after recovery", pm.has_pending_outcomes() is False)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)


def test_all():
    """pytest entry point -- the checks above ran at import; fail if any failed."""
    assert _FAIL == 0, f"{_FAIL} check(s) failed (see stdout)"


if __name__ == "__main__":
    sys.exit(1 if _FAIL else 0)
