#!/usr/bin/env python3
"""
Self-tests for the BLE command queue + chemical-write guard. No BLE/hardware --
exercises only the SQLite queue and aci_ble_lab.safety. Covers the device-scoped
atomic claim (regressions #3 livelock, #6 double-claim, #7 requeue crash) plus
the 2026-07 review findings: F3 chemical STARTS never enqueue (the BLE layer is
telemetry/climate only), F1 chemical STOPS always pass -- dosing freeze or not,
and F11 the command TTL (stale pending starts expire; stale 'sent' rows are
swept to 'failed' so their loss is visible). Run: python3 ble_db_test.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="bledb_test_"))
os.environ["BLE_DB_PATH"] = str(_TMP / "controller.db")
# DevA/DevB/DevT/DevS are climate-only (no DOSER_PORTS/PH_PORTS) -> guard passes.
# TestRes is a synthetic reservoir device so the chemical interlock can fire.
os.environ["DOSER_PORTS_TESTRES"] = "1,2,3,4"
os.environ["PH_PORTS_TESTRES"] = "3,4"
os.environ.pop("DOSING_DISABLED", None)
os.environ.pop("BLE_CMD_TTL_S", None)

import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"
from aci_ble_lab import db
from aci_ble_lab.safety import SafetyBlocked, guard_chemical_write

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


def row_status(cmd_id):
    with db._conn() as c:
        r = c.execute(
            "SELECT status, error FROM command_queue WHERE id=?", (cmd_id,)).fetchone()
        return (r["status"], r["error"] or "") if r else (None, "")


def blocked(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except SafetyBlocked:
        return True


def age_row(cmd_id, ts):
    with db._conn() as c:
        c.execute("UPDATE command_queue SET ts=? WHERE id=?", (ts, cmd_id))


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

print("\n== F3: chemical STARTS never enqueue (BLE layer is telemetry/climate only) ==")
check("doser START (work_type on, speed 5) raises SafetyBlocked with NO freeze",
      blocked(db.enqueue_command, "TestRes", 1, 2, 5))
check("pH-port START raises SafetyBlocked with NO freeze",
      blocked(db.enqueue_command, "TestRes", 3, 2, 1))
check("off-type work_type with nonzero speed is still a START (level_off runs)",
      blocked(db.enqueue_command, "TestRes", 2, 0, 4))
check("no chemical row reached the queue", pending_count() == 0)
check("climate port on the SAME device still enqueues fine",
      db.enqueue_command("TestRes", 6, 2, 5) > 0)
db.claim_next_command("TestRes")         # drain it again

print("\n== F1: chemical STOPS always pass -- freeze or not ==")
safety_state.disable_dosing("bledb test freeze")
sid = None
try:
    sid = db.enqueue_command("TestRes", 4, 0, 0)
except SafetyBlocked:
    pass
check("STOP (speed 0) on a pH port enqueues DURING an active freeze", sid is not None)
rs = db.claim_next_command("TestRes")
check("frozen STOP row drains normally at claim",
      rs is not None and rs["id"] == sid and rs["speed"] == 0)
check("chemical START is still blocked during the freeze",
      blocked(db.enqueue_command, "TestRes", 1, 2, 3))
safety_state.clear_dosing_disable()

print("\n== guard_chemical_write unit checks ==")
check("climate port is a no-op for the guard",
      not blocked(guard_chemical_write, "TestRes", 6, work_type=2, speed=5))
check("legacy call (no speed) on a chemical port is treated as a START",
      blocked(guard_chemical_write, "TestRes", 1))
check("legacy call (no speed) on a climate port still passes",
      not blocked(guard_chemical_write, "DevA", 1))
_old_co2 = os.environ.get("CO2_VALVE")
os.environ["CO2_VALVE"] = "TestAux:2"
check("CO2 valve START is blocked",
      blocked(guard_chemical_write, "TestAux", 2, work_type=1, speed=1))
check("CO2 valve STOP passes",
      not blocked(guard_chemical_write, "TestAux", 2, work_type=0, speed=0))
if _old_co2 is None:
    os.environ.pop("CO2_VALVE", None)
else:
    os.environ["CO2_VALVE"] = _old_co2

print("\n== F11: stale pending STARTS expire at claim (BLE_CMD_TTL_S) ==")
idt = db.enqueue_command("DevT", 5, 2, 4)
age_row(idt, time.time() - 9999)         # far past the 180s default TTL
check("claim skips the stale START (nothing returned)",
      db.claim_next_command("DevT") is None)
st, err = row_status(idt)
check("stale START marked 'expired', never executed",
      st == "expired" and "BLE_CMD_TTL_S" in err)
ids = db.enqueue_command("DevT", 5, 0, 0)
age_row(ids, time.time() - 9999)
rstop = db.claim_next_command("DevT")
check("stale STOP is exempt from the TTL (still drains)",
      rstop is not None and rstop["id"] == ids)
os.environ["BLE_CMD_TTL_S"] = "100000"
idf = db.enqueue_command("DevT", 6, 2, 3)
age_row(idf, time.time() - 9999)
rf = db.claim_next_command("DevT")
check("BLE_CMD_TTL_S env override widens the window (row still fresh)",
      rf is not None and rf["id"] == idf)
os.environ.pop("BLE_CMD_TTL_S", None)

print("\n== F11: stale 'sent' rows are swept to 'failed' (visible loss) ==")
idS1 = db.enqueue_command("DevS", 5, 2, 4)
idS2 = db.enqueue_command("DevS", 6, 2, 4)
idZ1 = db.enqueue_command("DevZ", 5, 2, 4)
db.claim_next_command("DevS")            # idS1 -> 'sent'
db.claim_next_command("DevS")            # idS2 -> 'sent'
db.claim_next_command("DevZ")            # idZ1 -> 'sent' (other device)
with db._conn() as c:                    # only idS1 predates the TTL
    c.execute("UPDATE command_queue SET sent_at=? WHERE id=?",
              (time.time() - 9999, idS1))
check("sweep fails exactly the stale row", db.sweep_stale_sent("DevS") == 1)
st1, err1 = row_status(idS1)
check("swept row is 'failed' with a previous-daemon note",
      st1 == "failed" and "previous daemon" in err1)
check("fresh 'sent' row untouched", row_status(idS2)[0] == "sent")
check("other device's 'sent' row untouched", row_status(idZ1)[0] == "sent")


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)


def test_all():
    """pytest entry point -- the checks above ran at import; fail if any failed."""
    assert _FAIL == 0, f"{_FAIL} check(s) failed (see stdout)"


if __name__ == "__main__":
    sys.exit(1 if _FAIL else 0)
