# Grow-Automation

Two complementary systems for a fully automated AC Infinity RDWC cannabis grow.

| System | Transport | What it does |
|---|---|---|
| **RDWC AI Advisor** | Cloud API | Polls all devices, doses nutrients, controls CO₂/lights/fans via Ollama LLM |
| **BLE Logger + Controller** | Bluetooth | Logs sensors at 1 Hz locally, controls devices, hybrid rules/ML/LLM climate control |

The cloud system is the main controller. The BLE system is the local-first layer — no internet required, sub-second polling, and it unlocks sensor data that the cloud API doesn't expose yet.

---

## RDWC AI Advisor (cloud)

Custom controller and AI advisor for a Recirculating Deep Water Culture (RDWC)
cannabis grow built around AC Infinity UIS controllers and a local LLM running
on Ollama.

The poller reads every sensor and port across all connected AC Infinity devices,
builds an aggregated snapshot (with trends, reservoir health, schedule deltas,
and CO2 emergency state), and feeds it to a local model. Every AI-proposed
action flows through a deterministic safety chain — schema validation,
reservoir gates, dose lockouts, and a CO2 pulse modulator — before any hardware
command fires. Schedule-driven outputs (lights, oscillating fans, CO2 valve)
are enforced by code regardless of AI behavior.

> **Status:** Advisory + supervised live control on lights / fans / CO2.
> Chemical dosing (pH and nutrients) is hard-blocked at the safety layer
> until the HDS3 hydro probe is wired and several Layer-1 safety items
> are complete. See [`UPGRADE_PRIORITY_TREE.md`](UPGRADE_PRIORITY_TREE.md).

### Hardware

| Device | Model | Role |
|--------|-------|------|
| Controller 1 ("4 x 4") | AC Infinity CTR89Q | Climate, light, airflow |
| Controller 2 ("Hydroponics Control") | AC Infinity CTR89Q | Reservoir — dosers + pH UP/DOWN |
| Power Strip | ADA4 | Hard on/off switching for high-draw devices |

### Quick start (cloud system)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
ollama serve           # in a separate terminal
python3 poller.py
```

See [`CLAUDE.md`](CLAUDE.md) for full protocol details, AI decision chain, safety gates, nutrient schedule, and config reference.

---

## BLE Logger + Controller (local)

Local-first BLE data logger, sensor decoder, and AI climate controller for AC Infinity
grow tent controllers (ACI_V3.5_CTRLER and compatible). No cloud. No Wi-Fi. Pure Bluetooth.

This is the Phase 2 local layer — eliminates cloud dependency for the BLE-capable controllers,
gives 1 Hz sensor resolution instead of the cloud API's polling interval, and exposes
sensor slots the cloud API doesn't surface yet.

### What it does

- **Logs** all sensor data at ~1 Hz into a local SQLite database
- **Decodes** T+H combos, CO₂, light, and hydro (water temp / pH / EC) sensors
- **Controls** fans and plugged-in devices over the existing BLE connection — no reconnect needed
- **Graphs** any time range with auto-classified panels per sensor type
- **AI controller** — hybrid rules / ML / LLM loop that reacts to live readings and tunes its own setpoints

### Hardware tested

| Device | Value |
|---|---|
| Controller | AC Infinity ACI_V3.5_CTRLER |
| Chip | ESP32 (AC Infinity BLE stack) |
| Sensors | T+H combo, CO₂+light, hydro (water temp/pH/EC) |

Update the MAC address in `scripts/logger.py` to match your controller.

### Prerequisites

- Python 3.11+
- Windows 10 1709 / 11, macOS 10.15+, or Linux with BlueZ ≥ 5.43
- Bluetooth adapter
- Controller with BLE mode enabled (the AC Infinity app must be closed — only one BLE central at a time)

### Installation

```bash
git clone <repo-url>
cd Grow-Automation
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### Quick start (BLE system)

**1. Log sensor data**
```bash
python scripts/logger.py
python scripts/logger.py --address AA:BB:CC:DD:EE:FF   # different controller
python scripts/logger.py --poll-interval 60             # slower port polling
```

Connects, subscribes to 1 Hz notifications, polls port states every 30 s, writes to `controller.db`. Auto-reconnects on disconnect.

**2. Graph the data**
```bash
python scripts/daily_graph.py               # today
python scripts/daily_graph.py --hours 2     # last 2 hours
python scripts/daily_graph.py --all         # everything in the DB
```
Auto-generates panels for: Air Temp, Humidity, VPD, CO₂, Light, Water Temp, pH, EC, Fan Speed.

**3. Control a device manually**
```bash
# Logger must be running (holds the BLE connection)
python scripts/ctl.py --port 1 --speed 7
python scripts/ctl.py --port 1 --off
```

**4. Run the AI climate controller**
```bash
# Logger must be running in a separate terminal
python scripts/controller_agent.py

# With LLM setpoint advisor (optional):
ANTHROPIC_API_KEY=sk-... python scripts/controller_agent.py
```

Configure ports and targets at the top of `scripts/controller_agent.py`:
```python
PORTS = {
    "ac":           1,    # controller port number (None = not plugged in yet)
    "humidifier":   2,
    "dehumidifier": 3,
}
TARGETS = {
    "temp_lo": 70.0,   # °F
    "temp_hi": 74.0,
    "hum_lo":  58.0,   # %RH
    "hum_hi":  65.0,
}
```

### AI controller — three layers

| Layer | Runs every | What it does |
|---|---|---|
| **Rules** | 30 s | Hysteresis on current readings — immediate response |
| **ML** | 30 s | Ridge regression on last 30 min → predicts 5 min ahead → pre-empts crossings |
| **LLM** | 30 min | Claude reviews trends → adjusts setpoints ±2°F / ±3%RH → logs reasoning |

LLM layer is optional — if `ANTHROPIC_API_KEY` is not set, rules + ML run normally.

### BLE protocol

The controller broadcasts a `0x1EFF` notification packet at ~1 Hz with a sensor tail of 4-byte groups:

```
[port_id, sensor_type, value_hi, value_lo]
value = ((value_hi << 8) | value_lo) / 100.0
```

BLE `port_id` values match the cloud API `sensorType` numbers exactly:

| port_id | Cloud sensorType | Measurement | Notes |
|---|---|---|---|
| 0 | 0 | External temp (°F) | |
| 2 | 2 | External humidity (%RH) | |
| 3 | 3 | External VPD (kPa) | |
| 4 | 4 | Built-in temp (°F) | |
| 6 | 6 | Built-in humidity (%RH) | |
| 7 | 7 | Built-in VPD (kPa) | |
| 11 | 11 | CO₂ (ppm) | BLE value × 100 = ppm |
| 12 | 12 | Light | BLE value × 100 |
| 13 | 13 | pH | |
| 14 | 14 | EC (µS/cm) | |
| 16 | 16 | TDS (ppm) | |
| 18 | 18 | Water temp (°F) | |

Sensor type codes in the packet rotate +8 each time a new sensor is added to the controller. The decoder handles this automatically with a byte-by-byte scan.

Commands are sent to characteristic `70d51001-2c7f-4e75-ae8a-d758951ce4e0`. Responses on `70d51002-2c7f-4e75-ae8a-d758951ce4e0`.

### BLE file map

```
aci_ble_lab/
  db.py              — SQLite schema + helpers
  common.py          — shared BLE utilities
  scan.py / inspect.py / listen.py / proxy.py — GATT tools
scripts/
  logger.py          — main 1 Hz data logger (start here)
  ctl.py             — manual device control via drop-file
  daily_graph.py     — graph generator
  controller_agent.py — hybrid rules/ML/LLM climate controller
  query_sensors.py   — DB query tool (avg/min/max by port)
  type_timeline.py   — when each sensor type first appeared
  [probe_*.py]       — BLE protocol reverse-engineering scripts (reference)
```

### Windows note

Only one BLE central connection is allowed at a time. If another process is holding it, the logger retries every 15 s. Find competing processes:
```powershell
tasklist /FI "IMAGENAME eq python.exe"
```

---

## Sensor type cross-reference

The cloud API `sensorType` integers and the BLE `port_id` bytes are the same numbering system. Data from both layers can be merged by matching on this common ID.
