#!/usr/bin/env python3
"""
probe_8020_write.py — Test writing to 00008020 (Per-Port Channel A, write+indicate).

The 8020 char reads as 600 bytes of zeros (8 ports x 75 bytes).
Hypothesis: write a 12-byte port config to activate a port.

Strategy:
  1. Subscribe to 8021 (indicate) to catch any pushed config updates
  2. Subscribe to status (70d51002) to detect config-block changes
  3. Establish stable-byte baseline (5 status packets)
  4. Write test payloads to 8020
  5. Report changes in STABLE bytes (not sensor bytes)

Also tests 8023 (write+indicate), which may be the single-port command char.
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
CHAR_AUTH    = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_STATUS  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_CMD_IN  = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_CMD_OUT = "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_8020    = "00008020-0000-1000-8000-00805f9b34fb"
CHAR_8021    = "00008021-0000-1000-8000-00805f9b34fb"
CHAR_8022    = "00008022-0000-1000-8000-00805f9b34fb"
CHAR_8023    = "00008023-0000-1000-8000-00805f9b34fb"
AUTH_TOKEN   = bytes.fromhex("11223344")

# Port config block in status packet starts at byte 27
# Pattern when all ports unconfigured: repeating ffff000001f00000ffffffff
# Testing hypothesis: port config is 11 or 12 bytes, first bytes identify port and speed

# Test payloads for 8020 write — vary byte 0 (port index) and byte 4/5 (speed?)
WRITES_8020 = [
    # Keep everything at zero — just check if ANY write triggers an indication on 8021
    (bytes(12),              "12 zero bytes — minimal write"),
    (bytes(20),              "20 zero bytes — matches 8021 initial value"),
    # Try ac infinity typical config: modify just a few bytes from the default pattern
    # Default: ff ff 00 00 01 f0 00 00 ff ff ff ff
    # Hypothesis: byte[0]=0xFF means "disabled", byte[0]=0x01 means "enabled port 1"
    (bytes.fromhex("010000000000000000000000"), "port=01, rest=zeros (12B)"),
    (bytes.fromhex("0100000000000000"),          "port=01, rest=zeros (8B)"),
    # Try writing what looks like a speed value
    (bytes.fromhex("01000500000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"),
     "75B config: port=1, speed=5, rest=zeros"),
]

# ff01 command variants to test alongside writes
FF01_CMDS = [
    (bytes.fromhex("1eff0100"), "1EFF cmd=01 query (stable baseline check)"),
    # Try set-style: 1EFF [cmd] [port] [speed]
    (bytes.fromhex("1eff010105"), "1EFF cmd=01 port=01 speed=05"),
    (bytes.fromhex("1eff020105"), "1EFF cmd=02 port=01 speed=05"),
    (bytes.fromhex("1eff050105"), "1EFF cmd=05 port=01 speed=05"),
    (bytes.fromhex("1eff060105"), "1EFF cmd=06 port=01 speed=05"),
    # Try port speed set in ac-infinity-style encoding
    (bytes.fromhex("1eff0101050000"), "1EFF cmd=01 port=01 speed=05 +2B"),
    (bytes.fromhex("1eff030105"), "1EFF cmd=03 port=01 speed=05"),
    # Longer: 1EFF + full port config (hypothesis: mirror the status packet format)
    (bytes.fromhex("1eff" + "01" + "01" + "05" + "00" * 5), "1EFF cmd=01 port=01 speed=05 pad(8B)"),
]


async def _scan_until_found(address: str) -> bool:
    found = asyncio.Event()
    def cb(dev, _):
        if dev.address.upper() == address.upper():
            found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), 60)
            return True
        except asyncio.TimeoutError:
            return False


def stable_bytes(packets: list[bytes]) -> set[int]:
    """Return set of byte offsets that did NOT change across all packets."""
    if len(packets) < 2:
        return set(range(len(packets[0])))
    stable = set()
    ref = packets[0]
    for i in range(len(ref)):
        if all(p[i] == ref[i] for p in packets[1:]):
            stable.add(i)
    return stable


def diff_stable(baseline_pkt: bytes, new_pkt: bytes, stable: set[int]) -> list[tuple[int, int, int]]:
    changes = []
    for i in stable:
        if i < len(baseline_pkt) and i < len(new_pkt):
            if baseline_pkt[i] != new_pkt[i]:
                changes.append((i, baseline_pkt[i], new_pkt[i]))
    return changes


async def main():
    init_schema()
    print("\n=== 8020/ff01 Write Test with Stable-Byte Diff ===\n")

    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
        print("Auth written.")
        await asyncio.sleep(0.3)

        status_queue: asyncio.Queue = asyncio.Queue()
        indications_8021 = []
        ff02_responses = []

        def on_status(_, data): status_queue.put_nowait(bytes(data))
        def on_8021(_, data):
            d = bytes(data)
            indications_8021.append(d)
            print(f"  [8021 indication] {d.hex()}  ({len(d)}B)")
        def on_ff02(_, data):
            d = bytes(data)
            ff02_responses.append(d)

        await client.start_notify(CHAR_STATUS, on_status)
        print("Subscribed to status.")
        try:
            await client.start_notify(CHAR_8021, on_8021)
            print("Subscribed to 8021 (indicate).")
        except Exception as e:
            print(f"  8021 sub failed: {e}")
        try:
            await client.start_notify(CHAR_CMD_OUT, on_ff02)
            print("Subscribed to ff02.")
        except Exception as e:
            print(f"  ff02 sub failed: {e}")

        # Collect baseline (5 status packets = ~5 seconds)
        print("\nCollecting 5-packet baseline...")
        baseline_packets = []
        while len(baseline_packets) < 5:
            try:
                pkt = await asyncio.wait_for(status_queue.get(), timeout=3.0)
                baseline_packets.append(pkt)
            except asyncio.TimeoutError:
                break

        if len(baseline_packets) < 3:
            print("Not enough baseline packets. Aborting.")
            return

        stable = stable_bytes(baseline_packets)
        last_baseline = baseline_packets[-1]
        print(f"  {len(baseline_packets)} packets collected.  {len(stable)} stable bytes.")

        # Show which bytes are naturally varying
        varying = set(range(len(last_baseline))) - stable
        print(f"  Naturally varying bytes: {sorted(varying)}")
        print()

        # Drain queue
        while not status_queue.empty():
            status_queue.get_nowait()

        # ── Test 8020 writes ──────────────────────────────────────────────────
        print("=== Testing 8020 writes ===")
        for payload, desc in WRITES_8020:
            indications_8021.clear()
            ff02_responses.clear()

            print(f"\n  Writing to 8020: {desc}")
            print(f"    Payload ({len(payload)}B): {payload[:16].hex()}{'...' if len(payload)>16 else ''}")
            try:
                await client.write_gatt_char(CHAR_8020, payload, response=True)
                print(f"    Write OK")
            except Exception as e:
                print(f"    Write FAILED: {e}")
                continue

            # Wait for response
            await asyncio.sleep(1.5)

            # Check 8021 indication
            if indications_8021:
                for ind in indications_8021:
                    print(f"    8021 indication: {ind.hex()}")

            # Check ff02
            if ff02_responses:
                for r in ff02_responses:
                    b4 = r[4] if len(r) > 4 else -1
                    print(f"    ff02 response: {r.hex()}  b4=0x{b4:02x}")

            # Check status packet change
            try:
                new_pkt = await asyncio.wait_for(status_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                print("    (no status packet within 2s)")
                continue

            changes = diff_stable(last_baseline, new_pkt, stable)
            if changes:
                print(f"    STABLE BYTES CHANGED:")
                for off, old, new in changes:
                    print(f"      byte[{off:3d}]: 0x{old:02x} -> 0x{new:02x}")
                last_baseline = new_pkt
                add_command(CHAR_8020, payload.hex(), desc,
                            source="probe", confirmed=False,
                            notes=f"Stable bytes changed: {[(o, f'0x{a:02x}->0x{b:02x}') for o,a,b in changes]}")
            else:
                print(f"    No stable-byte changes in status.")

        # ── Test ff01 commands ────────────────────────────────────────────────
        print("\n=== Testing ff01 set-style commands ===")
        for cmd_bytes, desc in FF01_CMDS:
            ff02_responses.clear()

            print(f"\n  cmd: {cmd_bytes.hex()}  ({desc})")
            try:
                await client.write_gatt_char(CHAR_CMD_IN, cmd_bytes, response=False)
            except Exception as e:
                print(f"    Write FAILED: {e}")
                continue

            await asyncio.sleep(1.5)

            if ff02_responses:
                for r in ff02_responses:
                    b4 = r[4] if len(r) > 4 else -1
                    print(f"    ff02: {r.hex()}  b4=0x{b4:02x}")

            try:
                new_pkt = await asyncio.wait_for(status_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                print("    (no status packet)")
                continue

            changes = diff_stable(last_baseline, new_pkt, stable)
            if changes:
                print(f"    STABLE BYTES CHANGED:")
                for off, old, new in changes:
                    print(f"      byte[{off:3d}]: 0x{old:02x} -> 0x{new:02x}")
                last_baseline = new_pkt
                add_command(CHAR_CMD_IN, cmd_bytes.hex(), desc,
                            source="probe", confirmed=False,
                            notes=f"Stable bytes changed: {[(o, f'0x{a:02x}->0x{b:02x}') for o,a,b in changes]}")
            else:
                print(f"    No stable-byte changes.")

        for uuid in (CHAR_STATUS, CHAR_CMD_OUT, CHAR_8021):
            try: await client.stop_notify(uuid)
            except: pass


if __name__ == "__main__":
    asyncio.run(main())
