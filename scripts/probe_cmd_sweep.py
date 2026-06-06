#!/usr/bin/env python3
"""
probe_cmd_sweep.py — Focused command sweep to identify valid command IDs.

Round 2 - based on probe_commands.py findings:
  - cmd=01 -> b4=0x05 (anomalous)
  - cmd=1eff0100 -> b4=0x02 (anomalous; 1EFF is status packet header)

This sweep tries:
  A. 1E FF XX 00  (XX = 00..3F) — sweep command IDs with 1EFF framing
  B. 1E FF 01 XX  (XX = 00..1F) — sweep argument byte for cmd=01
  C. 49 04 00 XX 00  — try response header format as command
  D. Single bytes 00..0F
  E. 1E FF XX 00 00 (3-byte payload)
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
AUTH_TOKEN   = bytes.fromhex("11223344")

# Sweep groups
def build_sweep():
    cmds = []
    # A: 1EFF sweep — cmd byte 00..3F
    for c in range(0x40):
        cmds.append((bytes([0x1E, 0xFF, c, 0x00]), f"1EFF sweep cmd=0x{c:02x}"))
    # B: 1EFF 01 argument sweep
    for a in range(0x20):
        cmds.append((bytes([0x1E, 0xFF, 0x01, a]), f"1EFF cmd=01 arg=0x{a:02x}"))
    # C: response-header-as-command format
    for c in range(0x10):
        cmds.append((bytes([0x49, 0x04, 0x00, c, 0x00]), f"49-04 cmd=0x{c:02x}"))
    # D: single byte sweep
    for b in range(0x10):
        cmds.append((bytes([b]), f"single byte 0x{b:02x}"))
    # E: 1EFF + 3-byte payload sweep
    for c in range(0x20):
        cmds.append((bytes([0x1E, 0xFF, c, 0x00, 0x00]), f"1EFF+3B cmd=0x{c:02x}"))
    return cmds


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


async def main(address: str, out_path: Path):
    init_schema()
    sweep = build_sweep()
    results = []

    print(f"\n=== AC Infinity Command Sweep (Round 2) ===")
    print(f"Commands to send: {len(sweep)}")
    print()

    print("Scanning...")
    if not await _scan_until_found(address):
        print("Not found.")
        return

    print("Connecting...")
    async with BleakClient(address) as client:
        print(f"Connected (MTU={client.mtu_size}B)")

        # Auth first
        try:
            await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=False)
            print("Auth written.")
        except Exception as e:
            print(f"Auth failed: {e}")

        await asyncio.sleep(0.3)

        # Subscribe to response channel
        cmd_responses = []
        def on_response(_, data):
            cmd_responses.append(bytes(data))

        try:
            await client.start_notify(CHAR_CMD_OUT, on_response)
            print("Subscribed to ff02.\n")
        except Exception as e:
            print(f"Could not subscribe to ff02: {e}")
            return

        await asyncio.sleep(0.3)

        # Track anomalies (b4 != 0x00)
        anomalies = []

        for cmd_bytes, desc in sweep:
            cmd_responses.clear()
            try:
                await client.write_gatt_char(CHAR_CMD_IN, cmd_bytes, response=False)
            except Exception as e:
                print(f"  WRITE ERR {cmd_bytes.hex()}: {e}")
                continue

            await asyncio.sleep(0.25)

            if cmd_responses:
                resp = cmd_responses[-1]
                b4 = resp[4] if len(resp) > 4 else -1
                results.append({
                    "cmd_hex": cmd_bytes.hex(),
                    "cmd_desc": desc,
                    "response_hex": resp.hex(),
                    "b4": b4,
                })
                if b4 != 0:
                    anomalies.append((cmd_bytes.hex(), resp.hex(), b4, desc))
                    print(f"  ANOMALY  {cmd_bytes.hex():<20}  resp={resp.hex()}  b4=0x{b4:02x}  ({desc})")
            else:
                results.append({
                    "cmd_hex": cmd_bytes.hex(),
                    "cmd_desc": desc,
                    "response_hex": None,
                    "b4": None,
                })

        print()
        print(f"Sweep done. {len(anomalies)} anomalous responses (b4 != 0x00):")
        for cmd, resp, b4, desc in anomalies:
            print(f"  cmd={cmd:<20}  resp={resp}  b4=0x{b4:02x}  {desc}")

        # Store anomalies in DB
        for cmd, resp, b4, desc in anomalies:
            add_command(
                char_uuid=CHAR_CMD_IN,
                hex_data=cmd,
                description=desc,
                response_hex=resp,
                source="sweep",
                confirmed=False,
                notes=f"Non-zero b4=0x{b4:02x} in response — may be recognized command"
            )

        if anomalies:
            summary = "; ".join(f"cmd={c} b4=0x{b:02x}" for c, _, b, _ in anomalies)
            add_note("sweep_anomalies",
                     f"Round-2 sweep found {len(anomalies)} anomalous responses: {summary}",
                     confidence="hypothesis")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
        print(f"\nResults: {out_path}")

        try:
            await client.stop_notify(CHAR_CMD_OUT)
        except Exception:
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--address", default=ADDRESS)
    p.add_argument("--out", default="captures/cmd_sweep.jsonl")
    args = p.parse_args()
    asyncio.run(main(args.address, Path(args.out)))
