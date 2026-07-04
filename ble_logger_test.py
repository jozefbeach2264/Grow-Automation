#!/usr/bin/env python3
"""
Self-tests for the BLE daemon (ble_logger.py). No radio/hardware -- exercises
the pure decode/matching helpers and the executor plumbing against a temp
SQLite queue. Covers the 2026-07 review findings: F2 the daemon loads
.env/labels.env itself (fresh process must classify chemical ports), F10 the
daemon survives transient sqlite3 errors (drain tick + session loop), F15 the
per-type sensor scaling + sentinel skip and seq-matched A5 port attribution
(incl. the 16-bit sequence wrap), plus the executor-side F1/F3 re-check in
_pop_filtered. Run: python3 ble_logger_test.py
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="blelog_test_"))
_REPO = Path(__file__).resolve().parent
# Set BEFORE importing ble_logger: its module-level load_dotenv never overrides
# pre-set env, so these stay authoritative for the synthetic reservoir device.
os.environ["BLE_DB_PATH"] = str(_TMP / "controller.db")
os.environ["DOSER_PORTS_BLE_TEST_RES"] = "1,2,3,4"
os.environ["PH_PORTS_BLE_TEST_RES"] = "3,4"

import safety_state
safety_state._STATE_FILE = _TMP / ".safety_state.json"

import ble_logger
from aci_ble_lab import db
from aci_ble_lab.safety import is_chemical_port

# The import above pulled in the real .env/labels.env (that's finding F2);
# scrub the knobs the checks below depend on.
os.environ.pop("DOSING_DISABLED", None)
os.environ.pop("BLE_CMD_TTL_S", None)
os.environ["BLE_DB_PATH"] = str(_TMP / "controller.db")

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


print("\n== F2: daemon loads .env/labels.env itself ==")
# In-process: the module import above ran load_dotenv, so the repo's committed
# labels.env roles must be visible to the guard.
check("guard sees the real doser ports after import (labels.env loaded)",
      is_chemical_port("Hydroponics Control", 1))
# The strict test: a FRESH daemon process with the role env vars stripped must
# still classify the doser port as chemical -- before the fix, the guard was
# silently voided because nothing loaded the env files.
_code = (
    "import os; "
    "assert 'DOSER_PORTS_HYDROPONICS_CONTROL' not in os.environ; "
    "import ble_logger; "
    "from aci_ble_lab.safety import is_chemical_port; "
    "print('CHEM' if is_chemical_port('Hydroponics Control', 1) else 'CLIMATE')"
)
_env = {k: v for k, v in os.environ.items()
        if not k.startswith(("DOSER_PORTS_", "PH_PORTS_")) and k != "CO2_VALVE"}
_out = subprocess.run([sys.executable, "-c", _code], cwd=_REPO, env=_env,
                      capture_output=True, text=True, timeout=120)
check("fresh daemon process classifies doser ports from labels.env",
      _out.returncode == 0 and _out.stdout.strip().splitlines()[-1:] == ["CHEM"])


print("\n== F15a: per-type sensor scaling + sentinel skip ==")


def grp(sensor_type, ble_code, raw):
    raw &= 0xFFFF
    return bytes((sensor_type, ble_code, (raw >> 8) & 0xFF, raw & 0xFF))


def tail_packet(*groups):
    return b"\x1e\xff" + bytes(98) + b"".join(groups)   # tail starts at offset 100


vals = {r["port"]: r["value"] for r in ble_logger.decode_sensor_tail(
    tail_packet(grp(14, 0x6F, 2334), grp(16, 0x6F, 1657), grp(13, 0x6F, 738)))}
check("EC uS/cm scales /10 (raw 2334 -> 233.4)", abs(vals.get(14, 0) - 233.4) < 1e-9)
check("TDS ppm scales /10 (raw 1657 -> 165.7)", abs(vals.get(16, 0) - 165.7) < 1e-9)
check("pH scales /100 (raw 738 -> 7.38)", abs(vals.get(13, 0) - 7.38) < 1e-9)

vals = {r["port"]: r["value"] for r in ble_logger.decode_sensor_tail(
    tail_packet(grp(13, 0x6F, 0x8000), grp(14, 0x6F, 0),
                grp(12, 0x6F, 0), grp(20, 0x6F, 0)))}
check("-32768 no-sensor sentinel skipped (no phantom pH 327.68)", 13 not in vals)
check("zero skipped for types where 0 means 'no reading'", 14 not in vals)
check("zero kept for light (lights off is a real reading)", vals.get(12) == 0.0)
check("zero kept for water level (empty reservoir is real)", vals.get(20) == 0.0)

vals = {r["port"]: r["value"] for r in ble_logger.decode_sensor_tail(
    tail_packet(grp(25, 0x6F, 500)))}
check("unknown type falls back to /100 (old behavior)",
      abs(vals.get(25, 0) - 5.0) < 1e-9)


print("\n== F15b: A5 responses matched by echoed seq, never queue order ==")
from ac_infinity_ble.util import crc16          # noqa: E402
from ac_infinity_ble.protocol import Protocol   # noqa: E402


def a5_frame(seq, valid=True):
    h = [0xA5, 0x1C, 0x00, 0x04, (seq >> 8) & 0xFF, seq & 0xFF]
    c = crc16(h)
    if not valid:
        c = [c[0] ^ 0xFF, c[1]]
    return bytes(h + c)


check("frame_seq reads the echoed seq when the header CRC verifies",
      ble_logger.frame_seq(a5_frame(7)) == 7)
check("frame_seq -> None on an unverifiable header",
      ble_logger.frame_seq(a5_frame(7, valid=False)) is None)
check("frame_seq -> None on a short frame",
      ble_logger.frame_seq(b"\xa5\x1c\x00\x00") is None)


async def _run_next(frames, seq, timeout=0.5):
    q: asyncio.Queue = asyncio.Queue()
    for f in frames:
        q.put_nowait(f)
    return await asyncio.wait_for(ble_logger._next_a5(q, seq), timeout)


got = asyncio.run(_run_next([a5_frame(6), a5_frame(7)], 7))
check("late frame from a timed-out port is dropped, not credited to the next",
      ble_logger.frame_seq(got) == 7)
got = asyncio.run(_run_next([a5_frame(9, valid=False)], 7))
check("unverifiable header degrades to order-based matching (not dropped)",
      got == a5_frame(9, valid=False))
timed_out = False
try:
    asyncio.run(_run_next([a5_frame(6)], 7))
except asyncio.TimeoutError:
    timed_out = True
check("a lone stale frame never satisfies the wrong request", timed_out)

check("_next_seq wraps at the protocol's 16-bit field", ble_logger._next_seq(0xFFFF) == 0)
check("_next_seq still increments normally", ble_logger._next_seq(6) == 7)
req = bytes(Protocol().get_model_data(9, 4, 0x10007))
check("protocol truncates seq to 16 bits (why the counter must wrap)",
      ble_logger.frame_seq(req) == 7)


print("\n== executor re-check (_pop_filtered): F3 stale chemical START drops, F1 STOP drains ==")
# A chemical START can no longer be enqueued, so simulate a row that predates
# an env/safety change by inserting it directly.
with db._conn() as c:
    r = c.execute(
        "INSERT INTO command_queue(ts, device, port, work_type, speed, source) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), "Ble Test Res", 3, 2, 4, "test"))
    stale_id = r.lastrowid
got = asyncio.run(ble_logger._pop_filtered("Ble Test Res"))
check("stale chemical-START row never reaches the write path", got is None)
with db._conn() as c:
    row = c.execute("SELECT status, error FROM command_queue WHERE id=?",
                    (stale_id,)).fetchone()
check("dropped row marked 'failed' with the SafetyBlocked reason",
      row["status"] == "failed" and "chemical START" in (row["error"] or ""))

safety_state.disable_dosing("ble_logger test freeze")
stop_id = db.enqueue_command("Ble Test Res", 3, 0, 0, source="test")
got = asyncio.run(ble_logger._pop_filtered("Ble Test Res"))
check("chemical STOP drains at the executor DURING an active freeze",
      got is not None and got["id"] == stop_id and got["speed"] == 0)
safety_state.clear_dosing_disable()


print("\n== F10: daemon survives transient sqlite3 errors ==")
_orig_claim = db.claim_next_command


def _locked(device):
    raise sqlite3.OperationalError("database is locked")


db.claim_next_command = _locked
try:
    got = asyncio.run(ble_logger._pop_filtered("Ble Test Res"))
finally:
    db.claim_next_command = _orig_claim
check("_pop_filtered turns a lock timeout into a skipped drain tick", got is None)

_session_calls = []


async def _fake_session(device_name, address, poll_ports, poll_sec):
    _session_calls.append(device_name)
    if len(_session_calls) == 1:
        raise sqlite3.OperationalError("database is locked")
    raise asyncio.CancelledError


_orig_session = ble_logger._run_session
ble_logger._run_session = _fake_session
try:
    asyncio.run(ble_logger._main("Ble Test Res", "AA:BB:CC:DD:EE:FF", [], 5, 0))
finally:
    ble_logger._run_session = _orig_session
check("session loop retries after a sqlite3 error instead of dying",
      len(_session_calls) == 2)


print("\n== F11 wiring: session start sweeps stale 'sent' rows ==")
_swept_for = []
_orig_sweep = db.sweep_stale_sent
_orig_scan = ble_logger._scan_until_found


async def _no_scan(address, timeout=60):
    return False


db.sweep_stale_sent = lambda device: (_swept_for.append(device), 0)[1]
ble_logger._scan_until_found = _no_scan
try:
    asyncio.run(ble_logger._run_session("Ble Test Res", "AA:BB:CC:DD:EE:FF", [], 30))
finally:
    db.sweep_stale_sent = _orig_sweep
    ble_logger._scan_until_found = _orig_scan
check("_run_session sweeps this device's stale 'sent' rows before connecting",
      _swept_for == ["Ble Test Res"])


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)


def test_all():
    """pytest entry point -- the checks above ran at import; fail if any failed."""
    assert _FAIL == 0, f"{_FAIL} check(s) failed (see stdout)"


if __name__ == "__main__":
    sys.exit(1 if _FAIL else 0)
