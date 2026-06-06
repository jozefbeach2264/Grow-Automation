#!/usr/bin/env python3
"""
proxy_ble.py — BLE MITM proxy: mirror the AC Infinity controller on this PC,
then let the AC Infinity app connect to us instead of the real controller.

We stay connected to the real controller and relay all traffic bidirectionally.
Every write from the phone and every notification from the controller is logged
to a .jsonl file.

Usage:
    python scripts/proxy_ble.py --address 50:78:7D:C5:0C:6E --profile inspect-50787d.json --out proxy-capture.jsonl

Setup:
    1. Run this script — it connects to the controller AND starts advertising.
    2. In the AC Infinity app on your phone, forget/disconnect from the controller.
    3. Scan for new devices — you will see the proxy (same service UUIDs).
    4. Connect to it.  All writes the app sends will be logged and relayed.

NOTE: The real controller may only allow one BLE connection.  If it does,
      the phone will connect to OUR PC (which stays connected to the
      controller), giving us a full man-in-the-middle view.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aci_ble_lab.common import (
    bold, dim, green, cyan, yellow,
    print_ok, print_warn, print_err,
)

_event_count = 0


def _on_event(direction: str, record: dict) -> None:
    global _event_count
    _event_count += 1

    uuid   = record["characteristic"]
    hexv   = record["hex"]
    nbytes = record["length"]
    ts     = record["timestamp"]

    spaced = " ".join(hexv[i:i+2] for i in range(0, len(hexv), 2))

    if "write" in direction:
        arrow = green("phone→ctrl")
    elif "notify" in direction:
        arrow = cyan("ctrl→phone")
    else:
        arrow = yellow("read")

    print(f"  {dim(ts)}  {arrow}  {cyan(uuid[:8])}…")
    print(f"    {spaced}  {dim(f'({nbytes}B)')}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BLE MITM proxy — captures phone→controller writes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--address", required=True,
                   help="Real controller BLE address (e.g. 50:78:7D:C5:0C:6E)")
    p.add_argument("--profile", required=True,
                   help="Path to inspect .json from inspect_ble.py")
    p.add_argument("--out", default="proxy-capture.jsonl",
                   help="Output .jsonl file for captured traffic (default: proxy-capture.jsonl)")
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    profile_path = Path(args.profile)
    if not profile_path.exists():
        print_err(f"Profile not found: {args.profile}")
        print_warn("Run inspect_ble.py first:  python scripts/inspect_ble.py --address <ADDR> --out inspect.json")
        sys.exit(1)

    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    print()
    print(bold("ACI BLE Lab — MITM Proxy"))
    print(f"  Controller : {cyan(args.address)}")
    print(f"  Profile    : {args.profile}")
    print(f"  Output     : {args.out}")
    print()
    print(bold("SETUP STEPS:"))
    print("  1. This PC will connect to the real controller and start advertising.")
    print("  2. In the AC Infinity app, forget/disconnect the controller.")
    print("  3. Scan for BLE devices — connect to the one with the same service UUIDs.")
    print("  4. Use the app normally.  Every write will be captured here.")
    print()
    print("─" * 72)

    # Import here so import errors surface clearly
    try:
        from aci_ble_lab.proxy import BleProxy
    except ImportError as exc:
        print_err(f"Import failed: {exc}")
        print_warn("Ensure winrt packages are installed in the venv.")
        sys.exit(1)

    proxy = BleProxy(
        address=args.address,
        profile=profile,
        output_path=args.out,
        on_event=_on_event,
    )

    try:
        await proxy.run()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print_err(str(exc))
        sys.exit(1)

    print()
    print("─" * 72)
    print(f"Session ended.  Captured {bold(str(_event_count))} event(s).")
    if _event_count > 0:
        print_ok(f"Traffic saved → {args.out}")
        print()
        print("Next step: run the analyzer:")
        print(f"  python scripts/analyze_packets.py --in {args.out}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\nStopped.  Captured {bold(str(_event_count))} event(s).")
