"""GATT inspector: connect, enumerate services/characteristics, attempt reads."""

import asyncio

from bleak import BleakClient
from bleak.exc import BleakError

from .common import now_iso


async def inspect_device(address: str) -> dict:
    """
    Connect to *address* and return a full GATT service/characteristic dump.

    Only characteristics with the ``read`` property are read.
    No writes are ever performed.
    """
    result: dict = {
        "address": address,
        "inspected_at": now_iso(),
        "connected": False,
        "services": [],
        "notify_characteristics": [],
        "error": None,
    }

    try:
        async with BleakClient(address) as client:
            if not client.is_connected:
                raise BleakError(f"BleakClient connected=False after context entry for {address}")

            result["connected"] = True

            for service in client.services:
                svc_record = {
                    "uuid": str(service.uuid),
                    "description": service.description or "",
                    "handle": service.handle,
                    "characteristics": [],
                }

                for char in service.characteristics:
                    props = list(char.properties)

                    char_record = {
                        "uuid": str(char.uuid),
                        "description": char.description or "",
                        "handle": char.handle,
                        "properties": props,
                        "value_hex": None,
                        "value_utf8": None,
                        "read_error": None,
                        "descriptors": [],
                    }

                    for desc in char.descriptors:
                        char_record["descriptors"].append({
                            "uuid": str(desc.uuid),
                            "handle": desc.handle,
                            "description": getattr(desc, "description", "") or "",
                        })

                    if "read" in props:
                        try:
                            raw = await client.read_gatt_char(char.uuid)
                            char_record["value_hex"] = raw.hex()
                            try:
                                char_record["value_utf8"] = raw.decode("utf-8")
                            except UnicodeDecodeError:
                                pass
                        except Exception as exc:
                            char_record["read_error"] = str(exc)

                    if "notify" in props or "indicate" in props:
                        result["notify_characteristics"].append(str(char.uuid))

                    svc_record["characteristics"].append(char_record)

                result["services"].append(svc_record)

    except BleakError as exc:
        result["error"] = str(exc)
    except asyncio.TimeoutError:
        result["error"] = "Connection timed out."

    return result
