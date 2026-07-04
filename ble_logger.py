#!/usr/bin/env python3
"""
ble_logger.py -- persistent BLE telemetry + command executor for ONE AC Infinity
controller.

Connects to the controller's BLE radio, subscribes to its 1Hz status packets,
periodically polls per-port state, and drains the SQLite command queue
(`aci_ble_lab.db.command_queue`).

Defense in depth: every write is guarded BOTH at enqueue (by `aci_ble_lab.db`)
AND here at the executor (by `aci_ble_lab.safety.guard_chemical_write`). A row
that became stale during a freeze is dropped at this layer.

Multi-controller setups: run one daemon per controller (systemd template unit
or shell wrapper). Each instance is keyed by --device-name; queue rows are
filtered by that name so daemons don't steal each other's commands.

Address resolution (in order):
  --address CLI flag
  $BLE_<SLUG>_MAC  env (slug = name_slug(--device-name))
  $BLE_DEFAULT_MAC env
  fail
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

# Load the same env files the poller/config layer loads. The daemon runs as
# its own process -- without this, DOSER_PORTS_<SLUG> / PH_PORTS_<SLUG> /
# CO2_VALVE are unset, the executor-side chemical guard classifies every port
# as climate (silently voided), and BLE_<SLUG>_MAC resolution can't work.
ENV_PATH    = Path(__file__).resolve().parent / ".env"
LABELS_PATH = Path(__file__).resolve().parent / "labels.env"
load_dotenv(ENV_PATH)
load_dotenv(LABELS_PATH)

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from ac_infinity_ble.protocol import Protocol
from ac_infinity_ble.util import crc16

from utils import name_slug
from ac_infinity_client import SENSOR_TYPE
from aci_ble_lab import db
from aci_ble_lab.safety import SafetyBlocked, guard_chemical_write

CHAR_WRITE = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"

TYPE_MULTIPORT = 9
TYPE_GLOBAL    = 20

DEFAULT_POLL_SEC      = 30
DEFAULT_RECONNECT_SEC = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ble_logger")


def resolve_address(device_name: str, cli_address: str | None) -> str:
    if cli_address:
        return cli_address.upper()
    per_device = os.getenv(f"BLE_{name_slug(device_name)}_MAC", "").strip()
    if per_device:
        return per_device.upper()
    fallback = os.getenv("BLE_DEFAULT_MAC", "").strip()
    if fallback:
        return fallback.upper()
    raise SystemExit(
        f"No BLE address for device {device_name!r}. Pass --address, or set "
        f"BLE_{name_slug(device_name)}_MAC / BLE_DEFAULT_MAC in .env."
    )


# port_id in the sensor tail == the cloud sensorType integer, so the per-type
# divisors from ac_infinity_client.SENSOR_TYPE apply (EC uS/cm + TDS ppm are
# /10, most others /100 -- never a blanket /100). Zero-value handling mirrors
# _parse_sensors: 0 means "no reading" except light(12)/water_level(20) where
# 0 is legitimate (lights off, empty reservoir).
_ALLOW_ZERO_TYPES = {12, 20}


def decode_sensor_tail(data: bytes) -> list[dict]:
    """Scan a 1EFF status packet's sensor tail for [port, type, v1, v2] groups.

    Values are signed int16 scaled by the per-type divisor from
    ac_infinity_client.SENSOR_TYPE (fallback /100 for unknown types, matching
    the old behavior). The no-sensor sentinels (-32768/0x8000, and 0 outside
    _ALLOW_ZERO_TYPES) are SKIPPED, not stored -- a disconnected probe must
    not poison sensor_readings with phantom values like pH 327.68."""
    out: list[dict] = []
    i = 100
    while i <= len(data) - 4:
        port_id, sensor_type, v1, v2 = data[i], data[i + 1], data[i + 2], data[i + 3]
        if (0 <= port_id <= 31) and (
            (0x20 <= sensor_type <= 0x7F) or sensor_type in (0x91, 0x92, 0x93)
        ):
            raw = (v1 << 8) | v2
            if raw >= 0x8000:
                raw -= 0x10000                      # signed int16
            if raw != -32768 and (raw != 0 or port_id in _ALLOW_ZERO_TYPES):
                _, divisor = SENSOR_TYPE.get(port_id, ("", 100.0))
                out.append({
                    "port": port_id, "sensor_type": sensor_type,
                    "value": raw / divisor,
                })
            i += 4                                  # group consumed either way
        else:
            i += 1
    return out


def frame_seq(data: bytes) -> int | None:
    """Echoed request sequence from an A5 response header, or None.

    The A5 header is `A5 xx [len16] [seq16] [crc16] ...` where the crc16
    covers bytes 0-5 (same layout Protocol._add_head builds). Only trust the
    seq bytes when that header CRC verifies, so an unknown firmware variant
    degrades to order-based matching instead of dropping every poll."""
    if len(data) < 8:
        return None
    if crc16(list(data[:6])) != [data[6], data[7]]:
        return None
    return (data[4] << 8) | data[5]


def _next_seq(seq: int) -> int:
    """Advance the request sequence, wrapped to the 16-bit field the protocol
    header actually carries (Protocol._add_head truncates to two bytes). An
    unbounded counter would stop matching frame_seq echoes after 65535 writes
    -- a few days of 24/7 polling -- and every poll would then look 'late'."""
    return (seq + 1) & 0xFFFF


async def _next_a5(queue: "asyncio.Queue[bytes]", seq: int) -> bytes:
    """Next A5 frame that belongs to request `seq`.

    decode_port_response carries no port identifier, so matching by queue
    order mis-attributes a late frame (one port times out, its A5 lands in
    the NEXT port's window and gets credited to the wrong port). Frames whose
    verifiable echoed sequence differs from `seq` are dropped; frames without
    a verifiable header (frame_seq None) are accepted as before."""
    while True:
        frame = await queue.get()
        echoed = frame_seq(frame)
        if echoed is None or echoed == seq:
            return frame
        log.warning("Dropped late A5 frame (echoed seq=%d, expected %d)", echoed, seq)


def decode_port_response(data: bytes) -> dict | None:
    if len(data) < 12 or data[0] != 0xA5 or data[1] != 0x1C:
        return None
    data_len = (data[2] << 8) | data[3]
    if data[9] != 1:
        return None
    payload = data[10:10 + data_len]
    result: dict = {}
    i = 0
    while i + 2 < len(payload):
        tag, length = payload[i], payload[i + 1]
        if tag == 0xFF:
            break
        value = payload[i + 2:i + 2 + length]
        if tag == 0x10 and length == 1: result["work_type"] = value[0]
        if tag == 0x11 and length == 1: result["level_off"] = value[0]
        if tag == 0x12 and length == 1: result["level_on"]  = value[0]
        i += 2 + length
    return result or None


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


async def _pop_filtered(device_name: str) -> dict | None:
    """Claim the next command for THIS device (device-scoped atomic claim), or None.

    The shared queue can carry rows for multiple controllers; the device-scoped claim
    simply never touches another daemon's rows -- they stay 'pending' for their own
    daemon (no cross-device requeue, so no livelock / unbounded queue growth / requeue
    crash). A stale chemical-START row is rejected here (marked failed) so the
    interlock is honored even if the row predates an env/safety change -- chemical
    STOPS (speed 0) always drain, freeze or not. A transient sqlite3 error (e.g. a
    lock timeout from another daemon) skips this drain tick instead of killing the
    session."""
    while True:
        try:
            row = db.claim_next_command(device_name)
        except sqlite3.Error as e:
            log.warning("Queue claim failed (%s); skipping this drain tick.", e)
            return None
        if row is None:
            return None
        try:
            guard_chemical_write(row["device"], row["port"],
                                 work_type=row["work_type"], speed=row["speed"])
        except SafetyBlocked as e:
            db.mark_command_failed(row["id"], str(e))
            log.warning("Executor dropped row %d: %s", row["id"], e)
            continue
        return row


async def _run_session(device_name: str, address: str,
                       poll_ports: list[int], poll_sec: int) -> None:
    # Rows a dead daemon left in 'sent' (claim commits BEFORE the GATT write)
    # would otherwise be lost silently -- fail them so the loss is visible.
    swept = db.sweep_stale_sent(device_name)
    if swept:
        log.warning("Failed %d stale 'sent' row(s) from a previous daemon run.", swept)

    log.info("Scanning for %s (device=%s) ...", address, device_name)
    if not await _scan_until_found(address, timeout=60):
        log.warning("Controller not found within 60s scan window.")
        return

    log.info("Connecting...")
    async with BleakClient(address, disconnected_callback=lambda _: None) as client:
        log.info("Connected (MTU=%d)", client.mtu_size)
        proto = Protocol()
        a5_queue: asyncio.Queue[bytes] = asyncio.Queue()
        reading_count = 0

        def on_notif(_handle, raw: bytearray) -> None:
            d = bytes(raw)
            if d[:2] == b"\xa5\x1c":
                a5_queue.put_nowait(d)
            elif d[:2] == b"\x1e\xff":
                sensors = decode_sensor_tail(d)
                if sensors:
                    try:
                        db.add_sensor_readings(time.time(), device_name, sensors)
                    except sqlite3.Error as e:
                        log.warning("Sensor write skipped: %s", e)
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
                if attempt >= 2:
                    log.warning("Could not subscribe: %s", e)
                else:
                    await asyncio.sleep(0.5)

        if not subscribed:
            return

        log.info("Subscribed. Polling=%s every %ds.  Ctrl+C to stop.",
                 poll_ports or "off", poll_sec)
        seq = 1
        t_last_poll = 0.0
        t_last_log  = 0.0

        while client.is_connected:
            now = time.time()

            if poll_ports and (now - t_last_poll) >= poll_sec:
                t_last_poll = now
                for port in poll_ports:
                    seq = _next_seq(seq)
                    # Flush before EACH request (not once per sweep) and match
                    # the response by echoed seq -- a late frame from a
                    # timed-out port must never be credited to the next one.
                    while not a5_queue.empty():
                        a5_queue.get_nowait()
                    try:
                        await client.write_gatt_char(
                            CHAR_WRITE,
                            proto.get_model_data(TYPE_MULTIPORT, port, seq),
                            response=False,
                        )
                        resp = await asyncio.wait_for(_next_a5(a5_queue, seq), 3.0)
                        state = decode_port_response(resp)
                        if state:
                            db.add_port_state(
                                time.time(), device_name, port,
                                state.get("work_type", 0),
                                state.get("level_off", 0),
                                state.get("level_on",  0),
                            )
                    except (asyncio.TimeoutError, BleakError):
                        pass

            pending = await _pop_filtered(device_name)
            if pending is not None:
                try:
                    seq = _next_seq(seq)
                    ble_cmd = proto.set_level(
                        TYPE_MULTIPORT,
                        pending["work_type"], pending["speed"],
                        pending["port"], seq,
                    )
                    await client.write_gatt_char(CHAR_WRITE, ble_cmd, response=False)
                    db.mark_command_done(pending["id"])
                    log.info("Wrote port=%d work_type=%d speed=%d [id=%d src=%s]",
                             pending["port"], pending["work_type"], pending["speed"],
                             pending["id"], pending.get("source", "?"))
                except Exception as e:
                    db.mark_command_failed(pending["id"], str(e))
                    log.warning("Write failed (id=%d): %s", pending["id"], e)

            if (now - t_last_log) >= 60:
                t_last_log = now
                log.info("Session readings: %d", reading_count)

            await asyncio.sleep(1.0)

        log.warning("Disconnected.")
        try:
            await client.stop_notify(CHAR_READ)
        except Exception:
            pass


async def _main(device_name: str, address: str, poll_ports: list[int],
                poll_sec: int, reconnect_sec: int) -> None:
    db.init_schema()
    log.info("ble_logger started.  device=%s address=%s  poll_ports=%s",
             device_name, address, poll_ports or "none")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    while not stop.is_set():
        try:
            await _run_session(device_name, address, poll_ports, poll_sec)
        except (BleakError, OSError, sqlite3.Error) as e:
            # sqlite3.Error: transient contention on the shared DB (e.g.
            # BEGIN IMMEDIATE lock timeout) must back off + retry like a BLE
            # drop, not kill the daemon -- queued stops still need draining.
            log.warning("Session error: %s", e)
        except asyncio.CancelledError:
            break
        if stop.is_set():
            break
        log.info("Reconnect in %ds ...", reconnect_sec)
        try:
            await asyncio.wait_for(stop.wait(), timeout=reconnect_sec)
        except asyncio.TimeoutError:
            pass

    log.info("ble_logger stopped.")


def main() -> None:
    p = argparse.ArgumentParser(description="Persistent AC Infinity BLE logger / executor")
    p.add_argument("--device-name", required=True,
                   help="Device name as it appears in the AC Infinity app (e.g. '4 x 4'). "
                        "Used to filter command_queue rows and tag readings.")
    p.add_argument("--address", default=None,
                   help="Controller BLE MAC. Defaults to BLE_<SLUG>_MAC then BLE_DEFAULT_MAC.")
    p.add_argument("--ports", type=int, nargs="*", default=list(range(1, 9)),
                   help="Ports to poll via get_model_data (default: 1-8)")
    p.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_SEC,
                   help=f"Seconds between port polls (default {DEFAULT_POLL_SEC})")
    p.add_argument("--reconnect-interval", type=int, default=DEFAULT_RECONNECT_SEC,
                   help=f"Seconds between reconnect attempts (default {DEFAULT_RECONNECT_SEC})")
    args = p.parse_args()

    address = resolve_address(args.device_name, args.address)
    poll_ports = [pt for pt in (args.ports or []) if pt > 0]
    try:
        asyncio.run(_main(args.device_name, address, poll_ports,
                          args.poll_interval, args.reconnect_interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
