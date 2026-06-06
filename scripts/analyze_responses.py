#!/usr/bin/env python3
"""Analyze command probe results."""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aci_ble_lab.db import init_schema, add_note

with open("captures/cmd_probe.jsonl") as f:
    rows = [json.loads(l) for l in f]

print("=== Response Pattern Analysis ===")
print(f"{'CMD HEX':<25}  {'RESP':<12}  CTR   B3    B4")
print("-" * 60)
for r in rows:
    if r["response_hex"] is None:
        print(f"{r['cmd_hex']:<25}  {'NO RESPONSE':<12}")
        continue
    resp = bytes.fromhex(r["response_hex"])
    ctr = resp[2] if len(resp) > 2 else -1
    b3  = resp[3] if len(resp) > 3 else -1
    b4  = resp[4] if len(resp) > 4 else -1
    flag = "  <-- non-zero" if b4 != 0 else ""
    print(f"{r['cmd_hex']:<25}  {r['response_hex']:<12}  0x{ctr:02x}  0x{b3:02x}  0x{b4:02x}{flag}")

# Summarize
non_zero_b4 = [(r["cmd_hex"], bytes.fromhex(r["response_hex"])[4])
               for r in rows if r["response_hex"] and bytes.fromhex(r["response_hex"])[4] != 0]

print()
print(f"Non-zero B4 responses ({len(non_zero_b4)}):")
for cmd, b4 in non_zero_b4:
    print(f"  cmd={cmd}  b4=0x{b4:02x}={b4}")

# Header analysis
print()
print("Response header bytes 0-1:")
headers = set()
for r in rows:
    if r["response_hex"]:
        h = r["response_hex"][:4]
        headers.add(h)
print(f"  Unique: {headers}")

print()
print("Summary:")
print("  Format: 49 04 [counter] 01 [code]")
print("  - 49 04 = fixed response header")
print("  - counter: increments per command (0x00..0x16 for 23 commands)")
print("  - byte 3: always 0x01")
print("  - byte 4 (code): 0x00=generic ACK, non-zero may indicate special handling")

init_schema()
add_note("ff01_response_format",
         "Every write to 0000ff01 produces a 5-byte notification on 0000ff02. "
         "Format: 49 04 [seq] 01 [code]. "
         "seq = session counter, increments per command (0x00 at session start). "
         "code: 0x00 on all tested commands except: "
         "cmd=01 -> code=0x05, cmd=1eff0100 -> code=0x02. "
         "All 23 diverse test commands (random bytes to proper frames) got responses, "
         "suggesting 0x00 means 'received but unknown command' and non-zero codes "
         "indicate recognized/special commands.",
         confidence="hypothesis")
print()
print("Response format note stored in DB.")
