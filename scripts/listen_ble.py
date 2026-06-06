#!/usr/bin/env python3
"""
listen_ble.py — Subscribe to BLE notifications and log raw hex to console and file.

Usage:
    # Subscribe to ALL notify/indicate characteristics:
    python scripts/listen_ble.py --address AA:BB:CC:DD:EE:FF --out notifications.jsonl

    # Target a specific characteristic UUID:
    python scripts/listen_ble.py --address AA:BB:CC:DD:EE:FF --char <UUID> --out notifications.jsonl

    # Multiple characteristics:
    python scripts/listen_ble.py --address AA:BB:CC:DD:EE:FF --char <UUID1> --char <UUID2> --out notifications.jsonl

    # Timed capture (30 seconds):
    python scripts/listen_ble.py --address AA:BB:CC:DD:EE:FF --duration 30 --out notifications.jsonl

IMPORTANT: Close the AC Infinity app on your phone before running this script.
           The controller may only allow one BLE central connection at a time.
NOTE: This script is READ-ONLY.  No writes are ever performed.
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

from aci_ble_lab.listen import listen_device
from aci_ble_lab.common import (
    print_ok,
    print_warn,
    print_err,
    bold,
    dim,
    green,
    cyan,
    yellow,
)

_notification_count = 0


def _on_subscribed(subscribed: list[str], skipped: list[tuple[str, str]]) -> None:
    print()
    print(f"Subscribed to {bold(str(len(subscribed)))} characteristic(s):")
    for u in subscribed:
        print(f"  {green(u)}")
    if skipped:
        print(f"\nSkipped {len(skipped)} (access denied / requires bonding):")
        for u, err in skipped:
            print(f"  {dim(u)}  {dim(err)}")
    print()
    print("Waiting for notifications…  Press Ctrl+C to stop.")
    print("─" * 72)


def _on_notification(record: dict) -> None:
    global _notification_count
    _notification_count += 1

    ts     = record["timestamp"]
    uuid   = record["characteristic"]
    hexv   = record["hex"]
    nbytes = record["length"]

    spaced = " ".join(hexv[i:i+2] for i in range(0, len(hexv), 2))
    print(f"  {dim(ts)}  {cyan(uuid)}")
    print(f"    {green(spaced)}  {dim(f'({nbytes}B)')}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Subscribe to BLE notifications and log raw hex (read-only, no writes).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--address", required=True,
                   help="BLE device address (e.g. AA:BB:CC:DD:EE:FF or UUID on macOS)")
    p.add_argument("--char", dest="char_uuids", action="append", default=None,
                   metavar="UUID",
                   help="Characteristic UUID to subscribe to (repeatable). "
                        "Omit to subscribe to all notify/indicate characteristics.")
    p.add_argument("--out", type=str, default=None,
                   help="Append notifications as JSON lines to this .jsonl file")
    p.add_argument("--duration", type=float, default=None,
                   help="Stop after this many seconds (default: run until Ctrl+C)")
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    print()
    print(bold("ACI BLE Lab — Notification Listener"))
    print(bold("!! READ-ONLY — no writes will be performed !!"))
    print(bold("!! Close the AC Infinity app on your phone first !!\n"))
    print(f"  Address   : {cyan(args.address)}")

    if args.char_uuids:
        print(f"  Chars     : {len(args.char_uuids)} specified")
        for u in args.char_uuids:
            print(f"              {u}")
    else:
        print(f"  Chars     : {yellow('all notify/indicate')}")

    duration_str = f"{args.duration}s" if args.duration else "until Ctrl+C"
    print(f"  Duration  : {duration_str}")
    print(f"  Output    : {args.out if args.out else dim('console only')}")
    print(f"\nConnecting to {cyan(args.address)} …")

    subscribed = await listen_device(
        address=args.address,
        char_uuids=args.char_uuids,
        output_path=args.out,
        duration=args.duration,
        on_notification=_on_notification,
        on_subscribed=_on_subscribed,
    )

    print()
    print("─" * 72)
    print(f"Session ended.  Received {bold(str(_notification_count))} notification(s).")
    if args.out and _notification_count > 0:
        print_ok(f"Notifications saved → {args.out}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\nStopped.  Captured {bold(str(_notification_count))} notification(s).")
        if _notification_count == 0:
            print_warn("No notifications received.  The controller may not push data passively.")
            print_warn("Try changing a setting in the AC Infinity app while this script is running.")
    except RuntimeError as exc:
        print_err(str(exc))
        sys.exit(1)
    except Exception as exc:
        print_err(str(exc))
        sys.exit(1)
