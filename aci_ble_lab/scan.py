"""BLE scanner: discover devices, flag AC Infinity candidates."""

import asyncio
from typing import Callable

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .common import is_aci_device, now_iso


def _adv_to_record(device: BLEDevice, adv: AdvertisementData) -> dict:
    name = device.name or adv.local_name or ""
    return {
        "address": device.address,
        "name": name,
        "rssi": adv.rssi,
        "tx_power": adv.tx_power,
        "service_uuids": list(adv.service_uuids),
        "manufacturer_data": {
            str(company_id): data.hex()
            for company_id, data in adv.manufacturer_data.items()
        },
        "service_data": {
            str(uuid): data.hex()
            for uuid, data in adv.service_data.items()
        },
        "is_aci": is_aci_device(name),
        "first_seen": now_iso(),
        "last_seen": now_iso(),
    }


async def scan_ble(
    timeout: float = 10.0,
    on_device: Callable[[dict, bool], None] | None = None,
    scanning_mode: str = "active",
) -> list[dict]:
    """
    Scan for BLE devices for *timeout* seconds.

    *on_device* is called with ``(record, is_new)`` on every advertisement.
    *scanning_mode* is ``"active"`` (sends scan requests, gets scan responses /
    complete names) or ``"passive"`` (listens only, no scan requests).
    Returns a list of unique device records sorted by RSSI descending.
    """
    seen: dict[str, dict] = {}

    def _cb(device: BLEDevice, adv: AdvertisementData) -> None:
        addr = device.address
        record = _adv_to_record(device, adv)
        is_new = addr not in seen
        if is_new:
            seen[addr] = record
        else:
            seen[addr]["last_seen"] = now_iso()
            seen[addr]["rssi"] = adv.rssi
            name = device.name or adv.local_name or ""
            if name:
                seen[addr]["name"] = name
                seen[addr]["is_aci"] = is_aci_device(name)
            # Merge new service UUIDs
            existing = set(seen[addr]["service_uuids"])
            existing.update(adv.service_uuids)
            seen[addr]["service_uuids"] = list(existing)

        if on_device is not None:
            on_device(seen[addr], is_new)

    async with BleakScanner(detection_callback=_cb, scanning_mode=scanning_mode):
        await asyncio.sleep(timeout)

    results = list(seen.values())
    results.sort(key=lambda r: (r["rssi"] or -999), reverse=True)
    return results
