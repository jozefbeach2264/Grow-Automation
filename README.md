# ACI BLE Lab

Local-first BLE data logger, sensor decoder, and AI climate controller for AC Infinity grow tent controllers (ACI_V3.5_CTRLER and compatible).

No cloud. No Wi-Fi. Pure Bluetooth.

---

## What it does

- **Logs** all sensor data from the controller at ~1 Hz into a local SQLite database
- **Decodes** T+H combos, CO₂, light, and hydro (water temp / pH / EC) sensors
- **Controls** fans and plugged-in devices (AC, humidifier, dehumidifier) over the existing BLE connection — no reconnect needed
- **Graphs** any time range with auto-classified panels per sensor type
- **AI controller** — hybrid rules / ML / LLM loop that reacts to live readings and tunes its own setpoints

---

## Hardware tested

| Device | Value |
|---|---|
| Controller | AC Infinity ACI_V3.5_CTRLER |
| MAC address | 50:78:7D:C5:0C:6E *(update in `scripts/logger.py`)* |
| Chip | ESP32 (AC Infinity BLE stack) |
| Sensors | T+H combo, CO₂+light, hydro (water temp/pH/EC) |

---

## Prerequisites

- Python 3.11+
- Windows 10 1709 / 11, macOS 10.15+, or Linux with BlueZ ≥ 5.43
- Bluetooth adapter (built-in or USB dongle)
- Controller with BLE mode enabled (the AC Infinity app must be closed — only one BLE central at a time)

---

## Installation

```bash
git clone <repo-url>
cd aci-ble-lab

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Quick start

### 1. Log sensor data

```bash
python scripts/logger.py
```

Connects to the controller, subscribes to 1 Hz status notifications, polls each port every 30 s, and writes everything to `controller.db`. Reconnects automatically on disconnect.

```bash
python scripts/logger.py --address AA:BB:CC:DD:EE:FF   # different controller
python scripts/logger.py --ports 1 2 3                  # poll specific ports only
python scripts/logger.py --poll-interval 60             # slower polling
```

### 2. Graph the data

```bash
python scripts/daily_graph.py               # today
python scripts/daily_graph.py --hours 2     # last 2 hours
python scripts/daily_graph.py --date 2026-06-05
python scripts/daily_graph.py --all         # everything in the DB
```

Auto-generates panels for whatever sensors are present: Air Temp, Humidity, VPD, CO₂, Light, Water Temp, pH, EC, Fan Speed.

### 3. Control a device manually

The logger must already be running (it holds the BLE connection).

```bash
python scripts/ctl.py --port 1 --speed 7    # turn port 1 ON at speed 7
python scripts/ctl.py --port 1 --off        # turn port 1 OFF
```

### 4. Run the AI climate controller

```bash
# Without LLM (rules + ML only):
python scripts/controller_agent.py

# With LLM setpoint advisor:
ANTHROPIC_API_KEY=sk-... python scripts/controller_agent.py
```

The logger must be running in a separate terminal. The controller agent reads the DB and sends commands through the same drop-file mechanism as `ctl.py`.

**Configure ports and targets** at the top of `scripts/controller_agent.py`:

```python
PORTS = {
    "ac":           1,    # port number on the controller (None = not plugged in)
    "humidifier":   2,
    "dehumidifier": 3,
}

TARGETS = {
    "temp_lo":  70.0,   # °F
    "temp_hi":  74.0,
    "hum_lo":   58.0,   # %RH
    "hum_hi":   65.0,
}
```

---

## AI controller — three layers

| Layer | Runs every | What it does |
|---|---|---|
| **Rules** | 30 s | Hysteresis control on current readings — immediate response |
| **ML** | 30 s | Ridge regression on last 30 min → predicts 5 min ahead → pre-empts threshold crossings |
| **LLM** | 30 min | Claude reviews trends → adjusts setpoints ±2°F / ±3%RH → logs reasoning |

The LLM layer is optional and degrades gracefully — if `ANTHROPIC_API_KEY` is not set, rules + ML run normally.

---

## BLE protocol notes

The controller broadcasts a `0x1EFF` notification packet at ~1 Hz containing a sensor tail of 4-byte groups:

```
[port_id, sensor_type, value_hi, value_lo]
value = ((value_hi << 8) | value_lo) / 100.0
```

Sensor type codes:
| Type range | Sensor | Notes |
|---|---|---|
| `0x62`, `0x6A`, `0x72`… | T+H combo — temp/humidity/VPD | Shifts +8 each time a new sensor is added |
| `0x67`, `0x6F`, `0x77`… | T+H (older sensor style) | Same rotation pattern |
| `0x21` | CO₂ | `value × 100 = ppm` |
| `0x41` | Light | `value × 100` |
| `0x61`, `0x69`… | Hydro sensor | Port 13=pH, 14=EC, 16=TDS, 18=water temp |

Commands are sent to characteristic `70d51001-2c7f-4e75-ae8a-d758951ce4e0` using the `ac_infinity_ble` protocol library. Responses arrive on `70d51002-2c7f-4e75-ae8a-d758951ce4e0`.

---

## Project structure

```
aci-ble-lab/
├── requirements.txt
├── README.md
├── aci_ble_lab/           # core library
│   ├── db.py              # SQLite schema + helpers (sensor_readings, port_readings, etc.)
│   ├── common.py          # shared BLE utilities
│   ├── scan.py            # BLE scanner
│   ├── inspect.py         # GATT enumerator
│   └── listen.py          # notification subscriber
└── scripts/
    ├── logger.py           # main 1 Hz data logger (run this first)
    ├── ctl.py              # manual device control via drop-file
    ├── daily_graph.py      # graph generator
    ├── controller_agent.py # hybrid rules/ML/LLM climate controller
    ├── query_sensors.py    # quick DB query tool (avg/min/max by port)
    ├── type_timeline.py    # shows when each sensor type first appeared
    └── [probe scripts]     # BLE protocol reverse-engineering scripts (reference)
```

---

## Database schema

`controller.db` is created automatically on first run.

| Table | Contents |
|---|---|
| `sensor_readings` | Normalized sensor data: one row per sensor per timestamp |
| `port_readings` | Polled port state (work_type, speed) every poll interval |
| `readings` | Legacy fixed-column format (kept for historical data) |

---

## Windows note

A single BLE central connection is allowed at a time. If you have another process holding the connection (e.g. a startup script), the logger will retry every 15 s until it gets it. To find competing processes:

```powershell
tasklist /FI "IMAGENAME eq python.exe"
```
