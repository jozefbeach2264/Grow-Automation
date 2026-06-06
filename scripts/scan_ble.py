#!/usr/bin/env python3
"""
scan_ble.py — Discover nearby BLE devices and highlight AC Infinity controllers.

Usage:
    python scripts/scan_ble.py
    python scripts/scan_ble.py --timeout 20 --out scan-results.json

IMPORTANT: Close the AC Infinity app on your phone before running this script.
           The controller may only allow one BLE central connection at a time.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# UTF-8 stdout/stderr on Windows before any prints or imports that print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make the parent package importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aci_ble_lab.scan import scan_ble
from aci_ble_lab.common import (
    save_json,
    print_ok,
    print_warn,
    print_err,
    aci_badge,
    bold,
    dim,
    green,
    cyan,
)


def _format_rssi(rssi) -> str:
    if rssi is None:
        return "  ?"
    return f"{rssi:>4}"


def _on_device(record: dict, is_new: bool) -> None:
    if not is_new:
        return

    badge  = aci_badge() + " " if record["is_aci"] else "       "
    addr   = cyan(record["address"])
    rssi   = _format_rssi(record.get("rssi"))
    name   = record["name"] or dim("(no name)")
    svc_count = len(record.get("service_uuids", []))
    svc_hint  = f"  {dim(f'[{svc_count} svc UUID(s)]')}" if svc_count else ""

    if record["is_aci"]:
        name = green(bold(name))

    print(f"  {badge}{addr}  RSSI {rssi}  {name}{svc_hint}")


def _print_header(timeout: float, mode: str = "active") -> None:
    print()
    print(bold("ACI BLE Lab — Scanner"))
    print(f"Scanning for {timeout}s  [{mode} mode]…  Press Ctrl+C to stop early.\n")
    print(bold("  !! Close the AC Infinity app on your phone before continuing !!"))
    print()
    print(f"  {'Badge':<9} {'Address':<20} {'RSSI':>4}  Name")
    print("  " + "─" * 70)


def _print_summary(results: list[dict], out_path: str | None) -> None:
    print()
    print("─" * 72)
    total = len(results)
    aci   = [r for r in results if r["is_aci"]]

    print(f"  Devices found:  {bold(str(total))}")

    if aci:
        print(f"  {aci_badge()} candidates:  {bold(str(len(aci)))}")
        for d in aci:
            svc_uuids = d.get("service_uuids", [])
            print(f"\n    Address : {cyan(d['address'])}")
            print(f"    Name    : {green(bold(d['name']))}")
            print(f"    RSSI    : {_format_rssi(d.get('rssi'))}")
            if svc_uuids:
                print(f"    Service UUIDs advertised:")
                for u in svc_uuids:
                    print(f"              {u}")
            mfr = d.get("manufacturer_data", {})
            if mfr:
                print(f"    Manufacturer data:")
                for cid, hexval in mfr.items():
                    print(f"              company 0x{int(cid):04X}  {hexval}")
    else:
        print_warn("No AC Infinity device detected.")
        print_warn("Check that the controller is powered on and BLE mode is active.")

    if out_path:
        print()
        print_ok(f"Results saved → {out_path}")

    print()
    if aci:
        print("Next step:  run inspect_ble.py with the address above.")
        print("  python scripts/inspect_ble.py --address <ADDRESS> --out services.json")
    print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan for nearby BLE devices and flag AC Infinity controllers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Scan duration in seconds (default: 10)")
    p.add_argument("--out", type=str, default=None,
                   help="Save results to this JSON file path")
    p.add_argument("--passive", action="store_true",
                   help="Use passive scanning (listen only, no scan requests). "
                        "Try this if the controller doesn't appear in active mode.")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    mode = "passive" if args.passive else "active"
    _print_header(args.timeout, mode)

    results = await scan_ble(timeout=args.timeout, on_device=_on_device, scanning_mode=mode)

    if args.out:
        save_json(results, args.out)

    _print_summary(results, args.out)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nScan stopped by user.")
    except Exception as exc:
        print_err(str(exc))
        sys.exit(1)
