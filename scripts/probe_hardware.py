#!/usr/bin/env python3
"""
probe_hardware.py -- Hardware fingerprinting and GATT handle walk.
Scans continuously until controller appears, then connects and interrogates it.
Results are stored in controller.db.
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, upsert_controller, add_note, dump_summary

ADDRESS = "50:78:7D:C5:0C:6E"

OUI_MAP = {
    "50:78:7D": "Espressif Inc. (ESP32 family)",
    "24:6F:28": "Espressif Inc.",
    "A4:CF:12": "Espressif Inc.",
    "D8:A0:1D": "Nordic Semiconductor",
    "F4:CE:36": "Nordic Semiconductor",
}

COMPANY_MAP = {
    0x004C: "Apple",
    0x0131: "Nordic Semiconductor ASA",
    0x02FF: "Espressif Incorporated",
    0x0590: "Espressif Systems",
    0x0902: "Unknown company 0x0902",
}


def ascii_repr(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


async def main():
    init_schema()
    print("\n=== AC Infinity Controller Hardware Probe ===\n")

    oui = ADDRESS[:8].upper()
    print(f"MAC Address : {ADDRESS}")
    print(f"OUI         : {oui}  ->  {OUI_MAP.get(oui, 'Unknown')}")
    print()

    # Scan continuously until the controller appears, then connect immediately
    print("Waiting for controller to advertise (enable BT pairing mode now)...")
    detected = asyncio.Event()
    mfr_info = []

    def on_detect(device, adv):
        if device.address.upper() == ADDRESS.upper() and not detected.is_set():
            for cid, raw in adv.manufacturer_data.items():
                cname = COMPANY_MAP.get(cid, f"Unknown (0x{cid:04X})")
                mfr_info.append((cid, cname, bytes(raw)))
            detected.set()

    async with BleakScanner(detection_callback=on_detect):
        elapsed = 0
        while not detected.is_set() and elapsed < 120:
            await asyncio.sleep(3)
            elapsed += 3
            if elapsed % 9 == 0:
                print(f"  [{elapsed}s] Still waiting...")

    if not detected.is_set():
        print("Controller not found after 2 minutes.")
        return

    print("  Controller detected -- connecting...")
    for cid, cname, raw in mfr_info:
        print(f"  Adv company ID : 0x{cid:04X}  ->  {cname}")
        print(f"  Adv payload    : {raw.hex()}  ({len(raw)}B)")
    print()

    async with BleakClient(ADDRESS) as client:
        mtu = client.mtu_size
        print(f"Connected!  MTU={mtu}B")
        print()

        mtu_hint = "BT 4.2+ with Data Length Extension" if mtu >= 104 else \
                   "Default MTU (BT 4.0/4.1 or DLE not negotiated)" if mtu == 23 else \
                   f"Negotiated {mtu}B"
        print(f"MTU hint: {mtu_hint}")
        print()

        # Service UUID analysis
        print("Services and UUID patterns:")
        for svc in client.services:
            uuid = str(svc.uuid)
            base = uuid[9:]
            if base == "0000-1000-8000-00805f9b34fb":
                kind = "BT SIG standard"
            elif "2c7f-4e75-ae8a-d758951ce4e0" in uuid:
                kind = "AC Infinity proprietary base"
            else:
                kind = "vendor/custom"
            print(f"  {uuid}  [{kind}]")
        print()

        # Nordic UART Service check
        nus = any("6e400001" in str(s.uuid) for s in client.services)
        print(f"Nordic UART Service (nRF52 indicator): {'YES' if nus else 'No'}")
        print()

        # Handle walk
        print("GATT handle walk (reading handles 1-80):")
        print(f"  {'HDL':>4}  {'HEX':<44}  {'LEN':>4}  ASCII")
        print(f"  {'----':>4}  {'---':<44}  {'---':>4}  -----")
        readable = {}
        for handle in range(1, 81):
            try:
                data = bytes(await client.read_gatt_char(handle))
                readable[handle] = data
                h = data.hex()
                display_hex = h[:44] + ("..." if len(h) > 44 else "")
                asc = ascii_repr(data[:22])
                print(f"  {handle:>4}  {display_hex:<44}  {len(data):>4}  {asc}")
            except Exception as exc:
                msg = str(exc).lower()
                if any(x in msg for x in ["not permitted", "not authorized",
                                           "insufficient", "application error"]):
                    print(f"  {handle:>4}  [ACCESS DENIED]")
                elif "not found" not in msg and "invalid" not in msg and \
                     "0x0001" not in msg and "attribute" not in msg and \
                     "gatt error" not in msg and msg.strip():
                    print(f"  {handle:>4}  [ERR] {str(exc)[:60]}")
        print()
        print(f"Readable handles: {sorted(readable.keys())}")
        print()

        # Hidden handles (beyond enumerated services)
        enumerated = set()
        for svc in client.services:
            for char in svc.characteristics:
                enumerated.add(char.handle)
        hidden = {h: d for h, d in readable.items() if h not in enumerated}
        if hidden:
            print(f"Hidden/extra readable handles (not in service list): {list(hidden.keys())}")
            for h, d in hidden.items():
                print(f"  handle {h}: {d.hex()}  |  {ascii_repr(d)}")
        else:
            print("No hidden handles found beyond enumerated services.")
        print()

        # Chip conclusion
        chip = OUI_MAP.get(oui, "Unknown")
        mfr_ids = [f"0x{c:04X}" for c, _, _ in mfr_info]
        print("=== Hardware Fingerprint Summary ===")
        print(f"  OUI vendor     : {chip}")
        print(f"  Company IDs    : {mfr_ids}")
        print(f"  MTU            : {mtu}B  ({mtu_hint})")
        print(f"  Nordic UART    : {'Yes (nRF52)' if nus else 'No'}")
        print(f"  Readable hdls  : {sorted(readable.keys())}")
        print()

        if "Espressif" in chip:
            conclusion = "ESP32 family (ESP32 / ESP32-C3 / ESP32-S3)"
            bt_stack = "Bluedroid or NimBLE on ESP-IDF"
        elif nus:
            conclusion = "Nordic nRF52 series"
            bt_stack = "SoftDevice (Nordic BT stack)"
        else:
            conclusion = "Unknown -- needs more data"
            bt_stack = "Unknown"

        print(f"  Conclusion     : {conclusion}")
        print(f"  BT stack       : {bt_stack}")

        # Update DB with live data
        upsert_controller(
            address=ADDRESS,
            mtu=mtu,
            chip_family=conclusion,
            bt_stack=bt_stack,
            company_id=",".join(mfr_ids) if mfr_ids else "0x0902",
        )
        if mfr_info:
            for cid, cname, raw in mfr_info:
                add_note("manufacturer_data",
                         f"Advertisement company ID 0x{cid:04X} ({cname}), "
                         f"payload={raw.hex()} ({len(raw)}B)",
                         confidence="confirmed")
        if hidden:
            for h, d in hidden.items():
                add_note("hidden_handle",
                         f"Handle {h} readable but not in GATT service list: {d.hex()}",
                         confidence="confirmed")

        print()
        print("DB updated. Run: python scripts/db_init.py  to see full summary.")


if __name__ == "__main__":
    asyncio.run(main())
