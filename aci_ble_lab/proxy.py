"""
BLE MITM proxy: mirror controller GATT services on the PC, relay all traffic,
log every read/write/notification in both directions.

Architecture:
    Phone ─BLE─▶ [PC GATT server] ─bleak─▶ [Real controller]
    Phone ◀─BLE─ [PC GATT server] ◀─bleak─ [Real controller]

All phone writes are captured, relayed to the real controller, and logged.
All controller notifications are relayed to the phone and logged.
"""

import asyncio
import uuid as uuid_mod
from typing import Callable, Optional

from bleak import BleakClient
from bleak.exc import BleakError

from winrt.windows.devices.bluetooth import BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
)
from winrt.windows.storage.streams import DataWriter

from .common import now_iso, append_jsonl


# Map bleak property strings → WinRT enum flags
_PROP_FLAGS: dict[str, int] = {
    "read":                   GattCharacteristicProperties.READ,
    "write":                  GattCharacteristicProperties.WRITE,
    "write-without-response": GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE,
    "notify":                 GattCharacteristicProperties.NOTIFY,
    "indicate":               GattCharacteristicProperties.INDICATE,
}

# Services we don't proxy (Windows manages Generic Attribute internally;
# Service Changed requires bonding which we skip).
_SKIP_SERVICES = {
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute Profile
}


def _to_buffer(data: bytes):
    """Convert Python bytes → WinRT IBuffer."""
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()


def _from_buffer(buf) -> bytes:
    """Convert WinRT IBuffer → Python bytes."""
    return bytes(buf)


class BleProxy:
    """
    Runs both a BLE central connection (to the real controller) and a GATT
    peripheral server (advertising to the phone) simultaneously.
    """

    def __init__(
        self,
        address: str,
        profile: dict,
        output_path: Optional[str] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.address = address
        self.profile = profile
        self.output_path = output_path
        self.on_event = on_event

        self._central: Optional[BleakClient] = None
        self._providers: list = []
        self._local_chars: dict[str, object] = {}  # uuid → GattLocalCharacteristic
        self._cached_values: dict[str, bytes] = {}  # uuid → last known bytes
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    # ──────────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────────

    def _log(self, direction: str, char_uuid: str, data: bytes, **extra) -> None:
        record = {
            "timestamp": now_iso(),
            "direction": direction,
            "characteristic": char_uuid,
            "hex": data.hex(),
            "bytes": list(data),
            "length": len(data),
            **extra,
        }
        if self.output_path:
            append_jsonl(record, self.output_path)
        if self.on_event:
            self.on_event(direction, record)

    # ──────────────────────────────────────────────────────────────────────────
    # Peripheral (GATT server) setup
    # ──────────────────────────────────────────────────────────────────────────

    async def _create_peripheral(self) -> None:
        for svc in self.profile.get("services", []):
            svc_uuid = svc["uuid"]
            if svc_uuid in _SKIP_SERVICES:
                continue

            svc_guid = uuid_mod.UUID(svc_uuid)
            result = await GattServiceProvider.create_async(svc_guid)
            if result.error != BluetoothError.SUCCESS:
                print(f"  WARN  service {svc_uuid} create failed: {result.error}")
                continue

            provider = result.service_provider
            self._providers.append(provider)

            for char in svc.get("characteristics", []):
                await self._add_local_char(provider.service, char)

    async def _add_local_char(self, local_service, char: dict) -> None:
        char_uuid = char["uuid"]
        char_guid = uuid_mod.UUID(char_uuid)
        props_list: list[str] = char.get("properties", [])

        flags = 0
        for p in props_list:
            flags |= _PROP_FLAGS.get(p, 0)
        if flags == 0:
            return

        params = GattLocalCharacteristicParameters()
        params.characteristic_properties = flags
        params.read_protection_level = GattProtectionLevel.PLAIN
        params.write_protection_level = GattProtectionLevel.PLAIN

        # Seed static readable value from inspect snapshot
        if char.get("value_hex") and "read" in props_list:
            self._cached_values[char_uuid] = bytes.fromhex(char["value_hex"])

        r = await local_service.create_characteristic_async(char_guid, params)
        if r.error != BluetoothError.SUCCESS:
            print(f"  WARN  char {char_uuid} create failed: {r.error}")
            return

        lc = r.characteristic
        self._local_chars[char_uuid] = lc

        if "read" in props_list:
            lc.add_read_requested(self._make_read_handler(char_uuid))
        if "write" in props_list or "write-without-response" in props_list:
            lc.add_write_requested(self._make_write_handler(char_uuid))

    # ──────────────────────────────────────────────────────────────────────────
    # Event handlers (sync shell + async body pattern required by WinRT events)
    # ──────────────────────────────────────────────────────────────────────────

    def _make_read_handler(self, char_uuid: str):
        proxy = self

        def _handler(sender, args):
            deferral = args.get_deferral()

            async def _body():
                try:
                    request = await args.get_request_async()
                    # Try a live read from the real controller first
                    if proxy._central and proxy._central.is_connected:
                        try:
                            data = bytes(await proxy._central.read_gatt_char(char_uuid))
                            proxy._cached_values[char_uuid] = data
                        except Exception:
                            data = proxy._cached_values.get(char_uuid, b"\x00")
                    else:
                        data = proxy._cached_values.get(char_uuid, b"\x00")
                    request.respond_with_value(_to_buffer(data))
                    proxy._log("phone←ctrl (read)", char_uuid, data)
                except Exception as exc:
                    print(f"  [read handler] {char_uuid[:8]}: {exc}")
                finally:
                    deferral.complete()

            asyncio.ensure_future(_body())

        return _handler

    def _make_write_handler(self, char_uuid: str):
        proxy = self

        def _handler(sender, args):
            deferral = args.get_deferral()

            async def _body():
                try:
                    request = await args.get_request_async()
                    data = _from_buffer(request.value)
                    request.respond()
                    proxy._log("phone→ctrl (write)", char_uuid, data)
                    await proxy._write_queue.put((char_uuid, data))
                except Exception as exc:
                    print(f"  [write handler] {char_uuid[:8]}: {exc}")
                finally:
                    deferral.complete()

            asyncio.ensure_future(_body())

        return _handler

    def _make_notify_handler(self, char_uuid: str, local_char):
        proxy = self

        def _handler(characteristic, data: bytearray) -> None:
            data_bytes = bytes(data)
            proxy._cached_values[char_uuid] = data_bytes
            proxy._log("ctrl→phone (notify)", char_uuid, data_bytes)

            try:
                clients = local_char.subscribed_clients
                if clients:
                    buf = _to_buffer(data_bytes)
                    asyncio.ensure_future(local_char.notify_value_async(buf))
            except Exception as exc:
                print(f"  [notify relay] {char_uuid[:8]}: {exc}")

        return _handler

    # ──────────────────────────────────────────────────────────────────────────
    # Central (bleak) connection
    # ──────────────────────────────────────────────────────────────────────────

    async def _scan_until_found(self, timeout_each: float = 8.0, max_attempts: int = 15) -> None:
        """Scan until the controller appears in the Windows BLE cache, then stop."""
        from bleak import BleakScanner
        print(f"  Scanning for {self.address} (make sure controller is in BT mode)…")
        for attempt in range(1, max_attempts + 1):
            found = False

            def _cb(device, adv_data):
                nonlocal found
                if device.address.upper() == self.address.upper():
                    found = True

            async with BleakScanner(detection_callback=_cb):
                await asyncio.sleep(timeout_each)

            if found:
                print(f"  Found controller on attempt {attempt}")
                return
            print(f"  [{attempt}/{max_attempts}] Not seen yet — retrying…")

        raise BleakError(
            f"Controller {self.address} not found after {max_attempts} scan attempts. "
            "Ensure Bluetooth is enabled on the controller."
        )

    async def _connect_central(self) -> None:
        await self._scan_until_found()
        self._central = BleakClient(self.address)
        await self._central.connect()
        if not self._central.is_connected:
            raise BleakError(f"Failed to connect to {self.address}")

        notify_props = {"notify", "indicate"}
        subscribed, skipped = [], []
        for service in self._central.services:
            for char in service.characteristics:
                if not set(char.properties) & notify_props:
                    continue
                uuid = str(char.uuid).lower()
                lc = self._local_chars.get(uuid)
                if lc is None:
                    continue
                try:
                    await self._central.start_notify(
                        char.uuid, self._make_notify_handler(uuid, lc)
                    )
                    subscribed.append(uuid)
                except Exception as exc:
                    skipped.append((uuid, str(exc)))

        print(f"  Central subscribed to {len(subscribed)} notify characteristics")
        if skipped:
            print(f"  Skipped {len(skipped)} (bonding required):")
            for u, e in skipped:
                print(f"    {u[:8]}… {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Write relay worker
    # ──────────────────────────────────────────────────────────────────────────

    async def _relay_writes(self) -> None:
        while self._running:
            try:
                char_uuid, data = await asyncio.wait_for(
                    self._write_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            if not (self._central and self._central.is_connected):
                print(f"  [relay] Central disconnected — dropping write to {char_uuid[:8]}")
                continue

            try:
                await self._central.write_gatt_char(char_uuid, data)
                print(f"  [relay] Relayed write → {char_uuid[:8]}  {data.hex()}")
            except Exception as exc:
                print(f"  [relay] Write failed {char_uuid[:8]}: {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True

        print("  Creating GATT peripheral services…")
        await self._create_peripheral()
        print(f"  Created {len(self._local_chars)} local characteristics "
              f"across {len(self._providers)} services")

        print(f"  Connecting to real controller {self.address}…")
        await self._connect_central()

        print("  Starting BLE advertisement…")
        adv_params = GattServiceProviderAdvertisingParameters()
        adv_params.is_connectable = True
        adv_params.is_discoverable = True
        started, aborted = [], []
        for provider in self._providers:
            provider.start_advertising_with_parameters(adv_params)
            status = int(provider.advertisement_status)
            svc_uuid = str(provider.service.uuid)[:8]
            if status == 2:
                started.append(svc_uuid)
            else:
                aborted.append((svc_uuid, status))

        if started:
            print(f"  Advertising {len(started)} service(s): {started}")
        if aborted:
            print(f"  WARNING: {len(aborted)} service(s) failed to advertise (status!=Started): {aborted}")
        if not started:
            print("  NOTE: No services advertising — phone may not find this PC.")
            print("  This PC's BT name is DESKTOP-EB9C85H. The AC Infinity app may")
            print("  filter by name. Rename PC to ACI_V3.5_CTRLER in Windows Settings")
            print("  (Settings → System → About → Rename this PC) then restart.")
        print("  Advertising started — open AC Infinity app or nRF Connect and scan")

        relay_task = asyncio.create_task(self._relay_writes())
        try:
            while self._running:
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._running = False
            relay_task.cancel()
            await self._stop()

    async def _stop(self) -> None:
        for provider in self._providers:
            try:
                provider.stop_advertising()
            except Exception:
                pass
        if self._central and self._central.is_connected:
            try:
                await self._central.disconnect()
            except Exception:
                pass
