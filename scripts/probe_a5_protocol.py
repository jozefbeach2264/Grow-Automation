#!/usr/bin/env python3
"""
probe_a5_protocol.py — Test the real 0xA5-framed command protocol.

KEY DISCOVERY: The ac-infinity-ble library writes commands to 70d51001
(not ff01) and reads responses from 70d51002 (not just passively receives status).

Command format: A5 00 [len_hi] [len_lo] [seq_hi] [seq_lo] [crc16_hdr] 00 [cmd_type] [data...] [crc16_payload]
  - Header bytes 0-1: 0xA5 0x00
  - Bytes 2-3: big-endian data length
  - Bytes 4-5: big-endian sequence number
  - Bytes 6-7: CRC16 of header (bytes 0-5)
  - Byte 8: 0x00 padding
  - Byte 9: command type (1=query model data, 3=set level)
  - Bytes 10+: command data
  - Last 2 bytes: CRC16 of bytes 8..end-2

This script:
  1. Sends get_model_data command (type 1) to 70d51001
  2. Captures ALL notifications from 70d51002 before and after
  3. Sends set_level command (type 3) with various device type assumptions
  4. Watches for response changes
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, add_command, add_note

# Import the library's Protocol directly
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")
from ac_infinity_ble.protocol import Protocol

ADDRESS = "50:78:7D:C5:0C:6E"
CHAR_70D51001 = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"  # WRITE (commands go here)
CHAR_70D51002 = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"  # READ (responses come here)
CHAR_FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"

proto = Protocol()

# Our device type from advertisement data[12] = 0x14 = 20
# Our device hw_type from status packet byte[3] = 9
# Try both
ADV_TYPE = 20
STATUS_TYPE = 9


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


async def main():
    init_schema()
    print("\n=== A5-Protocol Command Test ===\n")

    # Build and show the commands we'll test
    print("Pre-computing commands:")
    cmds = []

    for dev_type, label in [(ADV_TYPE, f"type={ADV_TYPE}(adv)"), (STATUS_TYPE, f"type={STATUS_TYPE}(status)")]:
        for b_val in [0, 1]:
            cmd = proto.get_model_data(dev_type, b_val, 1)
            print(f"  get_model_data({label}, b={b_val}): {cmd.hex()}")
            cmds.append((CHAR_70D51001, cmd, f"get_model_data {label} b={b_val} → 70d51001"))

    for dev_type, label in [(ADV_TYPE, f"type={ADV_TYPE}"), (STATUS_TYPE, f"type={STATUS_TYPE}")]:
        for work_type, wlabel in [(2, "ON"), (1, "OFF")]:
            for speed in [5, 3]:
                for b_val in [0, 1]:
                    cmd = proto.set_level(dev_type, work_type, speed, b_val, 1)
                    print(f"  set_level({label}, work={wlabel}, speed={speed}, b={b_val}): {cmd.hex()}")
                    cmds.append((CHAR_70D51001, cmd, f"set_level {label} work={wlabel} speed={speed} b={b_val}"))

    # Also try same commands to ff01
    for dev_type, label in [(ADV_TYPE, f"type={ADV_TYPE}"), (STATUS_TYPE, f"type={STATUS_TYPE}")]:
        cmd = proto.get_model_data(dev_type, 0, 1)
        cmds.append((CHAR_FF01, cmd, f"get_model_data {label} b=0 → ff01"))

    print()
    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        notifications_70d51002 = []
        notifications_ff02 = []

        def on_status(_, d):
            notifications_70d51002.append(bytes(d))

        def on_ff02(_, d):
            notifications_ff02.append(bytes(d))

        await client.start_notify(CHAR_70D51002, on_status)
        print("Subscribed to 70d51002.")
        try:
            await client.start_notify(CHAR_FF02, on_ff02)
            print("Subscribed to ff02.")
        except: pass

        # Baseline drain
        await asyncio.sleep(3)
        baseline_count = len(notifications_70d51002)
        print(f"\nBaseline: {baseline_count} status packets received.")
        if notifications_70d51002:
            print(f"  Last: {notifications_70d51002[-1].hex()[:60]}...")
        notifications_70d51002.clear()
        notifications_ff02.clear()
        print()

        for char_uuid, cmd, desc in cmds:
            notifications_70d51002.clear()
            notifications_ff02.clear()

            print(f"  SEND [{char_uuid[:8]}] {cmd.hex()}  ({desc})")
            try:
                await client.write_gatt_char(char_uuid, cmd, response=False)
            except Exception as e:
                print(f"    write error: {e}")
                continue

            await asyncio.sleep(2.0)

            if notifications_70d51002:
                for pkt in notifications_70d51002:
                    is_status = (len(pkt) == 127 and pkt[0] == 0x1E and pkt[1] == 0xFF)
                    marker = "(STATUS)" if is_status else "*** DIFFERENT ***"
                    print(f"    70d51002: {pkt.hex()[:60]}  ({len(pkt)}B) {marker}")

            if notifications_ff02:
                for pkt in notifications_ff02:
                    b4 = pkt[4] if len(pkt) > 4 else -1
                    print(f"    ff02: {pkt.hex()}  b4=0x{b4:02x}")

            if not notifications_70d51002 and not notifications_ff02:
                print(f"    (no notifications)")

        for uuid in (CHAR_70D51002, CHAR_FF02):
            try: await client.stop_notify(uuid)
            except: pass

        print("\nDone.")
        print()
        print("Key: if a 70d51002 notification is NOT 127 bytes or does NOT start with 1EFF,")
        print("it's a COMMAND RESPONSE (not a periodic status update).")


if __name__ == "__main__":
    asyncio.run(main())
