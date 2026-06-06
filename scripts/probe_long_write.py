#!/usr/bin/env python3
"""
probe_long_write.py — Test long writes to ff01 and 8020.

Hypothesis: The write protocol mirrors the 127-byte status packet.
  - Phone reads current 127-byte status from 70d51002
  - Phone modifies relevant bytes (port speed, mode, etc.)
  - Phone writes 127 bytes back to ff01
  - Controller applies the delta

Also tests checksum variants and the 8023 char.

This script uses the LIVE status packet as the base for writes.
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, add_command, add_note

ADDRESS = "50:78:7D:C5:0C:6E"
CHAR_AUTH   = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_STATUS = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_CMD_IN = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_CMD_OUT= "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_8020   = "00008020-0000-1000-8000-00805f9b34fb"
CHAR_8021   = "00008021-0000-1000-8000-00805f9b34fb"
CHAR_8023   = "00008023-0000-1000-8000-00805f9b34fb"
AUTH_TOKEN  = bytes.fromhex("11223344")


def xor_checksum(data: bytes) -> int:
    r = 0
    for b in data:
        r ^= b
    return r

def sum_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


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


def stable_bytes(packets: list[bytes]) -> set[int]:
    if len(packets) < 2: return set(range(len(packets[0])))
    ref = packets[0]
    return {i for i in range(len(ref)) if all(p[i] == ref[i] for p in packets[1:])}


def diff_stable(base: bytes, new: bytes, stable: set[int]) -> list[tuple]:
    return [(i, base[i], new[i]) for i in stable if i < len(base) and i < len(new) and base[i] != new[i]]


async def main():
    init_schema()
    print("\n=== Long Write Test (127B to ff01) ===\n")

    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
        await asyncio.sleep(0.3)

        status_queue: asyncio.Queue = asyncio.Queue()
        indications = []
        ff02_resp = []

        def on_status(_, d): status_queue.put_nowait(bytes(d))
        def on_ind(_, d):
            b = bytes(d)
            indications.append(b)
            print(f"  [indication] {b.hex()}")
        def on_ff02(_, d): ff02_resp.append(bytes(d))

        await client.start_notify(CHAR_STATUS, on_status)
        try: await client.start_notify(CHAR_8021, on_ind)
        except: pass
        try: await client.start_notify(CHAR_CMD_OUT, on_ff02)
        except: pass
        await asyncio.sleep(0.3)

        # Collect 5-packet baseline
        print("Collecting baseline...")
        pkts = []
        while len(pkts) < 5:
            try: pkts.append(await asyncio.wait_for(status_queue.get(), 3))
            except asyncio.TimeoutError: break
        if len(pkts) < 2:
            print("Not enough packets.")
            return

        stable = stable_bytes(pkts)
        base_pkt = pkts[-1]
        varying = sorted(set(range(len(base_pkt))) - stable)
        print(f"  Baseline: {len(pkts)} pkts, {len(stable)} stable, varying: {varying}")
        print(f"  Baseline: {base_pkt.hex()[:60]}...")

        # Drain queue
        while not status_queue.empty(): status_queue.get_nowait()

        async def test_write(char_uuid: str, payload: bytes, desc: str):
            nonlocal base_pkt
            ff02_resp.clear(); indications.clear()
            print(f"\n  [{char_uuid[:8]}] {desc}")
            print(f"    Payload ({len(payload)}B): {payload[:24].hex()}{'...' if len(payload)>24 else ''}")
            try:
                await client.write_gatt_char(char_uuid, payload, response=True)
                print(f"    Write OK")
            except Exception as e:
                print(f"    Write FAILED: {e}")
                return

            await asyncio.sleep(2.0)

            if ff02_resp:
                for r in ff02_resp:
                    b4 = r[4] if len(r) > 4 else -1
                    print(f"    ff02: {r.hex()}  b4=0x{b4:02x}")
            if indications:
                print(f"    indication: received")

            try:
                new_pkt = await asyncio.wait_for(status_queue.get(), 3)
                changes = diff_stable(base_pkt, new_pkt, stable)
                if changes:
                    print(f"    STABLE BYTES CHANGED:")
                    for off, old, new in changes:
                        print(f"      byte[{off:3d}]: 0x{old:02x} -> 0x{new:02x}")
                    base_pkt = new_pkt
                    add_command(char_uuid, payload.hex(), desc, source="long_write",
                                notes=f"Stable bytes changed: {changes}")
                else:
                    print(f"    No stable-byte changes.")
            except asyncio.TimeoutError:
                print(f"    (no status packet)")

        # ── Test 1: Mirror status packet as-is to ff01 ────────────────────────
        print("\n=== Test A: Mirror status packet to ff01 (unchanged) ===")
        await test_write(CHAR_CMD_IN, base_pkt, "mirror status → ff01 (unchanged)")

        # ── Test 2: Modify byte[27] from 0xFF to 0x01 (port 1 enable?) ───────
        print("\n=== Test B: Modify port config block byte[27] → 0x01 ===")
        mod = bytearray(base_pkt)
        mod[27] = 0x01
        await test_write(CHAR_CMD_IN, bytes(mod), "status[27]=0x01 → ff01")

        # ── Test 3: First 2 bytes of port block = port+speed ─────────────────
        print("\n=== Test C: Set bytes[27-28] to port=1 speed=5 ===")
        mod = bytearray(base_pkt)
        mod[27] = 0x01; mod[28] = 0x05
        await test_write(CHAR_CMD_IN, bytes(mod), "status[27]=0x01 [28]=0x05 → ff01")

        # ── Test 4: Try with XOR checksum appended ────────────────────────────
        print("\n=== Test D: Short command with XOR checksum ===")
        cmd = bytes.fromhex("1eff010105")
        cs = xor_checksum(cmd)
        await test_write(CHAR_CMD_IN, cmd + bytes([cs]), f"1eff010105 + XOR={cs:02x}")

        # Sum checksum
        cs_sum = sum_checksum(cmd)
        await test_write(CHAR_CMD_IN, cmd + bytes([cs_sum]), f"1eff010105 + SUM={cs_sum:02x}")

        # ── Test 5: Write to 8023 (Per-Port Channel D, write+indicate) ────────
        print("\n=== Test E: Write to 8023 ===")
        for payload, desc in [
            (bytes([0x01, 0x05]), "8023: port=1 speed=5 (2B)"),
            (bytes([0x01, 0x00, 0x05, 0x00]), "8023: port=1, 00, speed=5 (4B)"),
            (bytes.fromhex("1eff010105"), "8023: 1EFF cmd=01 port=1 speed=5"),
            (bytes(12), "8023: 12 zero bytes"),
        ]:
            await test_write(CHAR_8023, payload, desc)

        # ── Test 6: 8020 with full 600-byte write (all ports config) ─────────
        print("\n=== Test F: 8020 with 75-byte single-port config ===")
        # If 8020 = 8 ports x 75 bytes, write just the first port's block
        # Hypothesis: first 12 bytes of each 75-byte block = basic config
        port1_config = bytearray(75)
        port1_config[0] = 0x01  # port = 1
        port1_config[1] = 0x05  # speed = 5
        await test_write(CHAR_8020, bytes(port1_config), "8020: 75B port1 config (b0=1, b1=5)")

        for uuid in (CHAR_STATUS, CHAR_CMD_OUT, CHAR_8021):
            try: await client.stop_notify(uuid)
            except: pass

        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
