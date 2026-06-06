# AC Infinity System Knowledge Base

Complete reference for everything known about the AC Infinity hardware, cloud API, and BLE protocol.
Last updated: 2026-06-06. Both layers verified against live hardware.

---

## Table of Contents

1. [Hardware Inventory](#1-hardware-inventory)
2. [Cloud / Wi-Fi API](#2-cloud--wi-fi-api)
3. [BLE Protocol](#3-ble-protocol)
4. [Sensor Type Cross-Reference](#4-sensor-type-cross-reference)
5. [BLE Command Reference](#5-ble-command-reference)
6. [Database Schema](#6-database-schema)
7. [Known Constraints and Gotchas](#7-known-constraints-and-gotchas)
8. [Discovery Notes](#8-discovery-notes)
9. [Data Merge Layer](#9-data-merge-layer)
10. [Safety Layer](#10-safety-layer)

---

## 1. Hardware Inventory

### Controllers

| Device name (app) | Model | devType | Role |
|---|---|---|---|
| "4 x 4" | AC Infinity CTR89Q | 20 | Tent climate, lights, airflow |
| "RDWC Control" | AC Infinity CTR89Q | 20 | Reservoir — dosers, pH, hydro sensors |
| "Auxiliary Outputs" | AC Infinity ADA4 | 21 | Hard on/off outlets for high-draw devices |
| "ACI_V3.5_CTRLER" | AC Infinity grow controller | — | BLE-only controller (no cloud) |

### Device type integers

| devType | Model | Notes |
|---|---|---|
| 11 | Controller 69 Pro | |
| 18 | Controller 69 Pro+ | |
| 20 | Controller AI+ (CTR89Q) | Variable-speed fan/pump ports |
| 21 | Outlet AI (ADA4) | On/off outlet ports |
| 22 | Outlet AI+ | On/off outlet ports |

### Sensors confirmed connected

| Sensor | Model | Interface | Location |
|---|---|---|---|
| T+H combo probe | UIS | BLE (port_id 0/2/3) | Tent external probe |
| T+H combo probe | UIS | BLE (port_id 4/6/7) | Second zone |
| CO2 + light | UIS | BLE (port_id 11/12) | Tent |
| HDS3 hydro probe | HDS3 | Cloud + BLE (port_id 13/14/16/18) | Reservoir |
| Built-in T+H | — | Cloud (sensorType 4/6/7) | CTR89Q internal hub |
| External air probe | — | Cloud (sensorType 0/2/3) | Tent intake / outside ref |

### BLE controller hardware

- **Chip**: ESP32 (AC Infinity BLE stack)
- **MAC address**: `50:78:7D:C5:0C:6E` *(device-specific — scan to find yours)*
- **BLE name**: `ACI_V3.5_CTRLER`
- **MTU**: 23 bytes (observed on connection)
- **Firmware**: exposes 8 controllable ports (1–8), sensor data in 0x1EFF broadcast

### Doser port mapping (RDWC Control CTR89Q)

| Port | Label | Purpose |
|---|---|---|
| 1 | Floraflex V1 | Nutrient part A |
| 2 | Floraflex V2 | Nutrient part B |
| 3 | pH UP | pH adjustment up |
| 4 | pH DOWN | pH adjustment down |

Peristaltic pump spec: **21 mL/min per speed level**, linear across speed 1–10.
Ports 1+2 always dosed together at equal speed. Ports 3+4 are pH-port designated.

---

## 2. Cloud / Wi-Fi API

### Base URL and transport

```
http://www.acinfinityserver.com
```

**HTTP only — not HTTPS.** The app and all API calls use plain HTTP.

### Required headers (all requests)

```
User-Agent:   okhttp/4.12.0
Content-Type: application/x-www-form-urlencoded
appVersion:   1.9.7
phoneType:    1
token:        <appId from login>     (omit on login endpoint)
```

**DO NOT add `minversion: 3.5` to read or list endpoints** — causes 404. Only control
write endpoints need it (see Control Write Protocol below).

### Authentication

**Endpoint:** `POST /api/user/appUserLogin`

**Payload:**
```
appEmail:    <email>
appPasswordl: <password, first 25 chars only>  ← NOTE: API ignores chars beyond 25
```

**Response:** `data.appId` — use this as the `token` header on all subsequent requests.

Token is long-lived but expires occasionally. On HTTP 401 or `code: 999999` or `"appid"` in
error body, drop the cached token and re-authenticate.

**Token caching:** store in `.env` as `AC_INFINITY_TOKEN`. The client reads it on startup
and skips login if present. Written back automatically after a fresh login.

### Fetch all devices

**Endpoint:** `POST /api/user/devInfoListAll`

**Payload:**
```
userId: <token>
```

**Response:** array of device objects. All sensor data is under `raw["deviceInfo"]`.

Key nesting:
```
raw["deviceInfo"]["ports"]    ← port list (NOT "portInfos")
raw["deviceInfo"]["sensors"]  ← array of {sensorType, sensorData} pairs
```

### Sensor data structure

Each sensor entry:
```json
{"sensorType": 4, "sensorData": 7520}
```

`value = sensorData / divisor` (see sensor type table in §4).

**Sentinel values — always skip:**
- `sensorData <= -32768` (INT16_MIN) — HDS3 probe not submerged or not connected
- `sensorData == 0` for air sensors (types 0,2,3,4,6,7) and CO2 (type 11) — sensor absent
- `sensorData == 0` is **valid** for light (type 12) and water level (type 20) — pass through

### Port data structure

```json
{
  "port": 1,
  "portName": "Exhaust Fan",
  "online": 1,
  "curMode": 2,
  "loadState": 1,
  "speak": 7,
  "onSpead": 7
}
```

- `speak` — current actual speed (readback)
- `onSpead` — target speed from last write *(do NOT send this back as the new target)*
- `curMode` — current operating mode
- `loadState` — 0=off, 1=on

### Control write protocol

Solved 2026-05-30 via packet capture (CTR89Q light port 0→5).

**Endpoint:** `PUT /api/dev/modeAndSetting`

**Method:** PUT with payload as **URL query params** (`params=payload`, not `data=payload`)

**Required additional headers for CTR89Q:**
```
minversion: 3.5
devType:    20          ← device type integer as string
```

**Payload construction:**
1. Fetch current settings: `POST /api/dev/getdevModeSettingList` with `{devId, port}`
2. Take all **top-level scalar fields** from the response (exclude nested objects:
   `devSetting`, `fieldSet`, `ipcSetting`, `devTimeZone`, `devMacAddr`, `reportSeq`, `timestamp`)
3. Convert `None` → `0`, `True` → `"true"`, `False` → `"false"`
4. Override with control fields:

```python
payload.update({
    "devId":               dev_id,
    "externalPort":        port,
    "port":                port,
    "masterPort":          port,
    "atType":              2,              # 1=OFF, 2=manual/ON
    "onSelfSpead":         speed,         # ← THE ACTUAL TARGET (NOT onSpead)
    "modeAndSettingIdStr": "[16,18]",     # [16,17]=OFF, [16,18]=manual
    "modeSetid":           mode_set_id,   # from getdevModeSettingList response
    "restore":             "false",
    "onlyUpdateSpeed":     0,
})
```

**Critical:** `onSelfSpead` is the write target. `onSpead` is a readback field — sending
it back as the command was the original protocol bug. This is confirmed from app traffic capture.

**Fallback:** if PUT returns non-200, fall back to `POST /api/dev/addDevMode` with minimal
payload `{devId, externalPort, onSpead: speed, atType, modeSetid}` as params.

### Port ramp behavior (CTR89Q)

Ramps at **exactly 1 speed unit per second**, linear, symmetric (same rate up and down).
Measured 2026-05-30 on Growcraft X6 port. Formula:

```python
def ramp_seconds(target, current, buffer=2.0):
    return abs(target - current) + buffer   # buffer for API latency + settling
```

### Outlet control (ADA4)

Same `_control_port()` path as fan ports:
- Turn on:  `speed=10, atType=2`
- Turn off: `speed=0,  atType=1`
- `devType` header not required for ADA4 (optional)

### Error codes

| code | Meaning |
|---|---|
| 200 | Success |
| 999999 | Auth/session failure — re-authenticate |
| 401 (HTTP) | Token expired — re-authenticate |

---

## 3. BLE Protocol

### GATT profile

| Role | UUID | Properties |
|---|---|---|
| Write (commands) | `70d51001-2c7f-4e75-ae8a-d758951ce4e0` | write-without-response |
| Read / Notify | `70d51002-2c7f-4e75-ae8a-d758951ce4e0` | notify |

All commands go to the write characteristic. All responses and sensor broadcasts arrive on
the notify characteristic.

### Connection requirements

1. **Only one BLE central at a time.** The controller refuses a second connection while
   one is active. Close the AC Infinity app before connecting.
2. **Windows CCCD warmup required.** On Windows (WinRT BLE stack), you must write a
   warm-up command to the write characteristic *before* calling `start_notify`, or the
   CCCD subscription silently fails and no notifications arrive.

   ```python
   warm = proto.get_model_data(TYPE_GLOBAL, 0, 0)
   await client.write_gatt_char(CHAR_WRITE, warm, response=False)
   await asyncio.sleep(0.4)
   await client.start_notify(CHAR_READ, callback)
   ```

3. **Scan first.** Use BleakScanner to confirm the device is advertising before connecting.
   Connection attempts to a non-advertising device fail slowly.

### Protocol library

```python
from ac_infinity_ble.protocol import Protocol
proto = Protocol()
```

PyPI package: `ac-infinity-ble>=0.4.3`

Constants used:
```python
TYPE_MULTIPORT = 9    # per-port queries and control
TYPE_GLOBAL    = 20   # global/warmup queries
```

### 0x1EFF notification packet (sensor broadcast)

The controller sends this ~1 Hz while a BLE client is connected.

- **Header**: bytes 0–1 = `0x1E 0xFF`
- **Length**: grows with number of sensors connected. Observed: 127 bytes (no hydro),
  147 bytes (+ CO2/light + hydro), continues to grow as sensors are added.
- **Sensor tail**: starts around byte 100–115. Contains 4-byte groups:

```
[port_id: 1 byte] [sensor_type: 1 byte] [value_hi: 1 byte] [value_lo: 1 byte]
```

```python
value = ((value_hi << 8) | value_lo) / 100.0
```

- **Decoder approach**: byte-by-byte scan (NOT fixed 4-byte stride). Advances 4 bytes
  on a valid hit, 1 byte on a miss. This is critical — the packet is not aligned and
  grows unpredictably as sensors are added.

```python
i = 100   # start of sensor tail
while i <= len(data) - 4:
    port_id     = data[i]
    sensor_type = data[i + 1]
    v1, v2      = data[i + 2], data[i + 3]
    if (0 <= port_id <= 31) and (
        (0x20 <= sensor_type <= 0x7F) or sensor_type in (0x91, 0x92, 0x93)
    ):
        value = ((v1 << 8) | v2) / 100.0
        # record (port_id, sensor_type, value)
        i += 4
    else:
        i += 1
```

### BLE sensor type codes

T+H-style sensors use rotating type codes. **Each time a new sensor is added to the
controller, all existing T+H sensor type codes shift up by +8.** This is a firmware
behavior — the type code encodes position in a registration list, not a fixed sensor
identity.

| Type code | Sensor | Notes |
|---|---|---|
| `0x21` | CO2 | Fixed. BLE value × 100 = ppm |
| `0x41` | Light | Fixed. BLE value × 100 |
| `0x61`, `0x69`, `0x71`… | Hydro sensor channels | Fixed base, rotates +8 on new sensor. All hydro channels share same type code |
| `0x62`, `0x6A`, `0x72`… | T+H combo probe (external) | Rotates +8 per added sensor |
| `0x67`, `0x6F`, `0x77`… | T+H older style probe | Rotates +8 per added sensor |

Decoder must accept a **type range** (e.g., `0x60–0x7F`) rather than fixed values,
and average all matching readings within the window.

### 0xA5 0x1C response packet (port state)

Sent by controller in response to a `get_model_data(TYPE_MULTIPORT, port, seq)` query.

**Header validation:**
```python
data[0] == 0xA5 and data[1] == 0x1C and data[9] == 1
```

**Payload:** TLV-encoded starting at byte 10, length from `(data[2] << 8) | data[3]`.

| Tag | Length | Field | Values |
|---|---|---|---|
| `0x10` | 1 | `work_type` | 1=OFF, 2=ON (manual speed) |
| `0x11` | 1 | `level_off` | speed when off (usually 0) |
| `0x12` | 1 | `level_on` | speed when on (0–10) |
| `0xFF` | — | End of TLV | stop parsing |

---

## 5. BLE Command Reference

All commands constructed by `ac_infinity_ble.protocol.Protocol`.

### get_model_data — warmup / subscription trigger

```python
proto.get_model_data(TYPE_GLOBAL, 0, 0)
```

Send this to `CHAR_WRITE` before `start_notify`. Triggers the controller to begin sending
0x1EFF packets. Also used to poll per-port state:

```python
proto.get_model_data(TYPE_MULTIPORT, port_number, sequence_number)
```

Response arrives on `CHAR_READ` as a 0xA5 0x1C TLV packet.

### set_level — control a port

```python
proto.set_level(TYPE_MULTIPORT, work_type, speed, port_number, sequence_number)
```

| Argument | Values | Notes |
|---|---|---|
| `work_type` | 1=OFF, 2=ON | |
| `speed` | 0–10 | Ignored when work_type=1 (OFF); set to 0 |
| `port_number` | 1–8 | Physical port on controller |
| `sequence_number` | increment each call | Any integer, monotonically increasing |

No response packet is generated — command is fire-and-forget (write-without-response).

### Drop-file control channel

The logger holds the BLE connection. External processes send commands without reconnecting
by writing `aci_control.json` to the repo root:

```json
{"port": 1, "work_type": 2, "speed": 7}
```

The logger polls for this file every 1 second, executes the command, and deletes the file.
`ctl.py` writes this file and waits up to 3 seconds for acknowledgment (file deletion).

---

## 4. Sensor Type Cross-Reference

**The BLE `port_id` and the cloud API `sensorType` integer are the same numbering system.**
Data from both transports can be joined on this common ID. The BLE value formula
`((v1<<8)|v2)/100.0` produces the same result as `sensorData/divisor` from the cloud API
for all sensors except CO2 and light (see notes column).

| ID | Cloud field name | BLE port_id | Measurement | Scale | Notes |
|---|---|---|---|---|---|
| 0 | `temp_f_ext` | 0 | External air temp | ÷100 → °F | |
| 2 | `humidity_ext` | 2 | External humidity | ÷100 → %RH | |
| 3 | `vpd_ext` | 3 | External VPD | ÷100 → kPa | |
| 4 | `temp_f` | 4 | Built-in air temp | ÷100 → °F | |
| 6 | `humidity` | 6 | Built-in humidity | ÷100 → %RH | |
| 7 | `vpd` | 7 | Built-in VPD | ÷100 → kPa | |
| 11 | `co2_ppm` | 11 | CO2 | Cloud: raw ppm. BLE: value × 100 = ppm | Different scale |
| 12 | `light` | 12 | Light | Cloud: raw. BLE: value × 100 | Different scale |
| 13 | `ph` | 13 | pH | ÷100 → pH | |
| 14 | `ec_us` | 14 | EC | ÷100 → µS/cm | |
| 15 | `ec_ms` | 15 | EC | ÷100 → mS/cm | Not yet seen in BLE |
| 16 | `tds_ppm` | 16 | TDS | ÷100 → ppm | |
| 17 | `tds_ppt` | 17 | TDS | ÷100 → ppt | Not yet seen in BLE |
| 18 | `water_temp_f` | 18 | Water temperature | ÷100 → °F | |
| 19 | `water_temp_c` | 19 | Water temperature | ÷100 → °C | Not yet seen in BLE |
| 20 | `water_level` | — | Water level | raw | Not yet confirmed in BLE |

### Observed live BLE values (2026-06-06, ACI_V3.5_CTRLER)

| port_id | Measurement | Observed range | Notes |
|---|---|---|---|
| 0 | Air temp (external) | 72–75°F | Tent |
| 2 | Humidity (external) | 59–67%RH | |
| 3 | VPD (external) | 0.94–1.10 kPa | |
| 4 | Air temp (built-in) | 73–76°F | |
| 6 | Humidity (built-in) | 59–67%RH | |
| 7 | VPD (built-in) | 0.95–1.08 kPa | |
| 11 | CO2 | 7.13–7.44 stored → 713–744 ppm | |
| 12 | Light | 1.29–1.44 stored → 129–144 raw | |
| 13 | pH | 8.70–8.76 | Alkaline tap water, unadjusted |
| 14 | EC | 0.00 µS/cm | No nutrients in reservoir |
| 16 | TDS | 0.00 ppm | No nutrients |
| 18 | Water temp | 72–76°F | Warming with room temp |

---

## 6. Database Schema

SQLite database at `controller.db`. Created automatically by `init_schema()`.

### sensor_readings — primary sensor log

```sql
CREATE TABLE sensor_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,       -- Unix timestamp (float)
    port        INTEGER NOT NULL,    -- port_id (== cloud sensorType)
    sensor_type INTEGER NOT NULL,    -- BLE type code byte (rotates +8 per added sensor)
    value       REAL NOT NULL        -- ((v1<<8)|v2)/100.0
);
CREATE INDEX sensor_readings_ts   ON sensor_readings(ts);
CREATE INDEX sensor_readings_port ON sensor_readings(port);
```

### port_readings — polled port state

```sql
CREATE TABLE port_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    port        INTEGER NOT NULL,    -- physical port number 1-8
    work_type   INTEGER,             -- 1=OFF, 2=ON
    level_off   INTEGER,             -- speed when off (usually 0)
    level_on    INTEGER              -- speed when on (0-10)
);
CREATE INDEX port_readings_ts ON port_readings(ts);
```

### cloud_readings — cloud API sensor log

```sql
CREATE TABLE cloud_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,       -- Unix timestamp (float)
    dev_id      TEXT NOT NULL,       -- device ID string from cloud API
    dev_name    TEXT,                -- human-readable device name (e.g. "RDWC Control")
    sensor_id   INTEGER NOT NULL,    -- same integer as BLE port_id and cloud sensorType
    value       REAL NOT NULL        -- already scaled to /100 units (same space as sensor_readings)
);
CREATE INDEX cloud_readings_ts        ON cloud_readings(ts);
CREATE INDEX cloud_readings_sensor_id ON cloud_readings(sensor_id);
```

CO2 (sensor_id 11), light (12), and water level (20) arrive from the cloud in raw units.
`cloud_ingest.py` divides these by 100 before writing so they land in the same unit space
as `sensor_readings`. All other sensor_ids are already ÷100 from the cloud API.

### readings — legacy fixed-column log (historical, pre-sensor_readings)

```sql
CREATE TABLE readings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    p4_type INTEGER, p4_v1 INTEGER, p4_v2 INTEGER,   -- port_id 4 (built-in temp)
    p6_type INTEGER, p6_v1 INTEGER, p6_v2 INTEGER,   -- port_id 6 (built-in humidity)
    p7_type INTEGER, p7_v1 INTEGER, p7_v2 INTEGER    -- port_id 7 (built-in VPD)
);
```

### Protocol research tables (populated by investigation scripts)

```sql
-- Device identity
CREATE TABLE controller (id, address, name, model, hw_revision, sw_revision,
    chip_vendor, chip_family, bt_stack, mtu, company_id, oui, notes, updated_at);

-- GATT services and characteristics
CREATE TABLE services (uuid, description, handle, notes);
CREATE TABLE characteristics (uuid, service_uuid, description, handle,
    properties, initial_hex, notes);

-- Packet field analysis
CREATE TABLE status_fields (id, byte_offset, byte_length, field_name, description,
    encoding, unit, scale, min_observed, max_observed, example_values, confidence, notes);

-- Known write commands
CREATE TABLE commands (id, char_uuid, hex_data, description, setting_changed,
    response_hex, source, confirmed, timestamp, notes);

-- Free-form research notes
CREATE TABLE protocol_notes (id, topic, detail, confidence, created_at);

-- Capture session metadata
CREATE TABLE capture_sessions (id, path, started_at, ended_at, packet_count, description);
```

---

## 7. Known Constraints and Gotchas

### BLE

- **Single connection only.** One BLE central at a time. Logger retries every 15 s
  until it gets the connection.
- **Windows CCCD bug.** Must write a command before `start_notify` or notifications
  never arrive. See §3 connection requirements.
- **Type code rotation.** Each new sensor added to the controller shifts all T+H type
  codes up by +8. The decoder must use a type range, not fixed values.
- **Packet grows.** The 0x1EFF packet length increases (~12 bytes per new sensor pair).
  Never assume fixed offsets; always byte-scan from position 100.
- **No acknowledgment on set_level.** Commands are write-without-response. There is
  no confirmation that the port changed state — poll port_readings to verify.

### Cloud API

- **HTTP only.** Never use HTTPS — the server does not support it.
- **`onSelfSpead` not `onSpead`.** Sending `onSpead` back as the command target was
  the original control bug. `onSpead` is a readback field. The write target is `onSelfSpead`.
- **Password truncated at 25 chars.** Field is `appPasswordl` (typo in API, one `l`).
  API only reads the first 25 characters of the password.
- **`minversion: 3.5` header.** Required only on control write endpoints. Adding it to
  read/list endpoints causes 404.
- **`devType` header required for CTR89Q writes.** Must be the integer as a string.
- **Full settings dump required.** The PUT endpoint requires sending back all ~125
  top-level scalar settings from `getdevModeSettingList` — not just the changed field.
- **Nested objects excluded from write payload.** `devSetting`, `fieldSet`, `ipcSetting`,
  `devTimeZone`, `devMacAddr`, `reportSeq`, `timestamp` must be omitted.
- **Token in `.env` — do not edit manually.** Client writes it back automatically after
  re-auth. Manual edits may corrupt the file.
- **Zero sensors.** `sensorData == 0` means "not connected" for air and hydro sensors.
  It is a valid reading for light (type 12) and water level (type 20).
- **HDS3 sentinel.** `-32768` (or `sensorData <= -32768`) means probe not submerged.
  Normal behavior — not a bug.

### Co2 and light scaling difference

The cloud API returns CO2 in raw ppm (no divisor). The BLE packet stores CO2 as
`raw_ppm / 100`, so the BLE value must be multiplied by 100 to get ppm. Light has the
same discrepancy. All other sensors use ÷100 on both transports.

### Concurrent access

The cloud API and BLE are independent transports. Both can read simultaneously.
**Writing** has constraints:
- Only one BLE central can connect at a time — BLE control blocks other BLE clients.
- Cloud API writes have no such constraint — multiple processes can write simultaneously,
  but they will collide on the same hardware resource.

---

## 8. Discovery Notes

### BLE protocol reverse engineering

The BLE protocol was reverse-engineered from scratch via:

1. **GATT enumeration** — `scripts/inspect_ble.py` mapped all services and characteristics.
2. **Notification capture** — `scripts/listen_ble.py` captured raw 0x1EFF packets over time.
3. **Write probing** — `scripts/probe_*.py` series systematically tested write payloads
   and observed responses.
4. **Protocol library** — `ac_infinity_ble` (PyPI) provided the `Protocol` class, which
   was confirmed to generate valid packets for `get_model_data` and `set_level`.
5. **Packet analysis** — `debug_packet.bin` (captured live) confirmed the 4-byte sensor
   group layout and the byte position of the sensor tail.

### Sensor tail discovery

Original decoder used a fixed-stride scan (`range(112, len(data)-3, 4)`). When two new
sensors were added, the packet grew from 127 to 147 bytes and the fixed scan hit the wrong
alignment. Switched to byte-by-byte scan (advance 4 on hit, 1 on miss), which is robust
to any packet size.

### Type code rotation discovery

When CO2+light sensor was added, all existing T+H type codes shifted from `0x62→0x6A`
and `0x67→0x6F`. When hydro sensor was added, another +8 shift occurred. The pattern
is: type_code = base_type + (8 × sensor_registration_index). Confirmed by observing
multiple type codes at the same port_id across different BLE sessions.

### Cloud control protocol discovery (2026-05-30)

Original control attempts sent `onSpead` as the target — commands appeared to succeed
(HTTP 200) but the port did not change. App traffic capture via NetworkManager hotspot
(Alfa AWUS1900 as AP, phone routed through laptop, tcpdump on HTTP) revealed:
- The app sends `onSelfSpead` for the target, not `onSpead`
- Full settings dump is required (not just the changed field)
- `devType` header is required for CTR89Q

### BLE port_id == cloud sensorType

Discovered by cross-referencing: cloud API `SENSOR_TYPE` dict in `ac_infinity_client.py`
maps integer IDs (0, 2, 3, 4, 6, 7, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20) to sensor
labels. BLE `port_id` values observed live on the same hardware are exactly those same
integers. This means the two data streams can be joined directly on the sensor ID with
no translation.

### Hydro sensor BLE confirmation

Initially believed unsupported over BLE ("I've heard I can't add another sensor"). After
plugging in the HDS3 probe and restarting the BLE logger, four new ports appeared:
13 (pH), 14 (EC µS/cm), 16 (TDS), 18 (water temp). All use type code `0x61` with the
same +8 rotation pattern. Values match expected ranges for tap water.

---

## 9. Data Merge Layer

BLE and cloud are complementary, not redundant. BLE is high-frequency (≈1 Hz) and local,
but only covers the single controller the logger is connected to. The cloud API covers all
controllers (including "RDWC Control" and "Auxiliary Outputs") and doser port states, but
polls at most once per minute and adds network latency. The merge layer unifies them into
one snapshot that the controller agent and AI layer see as a single dict.

### Architecture

```
logger.py          → sensor_readings table   (BLE, ~1 Hz, single controller)
cloud_ingest.py    → cloud_readings table    (cloud API, every 60s, all controllers)
                          ↓
                  build_unified_snapshot()
                          ↓
             controller_agent.py / ai layer
```

### cloud_ingest.py

Script: `scripts/cloud_ingest.py`

Polls `fetch_all_devices()` every `--interval` seconds (default 60). For each online device:
1. Calls `parse_device(raw)` to get a dict of named sensor fields.
2. Maps field names back to `sensor_id` integers using the `SENSOR_TYPE` reverse map from
   `ac_infinity_client.py`.
3. Divides CO2 (11), light (12), and water level (20) by 100 to normalise to BLE unit space.
4. Calls `add_cloud_readings(ts, dev_id, dev_name, {sensor_id: value})`.

Error handling:
- `ACInfinityAuthError` → clears `AC_INFINITY_TOKEN` from env and re-authenticates.
- Other exceptions → exponential backoff: `min(30 × consecutive_errors, 600)` seconds.

Loads both `.env` (credentials, token) and `labels.env` (port labels, hide flags) on startup.

### add_cloud_readings()

```python
def add_cloud_readings(ts: float, dev_id: str, dev_name: str, readings: dict) -> None:
    """readings = {sensor_id: value} — values already in /100 units."""
```

Bulk-inserts one row per sensor_id into `cloud_readings`. Values must already be normalised
to the same ÷100 unit space as `sensor_readings` before calling this function.

### build_unified_snapshot()

```python
def build_unified_snapshot(ble_max_age: int = 60, cloud_max_age: int = 300) -> dict:
    """Returns {sensor_id: {"value": float, "source": "ble"|"cloud", "ts": float, "dev_name": str|None}}"""
```

Merge logic (in Python — SQLite has no FULL OUTER JOIN):

1. **BLE pass**: query `sensor_readings` for all ports with readings within `ble_max_age`
   seconds. Average multiple readings per port (handles type code rotation — multiple type
   codes at the same port_id are averaged together). Inserts into snapshot with `source="ble"`.

2. **Cloud pass**: query `cloud_readings` for all sensor_ids with readings within
   `cloud_max_age` seconds. For each sensor_id **not already in the snapshot**, insert with
   `source="cloud"` and the device name attached.

BLE wins any conflict — cloud only fills sensor_ids with no fresh BLE data. This means if
the BLE logger is up, tent sensors come from BLE (high resolution). If the logger is down,
cloud readings fill in. Sensors on other controllers (RDWC Control, Auxiliary Outputs) that
have no BLE logger always come from cloud.

### Typical age windows used at runtime

| Caller | ble_max_age | cloud_max_age | Reason |
|---|---|---|---|
| `controller_agent.py` main loop | 90s | 300s | Tolerates one missed BLE tick; cloud up to 5 min stale |
| `summary_30min()` (LLM context) | 60s | 300s | Wants current snapshot for LLM review |
| `test_merge.py` | 120s | 60s | Wider BLE window for testing; tight cloud to show injected data |

### Source tagging

Every entry in the snapshot carries `"source": "ble"` or `"source": "cloud"` and,
for cloud entries, `"dev_name"`. The controller agent logs source tags in the readings line:

```
Readings  : temp=73.9°F[B]  hum=64.5%[B]  vpd=1.01[B]
```

`B` = BLE, `C` = cloud. The LLM context includes the source and device name per sensor so
the model knows which readings are local vs. polled from a remote controller.

### CO2 and light unit normalisation

| Sensor | Cloud API value | Stored in cloud_readings | BLE sensor_readings |
|---|---|---|---|
| CO2 (id 11) | raw ppm (e.g. 750) | 7.50 (÷100) | 7.50 (same) |
| Light (id 12) | raw (e.g. 130) | 1.30 (÷100) | 1.30 (same) |
| Water level (id 20) | raw | ÷100 | not yet confirmed |
| All others | already ÷100 | same | same |

After normalisation both tables are in the same unit space and the snapshot can compare
values directly across sources.

### Running the merge stack

```bash
# Terminal 1 — BLE logger (1 Hz local readings)
python scripts/logger.py

# Terminal 2 — cloud ingest (60s cloud poll)
python scripts/cloud_ingest.py

# Terminal 3 — controller agent (reads unified snapshot every 30s)
python scripts/controller_agent.py
```

All three write to / read from the same `controller.db` via WAL mode, which allows
concurrent readers and one writer without locking conflicts.

---

## 10. Safety Layer

The safety layer in `scripts/controller_agent.py` runs before any actuator command is
issued. It cannot be bypassed by Layer 1 (rules), Layer 2 (ML), or Layer 3 (LLM). All
three layers route through `set_device()`, which gates on `safety_check()`.

### Cooldown and run-time constants

```python
SAFETY = {
    "ac_min_off_s":    180,   # compressor rest — 3 min minimum between AC cycles
    "ac_min_on_s":     60,    # 1 min minimum run before AC can turn off (short-cycle guard)
    "hum_min_off_s":   30,    # 30s minimum off-time between humidifier cycles
    "dehum_min_off_s": 30,    # 30s minimum off-time between dehumidifier cycles
    "sensor_max_age":  300,   # skip control if newest reading is older than 5 min
}
```

**Why 180s AC cooldown:** AC compressors require a minimum off-time (typically 3–5 min)
between cycles to allow refrigerant pressure to equalise. Starting the compressor before
pressure equalises causes mechanical stress and premature failure.

**Why 60s AC minimum run:** Running the compressor for less than a minute before turning
it off is ineffective (the refrigerant cycle hasn't completed) and stresses the motor.

### Absolute setpoint bounds

```python
SETPOINT_LIMITS = {
    "temp_lo": (60.0, 80.0),
    "temp_hi": (62.0, 85.0),
    "hum_lo":  (30.0, 70.0),
    "hum_hi":  (35.0, 80.0),
}
```

These are hard bounds applied to every LLM setpoint suggestion before the values are
written to `TARGETS`. The LLM prompt also asks for ±2°F / ±3%RH increments, but
`SETPOINT_LIMITS` is the backstop — it enforces correctness even if the model ignores
the prompt constraint or returns a hallucinated value.

If clamping creates an inversion (temp_lo ≥ temp_hi after clamping), both sides are
reverted to the current live values rather than guessing which end was wrong.

### State tracking

```python
_state    = {"ac": False, "humidifier": False, "dehumidifier": False}
_last_on  = {"ac": 0.0,   "humidifier": 0.0,   "dehumidifier": 0.0}
_last_off = {"ac": 0.0,   "humidifier": 0.0,   "dehumidifier": 0.0}
```

`_last_on` and `_last_off` are updated inside `_send()` immediately after the drop-file
command is confirmed. They survive across control layers within a single process run but
reset to 0.0 on restart — so cooldowns are not enforced across process restarts. On a
fresh start the 180s AC cooldown starts counting from epoch 0, meaning it will be
considered expired and the AC can fire immediately. This is intentional: the safe
assumption after a restart is that the compressor has had sufficient rest.

### safety_check() — per-command gate

Called by `set_device()` before every actuator command. Returns a reason string if
blocked, `None` if allowed.

| Check | Condition | Block reason |
|---|---|---|
| Conflict | Turning humidifier ON while dehumidifier is ON (or vice versa) | `"conflict: dehumidifier is on"` |
| Cooldown | Turning device ON before `min_off_s` has elapsed since last OFF | `"cooldown: ac off for 45s, need 180s"` |
| Min-run | Turning device OFF before `min_on_s` has elapsed since last ON | `"min-run: ac on for 12s, need 60s"` |

Blocked commands log at WARNING level:
```
2026-06-06 14:32:17  WARN   Safety BLOCK  ac            → ON   (cooldown: ac off for 45s, need 180s)
```

### safety_clamp_setpoints() — LLM output sanitisation

Called inside `layer_llm()` immediately after `json.loads()` parses the model response,
before any value is written to `TARGETS`.

1. Each key in `SETPOINT_LIMITS` is clamped to its `(lo, hi)` range.
2. Clamped values are logged at WARNING so operator can see when the model went out of bounds.
3. After clamping, inversion check: if `temp_lo >= temp_hi` or `hum_lo >= hum_hi`, revert
   both sides of the affected pair to the current live `TARGETS` values.

### safety_check_readings() — sensor validation

Called in the main loop every tick, before Layer 1 and Layer 2 execute. Returns a list
of issue strings. If the list is non-empty, control layers are skipped for that tick.

| Check | Condition | Issue string |
|---|---|---|
| Stale | Newest snapshot entry older than `sensor_max_age` (300s), or snapshot empty | `"stale readings: last update 320s ago (limit 300s)"` |
| Temp range | `temp` outside 40–120°F | `"temp out of plausible range: 138.4F (expected 40-120)"` |
| Humidity range | `hum` outside 1–99% | `"humidity out of plausible range: 0.0% (expected 1-99)"` |

The LLM review (`layer_llm`) is **not** blocked by sensor issues — it still fires on
schedule because it is diagnostic and may want to respond to the degraded state.

### Log output reference

```
# Normal operation
2026-06-06 14:30:00  INFO   Readings  : temp=73.9°F[B]  hum=64.5%[B]  vpd=1.01[B]
2026-06-06 14:30:00  INFO   ac            → ON   port=1  speed=7

# Safety block — cooldown
2026-06-06 14:31:00  WARN   Safety BLOCK  ac            → ON   (cooldown: ac off for 45s, need 180s)

# Safety block — conflict
2026-06-06 14:31:00  WARN   Safety BLOCK  humidifier    → ON   (conflict: dehumidifier is on)

# Stale sensor guard
2026-06-06 14:35:00  WARN   Safety: stale readings: last update 320s ago (limit 300s)
2026-06-06 14:35:00  WARN   Control layers skipped — resolve sensor issues before acting

# LLM setpoint clamp
2026-06-06 15:00:00  WARN   Safety clamp: temp_hi 87.0 → 85.0
```
