#!/usr/bin/env python3
"""
probe_cmd_with_diff.py — Send commands and diff status packets to find what changes.

For each command:
  1. Capture baseline status packet
  2. Send command
  3. Wait for next status packet
  4. Print which bytes changed

Hypotheses being tested:
  - 1E FF [cmd] 00 = query/read  (b4=0x02 seen in sweep)
  - 1E FF [cmd] [val] = set/write  (val != 00)
  - Does the status packet change after set commands?

Usage:
  python scripts/probe_cmd_with_diff.py
"""

import asyncio
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, add_command, add_note

ADDRESS    = "50:78:7D:C5:0C:6E"
CHAR_AUTH  = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_STATUS = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_CMD_IN = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_CMD_OUT = "0000ff02-0000-1000-8000-00805f9b34fb"
AUTH_TOKEN  = bytes.fromhex("11223344")


def diff_packets(a: bytes, b: bytes) -> list[tuple[int, int, int]]:
    """Return list of (offset, old_val, new_val) for changed bytes."""
    changes = []
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            changes.append((i, a[i], b[i]))
    return changes


def print_hex_diff(baseline: bytes, current: bytes, context: int = 2):
    changes = diff_packets(baseline, current)
    if not changes:
        print("    (no changes)")
        return
    changed_offsets = {o for o, _, _ in changes}
    for o, old, new in changes:
        print(f"    byte[{o:3d}]: 0x{old:02x} -> 0x{new:02x}  (delta {new-old:+d})")


# Commands to test for status-packet changes:
# Format: (hex, description, risky)
# risky=True means the command might change a real setting — skip unless explicitly enabled
COMMANDS_TO_TEST = [
    # Queries (4 bytes, payload=00) — all should be safe
    ("1eff0100", "1EFF cmd=01 query"),
    ("1eff0200", "1EFF cmd=02 query"),
    ("1eff0300", "1EFF cmd=03 query"),
    ("1eff0400", "1EFF cmd=04 query"),
    ("1eff0500", "1EFF cmd=05 query"),
    ("1eff0600", "1EFF cmd=06 query"),
    ("1eff0700", "1EFF cmd=07 query"),
    ("1eff0800", "1EFF cmd=08 query"),
    ("1eff0900", "1EFF cmd=09 query"),
    ("1eff0a00", "1EFF cmd=0A query"),
    ("1eff0b00", "1EFF cmd=0B query"),
    ("1eff0c00", "1EFF cmd=0C query"),
    ("1eff0d00", "1EFF cmd=0D query"),
    ("1eff0e00", "1EFF cmd=0E query"),
    ("1eff0f00", "1EFF cmd=0F query"),
    # Try 5-byte format: 1EFF [cmd] [port] [value]
    # port=0 (first channel), value=0 — should be safe (set speed to 0)
    # These are clearly SET commands — test at end
    ("1eff010100", "1EFF cmd=01 port=01 val=00"),
    ("1eff010001", "1EFF cmd=01 arg1=00 arg2=01"),
    ("1eff010200", "1EFF cmd=01 port=02 val=00"),
    # Try 6-byte format
    ("1eff01010000", "1EFF cmd=01 port=01 val=00 end=00"),
    ("1eff01000100", "1EFF cmd=01 b3=00 b4=01 b5=00"),
]


async def _scan_until_found(address: str, timeout: int = 60) -> bool:
    found = asyncio.Event()
    def cb(dev, _adv):
        if dev.address.upper() == address.upper():
            found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def main():
    init_schema()
    print("\n=== Command vs Status Diff Test ===\n")

    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        # Auth first
        await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
        print("Auth written.")
        await asyncio.sleep(0.3)

        # Subscribe
        status_queue: asyncio.Queue = asyncio.Queue()
        cmd_responses = []

        def on_status(_, data):
            status_queue.put_nowait(bytes(data))

        def on_cmd_resp(_, data):
            cmd_responses.append(bytes(data))
            resp = bytes(data)
            b4 = resp[4] if len(resp) > 4 else -1
            print(f"    ff02: {resp.hex()}  b4=0x{b4:02x}")

        await client.start_notify(CHAR_STATUS, on_status)
        print("Subscribed to status.")

        try:
            await client.start_notify(CHAR_CMD_OUT, on_cmd_resp)
            print("Subscribed to ff02.")
        except Exception as e:
            print(f"  ff02 sub failed: {e}")

        # Drain old status packets, get baseline
        print("\nWaiting for baseline status packet...")
        await asyncio.sleep(2)
        while not status_queue.empty():
            baseline = status_queue.get_nowait()

        print(f"Baseline ({len(baseline)}B): {baseline.hex()[:60]}...")
        print()

        changed_commands = []

        for hex_str, desc in COMMANDS_TO_TEST:
            cmd_bytes = bytes.fromhex(hex_str)
            cmd_responses.clear()

            # Wait for a fresh status packet to set as pre-command baseline
            try:
                pre = await asyncio.wait_for(status_queue.get(), timeout=3.0)
            except asyncio.TimeoutError:
                pre = baseline

            # Send command
            await client.write_gatt_char(CHAR_CMD_IN, cmd_bytes, response=False)

            # Wait for next status packet
            try:
                post = await asyncio.wait_for(status_queue.get(), timeout=3.0)
            except asyncio.TimeoutError:
                post = pre

            changes = diff_packets(pre, post)

            if changes:
                print(f"  CHANGED  {hex_str:<20}  {desc}")
                print_hex_diff(pre, post)
                changed_commands.append((hex_str, desc, changes))
            else:
                # Still show ff02 response if it was non-zero
                if cmd_responses:
                    resp = cmd_responses[-1]
                    b4 = resp[4] if len(resp) > 4 else -1
                    if b4 != 0:
                        print(f"  b4={b4:02x}     {hex_str:<20}  {desc}  (no status change)")
                    else:
                        print(f"  no change  {hex_str:<20}")
                else:
                    print(f"  no change  {hex_str:<20}")

        print()
        print(f"Commands that changed status: {len(changed_commands)}")
        for hex_str, desc, changes in changed_commands:
            offsets = [o for o, _, _ in changes]
            print(f"  {hex_str}  ({desc})  changed bytes: {offsets}")
            add_command(
                char_uuid=CHAR_CMD_IN,
                hex_data=hex_str,
                description=desc,
                source="status_diff",
                confirmed=False,
                notes=f"Status packet bytes changed: {offsets}"
            )

        if changed_commands:
            summary = "; ".join(f"cmd={h}" for h, _, _ in changed_commands)
            add_note("status_changing_commands",
                     f"Commands that caused status packet changes: {summary}",
                     confidence="hypothesis")

        for uuid in (CHAR_STATUS, CHAR_CMD_OUT):
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
