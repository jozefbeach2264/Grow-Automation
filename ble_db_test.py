#!/usr/bin/env python3
"""
Self-tests for the BLE command queue's device-scoped atomic claim (regressions #3
livelock, #6 double-claim, #7 requeue crash). No BLE/hardware -- exercises only the
SQLite queue. Run: python3 ble_db_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="bledb_test_"))
os.environ["BLE_DB_PATH"] = str(_TMP / "controller.db")
# No DOSER_PORTS/PH_PORTS for the test devices -> no port is chemical -> enqueue guard passes.
os.environ.pop("DOSING_DISABLED", None)

import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"
from aci_ble_lab import db

db.init_schema()

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


def pending_count():
    with db._conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM command_queue WHERE status='pending'").fetchone()[0]


# Enqueue interleaved rows for two controllers (work_type 2 = on, non-chemical ports).
idA1 = db.enqueue_command("DevA", 1, 2, 5)
idB1 = db.enqueue_command("DevB", 2, 2, 5)
idA2 = db.enqueue_command("DevA", 3, 2, 5)

print("\n== claim_next_command is device-scoped + atomic ==")
r = db.claim_next_command("DevA")
check("DevA claims its OWN oldest row (A1)", r and r["id"] == idA1 and r["device"] == "DevA")
check("claimed row marked sent -> next DevA claim is A2 (no double-claim)",
      db.claim_next_command("DevA")["id"] == idA2)
check("DevA queue now empty -> None", db.claim_next_command("DevA") is None)

print("\n== foreign rows are never touched (no requeue / no growth) ==")
before = pending_count()
check("DevB's row stayed pending while DevA drained its own", before == 1)
for _ in range(5):                       # the old code would livelock/grow here
    db.claim_next_command("DevA")
check("claiming for an empty device never grows the queue", pending_count() == before)
rb = db.claim_next_command("DevB")
check("DevB still claims its own row (B1)", rb and rb["id"] == idB1)
check("queue fully drained", pending_count() == 0)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
