#!/usr/bin/env python3
"""
control.py — Direct fan control for AC Infinity ACI_V3.5_CTRLER.

Protocol confirmed:
  - Commands: A5-framed, written to 70d51001
  - Responses: A5-framed, read from 70d51002
  - Status stream: 1EFF 127-byte notifications on 70d51002 (1Hz)

Usage:
  # Turn port 1 fan ON at speed 5:
  python scripts/control.py --port 1 --speed 5

  # Turn port 1 fan OFF:
  python scripts/control.py --port 1 --off

  # Query device state (port 0 = global, ports 1-8 for specific channels):
  python scripts/control.py --query --port 0

  # Set ALL ports (type=20 / no port selector):
  python scripts/control.py --speed 5 --all
"""

import argparse
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from bleak import BleakClient, BleakScanner
from ac_infinity_ble.protocol import Protocol

ADDRESS     = "50:78:7D:C5:0C:6E"
CHAR_WRITE  = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ   = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"

proto = Protocol()

# Device type constants
TYPE_GLOBAL  = 20   # No port suffix (type "A") — likely a global/default command
TYPE_MULTIPORT = 9  # 8-port CONTROLLER PRO — uses [0xFF, port_num] suffix


def parse_a5(data: bytes) -> dict:
    if len(data) < 12 or data[0] != 0xA5 or data[1] != 0x1C:
        return {}
    data_len = (data[2] << 8) | data[3]
    seq = (data[4] << 8) | data[5]
    cmd_type = data[9] if len(data) > 9 else -1
    payload = data[10:10+data_len]
    return {"len": data_len, "seq": seq, "cmd_type": cmd_type, "payload": payload}


def decode_state(payload: bytes) -> str:
    """Decode TLV state from get_model_data response payload."""
    if len(payload) < 9:
        return f"(short payload: {payload.hex()})"
    work_type = payload[2]   # tag 0x10 value at offset 2
    level_off = payload[5]   # tag 0x11 value at offset 5
    level_on  = payload[8]   # tag 0x12 value at offset 8
    mode = {1: "OFF", 2: "ON"}.get(work_type, f"0x{work_type:02x}")
    return f"mode={mode}  level_off={level_off}  level_on={level_on}"


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


async def run(address: str, cmd_bytes: bytes, desc: str):
    print(f"\nTarget  : {address}")
    print(f"Command : {desc}")
    print(f"Payload : {cmd_bytes.hex()}")

    print("\nScanning...")
    if not await _scan_until_found(address):
        print("Controller not found.")
        return

    print("Connecting...")
    async with BleakClient(address) as client:
        print(f"Connected (MTU={client.mtu_size}B)")

        response_queue: asyncio.Queue = asyncio.Queue()
        status_last = []

        def on_notif(_, d):
            d = bytes(d)
            if d[:2] == b'\xa5\x1c':
                response_queue.put_nowait(d)
            elif d[:2] == b'\x1e\xff' and len(d) == 127:
                if status_last:
                    status_last.pop()
                status_last.append(d)

        await asyncio.sleep(0.3)  # let service discovery settle

        # Warm up the session before subscribing.
        # Some cached Windows BLE sessions fail CCCD writes unless we first write
        # to the command characteristic, which establishes the session.
        for attempt in range(3):
            try:
                warm = proto.get_model_data(TYPE_GLOBAL, 0, 0)
                await client.write_gatt_char(CHAR_WRITE, warm, response=False)
                await asyncio.sleep(0.4 + attempt * 0.3)
                await client.start_notify(CHAR_READ, on_notif)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                print(f"  [warn] Could not subscribe after {attempt+1} attempts.")

        await asyncio.sleep(0.5)
        while not response_queue.empty():
            response_queue.get_nowait()

        # Send the command
        await client.write_gatt_char(CHAR_WRITE, cmd_bytes, response=False)

        try:
            resp = await asyncio.wait_for(response_queue.get(), timeout=5.0)
            parsed = parse_a5(resp)
            print(f"\nResponse: {resp.hex()}")
            if parsed.get("cmd_type") == 1:
                print(f"State   : {decode_state(parsed['payload'])}")
            elif parsed.get("cmd_type") == 3:
                p = parsed["payload"]
                print(f"Ack     : {p.hex()}")
                if len(p) >= 6:
                    wt = "ON" if p[2] == 0x12 else "OFF"
                    if len(p) >= 5 and p[4] == 0xFF:
                        port = p[5] if len(p) > 5 else "?"
                        print(f"Applied : mode={wt}  port={port}")
                    else:
                        speed = p[5] if len(p) > 5 else p[-1]
                        print(f"Applied : mode={wt}  speed={speed}")
        except asyncio.TimeoutError:
            print("\nTimeout: no response within 5s")

        try: await client.stop_notify(CHAR_READ)
        except: pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Control AC Infinity controller via BLE")
    p.add_argument("--address", default=ADDRESS, help="Controller MAC address")
    p.add_argument("--port", type=int, default=None,
                   help="Port number 1-8 (uses TYPE_MULTIPORT), or 0 for global query")
    p.add_argument("--speed", type=int, default=5, help="Speed 0-10 (default 5)")
    p.add_argument("--off", action="store_true", help="Turn off (work_type=1)")
    p.add_argument("--query", action="store_true", help="Query device state instead of setting")
    p.add_argument("--all", action="store_true",
                   help="Use type=20 (global set, no port number)")
    args = p.parse_args()

    speed = 0 if args.off else args.speed
    work_type = 1 if (args.off or speed == 0) else 2
    seq = 1

    if args.query:
        if args.port is not None:
            cmd = proto.get_model_data(TYPE_MULTIPORT, args.port, seq)
            desc = f"get_model_data(port={args.port})"
        else:
            cmd = proto.get_model_data(TYPE_GLOBAL, 0, seq)
            desc = "get_model_data(global)"
    elif args.all or args.port is None:
        cmd = proto.set_level(TYPE_GLOBAL, work_type, speed, 0, seq)
        desc = f"set_level(global, {'ON' if work_type==2 else 'OFF'}, speed={speed})"
    else:
        cmd = proto.set_level(TYPE_MULTIPORT, work_type, speed, args.port, seq)
        desc = f"set_level(port={args.port}, {'ON' if work_type==2 else 'OFF'}, speed={speed})"

    asyncio.run(run(args.address, cmd, desc))
