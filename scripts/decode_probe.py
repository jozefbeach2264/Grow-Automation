#!/usr/bin/env python3
"""
decode_probe.py — Decode advertisement payload and handle-53 structure from probe results.
Stores findings in controller.db.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aci_ble_lab.db import init_schema, add_note

init_schema()

# ── Advertisement payload decode ─────────────────────────────────────────────
# Company ID 0x0902 (unknown vendor), 27-byte payload from probe:
ADV_HEX = "50787dc50c6e5930545a330c140080008000000100800008000000"
payload = bytes.fromhex(ADV_HEX)

print("=== Advertisement Payload Decode ===")
print(f"Raw ({len(payload)}B): {payload.hex()}")
print()

# Bytes 0-5: own MAC address (little-endian BLE convention → reverse for display)
mac_le = payload[0:6]
mac_display = ":".join(f"{b:02X}" for b in reversed(mac_le))
mac_raw     = ":".join(f"{b:02X}" for b in mac_le)
print(f"  [0-5]  MAC (little-endian) : {mac_raw}")
print(f"         MAC (normal order)  : {mac_display}  (controller address)")

# Bytes 6-10: printable ASCII
fragment = payload[6:11]
try:
    frag_str = fragment.decode("ascii")
    printable = all(32 <= b < 127 for b in fragment)
except Exception:
    frag_str, printable = "", False
print(f"  [6-10] Serial fragment     : {fragment.hex()}  ASCII: {''.join(chr(b) if 32<=b<127 else '.' for b in fragment)}")

# Byte 11: 0x0C = 12 — matches fw_major observed in status packet (byte[2]=2? or separate)
b11 = payload[11]
print(f"  [11]   Firmware ref byte   : 0x{b11:02X} = {b11}  (matches 0x0C in fw/hw type)")

# Byte 12
b12 = payload[12]
print(f"  [12]   Unknown             : 0x{b12:02X} = {b12}")

# Bytes 13-26: 14 bytes — look for repeating patterns
tail = payload[13:]
print(f"  [13-26] Tail ({len(tail)}B)     : {tail.hex()}")

# Break into groups
for i in range(0, len(tail), 2):
    chunk = tail[i:i+2]
    if chunk:
        print(f"           [{13+i}-{13+i+len(chunk)-1}]  {chunk.hex()}")

# Identify bitmask regions
print()
print("  Flags analysis (tail bytes):")
for idx, b in enumerate(tail):
    if b:
        bits = bin(b)[2:].zfill(8)
        print(f"    offset {13+idx}: 0x{b:02X}  {bits}")

# ── Store adv decode ──────────────────────────────────────────────────────────
add_note("adv_payload_decode",
         f"Advertisement payload ({len(payload)}B, company ID 0x0902): "
         f"bytes[0-5]=MAC LE ({mac_raw}), "
         f"bytes[6-10]=serial fragment '{frag_str}' ({fragment.hex()}), "
         f"byte[11]=0x{b11:02X} (firmware/hw reference byte = {b11}), "
         f"byte[12]=0x{b12:02X} (unknown), "
         f"bytes[13-26]=capability flags ({tail.hex()}). "
         f"Non-zero flag bytes at offsets: "
         + ", ".join(f"[{13+i}]=0x{b:02X}" for i, b in enumerate(tail) if b),
         confidence="hypothesis")

print()
print("  Adv decode note stored.")


# ── Handle 53 (00008020) structure decode ────────────────────────────────────
# Handle 53 returned 600 bytes of zeros when read (unexpected for write+indicate char)
# 600 bytes analysis:

print()
print("=== Handle 53 (00008020) Structure Analysis ===")
print("Raw: 600 bytes of 0x00 (all zeros)")
print()

h53_size = 600
candidates = []
for ports in range(1, 20):
    if h53_size % ports == 0:
        bpp = h53_size // ports
        candidates.append((ports, bpp))

print("  Possible port/entry structures:")
for ports, bpp in candidates:
    marker = " <--" if ports in (8, 10, 16) else ""
    print(f"    {ports:2d} entries x {bpp:3d} bytes/entry{marker}")

print()
print("  Most likely interpretations:")
print("  (a) 8 ports x 75 bytes/port  — AC Infinity typically has up to 8 channels")
print("  (b) 10 ports x 60 bytes/port — possible with expansion ports")
print("  (c) 6 ports x 100 bytes/port — smaller model variant")
print()
print("  Context: 8020 is Per-Port Channel A (write+indicate).")
print("  600 zero bytes likely = full per-port config buffer, all ports unset.")
print("  Writing a port config here would set speed/schedule for that port.")
print()

# Compare to known config block in status packet (bytes 27-114 = 88 bytes)
# If 8 ports: 88 / 8 = 11 bytes/port in status vs 75 bytes/port here
# → status packet has compact status per port; 8020 has full config per port
print("  Cross-ref with status packet port_config_block (bytes 27-114 = 88B):")
print(f"    Status: 88B / 8 ports = 11B per port  (compact status)")
print(f"    Char  : 600B / 8 ports = 75B per port  (full config)")
print()

add_note("handle_53_structure",
         "Handle 53 (00008020, Per-Port Channel A) returns 600 zero bytes when read via direct handle. "
         "Char is listed as write+indicate only, but read succeeds via handle (not UUID). "
         "600B = 8 ports x 75B/port (most likely), or 10 ports x 60B/port. "
         "All zeros = all ports unconfigured. "
         "Cross-ref: status packet port_config_block is 88B (8 ports x 11B compact), "
         "so 8020 likely holds the full extended config (75B/port). "
         "Writing to 8020 with a port config payload should set port parameters.",
         confidence="hypothesis")

print("  Handle 53 analysis note stored.")


# ── Company ID 0x0902 ─────────────────────────────────────────────────────────
print()
print("=== Company ID 0x0902 Analysis ===")
print("  0x0902 = 2306 decimal")
print("  Not in Bluetooth SIG assigned company numbers for major vendors.")
print("  Espressif's assigned IDs are 0x02FF and 0x0590.")
print("  0x0902 may be: AC Infinity's own registered company ID, or")
print("  an unregistered/placeholder value used in their firmware.")
print("  The payload structure (MAC + serial) is consistent with Espressif custom adv format.")

add_note("company_id_0x0902",
         "Advertisement company ID 0x0902 (decimal 2306) does not match Espressif's official IDs "
         "(0x02FF, 0x0590). May be AC Infinity's own registered Bluetooth SIG company ID, "
         "or a custom/placeholder value in the firmware. "
         "Payload format (own MAC + serial fragment + flags) is typical of Espressif custom advertisements.",
         confidence="hypothesis")

print("  Company ID note stored.")

print()
print("=== Done. Run: python scripts/db_init.py  to see full summary. ===")
