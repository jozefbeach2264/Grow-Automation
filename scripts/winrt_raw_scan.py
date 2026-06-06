#!/usr/bin/env python3
"""
winrt_raw_scan.py — Raw WinRT BLE advertisement watcher, bypasses bleak entirely.
Dumps every advertisement packet Windows receives: address, RSSI, type, name, company IDs.
"""

import asyncio
import sys
import struct
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from winrt.windows.devices.bluetooth.advertisement import (
        BluetoothLEAdvertisementWatcher,
        BluetoothLEScanningMode,
    )
except ImportError:
    print("ERROR: winrt packages not found. Run: pip install winrt-runtime winrt-windows-devices-bluetooth-advertisement")
    sys.exit(1)


def int_to_mac(addr: int) -> str:
    return ":".join(f"{(addr >> (i * 8)) & 0xFF:02X}" for i in range(5, -1, -1))


seen: dict[str, dict] = {}


def on_received(watcher, args):
    try:
        addr = int_to_mac(args.bluetooth_address)
        rssi = args.raw_signal_strength_in_dbm
        adv  = args.advertisement
        name = adv.local_name or ""
        adv_type = str(args.advertisement_type)

        company_ids = []
        for section in adv.manufacturer_data:
            company_ids.append(f"0x{section.company_id:04X}")

        svc_uuids = [str(u) for u in adv.service_uuids]

        is_new = addr not in seen
        seen[addr] = {
            "address": addr,
            "rssi": rssi,
            "name": name,
            "adv_type": adv_type,
            "company_ids": company_ids,
            "service_uuids": svc_uuids,
        }

        if is_new:
            name_str = f'  name="{name}"' if name else ""
            co_str   = f"  co={company_ids}" if company_ids else ""
            svc_str  = f"  svc={svc_uuids}" if svc_uuids else ""
            print(f"  {addr}  RSSI:{rssi:>4}  [{adv_type}]{name_str}{co_str}{svc_str}")
    except Exception as exc:
        print(f"  [handler error] {exc}")


async def main():
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    print(f"\nRaw WinRT BLE scan for {timeout}s (bypasses bleak)")
    print("All advertisement types shown including connectable, scannable, non-connectable")
    print("-" * 80)

    watcher = BluetoothLEAdvertisementWatcher()
    watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE
    watcher.allow_extended_advertisements = True  # BLE 5.0 extended advertising
    watcher.use_coded_phy = True                  # coded PHY (long range)
    watcher.use_uncoded1_m_phy = True             # 1M PHY (standard)
    watcher.use_hardware_filter = False           # software filter — catches everything

    token = watcher.add_received(on_received)
    watcher.start()

    await asyncio.sleep(timeout)

    watcher.stop()
    watcher.remove_received(token)

    print("-" * 80)
    print(f"Total unique devices: {len(seen)}")
    non_apple = {k: v for k, v in seen.items() if "0x004C" not in v["company_ids"]}
    if non_apple:
        print(f"Non-Apple devices ({len(non_apple)}):")
        for d in non_apple.values():
            print(f"  {d['address']}  RSSI:{d['rssi']}  name={d['name']!r}  co={d['company_ids']}  svc={d['service_uuids']}")
    else:
        print("All devices are Apple — controller not in radio range of this PC.")


if __name__ == "__main__":
    asyncio.run(main())
