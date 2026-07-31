#!/usr/bin/env python3
"""
Self-tests for the persistent chemical freeze (safety_state.py). No hardware --
only the state file, redirected to a temp dir.

Focus: the 2026-07-31 review P0 finding that corrupt safety persistence failed
OPEN. A missing file is a fresh install (not disabled); a file that EXISTS but
will not parse means a trip may have been lost, so it must fail CLOSED.
Run: python3 safety_state_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="safety_state_test_"))

import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"
os.environ.pop("DOSING_DISABLED", None)

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
    for p in _TMP.iterdir():
        p.unlink()


def corrupt_copies():
    return sorted(p.name for p in _TMP.glob(".safety_state.json.corrupt.*"))


# =========================================================================== #
print("\n== missing file is a fresh install, not a lost trip ==")
reset()
check("no state file -> dosing NOT disabled", safety_state.is_dosing_disabled() is False)
check("no state file -> no reason", safety_state.dosing_disable_status()[1] is None)
check("reading a missing file writes nothing", list(_TMP.iterdir()) == [])


print("\n== normal round trip ==")
reset()
safety_state.disable_dosing("unit test trip")
disabled, reason = safety_state.dosing_disable_status()
check("disable_dosing persists", disabled is True and reason == "unit test trip")
safety_state.clear_dosing_disable()
check("clear_dosing_disable lifts it", safety_state.is_dosing_disabled() is False)


print("\n== corrupt file FAILS CLOSED ==")
reset()
safety_state._STATE_FILE.write_text('{"dosing_disabled": fal')      # truncated write
disabled, reason = safety_state.dosing_disable_status()
check("unparseable state file -> dosing DISABLED", disabled is True)
check("reason says fail-closed", "fail-closed" in (reason or ""))
check("the corrupt file is preserved for inspection", len(corrupt_copies()) == 1)

on_disk = json.loads(safety_state._STATE_FILE.read_text())
check("the live file is rewritten as a valid tripped state",
      on_disk["dosing_disabled"] is True)
check("the trip is timestamped", isinstance(on_disk.get("dosing_disabled_at"), float))
check("second read is a clean read, not another trip",
      safety_state.is_dosing_disabled() is True and len(corrupt_copies()) == 1)
safety_state.clear_dosing_disable()
check("a human can still clear it after inspection",
      safety_state.is_dosing_disabled() is False)

reset()
safety_state._STATE_FILE.write_text('["not", "an", "object"]')
check("valid JSON of the wrong shape also fails closed",
      safety_state.is_dosing_disabled() is True)

reset()
safety_state._STATE_FILE.write_bytes(b"\x00\x01\x02 binary garbage")
check("binary garbage fails closed", safety_state.is_dosing_disabled() is True)


print("\n== corruption cannot silently un-freeze, even if the rewrite fails ==")
reset()
safety_state._STATE_FILE.write_text("{{{ not json")
_orig_save = safety_state._save
safety_state._save = lambda state: None            # simulate a failed persist
try:
    check("still reports DISABLED when the tripped state cannot be persisted",
          safety_state.is_dosing_disabled() is True)
    check("the corrupt file is still on disk (copied, not moved)",
          safety_state._STATE_FILE.read_text() == "{{{ not json")
    check("so the NEXT read trips again rather than reading as fresh",
          safety_state.is_dosing_disabled() is True)
finally:
    safety_state._save = _orig_save


print("\n== env override is unchanged ==")
reset()
os.environ["DOSING_DISABLED"] = "true"
disabled, reason = safety_state.dosing_disable_status()
check("env override still disables", disabled is True)
check("env override reports itself", "env override" in (reason or ""))
os.environ.pop("DOSING_DISABLED", None)
check("unsetting the override re-enables", safety_state.is_dosing_disabled() is False)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
