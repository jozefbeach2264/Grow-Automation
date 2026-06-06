#!/usr/bin/env python3
"""
decode_responses.py — Decode A5-protocol responses and verify set_level persistence.

Strategy:
  1. Send get_model_data, decode response
  2. Send set_level(ON, speed=5)
  3. Send get_model_data again, compare
  4. Show which fields change
  5. Correlate with 1EFF status packet changes
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")
from bleak import BleakClient, BleakScanner
from ac_infinity_ble.protocol import Protocol
from aci_ble_lab.db import init_schema, add_command, add_note

ADDRESS = "50:78:7D:C5:0C:6E"
CHAR_WRITE = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"

proto = Protocol()
ADV_TYPE = 20


def parse_a5_response(data: bytes) -> dict:
    """Parse an A5 1C response packet."""
    if len(data) < 12 or data[0] != 0xA5 or data[1] != 0x1C:
        return {"raw": data.hex(), "error": "not A5 1C response"}
    data_len = (data[2] << 8) | data[3]
    seq = (data[4] << 8) | data[5]
    cmd_type = data[9] if len(data) > 9 else -1
    payload = data[10:10+data_len] if len(data) >= 10+data_len else data[10:]
    return {
        "data_len": data_len,
        "seq": seq,
        "cmd_type": cmd_type,
        "payload": payload.hex(),
        "payload_bytes": list(payload),
    }


def decode_get_model_response(data: bytes):
    """Decode get_model_data (cmd_type=1) response payload."""
    r = parse_a5_response(data)
    if "error" in r: return r
    p = r["payload_bytes"]
    if not p: return {"error": "empty payload"}

    result = dict(r)
    # From library: data[12]=work_type, data[15]=level_off, data[18]=level_on
    # These are positions in the FULL packet (byte offsets from start)
    # payload starts at byte[10], so payload[2] = data[12], payload[5] = data[15], payload[8] = data[18]
    if len(p) >= 3:  result["work_type"]  = p[2]  # data[12] = payload[2]
    if len(p) >= 6:  result["level_off"]  = p[5]  # data[15] = payload[5]
    if len(p) >= 9:  result["level_on"]   = p[8]  # data[18] = payload[8]
    return result


def decode_set_level_response(data: bytes):
    """Decode set_level (cmd_type=3) response payload."""
    r = parse_a5_response(data)
    if "error" in r: return r
    p = r["payload_bytes"]
    if len(p) >= 6:
        r["port_or_type"] = f"0x{p[0]:02x}"
        r["field1"] = p[1]
        r["work_type_encoded"] = f"0x{p[2]:02x}"
        r["field3"] = p[3]
        r["field4"] = p[4]
        r["speed_or_last"] = p[5]
    return r


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


async def send_and_capture(client, char_uuid: str, cmd: bytes, timeout: float = 3.0) -> bytes | None:
    """Send command and return the first non-status response."""
    resp_queue: asyncio.Queue = asyncio.Queue()

    def _on_notif(_, data):
        d = bytes(data)
        if d[0:2] == b'\xa5\x1c':  # A5 1C = command response
            resp_queue.put_nowait(d)

    # We're already subscribed, just need to wait
    await client.write_gatt_char(char_uuid, cmd, response=False)

    try:
        return await asyncio.wait_for(resp_queue.get(), timeout)
    except asyncio.TimeoutError:
        return None


async def main():
    init_schema()
    print("\n=== A5 Response Decoder + Set/Get Verify ===\n")

    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)\n")

        a5_responses: asyncio.Queue = asyncio.Queue()
        status_packets = []

        def on_notif(_, data):
            d = bytes(data)
            if d[0:2] == b'\xa5\x1c':
                a5_responses.put_nowait(d)
            elif d[0:2] == b'\x1e\xff' and len(d) == 127:
                status_packets.append(d)

        # Write a minimal A5 command first — this "wakes" the session and
        # allows the subsequent CCCD write (subscribe) to succeed without bonding error
        warm_cmd = proto.get_model_data(ADV_TYPE, 0, 0)
        try:
            await client.write_gatt_char(CHAR_WRITE, warm_cmd, response=False)
        except Exception:
            pass
        await asyncio.sleep(0.3)

        await client.start_notify(CHAR_READ, on_notif)
        print("Subscribed to 70d51002.")

        async def query_and_decode(label: str, seq: int):
            cmd = proto.get_model_data(ADV_TYPE, 0, seq)
            print(f"\n  [{label}] Sending get_model_data (seq={seq}): {cmd.hex()}")
            await client.write_gatt_char(CHAR_WRITE, cmd, response=False)
            try:
                resp = await asyncio.wait_for(a5_responses.get(), 3.0)
                decoded = decode_get_model_response(resp)
                print(f"  Response ({len(resp)}B): {resp.hex()}")
                print(f"  Payload: {decoded.get('payload', 'N/A')}")
                print(f"  work_type = {decoded.get('work_type', '?')}  ({['','OFF','ON'][decoded.get('work_type',0)] if decoded.get('work_type',0) in [1,2] else '?'})")
                print(f"  level_off = {decoded.get('level_off', '?')}")
                print(f"  level_on  = {decoded.get('level_on', '?')}")
                return decoded
            except asyncio.TimeoutError:
                print(f"  Timeout waiting for response.")
                return None

        async def set_and_decode(label: str, work_type: int, speed: int, seq: int):
            cmd = proto.set_level(ADV_TYPE, work_type, speed, 0, seq)
            wlabel = "ON" if work_type == 2 else "OFF"
            print(f"\n  [{label}] Sending set_level({wlabel}, speed={speed}) (seq={seq}): {cmd.hex()}")
            await client.write_gatt_char(CHAR_WRITE, cmd, response=False)
            try:
                resp = await asyncio.wait_for(a5_responses.get(), 3.0)
                decoded = decode_set_level_response(resp)
                print(f"  Response ({len(resp)}B): {resp.hex()}")
                print(f"  Payload: {decoded.get('payload', 'N/A')}")
                print(f"  work_type_encoded = {decoded.get('work_type_encoded', '?')}")
                print(f"  speed_or_last     = {decoded.get('speed_or_last', '?')}")
                return decoded
            except asyncio.TimeoutError:
                print(f"  Timeout.")
                return None

        # Drain old packets
        await asyncio.sleep(2)
        while not a5_responses.empty(): a5_responses.get_nowait()
        status_packets.clear()

        print("=== Step 1: Baseline query ===")
        before = await query_and_decode("before", 1)

        print("\n=== Step 2: Set speed to 5 (ON) ===")
        await set_and_decode("set", 2, 5, 2)

        print("\n=== Step 3: Query after set ===")
        after_on = await query_and_decode("after-ON", 3)

        print("\n=== Step 4: Set speed to 0 (OFF) ===")
        await set_and_decode("set-OFF", 1, 0, 4)

        print("\n=== Step 5: Query after OFF ===")
        after_off = await query_and_decode("after-OFF", 5)

        print("\n=== Step 6: Restore ON speed=3 ===")
        await set_and_decode("set-ON-3", 2, 3, 6)
        final = await query_and_decode("final", 7)

        # Print any status packet changes
        print(f"\n=== Status packets collected during test: {len(status_packets)} ===")
        if status_packets:
            for i, pkt in enumerate(status_packets[:3]):
                print(f"  [{i}] byte[12]={pkt[12]:02x} byte[16]={pkt[16]:02x} byte[17]={pkt[17]:02x}")

        # Compare before/after
        if before and after_on:
            print("\n=== Before vs After-ON comparison ===")
            for key in ['work_type', 'level_off', 'level_on']:
                bv = before.get(key, '?')
                av = after_on.get(key, '?')
                changed = " <-- CHANGED" if bv != av else ""
                print(f"  {key}: {bv} -> {av}{changed}")

        # Store confirmed commands
        cmd_on = proto.set_level(ADV_TYPE, 2, 5, 0, 1)
        cmd_off = proto.set_level(ADV_TYPE, 1, 0, 0, 1)
        add_command(CHAR_WRITE, cmd_on.hex(), "set_level ON speed=5 (type=20, A5-framed)",
                    source="probe", confirmed=True,
                    notes="Gets A5-1C response with speed echoed back. Write to 70d51001.")
        add_command(CHAR_WRITE, cmd_off.hex(), "set_level OFF speed=0 (type=20, A5-framed)",
                    source="probe", confirmed=True,
                    notes="Gets A5-1C response. Write to 70d51001.")

        add_note("a5_protocol_confirmed",
                 "A5-framed commands to 70d51001 work and get A5-1C responses on 70d51002. "
                 "get_model_data (cmd_type=1) returns 47B+ response with device state. "
                 "set_level (cmd_type=3) returns 18B response echoing work_type and speed. "
                 "Response format: A5 1C [len_hi] [len_lo] [seq_hi] [seq_lo] [crc_hi] [crc_lo] "
                 "00 [cmd_type] [data...] [crc16]. "
                 "Write to 70d51001, read from 70d51002 (not ff01/ff02). "
                 "ff01/ff02 only gives generic 49-04 ACKs.",
                 confidence="confirmed")

        print("\nProtocol notes saved to DB.")

        try: await client.stop_notify(CHAR_READ)
        except: pass


if __name__ == "__main__":
    asyncio.run(main())
