"""SQLite-backed BLE command queue + telemetry cache.

Design intent
-------------
The queue is an arms-length boundary between the caller (poller / dosing.py /
operator CLI) and the BLE daemon (`ble_logger.py`). A row in `command_queue`
is the only way a BLE write happens. That single chokepoint is where the
chemical-write guard lives, so the BLE channel cannot become a bypass.

`enqueue_command()` is the public entry: it validates the row, calls
`safety.guard_chemical_write()`, and inserts. The executor still re-checks the
guard before issuing the BLE write (defense in depth -- a row that becomes
stale during a freeze must not fire).

DB path defaults to `profiles/controller.db` to match the rest of the project's
runtime state. Override with BLE_DB_PATH for tests.
"""

import os
import sqlite3
import time
from pathlib import Path

from aci_ble_lab.safety import guard_chemical_write


def _db_path() -> Path:
    override = os.getenv("BLE_DB_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "profiles" / "controller.db"


def _conn() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(p, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_VALID_WORK_TYPES = {0, 1, 2, 3, 4}  # 0=off-mode, 2=on-mode, etc per protocol

# Commands are only meaningful near the moment they were issued -- a stale
# set_speed firing hours after a BLE outage could land against a completely
# different reservoir/climate state. Rows older than the TTL are marked
# 'expired' at claim time and never executed. STOPS (speed 0) are exempt:
# executing an old stop is at worst a no-op, dropping one could leave a pump
# running. Env-overridable via BLE_CMD_TTL_S (re-read each call, same dynamic
# model as the port classification).
_DEFAULT_CMD_TTL_S = 180.0


def _cmd_ttl_s() -> float:
    try:
        return float(os.getenv("BLE_CMD_TTL_S", "").strip() or _DEFAULT_CMD_TTL_S)
    except ValueError:
        return _DEFAULT_CMD_TTL_S


def init_schema() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS command_queue (
            id          INTEGER PRIMARY KEY,
            ts          REAL    NOT NULL,
            device      TEXT    NOT NULL,
            port        INTEGER NOT NULL,
            work_type   INTEGER NOT NULL,
            speed       INTEGER NOT NULL,
            source      TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            sent_at     REAL,
            done_at     REAL,
            error       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_queue_pending
            ON command_queue(status, id)
            WHERE status = 'pending';

        CREATE TABLE IF NOT EXISTS port_state (
            ts          REAL    NOT NULL,
            device      TEXT    NOT NULL,
            port        INTEGER NOT NULL,
            work_type   INTEGER NOT NULL,
            level_off   INTEGER NOT NULL,
            level_on    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_port_state_recent
            ON port_state(device, port, ts);

        CREATE TABLE IF NOT EXISTS sensor_readings (
            ts          REAL    NOT NULL,
            device      TEXT    NOT NULL,
            port        INTEGER NOT NULL,
            sensor_type INTEGER NOT NULL,
            value       REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_sensor_recent
            ON sensor_readings(device, port, ts);
        """)


def enqueue_command(device: str, port: int, work_type: int, speed: int,
                    source: str = "poller") -> int:
    """Validate, gate, and enqueue a BLE control command. Returns the new row id.

    Raises ValueError on out-of-range inputs and SafetyBlocked on any chemical
    START (the BLE layer moves no chemicals -- delivery goes through the
    bounded dose verb on the cloud path). Chemical STOPS (speed 0) always
    pass, dosing freeze or not."""
    if not (0 <= port <= 15):
        raise ValueError(f"port {port} out of range (0-15)")
    if not (0 <= speed <= 10):
        raise ValueError(f"speed {speed} out of range (0-10)")
    if work_type not in _VALID_WORK_TYPES:
        raise ValueError(f"work_type {work_type} not in {sorted(_VALID_WORK_TYPES)}")
    if not device:
        raise ValueError("device name is required")

    guard_chemical_write(device, port, work_type=work_type, speed=speed)

    with _conn() as c:
        r = c.execute(
            "INSERT INTO command_queue(ts, device, port, work_type, speed, source) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), device, port, work_type, speed, source),
        )
        return r.lastrowid


def claim_next_command(device: str) -> dict | None:
    """Atomically claim the oldest pending command FOR THIS DEVICE and mark it 'sent'.
    Returns the row dict or None.

    Device-scoped + atomic by design: BEGIN IMMEDIATE takes the write lock BEFORE the
    SELECT, so two daemons can never read-then-claim the same row (the old bare
    SELECT-then-UPDATE allowed a double-claim -> double dose), and a daemon never touches
    another controller's rows -- foreign rows stay 'pending' for their own daemon instead
    of being requeued (which livelocked the drain loop and could grow the queue without
    bound).

    Staleness cutoff: pending non-stop rows older than BLE_CMD_TTL_S are marked
    'expired' inside the same transaction and skipped -- a start enqueued during a
    BLE outage must not fire hours later. Stops are exempt (see _cmd_ttl_s)."""
    c = _conn()
    c.isolation_level = None                 # manage the transaction explicitly
    try:
        c.execute("BEGIN IMMEDIATE")         # take the write lock up front
        now = time.time()
        c.execute(
            "UPDATE command_queue SET status='expired', done_at=?, "
            "error='expired: exceeded BLE_CMD_TTL_S before a daemon claimed it' "
            "WHERE status='pending' AND device=? AND speed>0 AND ts<?",
            (now, device, now - _cmd_ttl_s()),
        )
        row = c.execute(
            "SELECT * FROM command_queue WHERE status='pending' AND device=? "
            "ORDER BY id LIMIT 1",
            (device,),
        ).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute(
            "UPDATE command_queue SET status='sent', sent_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        c.execute("COMMIT")
        return dict(row)
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()


def pop_next_command() -> dict | None:
    """DEPRECATED -- not device-scoped and NOT atomic (bare SELECT-then-UPDATE under
    sqlite3 default isolation lets two connections claim the same row). Kept only for
    compatibility; the daemon uses claim_next_command(device) instead."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM command_queue WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE command_queue SET status='sent', sent_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        return dict(row)


def command_status(cmd_id: int) -> str | None:
    """Current status of one queued command ('pending'/'sent'/'done'/'failed'/
    'expired'), or None if the row is gone. Lets a caller that enqueued a STOP wait
    for the daemon to actually issue it instead of assuming the queue was drained."""
    with _conn() as c:
        row = c.execute(
            "SELECT status FROM command_queue WHERE id=?", (cmd_id,)
        ).fetchone()
        return row["status"] if row else None


def latest_port_state(device: str, port: int, since_ts: float = 0.0) -> dict | None:
    """Most recent port_state row for (device, port) observed at/after `since_ts`,
    or None. `since_ts` is the point of the whole helper: confirming a stop needs
    evidence recorded AFTER the stop went out, never a stale pre-stop row."""
    with _conn() as c:
        row = c.execute(
            "SELECT ts, device, port, work_type, level_off, level_on FROM port_state "
            "WHERE device=? AND port=? AND ts>=? ORDER BY ts DESC LIMIT 1",
            (device, port, since_ts),
        ).fetchone()
        return dict(row) if row else None


def port_confirmed_off(device: str, port: int, since_ts: float = 0.0) -> bool:
    """True only when a port_state row recorded at/after `since_ts` PROVES the port
    is not running: off-mode (work_type 0) with level_off 0.

    Deliberately narrow. An off-type work_type still programs level_off and the port
    RUNS at level_off while "off" (see safety.guard_chemical_write), so only
    work_type 0 AND level_off 0 is conclusive; every other combination -- including
    modes where level_on is what runs -- reads as "not proven off"."""
    row = latest_port_state(device, port, since_ts)
    if not row:
        return False
    return int(row["work_type"]) == 0 and int(row["level_off"]) == 0


def mark_command_done(cmd_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE command_queue SET status='done', done_at=? WHERE id=?",
            (time.time(), cmd_id),
        )


def mark_command_failed(cmd_id: int, error: str = "") -> None:
    with _conn() as c:
        c.execute(
            "UPDATE command_queue SET status='failed', done_at=?, error=? WHERE id=?",
            (time.time(), error[:500], cmd_id),
        )


def sweep_stale_sent(device: str) -> int:
    """Fail rows stuck in 'sent' longer than BLE_CMD_TTL_S. Returns the count.

    The claim commits 'sent' BEFORE the GATT write goes out, so a daemon that
    crashed between the two leaves the row 'sent' forever -- never retried,
    never failed, silently lost while the caller believes it was handled.
    Called at daemon session start so the loss becomes visible instead."""
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "UPDATE command_queue SET status='failed', done_at=?, "
            "error='stale sent row swept at daemon start -- a previous daemon "
            "likely died before/at the GATT write' "
            "WHERE status='sent' AND device=? AND sent_at<?",
            (now, device, now - _cmd_ttl_s()),
        )
        return cur.rowcount


def add_port_state(ts: float, device: str, port: int,
                   work_type: int, level_off: int, level_on: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO port_state(ts, device, port, work_type, level_off, level_on) "
            "VALUES(?,?,?,?,?,?)",
            (ts, device, port, work_type, level_off, level_on),
        )


def add_sensor_readings(ts: float, device: str, readings: list[dict]) -> None:
    """readings = [{"port": 4, "sensor_type": 0x6f, "value": 75.4}, ...]"""
    with _conn() as c:
        c.executemany(
            "INSERT INTO sensor_readings(ts, device, port, sensor_type, value) "
            "VALUES(?,?,?,?,?)",
            [(ts, device, r["port"], r["sensor_type"], r["value"]) for r in readings],
        )


def recent_sensor_snapshot(device: str, max_age_sec: float = 60.0) -> dict[int, dict]:
    """Latest reading per (port, sensor_type) within the window. Empty if no data."""
    now = time.time()
    snapshot: dict[int, dict] = {}
    with _conn() as c:
        for row in c.execute("""
            SELECT port, sensor_type, value, ts FROM sensor_readings
            WHERE device = ? AND ts > ?
            ORDER BY ts ASC
        """, (device, now - max_age_sec)):
            key = (row["port"], row["sensor_type"])
            snapshot[key] = {
                "port": row["port"], "sensor_type": row["sensor_type"],
                "value": row["value"], "ts": row["ts"],
            }
    return snapshot
