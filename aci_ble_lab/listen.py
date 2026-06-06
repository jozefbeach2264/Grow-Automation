"""Notification listener: subscribe to notify/indicate characteristics and log raw hex."""

import asyncio
from typing import Callable

from bleak import BleakClient
from bleak.exc import BleakError

from .common import now_iso, append_jsonl


async def listen_device(
    address: str,
    char_uuids: list[str] | None = None,
    output_path: str | None = None,
    duration: float | None = None,
    on_notification: Callable[[dict], None] | None = None,
    on_subscribed: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """
    Connect to *address* and subscribe to BLE notifications.

    Parameters
    ----------
    address:        BLE device address.
    char_uuids:     Explicit list of characteristic UUIDs to subscribe to.
                    If None or empty, subscribe to ALL notify/indicate characteristics.
    output_path:    Path to a .jsonl file; each notification is appended.
    duration:       Seconds to listen before returning.  None = run until cancelled
                    (press Ctrl+C in the calling script).
    on_notification: Callback invoked with each notification record dict.
    on_subscribed:  Callback invoked once with the list of subscribed UUIDs,
                    right after subscription is established and before blocking.

    Returns
    -------
    List of characteristic UUIDs that were subscribed to.

    Notes
    -----
    NO writes are ever performed.  This function is read/observe only.
    """
    target_uuids: set[str] = {u.lower() for u in char_uuids} if char_uuids else set()
    subscribed: list[str] = []
    skipped: list[tuple[str, str]] = []

    def make_handler(uuid: str) -> Callable:
        def _handler(characteristic, data: bytearray) -> None:
            record = {
                "timestamp": now_iso(),
                "characteristic": uuid,
                "hex": data.hex(),
                "bytes": list(data),
                "length": len(data),
            }
            if on_notification is not None:
                on_notification(record)
            if output_path is not None:
                append_jsonl(record, output_path)
        return _handler

    async with BleakClient(address) as client:
        if not client.is_connected:
            raise BleakError(f"Failed to connect to {address}")

        for service in client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if not props & {"notify", "indicate"}:
                    continue

                uuid = str(char.uuid).lower()
                if target_uuids and uuid not in target_uuids:
                    continue

                try:
                    await client.start_notify(char.uuid, make_handler(uuid))
                    subscribed.append(uuid)
                except Exception as exc:
                    # Some characteristics (e.g. Service Changed) require bonding.
                    # Skip and continue rather than aborting the whole session.
                    skipped.append((uuid, str(exc)))

        if not subscribed:
            raise RuntimeError(
                "No notify/indicate characteristics matched (all failed to subscribe). "
                "Run inspect_ble.py first to find available UUIDs."
            )

        if on_subscribed is not None:
            on_subscribed(subscribed, skipped)

        try:
            if duration is not None:
                await asyncio.sleep(duration)
            else:
                # Block until Ctrl+C (KeyboardInterrupt) bubbles up through asyncio.run().
                while True:
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            for uuid in subscribed:
                try:
                    await client.stop_notify(uuid)
                except Exception:
                    pass

    return subscribed
