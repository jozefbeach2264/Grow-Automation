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

> **Status:** Advisory + supervised live control on lights / fans / CO2.
> Chemical dosing (pH and nutrients) is hard-blocked at the safety layer
> until the HDS3 hydro probe is wired and several Layer-1 safety items
> are complete. See [`UPGRADE_PRIORITY_TREE.md`](UPGRADE_PRIORITY_TREE.md).

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
  (`model_benchmark.py`), 100 % schema-valid on 32/32 hardware-command prompts at
  ~1.9 s median latency, fits in 4 GB VRAM.
- **Backup:** `phi4-mini` — also 100 % valid, slower (~3 s).
- **Reasoning models** (DeepSeek-R1 family) were tested and rejected — small
  math-tuned bases hallucinate schema-incompatible actions; ~35 % pass rate.

The AI's job is reasoning about sensor state. It does **not** directly control
hardware. Every action proposal flows through `validate_actions()` →
`filter_actions()` → `execute_actions()`. Scheduled outputs and CO2 pulses are
fired deterministically; the AI only sees the resulting state.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/jozefbeach2264/Grow-Automation.git
cd Grow-Automation

# 2. Install Python dependencies
pip install requests python-dotenv

# 3. Install Ollama (https://ollama.com/download) and pull the model
ollama pull qwen2.5:3b-instruct

# 4. Configure your environment
cp .env.example .env
# Edit .env with your AC Infinity credentials, grow calendar, role mappings.
# Keep ADVISORY_MODE=true until you have confirmed the cycle output looks sane.

# 5. Run
python poller.py
```

The poller authenticates, fetches all devices, then enters a poll loop. In
advisory mode the AI logs its reasoning but does not touch hardware. To enable
live control of non-chemical outputs, set `ADVISORY_MODE=false` in `.env` and
restart.

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

Cycles in advisory mode log everything the AI proposed and what the safety
chain did with it. The forensic trail is the first thing to consult when
something looks off.

---

## Hands-on utilities

| Script | Purpose |
|--------|---------|
| `poller.py` | Main poller and AI-driven controller. |
| `demo_cycle.py` | Deterministic demo — turn all aux outlets on, then sequentially ramp every variable-speed port 0 → 10 → hold → 0. |
| `ai_cycle_test.py` | AI-driven cycle test. Walks every port through 0 → 10 → 0 using the configured model. Useful for validating AI + hardware end-to-end. |
| `model_benchmark.py` | Head-to-head LLM benchmark. Edit `MODELS_UNDER_TEST` and run to compare schema validity and latency. |
| `ramp_probe.py` | Measure the linear ramp rate of a single port (used to establish the 1 unit/sec model). |

---

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — project context for AI agents and contributors.
- [`UPGRADE_PRIORITY_TREE.md`](UPGRADE_PRIORITY_TREE.md) — the full roadmap from
  current state to plant-ready, ordered by safety layer.
- Per-layer design docs:
  - [`AWAY_MODE_AI_TRIAGE_PLAN.md`](AWAY_MODE_AI_TRIAGE_PLAN.md)
  - [`TIMED_DOSING_PLAN.md`](TIMED_DOSING_PLAN.md)
  - [`READBACK_VERIFICATION_PLAN.md`](READBACK_VERIFICATION_PLAN.md)
  - [`EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md`](EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md)
  - [`WATCHDOG_HEARTBEAT_PLAN.md`](WATCHDOG_HEARTBEAT_PLAN.md)
  - [`EVENT_LOGGING_PLAN.md`](EVENT_LOGGING_PLAN.md)
  - [`STRAIN_PHENO_LEARNING_PLAN.md`](STRAIN_PHENO_LEARNING_PLAN.md)

---

## Reference

The AC Infinity write protocol used by this project was reverse-engineered by
capturing the official mobile app's traffic over a WiFi hotspot. The findings
are documented in `CLAUDE.md` under "AC Infinity API" and "Control writes". The
key field that took a while to identify: `onSelfSpead` is the new target speed,
**not** `onSpead` (which is the readback from the controller).

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
