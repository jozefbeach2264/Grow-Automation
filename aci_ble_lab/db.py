"""
Persistent SQLite knowledge base for the AC Infinity controller.

Tables
------
controller          - Device identity and hardware profile
services            - GATT services
characteristics     - GATT characteristics with known semantics
status_fields       - Decoded fields in the 127-byte status packet
commands            - Known write commands (hex + description)
protocol_notes      - Free-form findings and hypotheses
capture_sessions    - Metadata about .jsonl capture files
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "controller.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_schema() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS controller (
            id              INTEGER PRIMARY KEY,
            address         TEXT NOT NULL,
            name            TEXT,
            model           TEXT,
            hw_revision     TEXT,
            sw_revision     TEXT,
            chip_vendor     TEXT,
            chip_family     TEXT,
            bt_stack        TEXT,
            mtu             INTEGER,
            company_id      TEXT,
            oui             TEXT,
            notes           TEXT,
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS services (
            uuid            TEXT PRIMARY KEY,
            description     TEXT,
            handle          INTEGER,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS characteristics (
            uuid            TEXT PRIMARY KEY,
            service_uuid    TEXT REFERENCES services(uuid),
            description     TEXT,
            handle          INTEGER,
            properties      TEXT,   -- JSON array e.g. ["read","notify"]
            initial_hex     TEXT,   -- value from first inspect
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS status_fields (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            byte_offset     INTEGER NOT NULL,
            byte_length     INTEGER NOT NULL DEFAULT 1,
            field_name      TEXT NOT NULL,
            description     TEXT,
            encoding        TEXT,   -- uint8, uint16_be, int16_be, etc.
            unit            TEXT,
            scale           REAL DEFAULT 1.0,
            min_observed    REAL,
            max_observed    REAL,
            example_values  TEXT,   -- JSON
            confidence      TEXT DEFAULT 'hypothesis',  -- hypothesis/confirmed/verified
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS commands (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            char_uuid       TEXT NOT NULL,
            hex_data        TEXT NOT NULL,
            description     TEXT,
            setting_changed TEXT,
            response_hex    TEXT,
            source          TEXT,   -- mitm/probe/community
            confirmed       INTEGER DEFAULT 0,
            timestamp       TEXT,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS protocol_notes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            topic           TEXT NOT NULL,
            detail          TEXT NOT NULL,
            confidence      TEXT DEFAULT 'hypothesis',
            created_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS capture_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            path            TEXT NOT NULL,
            started_at      TEXT,
            ended_at        TEXT,
            packet_count    INTEGER,
            description     TEXT
        );

        -- Legacy fixed-column readings (kept for historical data)
        CREATE TABLE IF NOT EXISTS readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            p4_type     INTEGER,
            p4_v1       INTEGER,
            p4_v2       INTEGER,
            p6_type     INTEGER,
            p6_v1       INTEGER,
            p6_v2       INTEGER,
            p7_type     INTEGER,
            p7_v1       INTEGER,
            p7_v2       INTEGER
        );
        CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);

        -- Normalized sensor readings: one row per sensor per timestamp
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            port        INTEGER NOT NULL,
            sensor_type INTEGER NOT NULL,
            value       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sensor_readings_ts   ON sensor_readings(ts);
        CREATE INDEX IF NOT EXISTS sensor_readings_port ON sensor_readings(port);

        -- Per-port state polled via get_model_data
        CREATE TABLE IF NOT EXISTS port_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            port        INTEGER NOT NULL,
            work_type   INTEGER,
            level_off   INTEGER,
            level_on    INTEGER
        );
        CREATE INDEX IF NOT EXISTS port_readings_ts ON port_readings(ts);

        -- Cloud API sensor readings (polled by cloud_ingest.py)
        -- sensor_id matches BLE port_id and cloud API sensorType — same numbering.
        -- Values stored in same /100 units as sensor_readings for direct comparison.
        CREATE TABLE IF NOT EXISTS cloud_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            dev_id      TEXT NOT NULL,
            dev_name    TEXT,
            sensor_id   INTEGER NOT NULL,
            value       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cloud_readings_ts        ON cloud_readings(ts);
        CREATE INDEX IF NOT EXISTS cloud_readings_sensor_id ON cloud_readings(sensor_id);
        """)


# ── Helpers ──────────────────────────────────────────────────────────────────

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_controller(**kwargs) -> None:
    kwargs["updated_at"] = now()
    with _conn() as c:
        existing = c.execute("SELECT id FROM controller LIMIT 1").fetchone()
        if existing:
            sets = ", ".join(f"{k}=:{k}" for k in kwargs)
            c.execute(f"UPDATE controller SET {sets} WHERE id={existing['id']}", kwargs)
        else:
            cols = ", ".join(kwargs.keys())
            vals = ", ".join(f":{k}" for k in kwargs)
            c.execute(f"INSERT INTO controller ({cols}) VALUES ({vals})", kwargs)


def upsert_service(uuid: str, description: str, handle: int = None, notes: str = None) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO services(uuid,description,handle,notes)
            VALUES(:uuid,:description,:handle,:notes)
            ON CONFLICT(uuid) DO UPDATE SET
                description=excluded.description,
                handle=coalesce(excluded.handle, handle),
                notes=coalesce(excluded.notes, notes)
        """, dict(uuid=uuid, description=description, handle=handle, notes=notes))


def upsert_char(uuid: str, service_uuid: str, description: str,
                handle: int = None, properties: list = None,
                initial_hex: str = None, notes: str = None) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO characteristics(uuid,service_uuid,description,handle,properties,initial_hex,notes)
            VALUES(:uuid,:svc,:desc,:handle,:props,:hex,:notes)
            ON CONFLICT(uuid) DO UPDATE SET
                description=excluded.description,
                handle=coalesce(excluded.handle, handle),
                properties=coalesce(excluded.properties, properties),
                initial_hex=coalesce(excluded.initial_hex, initial_hex),
                notes=coalesce(excluded.notes, notes)
        """, dict(uuid=uuid, svc=service_uuid, desc=description, handle=handle,
                  props=json.dumps(properties) if properties else None,
                  hex=initial_hex, notes=notes))


def add_status_field(byte_offset: int, byte_length: int, field_name: str,
                     description: str = None, encoding: str = "uint8",
                     unit: str = None, scale: float = 1.0,
                     min_observed: float = None, max_observed: float = None,
                     example_values: list = None,
                     confidence: str = "hypothesis", notes: str = None) -> int:
    with _conn() as c:
        r = c.execute("""
            INSERT INTO status_fields
            (byte_offset,byte_length,field_name,description,encoding,unit,scale,
             min_observed,max_observed,example_values,confidence,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (byte_offset, byte_length, field_name, description, encoding, unit, scale,
              min_observed, max_observed,
              json.dumps(example_values) if example_values else None,
              confidence, notes))
        return r.lastrowid


def add_command(char_uuid: str, hex_data: str, description: str,
                setting_changed: str = None, response_hex: str = None,
                source: str = "probe", confirmed: bool = False, notes: str = None) -> int:
    with _conn() as c:
        r = c.execute("""
            INSERT INTO commands(char_uuid,hex_data,description,setting_changed,
                                 response_hex,source,confirmed,timestamp,notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (char_uuid, hex_data, description, setting_changed, response_hex,
              source, int(confirmed), now(), notes))
        return r.lastrowid


def add_cloud_readings(ts: float, dev_id: str, dev_name: str, readings: dict) -> None:
    """
    Write cloud API sensor readings to cloud_readings table.
    readings: {sensor_id: value} — values already scaled to /100 units (same as BLE).
    CO2 (sensor_id 11) and light (12) arrive as raw units from cloud; caller must divide
    by 100 before passing here so both sources stay in the same unit space.
    """
    with _conn() as c:
        c.executemany(
            "INSERT INTO cloud_readings(ts,dev_id,dev_name,sensor_id,value) VALUES(?,?,?,?,?)",
            [(ts, dev_id, dev_name, sid, val) for sid, val in readings.items()]
        )


def build_unified_snapshot(ble_max_age: int = 60, cloud_max_age: int = 300) -> dict:
    """
    Merge BLE and cloud sensor readings into one snapshot.

    Returns {sensor_id: {"value": float, "source": "ble"|"cloud", "ts": float, "dev_name": str|None}}

    BLE is preferred when a reading exists within ble_max_age seconds (higher frequency,
    local, no cloud dependency). Cloud fills in any sensor_id not covered by BLE within
    cloud_max_age seconds — this includes sensors on controllers without BLE, doser port
    states, and sensors from multi-controller setups.
    """
    import time as _time
    now = _time.time()
    snapshot: dict = {}

    with _conn() as c:
        for row in c.execute("""
            SELECT port AS sensor_id, AVG(value) AS value, MAX(ts) AS ts
            FROM sensor_readings
            WHERE ts > ?
            GROUP BY port
        """, (now - ble_max_age,)).fetchall():
            snapshot[row["sensor_id"]] = {
                "value": round(row["value"], 3),
                "source": "ble",
                "ts": row["ts"],
                "dev_name": None,
            }

        for row in c.execute("""
            SELECT sensor_id, AVG(value) AS value, MAX(ts) AS ts, dev_name
            FROM cloud_readings
            WHERE ts > ?
            GROUP BY sensor_id
        """, (now - cloud_max_age,)).fetchall():
            if row["sensor_id"] not in snapshot:
                snapshot[row["sensor_id"]] = {
                    "value": round(row["value"], 3),
                    "source": "cloud",
                    "ts": row["ts"],
                    "dev_name": row["dev_name"],
                }

    return snapshot


def add_sensor_readings(ts: float, sensors: list[dict]) -> None:
    """Insert one row per sensor: sensors = [{"port":4,"type":0x6f,"value":75.4}, ...]"""
    with _conn() as c:
        c.executemany(
            "INSERT INTO sensor_readings(ts,port,sensor_type,value) VALUES(?,?,?,?)",
            [(ts, s["port"], s["type"], s["value"]) for s in sensors]
        )


def query_sensor_readings(start_ts: float, end_ts: float) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT ts, port, sensor_type, value FROM sensor_readings "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts)
        ).fetchall()


def add_reading(ts: float,
                p4_type=None, p4_v1=None, p4_v2=None,
                p6_type=None, p6_v1=None, p6_v2=None,
                p7_type=None, p7_v1=None, p7_v2=None) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO readings(ts,p4_type,p4_v1,p4_v2,p6_type,p6_v1,p6_v2,p7_type,p7_v1,p7_v2)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (ts, p4_type, p4_v1, p4_v2, p6_type, p6_v1, p6_v2, p7_type, p7_v1, p7_v2))


def add_port_reading(ts: float, port: int, work_type: int, level_off: int, level_on: int) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO port_readings(ts,port,work_type,level_off,level_on)
            VALUES (?,?,?,?,?)
        """, (ts, port, work_type, level_off, level_on))


def query_readings(start_ts: float, end_ts: float) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM readings WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts)
        ).fetchall()


def query_port_readings(start_ts: float, end_ts: float, port: int = None) -> list:
    with _conn() as c:
        if port is not None:
            return c.execute(
                "SELECT * FROM port_readings WHERE ts BETWEEN ? AND ? AND port=? ORDER BY ts",
                (start_ts, end_ts, port)
            ).fetchall()
        return c.execute(
            "SELECT * FROM port_readings WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts)
        ).fetchall()


def add_note(topic: str, detail: str, confidence: str = "hypothesis") -> int:
    with _conn() as c:
        r = c.execute("""
            INSERT INTO protocol_notes(topic,detail,confidence,created_at)
            VALUES (?,?,?,?)
        """, (topic, detail, confidence, now()))
        return r.lastrowid


def dump_summary() -> str:
    lines = []
    with _conn() as c:
        ctrl = c.execute("SELECT * FROM controller LIMIT 1").fetchone()
        if ctrl:
            lines.append("=== Controller ===")
            for k in ctrl.keys():
                v = ctrl[k]
                if v is not None:
                    lines.append(f"  {k:16}: {v}")

        svcs = c.execute("SELECT * FROM services ORDER BY handle").fetchall()
        lines.append(f"\n=== Services ({len(svcs)}) ===")
        for s in svcs:
            lines.append(f"  {s['uuid']}  {s['description'] or ''}")

        chars = c.execute("SELECT * FROM characteristics ORDER BY handle").fetchall()
        lines.append(f"\n=== Characteristics ({len(chars)}) ===")
        for ch in chars:
            props = json.loads(ch['properties'] or '[]')
            lines.append(f"  {ch['uuid']}  [{','.join(props)}]  {ch['description'] or ''}")

        fields = c.execute("SELECT * FROM status_fields ORDER BY byte_offset").fetchall()
        lines.append(f"\n=== Status Packet Fields ({len(fields)}) ===")
        for f in fields:
            lines.append(f"  byte[{f['byte_offset']:3d}+{f['byte_length']}]  "
                         f"{f['field_name']:30}  {f['encoding']:10}  "
                         f"{f['confidence']:12}  {f['description'] or ''}")

        cmds = c.execute("SELECT * FROM commands ORDER BY id").fetchall()
        lines.append(f"\n=== Known Commands ({len(cmds)}) ===")
        for cmd in cmds:
            conf = "✓" if cmd['confirmed'] else "?"
            lines.append(f"  [{conf}] {cmd['char_uuid'][:8]}  {cmd['hex_data'][:32]}  {cmd['description']}")

        notes = c.execute("SELECT * FROM protocol_notes ORDER BY id").fetchall()
        lines.append(f"\n=== Protocol Notes ({len(notes)}) ===")
        for n in notes:
            lines.append(f"  [{n['confidence']}] {n['topic']}: {n['detail']}")

    return "\n".join(lines)
