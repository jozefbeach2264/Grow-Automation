# Grow Automation Upgrade Priority Tree

Status: draft master roadmap

## Purpose

This file organizes the upgrade work into layers so implementation stays focused.

Rule of thumb:

```text
Protect hardware/plants first.
Then prove supervised live control.
Then add away-mode automation.
Then build deeper logging and strain/pheno learning.
```

## Dependency Tree

```text
Layer 0: Current Safe Baseline
  |
  v
Layer 1: Safety Primitives
  |-- manual kill switch / automation disable
  |-- reservoir gate enforcement
  |-- action/playbook schema validation
  |-- device/port preflight checks
  |-- read-after-write verification
  |-- timed dosing with forced stop
  |-- persistent lockouts
  |-- minimal action records
  |-- active-dose crash recovery
  |
  v
Layer 2: Safe Supervised Live Testing
  |-- minimal action ledger
  |-- non-chemical outputs first
  |-- supervised diluted chemical tests
  |-- expected model verification
  |-- high-alert reservoir polling after risky events
  |
  v
Layer 3: Away-Mode Triage
  |-- deterministic stressor list
  |-- code-owned playbook registry
  |-- AI ranks stressors and selects allowed playbooks
  |-- alerts and escalation
  |
  v
Layer 4: Operational Logging And Review
  |-- cycle snapshots
  |-- sensors and port states
  |-- AI decisions
  |-- action lifecycle records
  |-- verification results
  |-- recent action summary
  |
  v
Layer 5: Learning And Optimization
  |-- strain/pheno identity
  |-- normalized dose-response data
  |-- source-tagged defaults/overrides/learned targets
  |-- predictive target recommendations
  |
  v
Layer 6: v1.1+ Interfaces And Analytics
  |-- manual event entry UI
  |-- exports
  |-- dashboard/TUI
  |-- long-term retention/downsampling
```

## Layer 0: Current Safe Baseline

Goal: keep current IO work from becoming dangerous while safety layers are built.

Basic form:

- Keep `ADVISORY_MODE=true`.
- Do not enable live chemical dosing.
- Use the program to observe, simulate, and test IO manually.
- Keep planning files as source of design truth.

Exit criteria:

- None. This is the holding pattern until Layer 1 is complete enough.

Hard rule:

```text
Do not enable live chemical dosing from AI raw actions.
```

## Layer 1: Safety Primitives

Goal: make individual hardware actions bounded, valid, recoverable, and verifiable.

### 1. Manual Kill Switch / Automation Disable

Basic form:

- Add a single config/state flag that disables all live automation.
- Add a stronger flag that disables chemical dosing only.
- Add an emergency helper that commands all doser ports to speed `0`.

Example state:

```json
{
  "automation_disabled": false,
  "dosing_disabled": false,
  "dosing_disabled_reason": null
}
```

Exit criteria:

- Any safety failure can put the system into alert-only mode.
- User can manually clear it after inspection.

### 2. Reservoir Gate Enforcement

Basic form:

- Enforce `dose_gate`, `ph_gate`, and `co2_gate` in code.
- The AI can recommend, but deterministic gates decide.

Rules:

- Nutrient dosing allowed only when `dose_gate == NORMAL`.
- pH dosing allowed only when `ph_gate == ALLOW`.
- CO2 increase allowed only when `co2_gate == ADVANCE`.
- CO2 reduction/off allowed under `HOLD` or `REDUCE`.

Exit criteria:

- A bad AI action is blocked even if the prompt fails.

### 3. Action / Playbook Schema Validation

Basic form:

- Validate all AI outputs before preflight or execution.
- Reject wrong types, unknown commands, bad values, wrong port type.

Rules:

- `set_outlet.value` must be a real boolean.
- `set_speed.value` must be an integer `0..10`.
- Unknown device/port/action is rejected.
- Chemical actions should move toward playbooks, not raw `set_speed`.

Exit criteria:

- `"false"` can never become `True`.
- Bad action shape is logged and ignored.

### 4. Device / Port Preflight Checks

Basic form:

- Before writing, verify target device and port are visible and usable.

Checks:

- Device exists in current snapshot.
- Device is online.
- Port exists.
- Port is online.
- Port type matches the action.
- Required sensors are present.
- For dosing, pump starts at speed `0`.

Exit criteria:

- No command is sent to a missing, offline, or wrong-type port.

Reference:

- `EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md`

### 5. Read-After-Write Verification

Basic form:

- After every hardware write, poll readback until expected state appears or timeout.

Examples:

- Outlet off -> readback `powered == false`.
- Speed stop -> readback `speed_actual == 0`.

Exit criteria:

- API success alone is not considered execution success.
- Doser stop verification is mandatory.

Reference:

- `READBACK_VERIFICATION_PLAN.md`

### 6. Timed Dosing With Forced Stop

Basic form:

- Replace raw open-ended doser speed with timed dosing.
- Use millisecond timing based on manufacturer flow rate.
- Use monotonic clock for duration.
- Account for ramp-up and ramp-down delivered volume.
- Always send stop in `finally`.

Key formula:

```text
flow_ml_ms = speed * 21 / 60000
```

Exit criteria:

- Doser action always has `ramp_up_ms`, `hold_ms`, `ramp_down_ms`.
- Pump stop is verified before action is marked successful.

Reference:

- `TIMED_DOSING_PLAN.md`

### 7. Persistent Lockouts

Basic form:

- Persist dose and pH lockout timestamps.
- Reload lockouts on startup.
- Safety failures can set `dosing_disabled=true`.

Exit criteria:

- Restart does not clear dose/pH lockouts.
- Interrupted or failed stop blocks further dosing.

### 8. Watchdog Heartbeat / Crash Recovery

Basic form:

- Persist heartbeat and active dose state.
- During active dose, heartbeat frequently.
- On restart, recover before AI/poll loop:
  - load active dose state
  - stop any active/nonzero doser
  - verify stop
  - estimate possible dose
  - enter high-alert polling

Exit criteria:

- Laptop/process crash during dosing creates a logged recovery event.
- Possible dose range is estimated.
- Reservoir is polled more frequently after recovery.

Reference:

- `WATCHDOG_HEARTBEAT_PLAN.md`

## Layer 2: Safe Supervised Live Testing

Goal: test live control safely while user is present.

Basic form:

1. Keep a minimal action ledger before any live test.
2. Enable live control only for low-risk outputs first.
3. Test outlet and fan/light actions with readback verification.
4. Run chemical tests only with diluted solution.
5. Use code-owned dose sizes.
6. Record strength factor and full-strength-equivalent dose.
7. Compare later snapshot against expected model.

Minimal action ledger:

- What was requested.
- What validation/gates allowed or blocked.
- What command was sent.
- What readback showed.
- Whether the outcome matched the expected model.

This can start as append-only JSONL or a small single-table SQLite ledger. The full
logging system can wait until the control path is proven.

Exit criteria:

- Non-chemical live actions are verified.
- Chemical timed dosing is proven with diluted solution.
- Failed verification disables further chemical dosing.

Hard rule:

```text
No unattended chemical dosing until Layer 1 and Layer 2 are working.
```

## Layer 3: Away-Mode Triage

Goal: allow the system to stabilize issues while user is away, using bounded playbooks.

Basic form:

- Deterministic code builds stressor list.
- Code generates allowed playbooks.
- AI ranks stressors and selects one allowed playbook.
- Code validates and executes playbook.
- Alerts/logs every action.

Important note:

- Reservoir temperature is reference/context only right now.
- No chiller action is planned.

Exit criteria:

- AI cannot invent raw hardware commands.
- Away-mode actions are selected from code-owned playbooks.

Reference:

- `AWAY_MODE_AI_TRIAGE_PLAN.md`

## Layer 4: Operational Logging And Review

Goal: make live-test history easier to query after the safe operating path works.

Basic form:

- Keep the minimal action ledger as the required safety record.
- Promote to SQLite when JSON/profile records become painful to query.
- Implement v1 only:
  - cycle summary
  - sensor readings
  - port states
  - AI decision summary
  - requested actions
  - validation/preflight result
  - command result
  - verification result
  - basic outcome status
  - recent action query

Exit criteria:

- The system can answer:
  - What did the AI request?
  - What was allowed?
  - What was blocked?
  - What command was sent?
  - What did readback show?
  - What result followed?

Reference:

- `EVENT_LOGGING_PLAN.md`

## Layer 5: Learning And Optimization

Goal: move from generic defaults to strain/pheno-specific recommendations.

Basic form:

- Track strain, pheno, run, reservoir volume, nutrient line, water source.
- Normalize dose-response by strength factor.
- Keep defaults source-tagged:
  - trusted default
  - user override
  - learned profile
  - current run calibration
- Learn:
  - pH response per full-strength-equivalent mL
  - nutrient response per mL
  - EC/TDS drift by stage
  - water use by stage
  - stress thresholds by pheno

Exit criteria:

- Learned recommendations include confidence and basis.
- Learning layer cannot bypass safety gates.

Reference:

- `STRAIN_PHENO_LEARNING_PLAN.md`

## Layer 6: v1.1+ Interfaces And Analytics

Goal: make the system easier to review and operate.

Basic form:

- Manual event entry helper.
- Export scripts.
- Dashboard or terminal UI.
- Log retention/downsampling.
- Analytics queries for strain/pheno learning.

Exit criteria:

- User can quickly log manual interventions.
- Historical data is easy to inspect.

## Immediate Implementation Priority

If moving from planning to code, start here:

```text
1. Manual kill switch / dosing disable state
2. Active solution volume config/snapshot
3. Schema validation for current raw actions
4. Reservoir gate enforcement
5. Device/port preflight checks
6. Read-after-write verification and execution records
7. Persistent lockouts and minimal action ledger
8. Timed dosing with forced stop and active-dose recovery
9. Supervised non-chemical live tests
10. HDS3 live/plausible check, then diluted chemical microdose tests
11. Playbook registry replacing raw AI pump actions
12. Full logging, away-mode hardening, and strain/pheno learning
```

## Open Items Not Yet Fully Planned

- Dedicated alerting channel plan.
- Sensor calibration/plausibility plan.
- Secrets and file-permission cleanup plan.
- Master config/versioning plan.

## Current Planning Files

```text
AWAY_MODE_AI_TRIAGE_PLAN.md
TIMED_DOSING_PLAN.md
READBACK_VERIFICATION_PLAN.md
EXECUTION_RECORDS_AND_PREFLIGHT_PLAN.md
WATCHDOG_HEARTBEAT_PLAN.md
EVENT_LOGGING_PLAN.md
STRAIN_PHENO_LEARNING_PLAN.md
```

---

## Claude Code Review Notes (2026-05-30)

### Overall assessment

The direction and layer ordering are correct. The plans are well-reasoned and Codex
understood the system. The main risk is complexity budget -- these plans represent
months of work if implemented fully, and several pieces of infrastructure are being
designed before the hardware prerequisites even exist.

### What is confirmed correct

**Timed dosing math** -- The ramp compensation numbers are right. At speed 1 the pH
pump delivers ~0.175 mL during ramp-up and another ~0.175 mL during ramp-down after
the stop command fires, so the minimum deliverable pulse is ~0.35 mL. The proposed
`PH_MICRODOSE_ML=0.5` clears that floor: hold_ms works out to ~428ms. The `finally`
block around the stop command is non-negotiable and is the single most important
safety change in the whole pile.

**Strength factor / full-strength-equivalent normalization** -- If calibration runs
use diluted solution and observations are not normalized, the system will learn that
full-strength solution is weaker than it really is and later doses will overshoot.
`STRENGTH_FACTOR` config + normalizing before averaging is the right approach.

**Playbook registry replacing raw AI actions** -- Correct long-term architecture. The
AI choosing from a code-owned list of bounded playbooks instead of inventing raw
hardware commands is the right safety model. The current raw-action path is fine for
advisory mode but must be replaced before away-mode unattended dosing.

**Watchdog with active dose state written before pump start** -- Correct. A crash or
laptop lid-close during a pH dose is a real scenario. The state must be on disk before
the pump moves.

**Layer ordering** -- Confirmed: safety primitives before logging, logging before
supervised live, supervised live before away-mode, away-mode before learning.

### Where to adjust

**SQLite event logging is too early in the queue.** It is listed as Layer 2 --
required before supervised live control. But the profile_manager JSON is adequate for
the data volume at this stage. Building 10+ table SQLite infrastructure before having
a single verified live action is backwards. Defer SQLite until JSON becomes genuinely
painful to query. Swap it with supervised non-chemical live control in the ordering.

**The AI contract change (raw actions -> selected_playbook) is a full refactor.** It
requires rebuilding the prompt, the playbook registry, execute_actions(), and
filter_actions() simultaneously. A safer path: add strict schema validation and
preflight checks on top of the existing raw-action path first. That is a smaller,
independently testable change. Migrate to playbooks once the validation layer is
proven in live use.

**Reservoir volume is missing and needed.** Timed dosing gives mL delivered into an
unknown reservoir size. A 0.5 mL pH dose hits differently in 5 gallons vs 20 gallons.
`RESERVOIR_VOLUME_GAL` needs to be in `.env` and in the snapshot before dosing
calibration data means anything.

### Hardware prerequisites the plans assume but do not state

These must exist before the corresponding layer is useful:

- **HDS3 probe submerged and reading** -- required before any chemical dosing work
  matters. pH gate hard-blocks dosing without a live pH reading. All of Layer 1
  chemical work is untestable until this is connected.
- **Ollama model fitting in VRAM** -- deepseek-r1:7b does not fit in the T2000 4GB.
  Pull of deepseek-r1:1.5b was interrupted. Nothing AI-related makes sense until
  inference takes under 30s per cycle.
- **Reservoir volume known and set** -- needed before dosing mL estimates are
  meaningful.

### Recommended implementation order given current hardware state

```text
1.  Pull deepseek-r1:1.5b, get AI cycle time under 30s         [blocking everything]
2.  Connect HDS3 probe, confirm pH/EC/TDS reading              [hardware prerequisite]
3.  Add RESERVOIR_VOLUME_GAL to .env and snapshot              [prerequisite for dosing math]
4.  Timed dosing with forced stop + stop verification           [highest-value safety work]
5.  Persistent lockouts survive restart                         [small, mostly done already]
6.  Schema validation + preflight on raw actions                [before flipping ADVISORY_MODE]
7.  Supervised live test on fans/outlets (non-chemical)         [Layer 3 entry]
8.  Supervised chemical test with diluted solution              [Layer 3 chemical]
9.  Playbook registry, migrate away from raw actions            [after live is proven]
10. SQLite event log, watchdog heartbeat                        [when queryability is needed]
11. Away-mode triage                                            [Layer 4, after 1-10 stable]
12. Strain/pheno learning                                       [Layer 5, long term]
```

### Hard rules inherited from this review

- Do not enable live chemical dosing before items 1-6 above are complete.
- Do not average diluted and full-strength calibration observations without normalizing
  to full-strength-equivalent first.
- Stop verification failure must block all further doser actions -- no exceptions.
