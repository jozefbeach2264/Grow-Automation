#!/usr/bin/env python3
"""
probe_commands.py — Systematic command probing on 0000ff01.

Strategy:
  1. Connect and subscribe to ff02 (command response) + 70d51002 (status)
  2. Write auth token 0x11223344 to 70d51001
  3. Fire candidate commands; log every response
  4. Store confirmed commands in controller.db

Usage:
  python scripts/probe_commands.py [--address ADDR] [--out captures/cmd_probe.jsonl]
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner
from aci_ble_lab.db import init_schema, add_command, add_note

ADDRESS = "50:78:7D:C5:0C:6E"

CHAR_AUTH    = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_STATUS  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_CMD_IN  = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_CMD_OUT = "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_8020    = "00008020-0000-1000-8000-00805f9b34fb"
CHAR_8021    = "00008021-0000-1000-8000-00805f9b34fb"

AUTH_TOKEN = bytes.fromhex("11223344")

# Candidate commands to try against 0000ff01.
# Format: (hex_string, description)
# Patterns informed by:
#   - Status packet header: 1E FF ...
#   - Common ESP32 BLE command framing
#   - AC Infinity app behavior (query-then-set pattern suspected)
CANDIDATES = [
    # ── Minimal probes ────────────────────────────────────────────────────────
    ("01",                   "single byte 0x01 — minimal command"),
    ("0100",                 "two bytes 0x0100"),
    ("1e",                   "status header byte 0x1E alone"),
    ("1eff",                 "status header 1E FF — echo the packet header"),
    ("1e00",                 "1E 00 — header + zero type"),
    ("1e01",                 "1E 01 — header + cmd type 1"),
    ("1e02",                 "1E 02 — query fw version?"),
    ("1e03",                 "1E 03"),
    # ── Length-prefixed style ─────────────────────────────────────────────────
    ("0101",                 "len=1, cmd=1"),
    ("020100",               "len=2, cmd=1, port=0"),
    # ── Common BLE device patterns ────────────────────────────────────────────
    ("aa01",                 "0xAA header + cmd 1 (common pattern)"),
    ("aa0100",               "0xAA header + cmd 1 + port 0"),
    ("aa0200",               "0xAA header + cmd 2"),
    ("5500",                 "0x55 start byte"),
    ("ff01",                 "FF 01 — simple query"),
    ("ff00",                 "FF 00"),
    # ── Auth echo / session ───────────────────────────────────────────────────
    ("11223344",             "auth token echo to ff01"),
    ("11223344 01",          "auth + cmd byte 1"),
    # ── Get/query style with port 0 ──────────────────────────────────────────
    ("0100 0000",            "get port 0 config (4B)"),
    ("0200 0000",            "query port 0 status"),
    # ── 0x1E framed with length ───────────────────────────────────────────────
    ("1eff 0100",            "1E FF len=1 cmd=0"),
    ("1eff 0101",            "1E FF len=1 cmd=1"),
    ("1eff 01 00 00",        "1E FF + 3 byte payload"),
]

def _clean_hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


async def _scan_until_found(address: str, timeout: int = 120) -> bool:
    found = asyncio.Event()
    def cb(dev, _adv):
        if dev.address.upper() == address.upper():
            found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def main(address: str, out_path: Path, args=None):
    init_schema()
    responses = []

    print(f"\n=== AC Infinity Command Probe ===")
    print(f"Target : {address}")
    print(f"Output : {out_path}")
    print()

    print("Scanning for controller...")
    if not await _scan_until_found(address):
        print("Controller not found — aborting.")
        return

    print("Controller found — connecting...")

    async with BleakClient(address) as client:
        print(f"Connected (MTU={client.mtu_size}B)")
        print()

        # ── Pair if requested / needed ────────────────────────────────────────
        if args.pair:
            print("Pairing device...")
            try:
                await client.pair()
                print("  Paired OK")
            except Exception as e:
                print(f"  Pair failed: {e}")
            await asyncio.sleep(1)

        # ── Authenticate FIRST (before subscribing) ───────────────────────────
        # Write auth token before enabling notifications; some devices gate
        # subscription on authenticated session.
        print(f"Writing auth token {AUTH_TOKEN.hex()} to 70d51001...")
        try:
            await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
            print("  Auth write sent (no-response)")
        except Exception as e:
            print(f"  Auth write failed: {e}")
            print("  Controller may have disconnected — re-check BT connection")

        await asyncio.sleep(0.5)

        # ── Subscribe to notification channels ────────────────────────────────
        status_packets = []
        cmd_responses = []

        def on_status(_, data):
            pkt = bytes(data)
            status_packets.append(pkt)

        def on_cmd_response(_, data):
            pkt = bytes(data)
            ts = time.time()
            cmd_responses.append((ts, pkt))
            print(f"  [ff02 response] {pkt.hex()}  ({len(pkt)}B)")

        for uuid, name, handler in [
            (CHAR_CMD_OUT, "command response (0000ff02)", on_cmd_response),
            (CHAR_STATUS,  "status (70d51002)",           on_status),
        ]:
            try:
                await client.start_notify(uuid, handler)
                print(f"Subscribed to {name}")
            except Exception as e:
                print(f"  [warn] could not subscribe to {name}: {e}")

        await asyncio.sleep(1)  # Let first status packet arrive

        await asyncio.sleep(0.5)

        # ── Read 8020 and 8021 before probing ─────────────────────────────────
        print()
        print("Reading per-port chars before probe:")
        for uuid, name in [(CHAR_8020, "8020"), (CHAR_8021, "8021")]:
            try:
                data = bytes(await client.read_gatt_char(uuid))
                nonzero = sum(1 for b in data if b)
                print(f"  {name}: {len(data)}B  non-zero bytes: {nonzero}  head: {data[:16].hex()}")
            except Exception as e:
                print(f"  {name}: read error: {e}")

        # ── Command probe loop ────────────────────────────────────────────────
        print()
        print(f"Probing {len(CANDIDATES)} candidate commands on 0000ff01:")
        print("-" * 70)

        for hex_str, desc in CANDIDATES:
            cmd_bytes = _clean_hex(hex_str)
            cmd_responses.clear()

            try:
                await client.write_gatt_char(CHAR_CMD_IN, cmd_bytes, response=False)
                write_ok = True
            except Exception as e:
                print(f"  WRITE ERR  {cmd_bytes.hex():20}  {desc[:40]}  -> {e}")
                write_ok = False

            if write_ok:
                # Wait briefly for response notification
                await asyncio.sleep(0.3)

                if cmd_responses:
                    for ts, resp in cmd_responses:
                        print(f"  RESPONSE   {cmd_bytes.hex():20}  -> {resp.hex()}  ({desc})")
                        responses.append({
                            "cmd_hex": cmd_bytes.hex(),
                            "cmd_desc": desc,
                            "response_hex": resp.hex(),
                            "timestamp": ts,
                        })
                        add_command(
                            char_uuid=CHAR_CMD_IN,
                            hex_data=cmd_bytes.hex(),
                            description=desc,
                            response_hex=resp.hex(),
                            source="probe",
                            confirmed=False,
                            notes=f"Got response on ff02: {resp.hex()}"
                        )
                else:
                    print(f"  no resp    {cmd_bytes.hex():20}  ({desc})")
                    responses.append({
                        "cmd_hex": cmd_bytes.hex(),
                        "cmd_desc": desc,
                        "response_hex": None,
                        "timestamp": time.time(),
                    })

        print()
        print(f"Probe complete. {sum(1 for r in responses if r['response_hex'])} commands got responses.")

        # ── Final status read ─────────────────────────────────────────────────
        await asyncio.sleep(1)
        if status_packets:
            last = status_packets[-1]
            print(f"\nLast status packet ({len(last)}B): {last.hex()}")

        # ── Save capture ──────────────────────────────────────────────────────
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as f:
                for r in responses:
                    f.write(json.dumps(r) + "\n")
            print(f"\nResults saved to {out_path}")

        for uuid in (CHAR_STATUS, CHAR_CMD_OUT):
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--address", default=ADDRESS)
    p.add_argument("--out", default="captures/cmd_probe.jsonl")
    p.add_argument("--pair", action="store_true", help="Pair/bond device before probing")
    args = p.parse_args()
    asyncio.run(main(args.address, Path(args.out), args))
