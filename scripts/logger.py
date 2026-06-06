#!/usr/bin/env python3
"""
logger.py — Persistent BLE logger for AC Infinity ACI_V3.5_CTRLER.

Runs forever:
  - Subscribes to 1EFF status packets (~1Hz), saves sensor tail to DB
  - Polls get_model_data for each port every POLL_INTERVAL seconds
  - Reconnects automatically on disconnect

Usage:
  python scripts/logger.py              # logs ports 1-8
  python scripts/logger.py --ports 1 2  # only poll ports 1 and 2

Data lands in controller.db tables:
  readings       — 1Hz sensor readings (probe raw bytes)
  port_readings  — polled per-port state (work_type, speed)
  command_queue  — picks up pending rows and issues set_level over BLE

To graph, run:
  python scripts/daily_graph.py
  python scripts/daily_graph.py --date 2026-06-02
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from ac_infinity_ble.protocol import Protocol
from aci_ble_lab.db import (
    init_schema, add_reading, add_port_reading, add_sensor_readings,
    pop_next_command, mark_command_failed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aci_logger")

ADDRESS      = "50:78:7D:C5:0C:6E"
CHAR_WRITE   = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ    = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
POLL_INTERVAL = 30   # seconds between port state polls
RECONNECT_DELAY = 15  # seconds before retry on disconnect

proto = Protocol()
TYPE_MULTIPORT = 9
TYPE_GLOBAL    = 20


def decode_sensor_tail(data: bytes) -> tuple[dict, list[dict]]:
    """
    Scan the sensor tail of a 1EFF packet for [port, type, v1, v2] groups.
    Ports 0-15 are valid; standard probes use types 0x60-0x7F, CO2 uses
    0x21, light uses 0x41.  Byte-by-byte search avoids alignment issues when
    the packet grows as more sensors are added.
    """
    legacy = {}
    sensors = []
    port_prefix = {4: "p4", 6: "p6", 7: "p7"}

    i = 100
    while i <= len(data) - 4:
        port_id     = data[i]
        sensor_type = data[i + 1]
        v1          = data[i + 2]
        v2          = data[i + 3]

        if (0 <= port_id <= 31) and (
            (0x20 <= sensor_type <= 0x7F) or sensor_type in (0x91, 0x92, 0x93)
        ):
            value = ((v1 << 8) | v2) / 100.0
            sensors.append({"port": port_id, "type": sensor_type, "value": value})
            prefix = port_prefix.get(port_id)
            if prefix:
                legacy[f"{prefix}_type"] = sensor_type
                legacy[f"{prefix}_v1"]   = v1
                legacy[f"{prefix}_v2"]   = v2
            i += 4
        else:
            i += 1

    return legacy, sensors


def decode_port_response(data: bytes) -> dict | None:
    if len(data) < 12 or data[0] != 0xA5 or data[1] != 0x1C:
        return None
    data_len = (data[2] << 8) | data[3]
    if data[9] != 1:
        return None
    payload = data[10:10 + data_len]
    result = {}
    i = 0
    while i + 2 < len(payload):
        tag = payload[i]
        length = payload[i + 1]
        if tag == 0xFF:
            break
        value = payload[i + 2:i + 2 + length]
        if tag == 0x10 and length == 1: result["work_type"] = value[0]
        if tag == 0x11 and length == 1: result["level_off"] = value[0]
        if tag == 0x12 and length == 1: result["level_on"]  = value[0]
        i += 2 + length
    return result if result else None


async def _scan_until_found(address: str, timeout: float = 60) -> bool:
    found = asyncio.Event()
    def cb(dev, _):
        if dev.address.upper() == address.upper():
            found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def run_session(poll_ports: list[int]) -> None:
    """Single BLE session: connect, collect, poll. Returns when disconnected."""
    log.info("Scanning for %s ...", ADDRESS)
    if not await _scan_until_found(ADDRESS, timeout=60):
        log.warning("Controller not found within 60s scan window.")
        return

    log.info("Connecting...")
    async with BleakClient(ADDRESS, disconnected_callback=lambda _: None) as client:
        log.info("Connected (MTU=%d)", client.mtu_size)

        a5_queue: asyncio.Queue = asyncio.Queue()
        reading_count = 0

        def on_notif(_, raw):
            d = bytes(raw)
            if d[:2] == b'\xa5\x1c':
                a5_queue.put_nowait(d)
            elif d[:2] == b'\x1e\xff':
                ts = time.time()
                legacy, sensors = decode_sensor_tail(d)
                add_reading(ts, **legacy)
                if sensors:
                    add_sensor_readings(ts, sensors)
                nonlocal reading_count
                reading_count += 1

        await asyncio.sleep(0.3)
        subscribed = False
        for attempt in range(3):
            try:
                warm = proto.get_model_data(TYPE_GLOBAL, 0, 0)
                await client.write_gatt_char(CHAR_WRITE, warm, response=False)
                await asyncio.sleep(0.4 + attempt * 0.3)
                await client.start_notify(CHAR_READ, on_notif)
                subscribed = True
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                else:
                    log.warning("Could not subscribe: %s", e)

        if not subscribed:
            return

        log.info("Subscribed. Logging... (Ctrl+C to stop)")
        seq = 1
        t_last_poll = 0.0
        t_last_log  = 0.0

        while True:
            if not client.is_connected:
                log.warning("Disconnected.")
                break

            now = time.time()

            # Periodic port polling
            if poll_ports and (now - t_last_poll) >= POLL_INTERVAL:
                t_last_poll = now
                while not a5_queue.empty():
                    a5_queue.get_nowait()
                for port in poll_ports:
                    seq += 1
                    cmd = proto.get_model_data(TYPE_MULTIPORT, port, seq)
                    try:
                        await client.write_gatt_char(CHAR_WRITE, cmd, response=False)
                        resp = await asyncio.wait_for(a5_queue.get(), 3.0)
                        state = decode_port_response(resp)
                        if state:
                            add_port_reading(time.time(), port,
                                             state.get("work_type", 0),
                                             state.get("level_off", 0),
                                             state.get("level_on",  0))
                    except (asyncio.TimeoutError, BleakError):
                        pass

            # Control command queue: pop and execute one pending command per tick
            pending = pop_next_command()
            if pending is not None:
                try:
                    seq += 1
                    ble_cmd = proto.set_level(
                        TYPE_MULTIPORT, pending["work_type"], pending["speed"], pending["port"], seq
                    )
                    await client.write_gatt_char(CHAR_WRITE, ble_cmd, response=False)
                    log.info("Control: port=%d work_type=%d speed=%d  [id=%d src=%s]",
                             pending["port"], pending["work_type"], pending["speed"],
                             pending["id"], pending.get("source", "?"))
                except Exception as e:
                    mark_command_failed(pending["id"])
                    log.warning("Control command failed (id=%d): %s", pending["id"], e)

            # Periodic console status
            if (now - t_last_log) >= 60:
                t_last_log = now
                log.info("Readings stored this session: %d", reading_count)

            await asyncio.sleep(1.0)

        try:
            await client.stop_notify(CHAR_READ)
        except Exception:
            pass


async def main(poll_ports: list[int]) -> None:
    init_schema()
    log.info("Logger started. Controller: %s  Poll ports: %s", ADDRESS, poll_ports or "none")
    while True:
        try:
            await run_session(poll_ports)
        except (BleakError, OSError) as e:
            log.warning("Session error: %s", e)
        except asyncio.CancelledError:
            log.info("Logger stopped.")
            return
        log.info("Reconnecting in %ds...", RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Persistent AC Infinity BLE data logger")
    p.add_argument("--address",  default=ADDRESS, help="Controller MAC address")
    p.add_argument("--ports",    type=int, nargs="*", default=list(range(1, 9)),
                   help="Ports to poll via get_model_data (default: 1-8)")
    p.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                   help=f"Seconds between port polls (default: {POLL_INTERVAL})")
    args = p.parse_args()
    POLL_INTERVAL = args.poll_interval
    poll_ports = [pt for pt in (args.ports or []) if pt > 0]

    try:
        asyncio.run(main(poll_ports))
    except KeyboardInterrupt:
        log.info("Stopped.")
