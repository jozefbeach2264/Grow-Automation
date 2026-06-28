# Grow Automation — Claude Context

## What this is

A custom polling, aggregation, and AI control layer for an RDWC (Recirculating Deep Water
Culture) hydroponic grow system built on AC Infinity cloud-connected hardware.

The AC Infinity app can only talk to one device at a time and has no cross-device logic.
This system collects all sensor data into one place, runs a local AI reasoning layer, and
issues automated control commands across all devices.

---

## Hardware

| Device | Model | API Type | Name in app | Role |
|--------|-------|----------|-------------|------|
| Controller 1 | CTR89Q | type 20 | "4 x 4" | Climate / lighting / air management |
| Controller 2 | CTR89Q | type 20 | "Hydroponics Control" | Reservoir — dosing, pH, hydro sensors |
| Power Strip | ADA4 | type 21 | "Auxiliary Outputs" | Hard on/off switching for high-draw devices |

**Sensors connected:**
- HDS3 hydro probe on Hydroponics Control — pH, EC (uS/cm and mS/cm), TDS ppm, water temp F
- Built-in temp/humidity/VPD on both CTR89Qs
- CO2 sensor, light sensor (on respective controllers)
- Water level sensor — pending; ultrasonic planned. Manual override active in the meantime.
- External air probes (additional temp/humidity zones)

**Air sensor display and AI visibility** (current config in `labels.env`):
- "4 x 4" **external probe (sensorType 0 / `temp_f_ext`) = Tent** = the in-canopy INSIDE TEMP and
  source of truth for tent air. *This* probe is the one that heat-soaks under the X6 (~93F at light
  setting 7, no exhaust). Verified 2026-06-05 by a body-heat probe test: cupped in-hand it climbed
  73->90F in 60s while the built-in sat flat at 75 -- so the in-canopy sensor is type0/external, NOT
  the built-in. The **built-in (sensorType 4 / `temp_f`) is the cooler secondary** (~80F under the
  same load), labeled `Tent_Intake` and suppressed via `HIDE_AIR_BUILTIN_4_X_4=true`.
  Labels: `AIR_LABEL_4_X_4=Tent_Intake` (built-in), `AIR2_LABEL_4_X_4=Tent` (external = the canopy/INSIDE).
- "Hydroponics Control" built-in = Outside reference (stable, far from tent). Its external probe
  is suppressed via `HIDE_AIR_EXT_HYDROPONICS_CONTROL=true`. Label: `AIR_LABEL_HYDROPONICS_CONTROL=Outside`.
- "Auxiliary Outputs" air sensors fully suppressed via `HIDE_AIR_AUXILIARY_OUTPUTS=true`.
- Per-side flags: `HIDE_AIR_<SLUG>` hides everything, `HIDE_AIR_BUILTIN_<SLUG>` hides only the
  built-in sensor hub, `HIDE_AIR_EXT_<SLUG>` hides only the external probe.
  Water temp, CO2, light, and hydro sensors are unaffected — only temp/humidity/VPD filter.

**X6 heat curve + 95F guardrail (built 2026-06-16).** Light setting 7, exhaust OFF: Tent (external)
ramps ~73 -> 93F over ~90 min and plateaus. Exhaust ON pulls it to a ~81.5F hold at full light
(-2.2F/min initial bite); light OFF returns it to room. Room (Hydroponics "Outside") held 73-77F
throughout. **DONE:** deterministic high-temp guardrail mirroring the CO2 dump --
`schedule.compute_temp_emergency` watches `HIGH_TEMP_SENSOR` (default `temp_f_tent`, the
external/type0 canopy probe); >= `AIR_TEMP_EMERGENCY_F` (95) forces `ROLE_EXHAUST` to max, holds
until < `AIR_TEMP_CLEAR_F` (88, hysteresis). Attached to the snapshot as `temp_emergency`,
actuated pre-AI + re-checked post-AI by `poller.enforce_temp_emergency` (detect-always /
actuate-in-LIVE, same contract as the CO2 dump). CLIMATE-ONLY: never touches chemicals or the CO2
valve, so it runs independently of the reservoir/CO2 emergencies. The AI is told (system prompt)
not to fight an active guardrail. Tests: `schedule_test.py` (29 cases). AIR_TEMP_MAX=85 is just a
soft target band, not the cutoff -- the guardrail's 95F is the hard one.

**Device display order** (controlled by `DISPLAY_ORDER_<SLUG>` in `labels.env`):
1. "4 x 4" — tent climate/lighting
2. "Hydroponics Control" — reservoir
3. "Auxiliary Outputs" — outlets

**Laptop running everything:** ThinkPad P1 Gen 3, NVIDIA Quadro T2000 Max-Q (4GB VRAM).
For consistent local-Ollama throughput the GPU clocks can be locked high; that's handled by an
external helper outside this repo (the old `nvidia-perf.service` unit was removed). Note:
power-limit changes (`-pl`) are NOT supported on Max-Q — clock-locking only.

---

## Doser pumps (Hydroponics Control ports 1–4)

AC Infinity peristaltic pump spec: **21 mL/min per speed level** (linear, speed 1–10).
All four ports are designated dosers via `DOSER_PORTS_HYDROPONICS_CONTROL=1,2,3,4` in `labels.env`.

| Port | Label | Purpose |
|------|-------|---------|
| 1 | Floraflex V1 | Nutrient part A (V1 veg / B1 bloom) |
| 2 | Floraflex V2 | Nutrient part B (V2 veg / B2 bloom) |
| 3 | PH UP | pH adjustment up |
| 4 | PH DOWN | pH adjustment down |

Ports 1+2 are ALWAYS dosed together at equal speed — never one without the other.
Ports 3+4 are pH ports via `PH_PORTS_HYDROPONICS_CONTROL=3,4` — safety gate applies longer lockout.

---

## File map

| File | Purpose |
|------|---------|
| `poller.py` | Main loop — polls devices, drives AI, adaptive sleep, displays readings |
| `ac_infinity_client.py` | AC Infinity cloud API client — auth, fetch, parse, control |
| `ai_advisor.py` | Ollama reasoning layer (qwen2.5:3b-instruct default), safety gate, res health, trend detection |
| `profile_manager.py` | Strain profiles, outcome tracking, calibration context builder |
| `grow_state.py` | Auto-compute current week + stage from `GROW_START_DATE` and `VEG_DAYS` |
| `runtime_state.py` | Heartbeat, active-dose record, high-alert window, event log -- crash recovery |
| `dosing.py` | Timed dosing with forced stop (#7) -- bounded doses, ramp math, playbooks |
| `schedule.py` | Schedule-driven expected states (light fade, osc fans) + deterministic emergencies: CO2 dump, CO2 pulse, high-temp exhaust guardrail |
| `schedule_test.py` | Self-tests for the high-temp exhaust guardrail (29 cases, mocked snapshots) |
| `event_log.py` | Structured cycle + action-lifecycle ledger over `events.jsonl` (cycle_id/action_id threading, `recent_actions()`) |
| `event_log_test.py` | Self-tests for the event ledger (40 cases, temp JSONL) |
| `diagnostics.py` | Deterministic stressor list + code-owned playbook registry (away-mode triage foundation; READ-ONLY, no actuation) |
| `diagnostics_test.py` | Self-tests for the stressor list + registry (32 cases) |
| `away_mode.py` | Away-mode triage executor: code-driven worst-first playbook dispatch (climate-only live, chemical/CO2 dry-run) |
| `away_mode_test.py` | Self-tests for the away-mode executor (17 cases) |
| `ppfd.py` | Light/PPFD framework: PPFD-map loader, grid stats, height interpolation, DLI math, level recommendation (advisory) |
| `ppfd_capture.py` | Interactive ingest tool — records an Apogee PPFD grid per level x height into `ppfd_map.json` |
| `ppfd_test.py` | Self-tests for the PPFD framework (37 cases, synthetic map) |
| `ppfd_map.json` | Measured/modeled PPFD map (Growcraft X6 grid at each level x canopy distance); committed hardware characterization. **2026-06-28: DUAL-HEIGHT predictive map (16in + 24in, levels 1-7), each grid source-tagged measured/predicted -- 24in L1-7 + 16in L5/6/7 measured, gaps predicted via the measured 16/24in center-ratio (~1.19); `ppfd.py` interpolates any canopy distance between. Levels 8-10 omitted (operator run-and-read).** `ppfd_map.example.json` = schema template |
| `ppfd_build_map.py` | Builds the SMOOTHED, PREDICTIVE `ppfd_map.json` for every level at every measured height -- one fixture beam-shape + per-level magnitude (measured where read, predicted for the gaps via the cross-height center-ratio), each grid source-tagged measured/predicted. Re-runnable as more readings come in |
| `bucket_ai_dose_test.py` | Supervised closed-loop bucket calibration harness (feedforward + creep; reworked 2026-06-04) |
| `bucket_dose_test.py` | Manual single-pump dose-response characterization |
| `ac_infinity_history.py` | Loader + merged store for trend history: `record_snapshot()` self-logs the poller's own reservoir readings (phone-free), `ingest()` accretes/dedupes the app's CSV "Device Data" exports, `load_history()` reads the merged series; `parse_export`/`latest_export` for single files (no cloud history API) |
| `ac_infinity_history_test.py` | Self-tests for the CSV loader + merged trend store + self-logging (44 cases, synthetic) |
| `dose_align.py` | Aligns logged doses with the CSV trend to recover real dose-response + refine K |
| `trend_db.py` | TimescaleDB trend store: hypertable + continuous aggregates, best-effort writes, query helpers (`record_snapshot_db`/`ingest_samples_db`/`bucketed`/`latest`/`stats`) |
| `trend_features.py` | Multi-window trend analysis for the AI (per-metric level/range/slope-per-hr) + `format_block` for the prompt/HUD |
| `migrate_trend_to_db.py` | One-time backfill of the JSONL trend store into TimescaleDB (idempotent) |
| `trend_db_test.py` | Self-tests for trend_db + trend_features (22 cases, throwaway schema, real TimescaleDB) |
| `sql/trend_schema.sql`, `sql/trend_policies.sql` | Trend hypertable + continuous aggregates; cagg refresh policies |
| `scripts/setup_timescaledb.sh` | One-time TimescaleDB bring-up (install, initdb, role/db, extension) |
| `utils.py` | Shared text utils (currently just `name_slug`) |
| `safety_state.py` | Persistent chemical-only freeze (dosers + pH + CO2 valve); never cuts climate |
| `ble_logger.py` | Persistent BLE daemon: 1Hz telemetry + drains `command_queue` for one controller |
| `aci_ble_lab/db.py` | SQLite command queue + sensor cache; gates chemical writes at enqueue |
| `aci_ble_lab/safety.py` | Port classification + `guard_chemical_write` (the universal chemical write check) |
| `aci_ble_lab/common.py` | BLE-side utilities (device-name matching, JSON IO) |
| `labels.env` | Port labels, doser ports, pH ports, per-port speed caps, HIDE_AIR flags |
| `.env` | Credentials, AI settings, safety thresholds, calendar, strain config |
| `profiles/` | Per-strain JSON files accumulating run history and calibration data |
| `profiles/.pending_outcomes.json` | Persistent queue of actions awaiting outcome readback |
| `Floraflex1.webp` | FloraFlex Full Tilt schedule reference image |
| `dwc res rules.jpeg` | DWC water/EC/pH trend diagnostic table reference image |

---

## BLE layer (optional local transport)

Forked from PR #2 (sethmblack); kept his protocol decoders and reused the
`ac-infinity-ble` library, dropped his ungated `ctl.py` / SQLite-queue bypass
and replaced them with a hard chemical-port guard.

**Architecture.** `ble_logger.py` is a long-running daemon (one per CTR89Q
controller) that holds the BLE connection, subscribes to the 1Hz status
packet, periodically polls per-port state, and drains a SQLite command queue
(`profiles/controller.db`). Callers (poller / dosing.py / future operator
tools) enqueue rows via `aci_ble_lab.db.enqueue_command(device, port,
work_type, speed, source)`; the daemon claims and writes them. The BLE
channel is **not** a parallel control path -- it is a transport that all the
existing safety code can use.

**Safety model -- defense in depth.** `aci_ble_lab.safety.guard_chemical_write`
classifies a port as chemical (it's in `DOSER_PORTS_<SLUG>`,
`PH_PORTS_<SLUG>`, or it is the `CO2_VALVE` outlet) and rejects the write if
`safety_state.dosing_disable_status()` is active. This guard runs in BOTH
`enqueue_command` AND the executor in `ble_logger.py` -- a row that became
stale during a freeze drops at the daemon before the write goes out.
Classification is recomputed on every call so `.env` edits take effect
mid-run (same model as `schedule.py`).

**Wiring.** Each controller needs `BLE_<SLUG>_MAC=AA:BB:CC:DD:EE:FF` in
`.env`, where `<SLUG>` is `name_slug(device_name)`. Run the daemon as:
`python ble_logger.py --device-name "4 x 4"`. Per-device daemons share the
queue; each silently re-queues rows addressed to a different device.

**What the BLE channel buys you.** ~1Hz local telemetry vs the cloud poller's
60s active interval, no dependency on AC Infinity's cloud (works during
outages), and a separately-attestable command path for audit. The cloud
transport (`ac_infinity_client.py`) stays as the default and as the fallback.

---

## Bucket calibration harness, dosing rework & trend data

**Supervised bucket calibration** (`bucket_ai_dose_test.py`; companion `bucket_dose_test.py`):
set target pH/TDS, the code calculates the dose, you confirm each, it hard-settles, re-reads,
online-updates K, logs to `profiles/bucket_test_log.jsonl`. **Reworked 2026-06-04:**

- **Nutrients (calculable):** one calculated **85% fast shot at high speed** (`FAST_DOSE_SPEED=8`)
  then **low-speed creeps** (`CREEP_DOSE_SPEED=2`). The dose is sized BEFORE any deadband check --
  it converges until the *calculated* dose drops below the pump's minimum deliverable pulse, never
  a ppm band. Bucket cap `BUCKET_MAX_DOSE_ML=250` replaces the 50 mL grow cap so the shot fires
  whole. Pair K ~3.5 ppm·gal/mL is stable across the run -> trustworthy. `load_calibration` now
  folds in the `axis=="pair"` records, so nutrient K self-updates from V1+V2 doses.
- **pH (NOT yet calculable):** the EC-normalized buffer constant is non-stationary (pH-down K
  ranged **112 -> 218** across EC 518 -> 878) and overshoots transiently (a 17 mL pH-down crashed
  pH to **4.60** then re-buffered to 5.4). So the feedforward is pulled: pH creeps a **fixed
  `PH_CREEP_ML=4` mL** per dose (`PH_DONE_TOL=0.05`), logging each `K_obs` to build the buffer map
  across pH bins. Calculate pH later once the map is dense.
- **V1/V2 ratio:** `NUTE_RATIO_<SLUG>` (e.g. `55/45`), default 50/50, clamped to 45-55% per part --
  a manual *volume* knob, never potency-driven. Implemented via per-port `{port: mL}` volumes in
  `timed_dose_pair` (each pump still stops on its own clock).

**Trend / history data -- CSV export, NOT the API.** The cloud API has **no history endpoint**
(confirmed by probing the API across 24 candidate endpoint names; only `appUserLogin`,
`devInfoListAll`, `getdevModeSettingList`, `addDevMode` exist; `devInfoListAll` returns current
values + a 1-bit trend *direction* only). Export **"Device Data" to CSV** from the AC Infinity app
(saved to `~/Downloads`, e.g. `AC INFINITY Data (N).csv`; 1-min resolution pH / TDS / water-temp /
leak / outside-air -- **TDS only, no EC**). **Transport off the phone:** the export lands on the
phone; get it to this machine via KDE Connect (the Pixel is already paired) or Taildrop, dropping
into the incoming dir (`~/Downloads`, override `ACI_EXPORT_DIR`). Read/ingest it via
**`ac_infinity_history.py`**: `ingest()` accretes every export into one deduped, continuous
per-device store (`trend_data/acinfinity_history.jsonl` + a content-hashed raw archive under
`trend_data/acinfinity/`, both gitignored) keyed by (device, timestamp), so overlapping or
re-exported windows stop fragmenting/overwriting; `load_history()` returns the whole merged series,
with `parse_export` / `latest_export` / `Export.window` / `.around` for single files.
**Phone-free primary path:** `record_snapshot(snapshot)` logs the reservoir sensors the poller
already reads each cycle into the SAME store (gated by `TREND_LOG_ENABLED`, default on; wired in
`poller.py` right after `build_snapshot`, error-swallowed), so the automation accretes its own dense
trend with no phone/app involved -- during dose windows the ACTIVE 60s cadence captures the
dose-response curves natively. The CSV export then drops to a one-time backfill of pre-automation
history; the denser/offline upgrade is the BLE `sensor_readings` table (`aci_ble_lab/db.py`, ~1Hz).
(Self-logging only accrues while the poller runs -- an always-on service is the 24/7 piece.)
**`dose_align.py`** (now ingests first, then reads the merged history) aligns logged doses with these
1-min curves to recover the real dose-response -- the dense data caught the 4.60 pH transient and
the +57 -> +45 nutrient settle that the single before/after reads miss. When the HDS3 EC channel
glitches (reads ~1/10 scale), rebuild EC from TDS: **`EC ~= 1.41 * TDS`** (steady ratio).

---

## Trend store — Postgres / TimescaleDB (AI trend analysis)

The queryable time-series home for trend data so the AI can analyze multi-hour/day patterns,
not just the previous-cycle `_trend()` delta. **Postgres 18 + TimescaleDB 2.27** (local, unix
socket, db `grow`, role = OS user via trust/peer auth — no password). **Opt-in + decoupled:**
writes are best-effort, the JSONL store stays the source of truth, and the control loop never
blocks on Postgres. Disable with `TREND_DB_ENABLED=false`.

- **Setup (one-time, sudo):** `sudo bash scripts/setup_timescaledb.sh` — installs
  postgresql/timescaledb/python-psycopg, initdb (`--encoding=UTF8`), enables the preload, creates
  role + db `grow`, `CREATE EXTENSION timescaledb`. Idempotent. (timescaledb is in the official
  `extra` repo — no AUR. `CREATE EXTENSION` needs superuser so it's done as `postgres`; the app
  role is non-superuser but owns its tables, so it can still create hypertables/caggs.)
- **Schema** (`sql/trend_schema.sql`, applied by `trend_db.ensure_schema()`): hypertable
  `trend_samples(ts, device, metric, value, source)`, dedup UNIQUE (device, metric, ts) — so a
  backfill row and a live row at the same instant can't double-count. Real-time continuous
  aggregates `trend_hourly` / `trend_daily` (`time_bucket` avg/min/max/first/last/n;
  `materialized_only=false` so reads are correct with no manual refresh). Policies in
  `sql/trend_policies.sql` (`ensure_policies()`).
- **Writes** (`trend_db.py`): `record_snapshot_db()` (poll) + `ingest_samples_db()` (csv) are
  mirrored from `ac_infinity_history.record_snapshot()` / `ingest()` via `_trend_db_write`
  (best-effort, `connect_timeout=3`). `migrate_trend_to_db.py` backfills the JSONL store once
  (22.8k metric-rows from the 5070 samples on first run).
- **Analysis** (`trend_features.py`): `trend_features()` → per-metric {last, avg, min, max,
  slope/hr (least-squares over hourly buckets), n} over a window; `format_block()` renders it.
  `poller.py` attaches `snapshot["trend_analysis"]` each cycle (HUD `[TREND]`), and
  `ai_advisor.ask_ai` injects the rendered block into the prompt (raw dict dropped from the JSON
  to save context). CSV backfill has no EC; live polls log `ec_us`.
- **Config:** `DATABASE_URL` (default `postgresql:///grow?host=/run/postgresql`),
  `TREND_DB_ENABLED` (default true). **Ops:** `python3 trend_db.py stats|ensure|policies`.
- **Tests:** `trend_db_test.py` (22, throwaway schema vs real TimescaleDB). **Deferred (Phase 5):**
  compression/retention policies; BLE `sensor_readings` → PG bridge for ~1Hz density. The
  event-log/profiles migration to PG (the 10-table design) stays deferred — this was trend-data only.

---

## AC Infinity API — critical details

- Base URL: `http://www.acinfinityserver.com` (HTTP, not HTTPS)
- Auth: POST `/api/user/appUserLogin` → returns `appId` used as `token` header
- Token is cached in `.env` as `AC_INFINITY_TOKEN` and reused across restarts
- **Do NOT add `minversion: 3.5` header to read/list endpoints** — causes 404.
  Only control endpoints (`/api/dev/modeAndSetting`, `/api/dev/addDevMode`) need it.
  Handled by `ai_control=True` param in `_post()`.
- All sensor data nested under `raw["deviceInfo"]` — not at the top level
- Ports at `raw["deviceInfo"]["ports"]` (not "portInfos")
- Sensor readings in `deviceInfo["sensors"][]` as `{sensorType, sensorData}` pairs
- Raw sensor values are integers scaled by 100 (divide by 100.0) -- EXCEPT HDS3
  EC uS/cm (type 14) and TDS ppm (type 16), which are scaled by 10. Per-type factors
  live in `SENSOR_TYPE` in `ac_infinity_client.py`; never assume a blanket /100.
- `-32768` (INT16_MIN) is the "no sensor connected" sentinel — skip these
- `sensorData == 0` also means no reading — skip these too

### Control write protocol (solved 2026-05-30 via packet capture)

- Shared control path in `ac_infinity_client.py`: `set_port_speed()` and `set_outlet()`
  both route through `_control_port()`.
- Working write pattern: `PUT /api/dev/modeAndSetting` with payload sent as URL query
  params (`params=payload`), `addDevMode` is retained only as a fallback.
- Required fields (discovered from app traffic capture):
  - `onSelfSpead` = the new target speed (NOT `onSpead` — that's a readback of the
    current state and was the original protocol bug)
  - Full TOP-LEVEL settings dump from `getdevModeSettingList` (~125 scalar fields),
    excluding nested objects (`devSetting`, `fieldSet`, `ipcSetting`, etc.)
  - `restore=false`, `onlyUpdateSpeed=0`, `modeAndSettingIdStr=[16,17]` (OFF) or
    `[16,18]` (manual), `modeSetid` preserved from current state
- Required headers: `token`, `minversion=3.5`, and `devType=<int>` for CTR89Q writes
  (e.g. `devType: 20`).
- Ramp behavior (measured 2026-05-30 on Growcraft X6 port): perfectly linear
  **1 speed unit per second**, symmetric both directions. `ramp_seconds(target, current)`
  in `ac_infinity_client.py` returns the appropriate wait time.
- All 8 variable-speed ports on both CTR89Qs and all 4 ADA4 outlets confirmed responding.

### Packet capture setup (for future protocol work)

NetworkManager hotspot on Alfa AWUS1900 (`wlp0s20f0u1`) routes the phone through the
laptop; tcpdump captures plain HTTP to `www.acinfinityserver.com` while the app fires
a control change. Requires `dnsmasq`. IP forwarding and iptables masquerade may need
to be enabled manually after NM brings the hotspot up.

### Sensor type map
```
4  = built-in temp F (*100)       6  = built-in humidity (*100)
7  = built-in VPD kPa (*100)      0  = external temp F (*100)
2  = external humidity (*100)      3  = external VPD (*100)
11 = CO2 ppm (raw)                12 = light (raw)
13 = pH (*100)                    14 = EC uS/cm (*10)   <- HDS3 scales /10 not /100
15 = EC mS/cm (*100, UNVERIFIED)  16 = TDS ppm (*10)    <- verified vs controller 2026-06-02
18 = water temp F (*100)          20 = water level (raw)
```

---

## AI layer (Ollama)

- Default model: `qwen2.5:3b-instruct` running locally via Ollama (`ollama serve`)
- Model was chosen via head-to-head benchmark (2026-05-30):
  100% schema-valid on 32/32 set_speed prompts at 1.9s median, fits in 4GB VRAM.
  Backup: `phi4-mini` (also 100%, slower). DeepSeek-R1 1.5B was rejected (~35% pass
  rate — math-tuned base, weak at structured output). Override via `OLLAMA_MODEL`
  in `.env`.
- Reasoning models (R1 family) wrap output in `<think>...</think>` tags — stripped
  before JSON parse. Harmless for non-reasoning models like Qwen.
- `warmup()` pre-loads model into VRAM on startup before first real call
- Context: `num_ctx=4096`, `num_predict=1200`, `temperature=0.2`, timeout 240s

**Every AI prompt contains (in order):**
1. System prompt — res health gates, DWC rules, FloraFlex schedule, Bugbee CO2 profile,
   target ranges — all built dynamically from `.env` via `_build_system_prompt()`
2. Current sensor snapshot (JSON) — includes trends, res_health block, grow_week/stage
3. System calibration — observed dose-response table from `profile_manager`
4. Strain history — week-averaged readings from previous runs of the same strain

---

## Decision priority chain

The res is the anchor. Nothing advances until the plant confirms it's ready.

```
1. Res health gate  (water + EC trends)
       |
       ├─ IDEAL/GOOD  → gates open, proceed to schedule
       ├─ WATCH       → hold CO2, evaluate dose case by case
       ├─ STALL       → hold everything, investigate environment
       ├─ STRESS      → reduce CO2, no nutrients, no pH
       └─ PROBLEM     → no nutrients, alert
       |
2. Safety gate  (filter_actions — runs regardless of res health)
       |
       ├─ Per-port dose lockout
       ├─ pH lockout + one pH action per cycle
       └─ Per-port + global speed caps
       |
3. Schedule targets  (only reached if gates pass)
       ├─ PPM target  (FloraFlex, per week, PPM_SCALE aware)
       ├─ CO2 target  (Bugbee, per week)
       └─ pH range    (PH_MIN / PH_MAX)
```

---

## Safety gate

All AI-proposed actions pass through `filter_actions()` in `ai_advisor.py` before any
API call. Rules (all thresholds configurable in `.env`):

0. **Chemical dosing interlock** — chemicals move ONLY via the `dose` playbook verb
   (routes to `dosing.timed_dose`). A raw `set_speed`>0 on a doser/pH port is rejected
   outright; the `dose` verb carries the chem gating (freeze, res-health gate, lockout,
   one-pH-per-cycle, mL ceiling). Stops (speed 0) are always allowed.
1. **Per-port dose lockout** — after a port fires, blocked for `DOSE_LOCKOUT_MINUTES`
2. **pH lockout** — pH ports blocked for `PH_LOCKOUT_MINUTES` after any pH action
3. **One pH action per cycle** — pH UP and pH DOWN cannot both fire in the same cycle
4. **Per-port speed cap** — `MAX_SPEED_HYDROPONICS_CONTROL_<N>` in `labels.env` overrides
   global `MAX_DOSER_SPEED` (climate speed ports only; chemicals are dose-verb only)
5. **mL/min ceiling** — `MAX_DOSE_ML_CYCLE` also caps a dose playbook's `target_ml`

---

## Read-after-write verification (`VERIFY_WRITES`)

A 200 from the AC Infinity API only proves the write was *accepted*, not that the port
physically changed. After each write the system polls readback until the port reports
the expected state (Level 2 verification):

- `read_port_state(token, dev_id, port)` / `verify_port_state(token, dev_id, port,
  expected, ...)` live in `ac_infinity_client.py`. `expected` is `{"powered": bool}` or
  `{"speed_actual": int, "tolerance": int}`. Returns `{ok, reason, observed, attempts,
  elapsed_sec}`. Timeouts use the ramp model (`ramp_seconds`); outlets use 15s.
- `ai_advisor.execute_actions` verifies every write (`_verify_executed_action`). Doser/pH
  use tolerance 0; fans/lights tolerance 1.
- **Critical auto-trip:** a doser/pH **STOP** that fails verification is retried once; if
  it still won't confirm, `disable_dosing()` FREEZES dosing — a pump that won't stop is a
  chemical hazard. (This is the safety link the dosing freeze was built for.)
- `poller.enforce_res_burst` also verifies + retries its doser stops (most critical case).
- Gated by `VERIFY_WRITES=true` (default). The `SIM` token always skips (sim_runner).

## Kill switch & reservoir-burst shutdown (`safety_state.py`)

Two **separate** safety concepts — deliberately not unified, because killing
ventilation/lighting is itself a hazard and must never cascade from a chemical fault.

**1. Chemical freeze (`dosing_disabled`) — chemicals only.**
- Blocks doser + pH ports in `filter_actions()`. Lights, fans, exhaust, CO2 keep
  running normally. Stops (speed 0 / outlet off) are always allowed.
- Sources (OR'd): env `DOSING_DISABLED=true` (coarse manual), or a persisted trip in
  `profiles/.safety_state.json` (atomic write, survives restart).
- API: `safety_state.disable_dosing(reason)` to trip (use for fail-safe auto-trips like
  a failed pump-stop once read-after-write lands), `clear_dosing_disable()` to lift.
- Corrupt/missing state file → treated as NOT disabled (won't silently block).

**2. Reservoir-burst shutdown — WATER/CHEMICAL ONLY. Never cuts lights or ventilation.**
- `ai_advisor.compute_res_burst()` detects; `poller.enforce_res_burst()` actuates as the
  highest-priority pre-AI step. Scope: stop all doser/pH ports + close the CO2 valve.
  It does not enumerate or command `ROLE_LIGHT`, `ROLE_EXHAUST`, or `ROLE_OSC_FANS`.
- On fire, also calls `disable_dosing()` so chemicals stay frozen until manual clear.
- **Inert by default.** Requires `RES_BURST_ENABLED=true`. Trips off the **boolean
  leak sensor** (`water_leak`, the `LEAK_SENSOR` device's `sensorType=20`), wet =
  nonzero. Needs `RES_BURST_DEBOUNCE` consecutive wet reads (default 2) so one noisy
  reading can't freeze dosing. Never triggers off `water_level` or the manual
  `WATER_LEVEL_TREND` (false-trip protection).
- Detected always (alert printed even in `ADVISORY_MODE`); actuates only in LIVE.
- Wet/dry raw values confirmed `0=dry / 1=wet` on the ACI water sensors (2026-06).

**3. Evac pump (`compute_evac_pump` / poller).** When the leak sensor is confirmed wet,
turn an evac pump outlet **ON** to pump water out; turn it **OFF** when the sensor reads
dry (so it never runs dry). Gated by `EVAC_PUMP=<device>:<port>` (blank = no action),
independent of `RES_BURST_ENABLED`. Fires a `set_outlet` only when the desired state
differs from the pump's current `powered` state (no redundant writes). Shares the one
debounced leak assessment (`snapshot["leak"]`, `_assess_leak`) with res-burst, so both
react to the same confirmed signal. Not a doser — unaffected by the dosing freeze.

## Timed dosing with forced stop (`dosing.py`)

Bounded doses that replace open-ended `set_port_speed` for chemicals (Layer 1 #7).
`calculate_timed_dose(speed, target_ml, flow_ml_min, ramp_rate)` is pure math: ramp-up
and ramp-down each deliver ~(S/2)*flow over the ramp time, the hold delivers the rest;
a `target_ml` below the minimum ramp-only pulse is rejected as below hardware resolution.

`timed_dose(token, dev, port, speed, target_ml, solution, strength, advisory)`:
1. verify port at 0, 2. persist a crash-safe active-dose record (watchdog leaves it alone),
3. start pump, best-effort start-confirm for long doses, 4. hold `on_ms` on a monotonic
clock, 5. ALWAYS stop in `finally` + verify (retry once), 6. on unverified stop ->
`safety_state.disable_dosing()` + high-alert. `timed_dose_pair(ports=[1,2], ...)` doses
nutrient V1+V2 together: both start, both stop, freeze if either start/stop fails.

- Flow model 21 mL/min/speed (override `FLOW_ML_MIN_<SLUG>_<port>`); ramp 1 speed/sec
  (`RAMP_SPEED_PER_SEC`). Dose sizes: `PH_MICRODOSE_ML`, `PH_SMALL_DOSE_ML`,
  `NUTE_MICRODOSE_ML_EACH`, `NUTE_SMALL_DOSE_ML_EACH` (code-owned).
- **Diluted tests:** `STRENGTH_FACTOR_<SLUG>_<port>` (<1.0) converts actual mL to
  full-strength-equivalent so diluted observations don't fool calibration. Dose math is
  always in actual mL delivered.
- **Playbooks** (`PLAYBOOKS` / `resolve_playbook`): the only chemical actions the AI may
  pick (e.g. `timed_ph_down_microdose`, `timed_nutrient_microdose`); code maps name ->
  speed + dose size. AI never chooses raw pump duration.
- **Wired into the live path (autonomous dosing):** the AI emits a `dose` action
  (`{device, action:"dose", playbook}`); `ai_advisor.execute_actions` resolves the
  port(s) and routes it to `timed_dose` / `timed_dose_pair`. Raw `set_speed` on a
  doser/pH port is rejected by `filter_actions` (the interlock). pH UP/DOWN resolve from
  `PH_PORTS` order (override `PH_UP_PORT_<SLUG>` / `PH_DOWN_PORT_<SLUG>`); the nutrient
  pair is the non-pH doser ports.
- **Gated by `AUTONOMOUS_DOSING`** (default false): dose actions are validated, gated, and
  logged as "would dose" but NOT actuated until it is set true after live validation.
- `timed_dose_pair` stops each pump at its OWN computed time (not `max(on_ms)`), so
  per-pump flow differences (`FLOW_ML_MIN_<SLUG>_<port>`, e.g. V2 ~16% faster) still
  deliver equal volume. The hold clock starts AFTER the start write (pre-start GET not
  charged against the dose).
- pH is always speed 1 (strictest path). Reservoir-gate / lockout / schema enforcement
  stays in `filter_actions`/`validate_actions` -- callers gate before dosing.
- **Hard doser settle:** `DOSE_SETTLE_SEC=300` / `dose_settle_seconds()` (env
  `DOSE_SETTLE_MINUTES`, default 5) is the canonical minimum wait after ANY doser/pH dose
  before the reservoir reading is trusted -- pH keeps drifting ~5 min past the apparent
  quick-settle (observed 2026-06-02). Enforced in BOTH the test harness (`bucket_dose_test.py`,
  no early exit) and the autonomous outcome-readback (`profile_manager._wait_for` -> doser/pH
  actions wait `max(OUTCOME_WAIT_CYCLES window, DOSE_SETTLE_SEC)`).
- Tests: `dosing_test.py` (34 cases). Live validation pending HDS3 + `RESERVOIR_VOLUME_GAL`.

## Watchdog & crash recovery (`runtime_state.py`)

Detects a process crash / power loss mid-dose and makes sure no chemical pump is left
running. Separate from `safety_state.py` (which owns the persistent dosing freeze) --
this module owns liveness. Two clocks per `docs/done/WATCHDOG_HEARTBEAT_PLAN.md`: wall clock for
timestamps/cross-restart math, monotonic for in-process durations (never time a dose
with the wall clock -- NTP can jump it).

- **Heartbeat** -> `profiles/.runtime_state.json` (atomic, corrupt-tolerant): phase, pid,
  `boot_id` (changes on reboot, so crash vs reboot is distinguishable), wall + monotonic
  time, last poll/api/readback ok. Written each cycle by `poller.heartbeat()`. Clean exit
  writes phase `shutdown`; any other phase on next start = unclean. `HEARTBEAT_ENABLED`.
- **Startup recovery** (`poller.recover_on_startup`, runs before AI/polling): diagnoses the
  last run (`diagnose_restart`), estimates an interrupted dose, stops any running chemical
  pump, then freezes dosing + opens high-alert. Crash mid-dose with nothing currently
  running still freezes (the gap is unknowable).
- **Nonzero-doser watchdog** (`poller.doser_watchdog`, every cycle): a doser/pH port
  running outside an active-dose window is an orphan -> stop + verify + retry + freeze +
  high-alert. Detect-always / actuate-in-LIVE (same contract as res-burst).
  `DOSER_WATCHDOG_ENABLED`. `_verified_doser_stop()` is the shared stop+verify+retry helper.
- **Active-dose record** (`begin_active_dose` / `active_dose_window_port` / `clear_active_dose`):
  written BEFORE a pump starts so a crash is recoverable. Structure wired now; timed dosing
  (#7) populates the planned fields. `active_dose_window_port()` returns None until then,
  so the watchdog treats every running doser as an orphan (correct -- nothing should dose yet).
- **High-alert window** (`start_high_alert` / `high_alert_status`): persisted faster
  reservoir polling after a scare; clamps the sleep down to `HIGH_ALERT_POLL_INTERVAL` for
  `HIGH_ALERT_DURATION_MINUTES`, auto-expires on read. Does not itself gate chemicals
  (the freeze does that independently).
- **Event log** `profiles/events.jsonl` (append-only JSONL, `record_event`): process_started,
  process_restarted, active_dose_*, stop_recovery_*, estimated_overdose_window, high_alert_*,
  clean_shutdown. Doubles as the Layer 2 action-ledger seed.
- Tests: `watchdog_test.py` (34 cases, mocked hardware + temp state files).
- Deferred to #7/hardware: precise dose-estimate math, sensor/API freshness watchdogs
  (need HDS3), systemd `Restart=always`/`WatchdogSec`.

## Event ledger (`event_log.py`)

Structured cycle + action-lifecycle log built ON TOP of the same append-only
`profiles/events.jsonl` (via `runtime_state.record_event`). NOT a new store -- per the
architect review, JSONL stays until it's genuinely painful to query; the 10-table SQLite
design in `EVENT_LOGGING_PLAN.md` is deferred to v1.1. All helpers swallow errors --
logging must never take down the control loop.

- **`start_cycle(snapshot, mode)` -> cycle_id** (one per poll): records grow week/stage,
  res-health gates, flat sensor map, schedule-delta count, and which emergencies are active.
  Called in `poller.py` right after `build_snapshot`.
- **`log_ai_decision(cycle_id, result, latency)`**: assessment (clipped), action count,
  next_check, parsed_ok, latency. Called after `ask_ai`.
- **Per-action lifecycle** threaded through `ai_advisor.execute_actions(..., cycle_id=)`:
  every proposed action gets an `action_request` + an `action_validation` at the stage that
  decided it (`schema` / `safety_gate` / passed), plus an `action_execution` (sent? success?
  verified? error) for the ones that ran. Precise per-action reject reasons come from the
  optional `reasons` collector on `validate_actions` / `filter_actions` (e.g. `unknown_device`,
  `value_range`, `raw_chem_not_permitted`, `ph_gate_hold`, `lockout_active`) -- opt-in, so
  omitting it leaves the gates' behavior identical.
- **`recent_actions(limit, window_hours)`**: compact newest-first summary of executed actions
  (age, device/port, command, success, verified) for the AI prompt / HUD so corrections
  aren't repeated blindly.
- Event types: `cycle`, `ai_decision`, `action_request`, `action_validation`,
  `action_execution`, `action_outcome` -- alongside the watchdog/recovery events already there.
- Tests: `event_log_test.py` (40 cases) + reason-collector cases in `safety_gate_test.py`.

## Deterministic stressor list (`diagnostics.py`) -- away-mode foundation

Layer 3 (away-mode triage) FOUNDATION, built READ-ONLY. `build_diagnostics(snapshot)`
attaches `snapshot["diagnostics"]` = `{stressors, count, worst_severity}` where each
stressor is `{name, severity, evidence, likely_effect, allowed_playbooks}`. It is a pure
function of the snapshot (thresholds from `.env`, re-read each call) and **does not actuate
anything** and **does not change the AI action contract** -- the raw-action ->
`selected_playbook` contract switch is the riskier, separately-gated next step (architect
review). The block flows to the HUD (`[DIAG]`), the AI prompt (situational awareness, via
the serialized snapshot), and the ledger (`event_log.log_stressors`).

- Stressors emit ONLY for sensors actually present + out of band (so the disconnected HDS3 /
  CO2 stay quiet instead of false-alarming), plus `device_offline` and water-level trend.
  Supported: `tent_temp_high/low`, `humidity_high/low`, `vpd_high/low`, `ph_high/low`,
  `tds_high/low`, `water_temp_high/low` (alert-only, no chiller), `co2_high`,
  `co2_high_while_res_stalled`, `water_level_rising/static`, `device_offline`.
- Severity: info/watch/medium/high/critical; sorted critical-first. Tent temp escalates to
  `critical` at `AIR_TEMP_EMERGENCY_F` (ties to the exhaust guardrail). Water temp capped at
  `medium` (reference-only).
- Canopy sensor keys resolve via `HIGH_TEMP_SENSOR` / `CANOPY_HUMIDITY_SENSOR` /
  `CANOPY_VPD_SENSOR` (defaults `temp_f_tent` / `humidity_tent` / `vpd_tent`).
- `PLAYBOOKS` is the code-owned registry (tier, actuates, chemical flag, summary);
  `allowed_playbooks(name)` returns the per-stressor allow-list with `alert_only` always
  last. Climate playbooks are Tier 1/2; chemical ones are Tier 3 and still inert (the
  away-mode executor that dispatches them is the deferred remainder). Tests:
  `diagnostics_test.py` (32 cases).

## Away-mode triage executor (`away_mode.py`) -- Layer 3

Code-driven deterministic dispatch over the `diagnostics` stressor list. The AI action
contract is deliberately UNCHANGED (the AI stays advisory) -- per the architect review, the
raw-action -> `selected_playbook` prompt refactor is a separate, riskier step. Here CODE owns
the triage: each cycle it alerts on the worst stressor and dispatches the worst ACTIONABLE
stressor's top allowed playbook (one dispatch + one alert per cycle).

- **Gating.** Inert unless `AWAY_MODE=true`. Detect-always / actuate-in-LIVE (same contract as
  the CO2 dump / high-temp guardrail): it alerts + logs intent in any mode, but only ACTUATES a
  live climate playbook when `ADVISORY_MODE=false`. Wired in `poller.py` after the schedule
  fallback + emergency re-checks; runs in both modes.
- **Dispatch policy** (current hardware reality -- reservoir + CO2 disconnected):
  - `increase_exhaust_one_step` -> **LIVE**: steps `ROLE_EXHAUST` by `AWAY_EXHAUST_STEP` (default
    1), capped at `AWAY_EXHAUST_MAX` (10). No-op (skips to next playbook) when already at cap.
    **Yields to the high-temp guardrail** -- returns None while `temp_emergency` is active so it
    doesn't fight the fan the guardrail is slamming to max. Composes nicely: away-mode ramps
    exhaust as the tent climbs 85->95F, the guardrail catches >=95F.
  - `reduce_light_one_step` -> **advisory** (never actuates yet): the schedule enforcer pins
    light intensity, so going live needs a schedule-aware light override (deferred).
  - `disable_co2` -> **dry** (valve disconnected); `timed_*_microdose` -> **dry** (Tier 3,
    gated). Both log "would dispatch".
  - `alert_only` -> log + notify (`event_log.log_alert`, the alert-channel seed).
- **Selection.** `select(snapshot)` walks stressors worst-first (diagnostics already sorts by
  severity); for each it takes the first allowed playbook with an applicable plan. Returns the
  worst stressor (always alerted) + the chosen dispatch (or None -> alert-only).
- **Ledger.** Every dispatch logs an `action_request` (source `away_mode`) + `action_validation`
  (stage `away_dispatch`) + `action_execution`; alerts log an `alert` event. Tests:
  `away_mode_test.py` (17 cases).
- Config: `AWAY_MODE`, `AWAY_EXHAUST_STEP`, `AWAY_EXHAUST_MAX`, `AWAY_LIGHT_FLOOR`.
- **Deferred:** the AI `selected_playbook` contract change; live light reduction (needs the
  schedule override); live chemical/CO2 playbooks (need HDS3 + reconnected hardware); a real
  alert channel (KDE/desktop/email) beyond the console + ledger.

## Light / PPFD framework (`ppfd.py`)

Turns an Apogee-measured PPFD map of the Growcraft X6 into canopy PPFD + DLI awareness, a
level recommendation, AND (opt-in) closed-loop light control. Advisory by default; when
`PPFD_CONTROL=true` it drives the light to the level that hits the stage DLI target.

- **Map** `ppfd_map.json` (repo root, committed -- it's a stable hardware characterization,
  not per-grow runtime data; `ppfd_map.example.json` is the schema template). Shape:
  `heights_in -> level(1-10) -> {grid: [[..]]}` where `grid` is the full PPFD reading matrix
  at 6-inch spacing across the 48x48 footprint. Stats are computed over ALL cells, so any
  rectangular grid (9x9, 8x8) works. Build it interactively with `python3 ppfd_capture.py`, or
  regenerate the SMOOTHED map from raw readings with `python3 ppfd_build_map.py` (24in populated for
  levels 1-7 as of 2026-06-26 -- smoothed/modeled from the raw Apogee grids; real uniformity ~0.76-0.83).
- **Derived per level x height**: avg / min / max / center / **uniformity (min/avg)** -- the
  point of mapping a grid instead of one point. `PPFD_METRIC` (default `avg`) picks which
  metric drives DLI + recommendations; min/uniformity are always surfaced.
- **Height**: `ppfd_for(level, distance_in)` linearly interpolates between the two nearest
  MEASURED heights (clamped outside the range). Current canopy distance from
  `CANOPY_DISTANCE_IN` (bump it as the plant grows, like the float line). Level 0 -> 0 PPFD.
- **DLI**: `dli(ppfd, hours) = ppfd * hours * 3600 / 1e6`. `recommend_level(target_dli, ...)`
  picks the level whose DLI lands closest to the per-stage target (`DLI_TARGET_<STAGE>`,
  defaults seedling 15 / veg 35 / bloom 45 mol/m2/day).
- **Snapshot**: `build_ppfd_block` attaches `snapshot["ppfd"]` (level, distance, PPFD, min,
  uniformity, DLI, target_dli, recommended_level, level_table, control_armed). Surfaced on the
  HUD as `[LIGHT]` and flows to the AI as context. Inert (block omitted) when no map exists.
- **Closed-loop control (opt-in)**: when `PPFD_CONTROL=true`, `schedule.expected_light_state`
  resolves its plateau intensity from `ppfd.controlled_level(stage)` -- the level whose DLI
  lands closest to the stage target -- instead of the static `LIGHT_INTENSITY`. Sunrise/sunset
  fades + photoperiod are unchanged (they ramp toward that level). The schedule enforcer then
  drives the light to it like any other schedule output. **Falls back to `LIGHT_INTENSITY`** if
  the map is missing/incomplete, so lighting never breaks. The reason string carries a
  `[PPFD ctrl: ...]` tag (visible on the `[SCHED] light` HUD line). Away-mode's `reduce_light`
  stays advisory, so no controller fights another over the light.
- Tests: `ppfd_test.py` (42 cases) + the `expected_light_state` PPFD-override case in
  `schedule_test.py`. Config: `CANOPY_DISTANCE_IN`, `PPFD_METRIC`, `PPFD_CONTROL`,
  `DLI_TARGET_<STAGE>`.
- **Refinement deferred**: fades slightly undershoot the target DLI (plateau assumes full
  photoperiod) -- subtract the fade contribution if precise DLI matters; and a heat-override so
  a tent-temp stressor can pull the PPFD level down.

### Two `sensorType=20` water sensors (split by device)

Both the leak detector and the reservoir-level float report as `sensorType=20`, so
they collide on the cross-device sensor merge unless separated. `build_snapshot`
splits them by device name:
- `LEAK_SENSOR` device (default `Auxiliary Outputs`, accessPort 1) -> `water_leak`
  (boolean; feeds res-burst only).
- Any other device's `sensorType=20` (here `Hydroponics Control`, accessPort 2) ->
  `water_level` (reservoir level; feeds `res_health` trends / manual override).

---

## Reservoir health gate

`res_health_check()` in `ai_advisor.py` evaluates water + EC trends each cycle.
Result is attached to every snapshot as `snapshot["res_health"]` and printed each cycle.

| State | Water | EC | CO2 gate | Dose gate | pH gate |
|-------|-------|----|----------|-----------|---------|
| IDEAL | FALLING | FALLING/STATIC | ADVANCE | NORMAL | ALLOW |
| WATCH | FALLING | RISING | HOLD | HOLD | ALLOW |
| WATCH | STATIC | FALLING | HOLD | NORMAL | ALLOW |
| STALL | STATIC | STATIC | HOLD | HOLD | HOLD |
| STRESS | STATIC | RISING | REDUCE | NONE | HOLD |
| PROBLEM | RISING | any | REDUCE | NONE | HOLD |
| UNKNOWN | — | — | HOLD | HOLD | ALLOW |

- **CO2 ADVANCE**: push toward week's Bugbee target
- **CO2 HOLD**: maintain current CO2, do not increase
- **CO2 REDUCE**: drop 10-15% to ease transpiration load
- **DOSE NORMAL**: dose nutrients to PPM target
- **DOSE HOLD**: do not dose — plant not consuming correctly
- **DOSE NONE**: no nutrients under any circumstances
- **pH HOLD**: resolve water/EC issue before touching pH

Week advancement: AI only suggests advancing `GROW_WEEK` if state has been IDEAL or GOOD
for at least 2 consecutive cycles.

### Water level source (FLOAT / manual / analog)

`build_snapshot` resolves the `water_level` trend from one of three sources, in priority
order, and reports which via `snapshot["water_level_source"]`:

1. **`WATER_LEVEL_FLOAT=true` — boolean magnetic float (current setup).** The reservoir
   sensor (`sensorType=20` on Hydroponics Control, accessPort 2) is a wet/dry float the
   user repositions daily to the expected drawdown line. **dry(0) → FALLING** (water fell
   below today's line), **wet(nonzero) → STATIC**. `_trend` is bypassed for it — a 0↔1
   delta never clears the 1.0 threshold, so it would otherwise read STATIC forever. A
   single float can't detect RISING; use the manual override for problem states. Source
   reported as `FLOAT`; poller prints `[FLOAT] Water level: ...`.
2. **`WATER_LEVEL_TREND` manual override** (`FALLING`/`STATIC`/`RISING`) — fallback when
   FLOAT is off or the float isn't reading. Source `MANUAL`.
3. **Analog/depth sensor via `_trend`** — for a future ultrasonic sensor. Set
   `WATER_LEVEL_FLOAT=false` when one is installed. Source `SENSOR`.

If none resolve, source is `MISSING` and the res-health gate sees UNKNOWN (holds all).
Note the float is the reservoir-LEVEL sensor; the LEAK sensor is separate (`water_leak`).

---

## Trend detection

`_trend()` in `ai_advisor.py` compares each sensor reading against the previous cycle.
Thresholds for RISING/FALLING vs STATIC:

| Sensor | Threshold |
|--------|-----------|
| pH | 0.05 |
| EC mS/cm | 0.05 |
| EC uS/cm | 5.0 |
| TDS ppm | 10.0 |
| Water level | 1.0 |

Trends are attached to snapshot as `snapshot["trends"]` and feed both the DWC diagnostic
rules and the res health gate.

---

## DWC diagnostic rules (source: `dwc res rules.jpeg`, 420Magazine)

AI evaluates WATER LEVEL + EC + PH as combined trends — never individually.

| Water | EC | pH | Diagnosis / Action |
|-------|----|----|--------------------|
| STATIC | STATIC | STATIC | Plant not feeding. Lower EC slightly. |
| STATIC | STATIC | RISING | pH buffers raising pH. Lower EC or change res. |
| STATIC | STATIC | FALLING | Media rinsed at low pH, or excess CO2. Change res, check air. |
| STATIC | RISING | STATIC | Plant leeching nutrition. Raise EC. |
| STATIC | RISING | RISING | Plant leeching (unusual). Alkaline nutes leeching back. |
| STATIC | RISING | FALLING | Acid rain risk. Res change + raise EC. |
| STATIC | FALLING | STATIC | Plant eating not drinking. Lower EC or res change. |
| STATIC | FALLING | RISING | Lower EC slightly. Rising pH is a good sign. |
| STATIC | FALLING | FALLING | Acid rain effect. Lower EC after res change. |
| FALLING | STATIC | STATIC | Perfect. EC and pH correct. |
| FALLING | STATIC | RISING | Normal. No action unless other symptoms. |
| FALLING | STATIC | FALLING | Res change. Lower EC if >1.4 mS/cm, raise if <1.0. |
| FALLING | RISING | STATIC | Drinking more than eating. Lower EC. |
| FALLING | RISING | RISING | Drinking more than eating. Lower EC. |
| FALLING | RISING | FALLING | Drinking more than eating. Lower EC. Res change for acid rain. |
| FALLING | FALLING | STATIC | Hungry plant. Raise EC. Good situation. |
| FALLING | FALLING | RISING | Almost perfect. Raise EC slightly. |
| FALLING | FALLING | FALLING | Res change. Raise EC on new res. |

---

## Nutrient schedule — FloraFlex Full Tilt (source: `Floraflex1.webp`)

Per-gallon low strength RDWC schedule. Ports 1+2 always dosed together at equal speed.

**Veg:**
| Week | Nutrients | Default PPM (500 scale) | Default PPM (700 scale) |
|------|-----------|------------------------|------------------------|
| 1-3 | V1 + V2 equal | 500 | 700 |
| 4+ | V1 + V2 equal | 600 | 840 |

**Bloom:**
| Week | Nutrients | Default PPM (500 scale) | Default PPM (700 scale) |
|------|-----------|------------------------|------------------------|
| 1-5 | B1 + B2 equal | 700 | 980 |
| 6 | B1 + B2 reduced + Full Tilt | 500 | 700 |
| 7 | Full Tilt only | 200 | 280 |
| 8 | FLUSH — no nutrients | 0 | 0 |

### PPM system

- `PPM_SCALE=500` (Hanna/Eutech) or `PPM_SCALE=700` (Truncheon/Bluelab) — set to match your meter
- FloraFlex EC defaults baked into `_FLORAFLEX_EC` in `ai_advisor.py`, multiplied by `PPM_SCALE` at runtime
- Override any week: `PPM_VEG_WK2=550` or `PPM_BLOOM_WK3=1050` in `.env`
- `EC_TOLERANCE=0.1` mS/cm is scaled to PPM automatically (+/-50 PPM on 500 scale)
- AI uses `tds_ppm` from sensor as primary reading against PPM target

### Grow calendar (auto-week)

When `GROW_START_DATE` is set in `.env`, `GROW_WEEK` and `GROW_STAGE` are computed
automatically from elapsed days. Manual `GROW_WEEK` / `GROW_STAGE` become fallback
values for when `GROW_START_DATE` is blank.

```
GROW_START_DATE=2026-05-30   # day 1 of veg
VEG_DAYS=28                  # planned veg duration -- triggers stage flip to bloom
FLOWER_DAYS=63               # planned bloom duration (informational, HUD display)
```

- Day 1 = start date. Days 1-7 = week 1, 8-14 = week 2, etc.
- After `VEG_DAYS` elapsed, stage auto-flips to `bloom` and the week counter resets.
- HUD shows `wk3 veg (day 18/28)` — pulls from `days_into_current_stage()` in `grow_state.py`.

Defaults driven by the calendar: PPM (FloraFlex), CO2 (Bugbee), pH range (stage-aware).

### Extend week

FloraFlex recommends: extend veg at week 4, extend bloom at week 6.
When set, `GROW_WEEK` tracks real calendar weeks while the nutrient target stays pinned.

```
EXTEND_VEG_WEEK=4    # hold veg at wk4 target regardless of GROW_WEEK
EXTEND_BLOOM_WEEK=6  # hold bloom at wk6 target; raise to 7+ when ready to flush
```

Uncomment in `.env` to activate. Clear or raise to allow advancing past it.
Both CO2 and PPM targets respect the extend cap via `_get_ppm_target()` / `_get_co2_target()`.

---

## CO2 profile — Dr. Bruce Bugbee, Utah State University

Baked into `_BUGBEE_CO2` in `ai_advisor.py`. Override any week with `CO2_<STAGE>_WK<N>=<ppm>`.
`CO2_TOLERANCE=100` ppm dead-band. Respects `EXTEND_*_WEEK` cap same as PPM targets.

| Week | Veg | Bloom |
|------|-----|-------|
| 1 | 800 ppm | 1200 ppm |
| 2 | 900 ppm | 1500 ppm |
| 3 | 1000 ppm | 1500 ppm |
| 4 | 1200 ppm | 1500 ppm |
| 5 | 1200 ppm | 1200 ppm |
| 6 | 1200 ppm | 1000 ppm |
| 7 | 1200 ppm | 800 ppm |
| 8 | 1200 ppm | 400 ppm (flush — ambient) |

Key: 1200 ppm = ~95% of yield benefit. Peak 1500 ppm during early-mid bloom maximizes
flower development. Taper late bloom to reduce stress and allow natural ripening.
CO2 only advances toward target when `res_health.co2_gate == ADVANCE`.

---

## pH targets — stage-driven

`_PH_DEFAULTS` in `ai_advisor.py` provides per-stage / per-week pH ranges. Active range
is rendered into the AI system prompt every cycle from `_get_ph_range()`.

| Stage | Week | pH range |
|-------|------|----------|
| Veg   | all  | 5.5 - 6.0 (acidic for N uptake) |
| Bloom | 1-7  | 5.8 - 6.2 (higher for P/K availability) |
| Bloom | 8    | 6.0 - 6.5 (flush — widens for final swing) |

Override hierarchy (per side, MIN and MAX independent):
1. `PH_MIN_<STAGE>_WK<N>` / `PH_MAX_<STAGE>_WK<N>` per-week override
2. `PH_MIN` / `PH_MAX` legacy global override (commented out in `.env` by default)
3. `_PH_DEFAULTS[stage][week]` built-in default

---

## Calibration / self-learning

The system observes what each action actually does and builds a dose-response table
per strain in `profiles/<strain>.json`. Injected into every AI prompt so the model
computes instead of guesses: *"need pH +0.3; speed-2 gives +0.28 observed → use speed 2."*

Flow:
1. Action executes → `track_actions()` queues pending outcome with before-snapshot
2. `OUTCOME_WAIT_CYCLES` later → `record_outcomes()` diffs before/after sensors
3. Delta averaged into `calibration[action_key]["averages"]`
4. Requires `MIN_CAL_OBSERVATIONS = 2` before context injected into prompts

Strain profile fields in `.env`: `STRAIN_NAME`, `GROW_WEEK`, `GROW_STAGE`, `RUN_ID`.
Bump `RUN_ID` (run_1 → run_2) at start of each new grow — previous run becomes the
historical baseline for the AI on the next run with the same strain.

---

## Adaptive polling

| Mode | Interval | Trigger |
|------|----------|---------|
| ACTIVE | `POLL_INTERVAL_ACTIVE` (60s) | Actions just fired OR outcomes still pending |
| STABLE | `POLL_INTERVAL_STABLE` (900s) | No actions, no pending outcomes |

AI's `next_check_seconds` can shorten the interval but never extend beyond mode ceiling.

---

## Full config reference

### `.env`
```
# Credentials
AC_INFINITY_EMAIL=
AC_INFINITY_PASSWORD=
AC_INFINITY_TOKEN=              # auto-written on login, do not edit manually

# Polling
POLL_INTERVAL=30                # fallback when AI disabled
POLL_INTERVAL_STABLE=900        # 15 min — stable res
POLL_INTERVAL_ACTIVE=60         # 1 min — during adjustments

# AI
AI_ENABLED=true
ADVISORY_MODE=true              # false = live control (executes actions)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:3b-instruct

# Safety gate
DOSE_LOCKOUT_MINUTES=15
PH_LOCKOUT_MINUTES=20
MAX_DOSER_SPEED=2               # global speed cap for doser ports (per-port overrides win)
MAX_DOSE_ML_CYCLE=50
OUTCOME_WAIT_CYCLES=2           # cycles (× POLL_INTERVAL_ACTIVE) before reading outcome
VERIFY_WRITES=true              # read-after-write verify; failed doser/pH stop -> retry then freeze dosing
AUTONOMOUS_DOSING=false         # master gate: actuate AI chemical doses (off = validate + log only, even in LIVE)
DOSER_WATCHDOG_DEBOUNCE=2       # consecutive stopped-orphan reads before the persistent dosing freeze

# Reservoir
RESERVOIR_VOLUME_GAL=60         # active filled volume; anchors mL->ppm/pH dose math. Default 60, <=0 falls back to 60. Surfaced in AI snapshot as reservoir_volume_gal.

# Safety kill switches
DOSING_DISABLED=false           # true = freeze doser/pH ports only (climate untouched)
RES_BURST_ENABLED=false         # arm reservoir-burst shutdown (water/chemical only; never cuts lights/vent)
RES_BURST_DEBOUNCE=2            # consecutive WET leak reads before tripping (1 = first read)
LEAK_SENSOR=Auxiliary Outputs   # device whose sensorType=20 is the boolean leak detector (-> water_leak)
EVAC_PUMP=                      # <device>:<port> evac pump outlet (ON while leak wet, OFF when dry); blank = none

# Water level -- FLOAT (boolean magnetic float, repositioned daily) takes priority
WATER_LEVEL_FLOAT=true          # true: dry(0)->FALLING, wet(1)->STATIC. false when analog/ultrasonic installed
WATER_LEVEL_TREND=FALLING       # manual fallback: FALLING / STATIC / RISING / blank

# Nutrient targets
PPM_SCALE=500                   # 500=Hanna/Eutech  700=Truncheon/Bluelab
EC_TOLERANCE=0.1                # mS/cm dead-band, auto-scaled to PPM
# PPM_VEG_WK<N>=              # per-week override, e.g. PPM_VEG_WK2=550
# PPM_BLOOM_WK<N>=            # e.g. PPM_BLOOM_WK3=1050

# CO2
CO2_TOLERANCE=100               # ppm dead-band
# CO2_VEG_WK<N>=              # per-week override
# CO2_BLOOM_WK<N>=

# Extend week (FloraFlex: extend veg at wk4, bloom at wk6)
# EXTEND_VEG_WEEK=4
# EXTEND_BLOOM_WEEK=6

# Environment targets (air side)
PH_MIN=5.8
PH_MAX=6.2
TDS_MIN=800
TDS_MAX=1600
WATER_TEMP_MIN=65
WATER_TEMP_MAX=72
AIR_TEMP_MIN=70
AIR_TEMP_MAX=85
HUMIDITY_MIN=50
HUMIDITY_MAX=70
VPD_MIN=0.8
VPD_MAX=1.5

# Strain profile
STRAIN_NAME=                    # e.g. "White Widow"
RUN_ID=run_1

# Grow calendar (auto-week)
GROW_START_DATE=2026-05-30      # leave blank to disable auto-mode
VEG_DAYS=28
FLOWER_DAYS=63

# Fallback when GROW_START_DATE is blank
GROW_WEEK=1
GROW_STAGE=veg                  # veg or bloom
```

### `labels.env`
```
PORT_<DEVICE_SLUG>_<N>=Label        # display label for port N
DOSER_PORTS_<DEVICE_SLUG>=1,2,3,4  # ports displayed as mL/min
PH_PORTS_<DEVICE_SLUG>=3,4         # ports with longer pH lockout
MAX_SPEED_<DEVICE_SLUG>_<N>=5      # per-port speed cap (0-10)

# Air sensor label overrides (device-specific)
AIR_LABEL_<DEVICE_SLUG>=Outside     # rename built-in sensor label in display + AI snapshot
AIR2_LABEL_<DEVICE_SLUG>=Tent       # rename external probe label

# Suppress irrelevant air sensors from HUD and AI snapshot
# Does NOT affect water temp or hydro sensors — only temp/humidity/VPD
HIDE_AIR_<DEVICE_SLUG>=true

# Device display order (lower number = printed first)
DISPLAY_ORDER_<DEVICE_SLUG>=1
```

Device name slugs: uppercase, non-alphanumeric → underscore.
`"Hydroponics Control"` → `HYDROPONICS_CONTROL`, `"4 x 4"` → `4_X_4`, `"Auxiliary Outputs"` → `AUXILIARY_OUTPUTS`.

`HIDE_AIR` is checked in both `print_device()` (poller.py) and `build_snapshot()` (ai_advisor.py).
The air sensor loop in `build_snapshot()` is gated by this flag; CO2 and light are always included.
`DISPLAY_ORDER` is read in `poller.py` main loop via `os.getenv()` and used to sort the device list before display.

---

## Running

```bash
# Start Ollama (if not running as a service)
ollama serve

# Run the poller
python3 poller.py

# One-shot raw API dump (debug)
python3 -c "
from dotenv import load_dotenv; from pathlib import Path
load_dotenv(Path('.env')); load_dotenv(Path('labels.env'))
import json, os
from ac_infinity_client import get_or_refresh_token, fetch_all_devices
token = get_or_refresh_token(os.getenv('AC_INFINITY_EMAIL'), os.getenv('AC_INFINITY_PASSWORD'), '.env')
print(json.dumps(fetch_all_devices(token), indent=2))
"
```

---

## Known issues / gotchas

- HDS3 probes return `-327.68` (INT16_MIN/100) when not submerged — normal, not a bug
- All user-visible strings must use ASCII (F not degF, uS/cm not µS/cm, -- not em-dash).
  `poller.py` sets `sys.stdout` to UTF-8 TextIOWrapper but print statements in other
  modules still need ASCII-safe strings to avoid codec errors.
- Token expires occasionally — poller catches `ACInfinityAuthError` from the client
  (raised on HTTP 401, code 999999, or `appid` mentions in error body) and re-auths.
- GPU power limit (`-pl`) NOT supported on Max-Q — clock-locking only
- First poll cycle shows UNKNOWN trends (no previous data) — gates conservatively HOLD.
  Normal from cycle 2 onward.
- `HIDE_AIR` suppresses temp/humidity/VPD only. CO2, light, water temp, pH, TDS, EC are
  unaffected — always included in both HUD and AI snapshot regardless of flag.
- AI failure backoff: if Ollama is down or returns junk, the poller backs off
  exponentially (30s → 60 → 120 → ... → 1800s cap) instead of hammering at `POLL_INTERVAL`.
- Lockout scope: dose lockouts only apply to ports listed in `DOSER_PORTS_<SLUG>`.
  Fans, lights, and outlets can be re-issued any time.

---

## Phase 2 — local API discovery (planned)

Goal: eliminate cloud dependency, sub-second polling, unlock unreleased sensor slots.

Evidence the hardware is ahead of the software:
- `sensorCount: 8` returned but not all slots exposed in app
- AC Infinity announced a sensor port splitter (not yet shipped) — firmware likely ready
- Undefined sensorType integers already visible in raw API responses

Plan: use Alfa AWUS1900 (RTL8814AU, on hand) in monitor mode to capture traffic between
controllers and `acinfinityserver.com`, scan controller LAN IPs for local HTTP/WebSocket
endpoints, cross-reference with BLE capture at `~/aci-btmon.txt`.
