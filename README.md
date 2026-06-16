# Grow-Automation

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

> **Status:** Live supervised control on lights / fans / CO2, and **supervised
> chemical dosing is validated on hardware** — the HDS3 hydro probe is wired and
> the Layer-1 dosing safety is in (timed forced-stop doses, read-after-write
> verify, crash-recovery watchdog, chemical interlock). Dosing runs through the
> bounded `dosing` path and the supervised bucket-calibration harness; **fully
> autonomous dosing stays gated behind `AUTONOMOUS_DOSING` (default off)** while
> the calibration matures. See [`UPGRADE_PRIORITY_TREE.md`](UPGRADE_PRIORITY_TREE.md).

---

## Hardware

| Device | Model | Role |
|--------|-------|------|
| Controller 1 ("4 x 4") | AC Infinity CTR89Q | Climate, light, airflow |
| Controller 2 ("Hydroponics Control") | AC Infinity CTR89Q | Reservoir — dosers + pH UP/DOWN |
| Outlet strip ("Auxiliary Outputs") | AC Infinity ADA4 | Mains outlets + UIS for CO2/light/water-level sensors |
| Hydro probe | AC Infinity HDS3 | pH, EC/TDS, water temp (UIS) |
| Light | Growcraft X6 (0-10V dim) | Photoperiod |
| Nutrients | FloraFlex Full Tilt (V1+V2 veg / B1+B2 bloom) | Two-part nutrient line |
| Compute | ThinkPad P1 Gen 3, NVIDIA T2000 (4 GB) | Poller + local LLM |

---

## AI layer

- **Default model:** `qwen2.5:3b-instruct` — chosen via head-to-head benchmark
  (2026-05-30), 100 % schema-valid on 32/32 hardware-command prompts at
  ~1.9 s median latency, fits in 4 GB VRAM.
- **Backup:** `phi4-mini` — also 100 % valid, slower (~3 s).
- **Reasoning models** (DeepSeek-R1 family) were tested and rejected — small
  math-tuned bases hallucinate schema-incompatible actions; ~35 % pass rate.

The AI's job is reasoning about sensor state. It does **not** directly control
hardware. Every action proposal flows through `validate_actions()` →
`filter_actions()` → `execute_actions()`. Scheduled outputs and CO2 pulses are
fired deterministically; the AI only sees the resulting state.

---

## Quick start (Linux / macOS)

```bash
# 1. Clone
git clone https://github.com/jozefbeach2264/Grow-Automation.git
cd Grow-Automation

# 2. Install Python dependencies
pip install requests python-dotenv

# 3. (Optional) Install Ollama and pull a model -- skip if running AI-less
#    https://ollama.com/download
ollama pull qwen2.5:3b-instruct

# 4. Configure your environment
cp .env.example .env
# Edit .env with your AC Infinity credentials, grow calendar, role mappings.
# Keep ADVISORY_MODE=true until you have confirmed the cycle output looks sane.

# 5. Run
python poller.py
```

## Quick start (Windows)

The system is pure Python with no Linux-specific dependencies, so it runs
unchanged on Windows.

```powershell
# 1. Install Python 3.11+ from python.org (tick "Add to PATH")
# 2. (Optional) Install Ollama for Windows from https://ollama.com/download/windows
# 3. In Windows Terminal or PowerShell:

git clone https://github.com/jozefbeach2264/Grow-Automation.git
cd Grow-Automation

python -m venv venv
.\venv\Scripts\Activate.ps1            # PowerShell
# (use venv\Scripts\activate.bat in cmd, source venv/Scripts/activate in Git Bash)

pip install requests python-dotenv

ollama pull qwen2.5:3b-instruct        # only if running with AI

copy .env.example .env
notepad .env                            # fill in credentials, calendar, role mappings

python poller.py
```

For long-running deployment on Windows, three options ranked best to simplest:

1. **NSSM** ([nssm.cc](https://nssm.cc/)) — wraps `python poller.py` as a real
   Windows service with auto-restart. Closest equivalent to systemd. Recommended
   once you have plants in the loop.
2. **Task Scheduler** — set "Trigger: at log on" with action
   `python.exe poller.py` and a working directory. Set-and-forget for personal use.
3. **Leave Windows Terminal open** — fine for development.

Command syntax that differs from Linux/macOS:

| Linux/macOS | Windows |
|---|---|
| `python3 poller.py` | `python poller.py` (or `py poller.py`) |
| `source venv/bin/activate` | `venv\Scripts\Activate.ps1` (PowerShell) |
| `cp .env.example .env` | `copy .env.example .env` |
| `kill 1234` | `taskkill /F /PID 1234` |

---

## Run modes

The poller picks behavior from two `.env` flags:

| `AI_ENABLED` | `ADVISORY_MODE` | What happens |
|---|---|---|
| `true` | `true` | AI runs, logs proposals + deterministic enforcement plan, **no hardware writes** |
| `true` | `false` | **Default live mode.** AI proposes, deterministic chain validates and executes, schedule + CO2 enforcement fire on top |
| `false` | `false` | **Deterministic-only.** No LLM ever called. Schedule (lights/fans), CO2 pulse modulator, CO2 emergency dump, reservoir gates all still active. Sensor monitoring + trends tracked. Manual control via the AC Infinity app for anything chemistry-related |
| `false` | `true` | Polling display only. No AI, no enforcement. Pure sensor read-out |

**The deterministic-only mode (`AI_ENABLED=false ADVISORY_MODE=false`) is the
right pick if:**

- You don't want to run a local LLM
- Your hardware can't run Ollama (e.g. Raspberry Pi)
- You want a smart-thermostat + safety supervisor and prefer manual control of
  nutrient and pH dosing

In this mode the system becomes: schedule-driven lights and fans, hysteresis-
band CO2 pulse around your per-week target, deterministic CO2 emergency dump
on threshold breach, and live sensor + trend display. No LLM dependency.

---

## Configuration

All operational config is in `.env` and reloaded on every cycle (so edits take
effect without restart, except for a few startup-time settings). Highlights:

- **Grow calendar** — `GROW_START_DATE` + `VEG_DAYS` auto-computes the current
  grow week and stage. pH / PPM / CO2 targets are stage-driven by default
  (FloraFlex for nutrients, Bugbee for CO2).
- **Schedule-driven outputs** — `LIGHT_HOURS_ON` / `LIGHT_HOURS_OFF` /
  `LIGHT_CYCLE_START` / `LIGHT_INTENSITY`, plus role mappings `ROLE_LIGHT`,
  `ROLE_OSC_FANS`, `ROLE_EXHAUST`. Hardware roles are env-driven, not hardcoded.
- **CO2 control** — `CO2_VALVE`, `CO2_PULSE_BAND_PPM` (deadband around target),
  `CO2_EMERGENCY_PPM` (hard cap), `CO2_DUMP_CLEAR_PPM` (hysteresis clear).
- **Safety bounds** — `MAX_DOSER_SPEED`, `DOSE_LOCKOUT_MINUTES`,
  `PH_LOCKOUT_MINUTES`, `MAX_DOSE_ML_CYCLE`.

Port labels and device-specific config live in `labels.env`.

---

## Safety model

The system is built around the principle that the AI proposes and deterministic
code disposes. Layers in order of precedence:

1. **CO2 emergency dump** — if `co2_ppm` exceeds `CO2_EMERGENCY_PPM`, code
   forces the valve OFF and ramps exhaust to max with hysteresis. Highest
   priority — runs before the AI cycle.
2. **Action schema validation** — every AI action is checked against a strict
   schema (verb whitelist, value type, port type, device existence) before any
   safety gate runs. Malformed actions are rejected with a specific reason
   logged, not silently dropped.
3. **Reservoir gate enforcement** — `dose_gate`, `ph_gate`, `co2_gate` from
   `res_health_check()` are enforced deterministically in `filter_actions()`.
   The AI cannot override them by ignoring them in its response.
4. **CO2 pulse modulator** — hysteresis-band on/off control around the
   per-week target. Sits underneath the gate; gate=HOLD/REDUCE forces OFF.
5. **Persistent lockouts** — dose/pH cooldown clocks are written to
   `profiles/.lockouts.json` after every action, so a process restart does not
   reset the clock and let the AI re-dose immediately.
6. **Schedule enforcement fallback** — after the AI cycle, any schedule deltas
   the AI didn't issue corrections for are fired deterministically.
7. **Hard blocks** — pH dosing requires a live pH sensor reading; nutrient
   dosing requires `dose_gate == NORMAL`; chemical dosing requires the HDS3
   to be reading.
8. **Bounded doses + read-after-write** — chemicals only move via timed, bounded
   doses (`dosing.py`) that always force-stop and *verify*; a stop that won't
   confirm freezes dosing. A crash-recovery watchdog kills any orphaned pump and
   freezes on an unclean restart.

Cycles in advisory mode log everything the AI proposed and what the safety
chain did with it. The forensic trail is the first thing to consult when
something looks off.

---

## Chemical dosing & calibration

Chemicals never use open-ended `set_speed`. Every dose is a **bounded, timed dose**
(`dosing.py`): verify the port is at zero, record a crash-safe active-dose marker,
run the pump on a monotonic clock, and **always force-stop + verify** in a
`finally` — an unconfirmed stop freezes dosing. Nutrient V1+V2 fire together
(`timed_dose_pair`), each pump stopping on its own clock so a per-port flow or a
deliberate V1/V2 volume split still delivers the right amount.

The system **learns each dose's response and feeds forward** instead of guessing:

- **Nutrients (linear):** one calculated **85% fast shot at high speed**, then a
  **low-speed creep** onto the EC/TDS target — converging to the pump's resolution,
  not a coarse band. The pair constant `K = ΔTDS·gal/mL` self-updates per dose.
- **pH (non-linear):** the EC-normalized buffer constant drifts, so pH uses
  **cautious fixed creeps**, logging a buffer sample per pH bin to earn the right
  to calculate it later.
- **CO2:** `co2_pulse_test.py` calibrates valve-open time to a ppm shot — with a
  long equalize and one settled shot at a time, because the tent has a ~4-min
  mixing/sensor lag and an exponential decay back toward ambient.

Calibration runs are supervised against a real reservoir and log to `profiles/`;
the learned constants inject into every AI prompt so the model computes doses.
There is no cloud history API, so dense 1-min trend curves come from the app's CSV
export (`ac_infinity_history.py`), aligned to dose events by `dose_align.py`.

---

## Hands-on utilities

| Script | Purpose |
|--------|---------|
| `poller.py` | Main poller and AI-driven controller. |
| `ai_cycle_test.py` | AI-driven cycle test. Walks every port through 0 → 10 → 0 using the configured model. Useful for validating AI + hardware end-to-end. |
| `dosing.py` | Timed, bounded doses with forced stop + verify, ramp math, and the V1+V2 pair path. |
| `bucket_dose_test.py` | Manual single-pump dose-response characterization (mL → ΔpH / ΔTDS). |
| `bucket_ai_dose_test.py` | Supervised closed-loop bucket calibration — 85% fast shot + creep, online K update. |
| `co2_pulse_test.py` | Supervised CO2 shot calibration (valve-open time → ppm; long equalize for the mixing lag). |
| `ac_infinity_history.py` | Loads the app's CSV "Device Data" exports — 1-min trend history. |
| `dose_align.py` | Aligns logged dose events with the 1-min CSV curves to recover the true dose-response. |

---

## Tests

Deterministic self-tests — no hardware, no network (writes/readbacks are mocked,
state files redirected to temp dirs). Run any individually, e.g. `python3 schedule_test.py`:

| Suite | Covers |
|-------|--------|
| `core_logic_test.py` | `res_health_check` gate table + `_trend` rate-normalized classification |
| `schedule_test.py` | High-temp exhaust guardrail, CO2 dump/pulse, light/fan schedule deltas |
| `diagnostics_test.py` | Deterministic stressor list + playbook registry |
| `away_mode_test.py` | Away-mode triage executor: selection, live/advisory/dry dispatch, gating |
| `event_log_test.py` | Cycle + action-lifecycle ledger, `recent_actions()` |
| `safety_gate_test.py` | `validate_actions` / `filter_actions` gates, dose verb, reason collectors |
| `dosing_test.py` | Timed dosing ramp math, forced stop, freeze-on-unverified-stop |
| `watchdog_test.py` | Heartbeat, crash recovery, orphan-pump watchdog |

```bash
# Run them all
for t in core_logic schedule diagnostics away_mode event_log safety_gate dosing watchdog; do
  python3 ${t}_test.py || break
done
```

---

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — project context for AI agents and contributors.
- [`UPGRADE_PRIORITY_TREE.md`](UPGRADE_PRIORITY_TREE.md) — the full roadmap from
  current state to plant-ready, ordered by safety layer.
- Per-layer design docs:
  - [`AWAY_MODE_AI_TRIAGE_PLAN.md`](AWAY_MODE_AI_TRIAGE_PLAN.md)
  - [`TIMED_DOSING_PLAN.md`](docs/done/TIMED_DOSING_PLAN.md)
  - [`READBACK_VERIFICATION_PLAN.md`](docs/done/READBACK_VERIFICATION_PLAN.md)
  - [`EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md`](EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md)
  - [`WATCHDOG_HEARTBEAT_PLAN.md`](docs/done/WATCHDOG_HEARTBEAT_PLAN.md)
  - [`EVENT_LOGGING_PLAN.md`](EVENT_LOGGING_PLAN.md)
  - [`STRAIN_PHENO_LEARNING_PLAN.md`](STRAIN_PHENO_LEARNING_PLAN.md)

---

## Reference

The AC Infinity write protocol used by this project was reverse-engineered by
capturing the official mobile app's traffic over a WiFi hotspot. The findings
are documented in `CLAUDE.md` under "AC Infinity API" and "Control writes". The
key field that took a while to identify: `onSelfSpead` is the new target speed,
**not** `onSpead` (which is the readback from the controller).

The cloud API exposes only *current* sensor values — there is **no history
endpoint** (confirmed against the reverse-engineered API). Trend history comes
from the app's "Device Data" CSV export, ingested by `ac_infinity_history.py`.

Reference materials in repo:

- `dwc res rules.jpeg` — DWC reservoir diagnostic rules (water level × EC × pH
  trend matrix). Lives in the AI prompt.
- `Floraflex1.webp` — FloraFlex Full Tilt schedule used for default PPM targets.

---

## Disclaimer

This is hobbyist software controlling real hardware that pumps real chemicals
into a real reservoir holding real plants. It is provided as-is with no
warranty. Read the safety design before flipping `ADVISORY_MODE=false`. Do not
enable chemical dosing without first completing the Layer-1 items in
`UPGRADE_PRIORITY_TREE.md`. The author is not responsible for crop loss,
equipment damage, root rot, or runaway CO2 enrichment.

---

## License

MIT — see `LICENSE` if present, otherwise treat as "do what you want, no
warranty".
