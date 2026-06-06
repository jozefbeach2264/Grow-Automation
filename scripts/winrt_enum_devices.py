#!/usr/bin/env python3
"""
winrt_enum_devices.py — Query Windows device information database for all known BLE devices.
Shows devices Windows has cached even if they are not currently advertising.
"""

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
except ImportError:
    print("ERROR: winrt packages not found.")
    sys.exit(1)


async def main():
    print("\nQuerying Windows device database for known BLE devices...")
    selector = BluetoothLEDevice.get_device_selector()
    devices = await DeviceInformation.find_all_async(selector)

    if not devices or len(devices) == 0:
        print("No BLE devices found in Windows device database.")
        return

    print(f"Found {len(devices)} BLE device(s) in Windows cache:\n")
    for d in devices:
        print(f"  Name : {d.name!r}")
        print(f"  ID   : {d.id}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
