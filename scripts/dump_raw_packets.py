#!/usr/bin/env python3
"""
dump_raw_packets.py — Capture 15 raw 1EFF status packets and print all bytes
side-by-side so we can spot which ones change (= sensor data).
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

ADDRESS    = "50:78:7D:C5:0C:6E"
CHAR_WRITE = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
proto = Protocol()


async def main():
    found = asyncio.Event()
    def cb(dev, _):
        if dev.address.upper() == ADDRESS.upper():
            found.set()
    print("Scanning...")
    async with BleakScanner(detection_callback=cb):
        await asyncio.wait_for(found.wait(), 60)

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size})\n")

        packets = []
        done = asyncio.Event()

        def on_notif(_, raw):
            d = bytes(raw)
            if d[:2] == b'\x1e\xff' and len(d) == 127:
                packets.append(d)
                print(f"  pkt {len(packets):2d} received", end="\r")
                if len(packets) >= 15:
                    done.set()

        await asyncio.sleep(0.3)
        for attempt in range(3):
            try:
                warm = proto.get_model_data(20, 0, 0)
                await client.write_gatt_char(CHAR_WRITE, warm, response=False)
                await asyncio.sleep(0.4 + attempt * 0.3)
                await client.start_notify(CHAR_READ, on_notif)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)

        print("Waiting for 15 status packets...")
        await asyncio.wait_for(done.wait(), 90)
        print(f"\nGot {len(packets)} packets.\n")

        try:
            await client.stop_notify(CHAR_READ)
        except Exception:
            pass

    # Print byte grid: row=byte_offset, col=packet_index
    # Highlight bytes that vary across packets
    if len(packets) < 2:
        print("Not enough packets.")
        return

    n = len(packets)
    print(f"{'Byte':>4}  {'hex values across packets':}")
    print("-" * 80)

    changing = []
    for i in range(127):
        vals = [p[i] for p in packets]
        is_changing = len(set(vals)) > 1
        if is_changing:
            changing.append(i)
        flag = " <-- CHANGES" if is_changing else ""
        vals_str = " ".join(f"{v:02x}" for v in vals)
        print(f"[{i:3d}]  {vals_str}{flag}")

    print()
    print(f"Bytes that change across packets: {changing}")
    print()

    # Print last packet in blocks of 8 for reference
    p = packets[-1]
    print("Last packet (reference):")
    for base in range(0, 127, 16):
        block = p[base:base+16]
        hex_part = " ".join(f"{b:02x}" for b in block)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in block)
        print(f"  [{base:3d}]  {hex_part:<48}  {asc_part}")


if __name__ == "__main__":
    asyncio.run(main())
