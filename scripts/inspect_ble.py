#!/usr/bin/env python3
"""
inspect_ble.py — Connect to a BLE device and dump all GATT services/characteristics.

Usage:
    python scripts/inspect_ble.py --address AA:BB:CC:DD:EE:FF
    python scripts/inspect_ble.py --address AA:BB:CC:DD:EE:FF --out services.json

IMPORTANT: Close the AC Infinity app on your phone before running this script.
           The controller may only allow one BLE central connection at a time.
"""

import argparse
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aci_ble_lab.inspect import inspect_device
from aci_ble_lab.common import (
    save_json,
    print_ok,
    print_warn,
    print_err,
    bold,
    dim,
    green,
    cyan,
    yellow,
)


def _prop_display(props: list[str]) -> str:
    highlights = {"read", "write", "notify", "indicate", "write-without-response"}
    parts = []
    for p in props:
        if p in highlights:
            parts.append(yellow(p) if "write" in p else green(p) if p in {"notify", "indicate"} else p)
        else:
            parts.append(dim(p))
    return "  ".join(parts)


def _print_result(result: dict) -> None:
    print()
    print(bold("ACI BLE Lab — GATT Inspector"))
    print(f"  Address   : {cyan(result['address'])}")
    print(f"  Connected : {result['connected']}")
    print(f"  Timestamp : {result['inspected_at']}")

    if result.get("error"):
        print_err(f"Error: {result['error']}")
        return

    services = result.get("services", [])
    print(f"  Services  : {len(services)}")
    notify_chars = result.get("notify_characteristics", [])
    if notify_chars:
        print(f"  {green('Notify/Indicate characteristics:')}")
        for u in notify_chars:
            print(f"    {green(u)}")

    print()
    for i, svc in enumerate(services, 1):
        svc_name = svc["description"] or svc["uuid"]
        print(f"  {bold(f'Service {i}:')} {cyan(svc['uuid'])}  {dim(svc_name)}  handle={svc['handle']}")

        for char in svc["characteristics"]:
            char_name = char["description"] or char["uuid"]
            props_str  = _prop_display(char["properties"])
            print(f"    ├─ {char['uuid']}  h={char['handle']}")
            print(f"    │    name  : {dim(char_name)}")
            print(f"    │    props : {props_str}")

            if char.get("value_hex") is not None:
                hex_val  = char["value_hex"]
                utf8_val = char.get("value_utf8")
                val_str  = f"  → utf8: \"{utf8_val}\"" if utf8_val else ""
                print(f"    │    value : {hex_val}{val_str}")
            elif char.get("read_error"):
                print(f"    │    value : {dim('(read error: ' + char['read_error'] + ')')}")

            for desc in char.get("descriptors", []):
                print(f"    │      └─ desc {desc['uuid']}  h={desc['handle']}  {dim(desc.get('description', ''))}")

        print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Connect to a BLE device and dump its full GATT profile.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--address", required=True,
                   help="BLE device address (e.g. AA:BB:CC:DD:EE:FF or UUID on macOS)")
    p.add_argument("--out", type=str, default=None,
                   help="Save GATT dump to this JSON file path")
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    print(f"\nConnecting to {cyan(args.address)} …")
    print(bold("!! Close the AC Infinity app on your phone first !!\n"))

    result = await inspect_device(args.address)

    _print_result(result)

    if args.out:
        save_json(result, args.out)
        print_ok(f"GATT dump saved → {args.out}")
        print()

    if result.get("notify_characteristics"):
        print("Next step:  subscribe to notifications.")
        print("  python scripts/listen_ble.py --address " + args.address + " --out notifications.jsonl")
        print("  (or target a specific char with --char <UUID>)")
    elif result["connected"]:
        print_warn("No notify/indicate characteristics found on this device.")

    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInspection cancelled.")
    except Exception as exc:
        print_err(str(exc))
        sys.exit(1)
