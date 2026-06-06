#!/usr/bin/env python3
"""
probe_checksum.py — Find the checksum algorithm by sweeping all 256 values.

Hypothesis: commands need a checksum byte appended to be valid.
  - Try 1EFF010105XX for XX = 00..FF
  - Any response with b4 != 0x00 reveals the correct checksum byte
  - Try multiple base command lengths to confirm the pattern

Also tests common checksums on valid-looking commands.
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, add_note

ADDRESS    = "50:78:7D:C5:0C:6E"
CHAR_AUTH  = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_CMD_IN  = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_CMD_OUT = "0000ff02-0000-1000-8000-00805f9b34fb"
AUTH_TOKEN = bytes.fromhex("11223344")

# Base commands to sweep (checksum byte appended as byte N+1)
BASE_CMDS = [
    (bytes.fromhex("1eff010105"), "1EFF cmd=01 port=1 speed=5"),
    (bytes.fromhex("1eff020105"), "1EFF cmd=02 port=1 speed=5"),
    (bytes.fromhex("1eff030105"), "1EFF cmd=03 port=1 speed=5"),
    (bytes.fromhex("1eff0100"),   "1EFF cmd=01 query"),
]


async def _scan_until_found(address: str) -> bool:
    found = asyncio.Event()
    def cb(dev, _):
        if dev.address.upper() == address.upper(): found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), 60)
            return True
        except asyncio.TimeoutError:
            return False


async def sweep_checksum(client, base: bytes, desc: str) -> list[tuple]:
    """Sweep all 256 checksum bytes appended to base. Returns list of (byte, response) anomalies."""
    print(f"\n  Sweeping checksum for: {base.hex()}  ({desc})")
    anomalies = []
    responses = []

    for cs in range(256):
        cmd = base + bytes([cs])
        resp_buf = []

        def _on_resp(_, d): resp_buf.append(bytes(d))

        try:
            await client.write_gatt_char(CHAR_CMD_IN, cmd, response=False)
        except Exception as e:
            print(f"    Write error at cs=0x{cs:02x}: {e}")
            break

        await asyncio.sleep(0.18)  # ~180ms gap between commands

        # Collect any pending responses (they arrive async)
        await asyncio.sleep(0)  # yield to event loop

        responses.append(resp_buf.copy())

    # The responses arrive via notification — collect them differently
    # (we need to re-run with a shared collection)
    return anomalies


async def main():
    init_schema()
    print("\n=== Checksum Sweep ===\n")

    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
        await asyncio.sleep(0.3)
        print("Auth written.")

        resp_queue: asyncio.Queue = asyncio.Queue()

        def on_resp(_, d):
            resp_queue.put_nowait(bytes(d))

        try:
            await client.start_notify(CHAR_CMD_OUT, on_resp)
            print("Subscribed to ff02.\n")
        except Exception as e:
            print(f"ff02 sub failed: {e}")
            return

        for base, desc in BASE_CMDS:
            print(f"Sweep: {base.hex()}  ({desc})")
            anomalies = []
            last_b4 = None

            for cs in range(256):
                # Drain old responses
                while not resp_queue.empty():
                    resp_queue.get_nowait()

                cmd = base + bytes([cs])
                try:
                    await client.write_gatt_char(CHAR_CMD_IN, cmd, response=False)
                except Exception as e:
                    print(f"  write error cs=0x{cs:02x}: {e}")
                    break

                await asyncio.sleep(0.25)

                try:
                    resp = await asyncio.wait_for(resp_queue.get(), timeout=0.3)
                    b4 = resp[4] if len(resp) > 4 else -1
                    if b4 != 0:
                        anomalies.append((cs, resp.hex(), b4))
                        print(f"  cs=0x{cs:02x}  resp={resp.hex()}  b4=0x{b4:02x}  *** ANOMALY ***")
                    elif cs % 32 == 0:
                        print(f"  cs=0x{cs:02x}  resp={resp.hex()}  (b4=0x00)")
                except asyncio.TimeoutError:
                    print(f"  cs=0x{cs:02x}  NO RESPONSE")

            if anomalies:
                print(f"\n  === ANOMALIES for {base.hex()} ===")
                for cs, resp, b4 in anomalies:
                    print(f"    cs=0x{cs:02x}  resp={resp}  b4=0x{b4:02x}")
                add_note("checksum_anomalies",
                         f"Checksum sweep on {base.hex()}: anomalies at cs=" +
                         ", ".join(f"0x{c:02x}(b4={b})" for c, _, b in anomalies),
                         confidence="hypothesis")
            else:
                print(f"  No anomalies — checksum byte may not be the issue")

            print()

        try: await client.stop_notify(CHAR_CMD_OUT)
        except: pass


if __name__ == "__main__":
    asyncio.run(main())
