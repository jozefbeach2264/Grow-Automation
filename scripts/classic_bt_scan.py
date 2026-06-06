#!/usr/bin/env python3
"""
classic_bt_scan.py — Discover Classic Bluetooth (BR/EDR) devices using WinRT DeviceWatcher.
Use this if the controller does not appear in BLE scans.
"""

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from winrt.windows.devices.bluetooth import BluetoothDevice
    from winrt.windows.devices.enumeration import DeviceInformation, DeviceWatcher, DeviceWatcherStatus
except ImportError:
    print("ERROR: winrt packages not found.")
    sys.exit(1)


def int_to_mac(addr: int) -> str:
    return ":".join(f"{(addr >> (i * 8)) & 0xFF:02X}" for i in range(5, -1, -1))


async def main():
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    found: dict[str, str] = {}

    print(f"\nClassic Bluetooth device discovery for {timeout}s...")
    print("(This finds BR/EDR devices, not BLE — different radio)")
    print("-" * 60)

    selector = BluetoothDevice.get_device_selector()
    watcher = DeviceInformation.create_watcher_aqs_filter(selector)

    def on_added(sender, info):
        name = info.name or "(no name)"
        dev_id = info.id
        if dev_id not in found:
            found[dev_id] = name
            print(f"  FOUND: {name!r}  id={dev_id}")

    def on_updated(sender, update):
        pass

    def on_removed(sender, info):
        pass

    def on_enum_completed(sender, obj):
        print("  (initial enumeration complete, watching for new devices...)")

    watcher.add_added(on_added)
    watcher.add_updated(on_updated)
    watcher.add_removed(on_removed)
    watcher.add_enumeration_completed(on_enum_completed)

    watcher.start()
    await asyncio.sleep(timeout)
    watcher.stop()

    print("-" * 60)
    print(f"Total Classic BT devices seen: {len(found)}")
    if not found:
        print("No Classic Bluetooth devices found.")
        print("-> Controller is likely BLE, not Classic BT.")
    else:
        for dev_id, name in found.items():
            print(f"  {name!r}  {dev_id}")


if __name__ == "__main__":
    asyncio.run(main())
