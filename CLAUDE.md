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
| Controller 2 | CTR89Q | type 20 | "RDWC Control" | Reservoir — dosing, pH, hydro sensors |
| Power Strip | ADA4 | type 21 | "Auxiliary Outputs" | Hard on/off switching for high-draw devices |

**Sensors connected:**
- HDS3 hydro probe on RDWC Control — pH, EC (uS/cm and mS/cm), TDS ppm, water temp F
- Built-in temp/humidity/VPD on both CTR89Qs
- CO2 sensor, light sensor (on respective controllers)
- Water level sensor — pending; ultrasonic planned. Manual override active in the meantime.
- External air probes (additional temp/humidity zones)

**Air sensor display and AI visibility** (current config in `labels.env`):
- "4 x 4" external probe = Tent (source of truth for tent air). Built-in is suppressed
  via `HIDE_AIR_BUILTIN_4_X_4=true` because it heat-soaks during tests.
  Labels: `AIR_LABEL_4_X_4=Tent_Intake`, `AIR2_LABEL_4_X_4=Tent`.
- "RDWC Control" built-in = Outside reference (stable, far from tent). Its external probe
  is suppressed via `HIDE_AIR_EXT_RDWC_CONTROL=true`. Label: `AIR_LABEL_RDWC_CONTROL=Outside`.
- "Auxiliary Outputs" air sensors fully suppressed via `HIDE_AIR_AUXILIARY_OUTPUTS=true`.
- Per-side flags: `HIDE_AIR_<SLUG>` hides everything, `HIDE_AIR_BUILTIN_<SLUG>` hides only the
  built-in sensor hub, `HIDE_AIR_EXT_<SLUG>` hides only the external probe.
  Water temp, CO2, light, and hydro sensors are unaffected — only temp/humidity/VPD filter.

**Device display order** (controlled by `DISPLAY_ORDER_<SLUG>` in `labels.env`):
1. "4 x 4" — tent climate/lighting
2. "RDWC Control" — reservoir
3. "Auxiliary Outputs" — outlets

**Laptop running everything:** ThinkPad P1 Gen 3, NVIDIA Quadro T2000 Max-Q (4GB VRAM).
GPU clocks locked to 2100MHz via `nvidia-perf.service` for consistent Ollama performance.
Power limit change is NOT supported on Max-Q — do not add `-pl` to the service file.
2026-05-26: `nvidia-perf.service` was disabled so the laptop can return to normal driver-managed clocks for everyday use.
When local Ollama throughput matters again, re-enable with `sudo systemctl enable --now nvidia-perf.service` after confirming `nvidia-smi` works post-boot.

---

## Doser pumps (RDWC Control ports 1–4)

AC Infinity peristaltic pump spec: **21 mL/min per speed level** (linear, speed 1–10).
All four ports are designated dosers via `DOSER_PORTS_RDWC_CONTROL=1,2,3,4` in `labels.env`.

| Port | Label | Purpose |
|------|-------|---------|
| 1 | Floraflex V1 | Nutrient part A (V1 veg / B1 bloom) |
| 2 | Floraflex V2 | Nutrient part B (V2 veg / B2 bloom) |
| 3 | PH UP | pH adjustment up |
| 4 | PH DOWN | pH adjustment down |

Ports 1+2 are ALWAYS dosed together at equal speed — never one without the other.
Ports 3+4 are pH ports via `PH_PORTS_RDWC_CONTROL=3,4` — safety gate applies longer lockout.

---

## File map

| File | Purpose |
|------|---------|
| `poller.py` | Main loop — polls devices, drives AI, adaptive sleep, displays readings |
| `ac_infinity_client.py` | AC Infinity cloud API client — auth, fetch, parse, control |
| `ai_advisor.py` | Ollama reasoning layer (qwen2.5:3b-instruct default), safety gate, res health, trend detection |
| `profile_manager.py` | Strain profiles, outcome tracking, calibration context builder |
| `grow_state.py` | Auto-compute current week + stage from `GROW_START_DATE` and `VEG_DAYS` |
| `utils.py` | Shared text utils (currently just `name_slug`) |
| `ramp_probe.py` | Standalone CTR89Q port ramp-rate measurement tool |
| `labels.env` | Port labels, doser ports, pH ports, per-port speed caps, HIDE_AIR flags |
| `.env` | Credentials, AI settings, safety thresholds, calendar, strain config |
| `profiles/` | Per-strain JSON files accumulating run history and calibration data |
| `profiles/.pending_outcomes.json` | Persistent queue of actions awaiting outcome readback |
| `nvidia-perf.service` | Systemd service locking GPU clocks at 2100MHz on boot |
| `Floraflex1.webp` | FloraFlex Full Tilt schedule reference image |
| `dwc res rules.jpeg` | DWC water/EC/pH trend diagnostic table reference image |

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
- Raw sensor values are integers scaled by 100 (divide by 100.0 for real value)
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
13 = pH (*100)                    14 = EC uS/cm (*100)
15 = EC mS/cm (*100)              16 = TDS ppm (*100)
18 = water temp F (*100)          20 = water level (raw)
```

---

## AI layer (Ollama)

- Default model: `qwen2.5:3b-instruct` running locally via Ollama (`ollama serve`)
- Model was chosen via head-to-head benchmark (`model_benchmark.py`, 2026-05-30):
  100% schema-valid on 32/32 set_speed prompts at 1.9s median, fits in 4GB VRAM.
  Backup: `phi4-mini` (also 100%, slower). DeepSeek-R1 1.5B was rejected (~35% pass
  rate — math-tuned base, weak at structured output). Override via `OLLAMA_MODEL`
  in `.env`.
- Reasoning models (R1 family) wrap output in `<think>...</think>` tags — stripped
  before JSON parse. Harmless for non-reasoning models like Qwen.
- `warmup()` pre-loads model into VRAM on startup before first real call
- Context: `num_ctx=4096`, `num_predict=768`, `temperature=0.2`, timeout 240s

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

1. **Per-port dose lockout** — after a port fires, blocked for `DOSE_LOCKOUT_MINUTES`
2. **pH lockout** — pH ports blocked for `PH_LOCKOUT_MINUTES` after any pH action
3. **One pH action per cycle** — pH UP and pH DOWN cannot both fire in the same cycle
4. **Per-port speed cap** — `MAX_SPEED_RDWC_CONTROL_<N>` in `labels.env` overrides
   global `MAX_DOSER_SPEED`; effective cap = `min(per_port, global, mL_ceiling)`
5. **mL/min ceiling** — `MAX_DOSE_ML_CYCLE` ÷ 21 = max speed

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
`"RDWC Control"` → `RDWC_CONTROL`, `"4 x 4"` → `4_X_4`, `"Auxiliary Outputs"` → `AUXILIARY_OUTPUTS`.

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
- GPU power limit (`-pl`) NOT supported on Max-Q — `nvidia-perf.service` only locks clocks
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
