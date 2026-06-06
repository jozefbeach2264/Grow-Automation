#!/usr/bin/env python3
"""
db_init.py — Initialize controller.db with everything known so far.
Safe to re-run; all inserts are upserts.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aci_ble_lab.db import (
    init_schema, upsert_controller, upsert_service, upsert_char,
    add_status_field, add_note, dump_summary
)

init_schema()
print("Schema created.")

# ── Controller identity ───────────────────────────────────────────────────────
upsert_controller(
    address="50:78:7D:C5:0C:6E",
    name="ACI_V3.5_CTRLER",
    model="AC Infinity",
    hw_revision="1.1",
    sw_revision="12.8.15",
    oui="50:78:7D",
    chip_vendor="Espressif Inc.",
    chip_family="ESP32 (likely ESP32-C3 or ESP32-S3 based on BT 5.0 support)",
    company_id="0x0902",
    notes="Controller for AC Infinity grow tent equipment. Single BLE connection allowed.",
)

# ── GATT Services ─────────────────────────────────────────────────────────────
upsert_service("00001801-0000-1000-8000-00805f9b34fb", "Generic Attribute Profile", handle=1)
upsert_service("00001800-0000-1000-8000-00805f9b34fb", "Generic Access Profile", handle=20)
upsert_service("0000ffff-0000-1000-8000-00805f9b34fb", "Vendor Command Channel", handle=40,
               notes="Primary command/response channel. ff01=write cmd, ff02=notify response")
upsert_service("70d51000-2c7f-4e75-ae8a-d758951ce4e0", "AC Infinity Status/Auth Service", handle=46,
               notes="70d51001=auth token (r/w), 70d51002=127B status notify at ~1Hz")
upsert_service("00008018-0000-1000-8000-00805f9b34fb", "Per-Port Control Service", handle=52,
               notes="4 indicate characteristics - likely per-channel config r/w")
upsert_service("0000180a-0000-1000-8000-00805f9b34fb", "Device Information", handle=65)

# ── GATT Characteristics ──────────────────────────────────────────────────────
upsert_char("00002a05-0000-1000-8000-00805f9b34fb", "00001801-0000-1000-8000-00805f9b34fb",
            "Service Changed", handle=2, properties=["indicate"],
            notes="Requires bonding — skip on subscribe")

upsert_char("00002b29-0000-1000-8000-00805f9b34fb", "00001801-0000-1000-8000-00805f9b34fb",
            "Client Supported Features", handle=5, properties=["write","read"], initial_hex="01")

upsert_char("00002b2a-0000-1000-8000-00805f9b34fb", "00001801-0000-1000-8000-00805f9b34fb",
            "Database Hash", handle=7, properties=["read"],
            initial_hex="00000000000000000000000000000000")

upsert_char("00002a00-0000-1000-8000-00805f9b34fb", "00001800-0000-1000-8000-00805f9b34fb",
            "Device Name", handle=21, properties=["read"], initial_hex="4143495f56332e355f4354524c4552",
            notes="ASCII: ACI_V3.5_CTRLER")

upsert_char("00002a01-0000-1000-8000-00805f9b34fb", "00001800-0000-1000-8000-00805f9b34fb",
            "Appearance", handle=23, properties=["read"], initial_hex="0000")

upsert_char("0000ff01-0000-1000-8000-00805f9b34fb", "0000ffff-0000-1000-8000-00805f9b34fb",
            "Secondary command input (fallback only)", handle=41, properties=["write"],
            notes="CONFIRMED: ff01/ff02 is a secondary/fallback channel. "
                  "Only returns generic 5-byte ACKs: 49 04 [seq] 01 [code]. "
                  "Does NOT control device state. Real commands go to 70d51001.")

upsert_char("0000ff02-0000-1000-8000-00805f9b34fb", "0000ffff-0000-1000-8000-00805f9b34fb",
            "Secondary response channel (fallback only)", handle=43, properties=["read","notify"],
            initial_hex="00",
            notes="CONFIRMED: Response to ff01 writes. Pattern: 49 04 [seq] 01 [code]. "
                  "Generic ACK only — does not reflect device state changes.")

upsert_char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", "70d51000-2c7f-4e75-ae8a-d758951ce4e0",
            "A5-framed command write (PRIMARY WRITE CHAR)", handle=47,
            properties=["write","read"], initial_hex="11223344",
            notes="CONFIRMED PRIMARY WRITE CHAR. Send A5-framed commands here. "
                  "A5 00 [len_hi][len_lo][seq_hi][seq_lo][crc16_hdr] 00 [cmd_type] [data...] [crc16_payload]. "
                  "Use ac_infinity_ble.protocol.Protocol() to build commands. "
                  "Initial read value 0x11223344 is device state, NOT an auth token.")

upsert_char("70d51002-2c7f-4e75-ae8a-d758951ce4e0", "70d51000-2c7f-4e75-ae8a-d758951ce4e0",
            "A5 responses + 1EFF status notify (PRIMARY READ CHAR)", handle=49,
            properties=["read","notify"],
            notes="CONFIRMED PRIMARY READ CHAR. Two packet types: "
                  "(1) A5 1C responses to commands — cmd_type=1 for get_model_data, cmd_type=3 for set_level; "
                  "(2) 1E FF 127-byte status pushed at ~1Hz. "
                  "Must warm-up with A5 write to 70d51001 before subscribing to avoid WinError -2147023673.")

upsert_char("00008020-0000-1000-8000-00805f9b34fb", "00008018-0000-1000-8000-00805f9b34fb",
            "Per-Port Channel A (w+indicate)", handle=53, properties=["write","indicate"],
            notes="Likely port/channel-specific config or control")

upsert_char("00008021-0000-1000-8000-00805f9b34fb", "00008018-0000-1000-8000-00805f9b34fb",
            "Per-Port Channel B (r+indicate)", handle=56, properties=["read","indicate"],
            initial_hex="0000000000000000000000000000000000000000")

upsert_char("00008022-0000-1000-8000-00805f9b34fb", "00008018-0000-1000-8000-00805f9b34fb",
            "Per-Port Channel C (w+indicate)", handle=59, properties=["write","indicate"])

upsert_char("00008023-0000-1000-8000-00805f9b34fb", "00008018-0000-1000-8000-00805f9b34fb",
            "Per-Port Channel D (w+indicate)", handle=62, properties=["write","indicate"])

upsert_char("00002a24-0000-1000-8000-00805f9b34fb", "0000180a-0000-1000-8000-00805f9b34fb",
            "Model Number", handle=66, properties=["read"],
            initial_hex="414320496e66696e69747900", notes="ASCII: AC Infinity")

upsert_char("00002a27-0000-1000-8000-00805f9b34fb", "0000180a-0000-1000-8000-00805f9b34fb",
            "Hardware Revision", handle=68, properties=["read"],
            initial_hex="312e31", notes="ASCII: 1.1")

upsert_char("00002a28-0000-1000-8000-00805f9b34fb", "0000180a-0000-1000-8000-00805f9b34fb",
            "Software Revision", handle=70, properties=["read"],
            initial_hex="31322e382e3135", notes="ASCII: 12.8.15")

# ── 127-byte status packet field map (from observations) ─────────────────────
# Characteristic: 70d51002
# Sample: 1eff02090c79008302000091040000000000000000000000000000
#         ffff000001f00000ffffffff000001f00000ffffffff000001f00000ffffffff000001f00000
#         ffffffff000001f00000ffffffff000001f00000ffffffff000001f00000ffffffff000001f00000
#         ffff...
#         04671d9206671842076700 6e

add_status_field(0, 1, "pkt_header_1e", "Always 0x1E — packet start marker", "uint8",
                 confidence="confirmed", notes="constant=0x1E")
add_status_field(1, 1, "pkt_header_ff", "Always 0xFF", "uint8",
                 confidence="confirmed", notes="constant=0xFF")
add_status_field(2, 1, "fw_major", "Firmware major version (observed: 2)", "uint8",
                 min_observed=2, max_observed=2, confidence="hypothesis")
add_status_field(3, 1, "hw_type", "Hardware type (observed: 0x09=9)", "uint8",
                 example_values=[9], confidence="hypothesis")
add_status_field(4, 1, "hw_variant", "Hardware variant (observed: 0x0C=12)", "uint8",
                 example_values=[12], confidence="hypothesis")
add_status_field(5, 1, "byte5_unknown", "Unknown — stable at 0x79", "uint8",
                 example_values=[0x79], confidence="unknown")
add_status_field(6, 1, "byte6_unknown", "Unknown — stable at 0x00", "uint8",
                 example_values=[0], confidence="unknown")
add_status_field(7, 1, "byte7_flags", "Flags byte (observed: 0x83=131)", "uint8",
                 example_values=[0x83], confidence="hypothesis")
add_status_field(8, 1, "byte8_unknown", "Unknown — stable 0x02", "uint8",
                 example_values=[2], confidence="unknown")
add_status_field(9, 2, "bytes9_10", "Unknown — stable 0x0000", "uint16_be",
                 example_values=[0], confidence="unknown")
add_status_field(11, 1, "byte11_flags2", "Unknown flags (observed: 0x91=145)", "uint8",
                 example_values=[0x91], confidence="unknown")
add_status_field(12, 1, "byte12_unknown", "Stable 0x04", "uint8",
                 example_values=[4], confidence="unknown")
add_status_field(13, 14, "bytes13_26", "Unknown header padding — all 0x00", "bytes",
                 confidence="unknown", notes="14 zero bytes")

# Per-port block: bytes 27-114 = 88 bytes = 8 ports x 11 bytes each (hypothesis)
# Pattern: ff ff 00 00 01 f0 00 00 ff ff ff ff  (12 bytes per port if 7 ports + padding)
# Actually: 88 / 8 = 11 bytes per port; could also be 9 ports x ~9.7 (not clean)
# Observation: the repeating unit appears to be:  ff ff 00 00 01 f0 00 00 ff ff ff ff
# That's 12 bytes. 88 / 12 = 7.33 — not clean either.
# Most likely 8 ports x 11 bytes = 88 bytes OR some other structure
add_status_field(27, 88, "port_config_block",
                 "Per-port configuration block — 88 bytes, repeating pattern",
                 "bytes", confidence="hypothesis",
                 notes="Pattern: ffff000001f00000ffffffff (12B) repeats. "
                       "Currently all default/unset. Changes when ports are configured.")

# Tail: bytes 115-126 = 12 bytes = 3 sensor groups x 4 bytes
add_status_field(115, 4, "sensor_group_A",
                 "Sensor group A — likely port 4 readings",
                 "bytes", confidence="hypothesis",
                 notes="byte[115]=port_id(4), byte[116]=0x67, byte[117]=temp_raw, byte[118]=humidity_or_other")
add_status_field(115, 1, "sensor_A_port_id", "Port identifier (observed: 4)", "uint8",
                 min_observed=4, max_observed=4, confidence="hypothesis")
add_status_field(116, 1, "sensor_A_type", "Sensor type (stable: 0x67=103)", "uint8",
                 example_values=[0x67], confidence="hypothesis")
add_status_field(117, 1, "sensor_A_value1",
                 "Sensor A primary reading — changes over time", "uint8",
                 min_observed=0x1d, max_observed=0x1d, confidence="hypothesis",
                 notes="Temp in tenths-of-degree? 0x1D=29 (29°C?)")
add_status_field(118, 1, "sensor_A_value2",
                 "Sensor A secondary reading — oscillates ~8-26 range", "uint8",
                 min_observed=136, max_observed=166, confidence="hypothesis",
                 notes="Humidity x10? or raw ADC?")

add_status_field(119, 4, "sensor_group_B", "Sensor group B — port 6", "bytes",
                 confidence="hypothesis")
add_status_field(119, 1, "sensor_B_port_id", "Port identifier (observed: 6)", "uint8",
                 example_values=[6], confidence="hypothesis")
add_status_field(120, 1, "sensor_B_type", "Sensor type (stable: 0x67)", "uint8",
                 example_values=[0x67], confidence="hypothesis")
add_status_field(121, 1, "sensor_B_value1", "Sensor B primary reading", "uint8",
                 min_observed=0x18, max_observed=0x18, confidence="hypothesis")
add_status_field(122, 1, "sensor_B_value2", "Sensor B secondary — oscillates", "uint8",
                 min_observed=126, max_observed=226, confidence="hypothesis",
                 notes="Most variable byte in the packet")

add_status_field(123, 4, "sensor_group_C", "Sensor group C — port 7", "bytes",
                 confidence="hypothesis")
add_status_field(123, 1, "sensor_C_port_id", "Port identifier (observed: 7)", "uint8",
                 example_values=[7], confidence="hypothesis")
add_status_field(124, 1, "sensor_C_type", "Sensor type (stable: 0x67)", "uint8",
                 example_values=[0x67], confidence="hypothesis")
add_status_field(125, 1, "sensor_C_value1", "Sensor C primary (stable: 0x00)", "uint8",
                 example_values=[0], confidence="hypothesis")
add_status_field(126, 1, "sensor_C_value2", "Sensor C secondary — changes slowly", "uint8",
                 min_observed=0x6e, max_observed=0x74, confidence="hypothesis")

# ── Protocol hypotheses ───────────────────────────────────────────────────────
add_note("auth_sequence",
         "Phone likely writes 0x11223344 to 70d51001 first (auth/session token). "
         "Value matches the static read value from the controller.",
         confidence="hypothesis")

add_note("command_channel",
         "0000ff01 is write-only command input. 0000ff02 sends the response notification. "
         "Pattern matches request-response protocol over BLE.",
         confidence="hypothesis")

add_note("per_port_control",
         "Service 00008018 has 4 indicate characteristics (8020-8023). "
         "Likely used for per-port/channel configuration. "
         "8020 and 8022/8023 are write+indicate (send config, get ack). "
         "8021 is read+indicate (may push port status).",
         confidence="hypothesis")

add_note("sensor_encoding",
         "In status packet tail, byte pattern [port_id, 0x67, val1, val2]. "
         "0x67=103 decimal appears as sensor type code. "
         "Sensor values change slowly (~1 unit/minute). "
         "May encode temp in 0.1°C steps and humidity in 0.1% steps.",
         confidence="hypothesis")

add_note("chip_identification",
         "OUI 50:78:7D is Espressif Inc. (ESP32 family). "
         "Almost certainly ESP32-C3 or ESP32-S3 based on BT 5.0 advertisement support. "
         "Firmware 12.8.15 suggests mature AC Infinity codebase.",
         confidence="confirmed")

add_note("single_connection",
         "Controller only allows ONE BLE central connection at a time. "
         "Must disconnect listener/proxy before phone can connect, and vice versa.",
         confidence="confirmed")

add_note("a5_protocol_confirmed",
         "A5-framed commands to 70d51001 are CONFIRMED working with A5-1C responses on 70d51002. "
         "Command format: A5 00 [len_hi][len_lo][seq_hi][seq_lo][crc16_hdr_2B] 00 [cmd_type] [data...] [crc16_payload_2B]. "
         "Response format: A5 1C [len_hi][len_lo][seq_hi][seq_lo][crc16_hdr_2B] 00 [cmd_type] [payload...] [crc16_payload_2B]. "
         "Use ac_infinity_ble.protocol.Protocol() (pip: ac-infinity-ble==0.4.3) to build all commands — "
         "it handles CRC16 automatically. "
         "ff01/ff02 only give generic 49-04 ACKs and do not control device state.",
         confidence="confirmed")

add_note("device_type_for_commands",
         "CONTROLLER PRO (ACI_V3.5_CTRLER) requires TYPE=9 (not 20) for per-port commands. "
         "TYPE_GLOBAL=20: commands acknowledged but state does NOT persist in get_model_data queries. "
         "TYPE_MULTIPORT=9: state DOES persist; use for real control. "
         "Device type 9 adds [0xFF, port_num] suffix to command data. "
         "Port numbers are 1-8 for the 8-port CONTROLLER PRO. "
         "Status packet byte[3]=0x09=9 confirms device is type 9.",
         confidence="confirmed")

add_note("set_level_command",
         "set_level(type=9, work_type, level, port_num, seq) builds: "
         "cmd data = [0x10, 0x01, work_type, work_type+16, 0x01, level, 0xFF, port_num]. "
         "work_type: 1=OFF, 2=ON. level: 0-10. "
         "ACK response payload for type=9: [0x10, 0x00, work_type+16, 0x00, 0xFF, port_num]. "
         "Speed is NOT echoed in multiport ACK — use get_model_data to verify. "
         "Confirmed: set_level(9, 2, 5, 1, seq) sets port 1 ON at speed 5.",
         confidence="confirmed")

add_note("get_model_data_response",
         "get_model_data(type=9, port_num, seq) returns TLV payload: "
         "[0x10, 0x01, work_type]  — 1=OFF, 2=ON; "
         "[0x11, 0x01, level_off]  — speed when OFF; "
         "[0x12, 0x01, level_on]   — speed when ON; "
         "[0x13, 0x0A, 10 bytes]   — extended data (timer/schedule?); "
         "[0x14, 0x04, 4 bytes], [0x15, 0x04, 4 bytes], [0x16, 0x08, 8 bytes], [0x17, 0x04, 4 bytes]; "
         "[0xFF, port_num]         — port suffix echoed at end. "
         "state persists across BLE sessions (survives disconnect/reconnect).",
         confidence="confirmed")

add_note("windows_cccd_subscribe_workaround",
         "On Windows, subscribing to 70d51002 (start_notify) fails with WinError -2147023673 "
         "unless a warm-up A5 write to 70d51001 is done first. "
         "Fix: write proto.get_model_data(TYPE_GLOBAL, 0, 0) to CHAR_WRITE before start_notify. "
         "Use retry loop (3 attempts, increasing sleep). "
         "MTU=517B on cached sessions can also cause issues; MTU=23B indicates fresh session.",
         confidence="confirmed")

print("Database seeded with known data.")
print()
print(dump_summary())
