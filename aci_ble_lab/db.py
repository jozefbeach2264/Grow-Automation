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

    Raises ValueError on out-of-range inputs and SafetyBlocked on a chemical
    write during a dosing freeze."""
    if not (0 <= port <= 15):
        raise ValueError(f"port {port} out of range (0-15)")
    if not (0 <= speed <= 10):
        raise ValueError(f"speed {speed} out of range (0-10)")
    if work_type not in _VALID_WORK_TYPES:
        raise ValueError(f"work_type {work_type} not in {sorted(_VALID_WORK_TYPES)}")
    if not device:
        raise ValueError("device name is required")

    guard_chemical_write(device, port)

    with _conn() as c:
        r = c.execute(
            "INSERT INTO command_queue(ts, device, port, work_type, speed, source) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), device, port, work_type, speed, source),
        )
        return r.lastrowid


def pop_next_command() -> dict | None:
    """Atomically claim the oldest pending command. Returns the row dict or None."""
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
