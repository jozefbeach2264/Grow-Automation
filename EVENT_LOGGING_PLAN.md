# Event Logging System Plan

Status: SEEDED -- append-only event log live; structured v1 pending (2026-06-02)

## Implementation status (2026-06-02)

SEEDED: `runtime_state.record_event()` writes an append-only JSONL log at
`profiles/events.jsonl`, one JSON object per line (wall_time_utc, wall_ts, monotonic,
pid, type, + event fields). Currently carries watchdog/recovery events
(process_started, process_restarted, active_dose_*, stop_recovery_*,
estimated_overdose_window, high_alert_*, clean_shutdown). This is the v1 ledger seed.

REMAINING (v1 -> full): per-cycle poll snapshot + sensor readings + port states + AI
decision summary + per-action lifecycle records flowing into the same log (or a small
SQLite table), then retention/downsampling. Hook the existing `profile_manager.log_cycle`
and `ai_advisor.execute_actions` paths into `record_event` so every cycle and action is
captured, not just safety events.

## Goal

Create a logging system that records all grow data, recent actions, and action results
in a structured way.

The log should become the source of truth for:

- Debugging what happened.
- Verifying live-control safety.
- Building strain/pheno learning data.
- Reviewing AI decisions.
- Auditing hardware actions.
- Understanding whether an action helped, hurt, or did nothing.

## Version Scope

### v1: Basic operational logging

Implement only the basics needed for safe live-control development:

- Poll cycle summary.
- Sensor readings.
- Port states.
- AI decision summary.
- Requested actions.
- Validation/preflight result.
- Hardware command result.
- Verification result.
- Basic outcome status.
- Recent action query.

Avoid dashboard/export/analysis complexity in v1.

### v1.1: Advanced logging and learning support

Defer advanced features until the control path is safe and stable:

- Full raw AI prompt/response retention policy.
- Detailed stressor history.
- Manual intervention UI.
- Export tooling.
- Long-term downsampling/retention.
- Rich outcome analytics.
- Strain/pheno learning queries.
- Dashboard or TUI.

## Current Gap

`profile_manager.py` currently logs some cycle data and calibration outcomes, but it is
not a full event log.

Missing or incomplete areas:

- Raw requested AI actions.
- Safety-filtered actions.
- Rejected actions with reasons.
- Actual executed actions.
- Hardware command results.
- Read-after-write verification.
- Forced stop verification.
- Action outcomes linked to exact before/after snapshots.
- Alerts.
- Manual user interventions.
- Device/port offline events.
- Sensor missing or implausible events.

## Storage Direction

Use SQLite for the durable event log.

JSON profiles can remain as summaries/cache, but SQLite should become the primary
history store because it is queryable and safer for long-running event data.

Suggested file:

```text
grow_events.sqlite3
```

Optional exports:

```text
exports/cycles.csv
exports/actions.csv
exports/outcomes.csv
```

## Logging Principles

- Append-only by default.
- Never store credentials or tokens.
- Every action should have a unique `action_id`.
- Every poll cycle should have a unique `cycle_id`.
- Every outcome should link back to the executed action.
- Store both raw values and normalized/derived values where useful.
- Log rejected actions, not just executed actions.
- Log uncertainty and missing data explicitly.
- Keep enough context to reconstruct why the system acted.

## Core Tables

### `cycles`

One row per poll cycle.

Fields:

```text
cycle_id
timestamp
run_id
strain_name
pheno_id
grow_week
grow_stage
day_in_stage
water_level_source
res_health_state
res_water_trend
res_ec_trend
res_ph_trend
co2_gate
dose_gate
ph_gate
mode
```

### `sensor_readings`

One row per sensor reading per cycle.

Fields:

```text
cycle_id
device_name
sensor_name
value
unit
source
valid
invalid_reason
```

### `port_states`

One row per device port per cycle.

Fields:

```text
cycle_id
device_name
device_type
port
port_name
online
mode
speed_actual
speed_target
powered
is_doser
is_ph_port
is_outlet
```

### `stressors`

One row per deterministic stressor detected in a cycle.

Fields:

```text
cycle_id
name
severity
evidence
likely_effect
allowed_playbooks_json
```

### `ai_decisions`

One row per AI response.

Fields:

```text
cycle_id
model
prompt_version
raw_response
parsed_ok
parse_error
assessment
concerns_json
selected_playbook
ranked_stressors_json
next_check_seconds
notify_user
message
latency_sec
```

For the current raw-action system, also store:

```text
proposed_actions_json
```

### `action_requests`

One row per action the AI or deterministic controller requested.

Fields:

```text
action_id
cycle_id
source
device_name
port
action_type
requested_value
requested_playbook
reason
created_at
```

`source` examples:

```text
ai
deterministic_emergency
manual_user
scheduled
```

### `action_validation`

One row per validation result.

Fields:

```text
action_id
cycle_id
valid
rejected_reason
safety_gate_reason
effective_value
effective_playbook
```

Examples of rejection reasons:

```text
unknown_device
offline_port
wrong_port_type
invalid_value_type
dose_gate_hold
ph_gate_hold
co2_gate_hold
lockout_active
missing_sensor
below_minimum_deliverable_dose
```

### `action_execution`

One row per action actually attempted against hardware.

Fields:

```text
action_id
cycle_id
started_at
finished_at
executed
success
device_name
port
command_type
value_sent
error
readback_before_json
readback_after_json
```

For timed dosing:

```text
target_ml
estimated_actual_ml
strength_factor
full_strength_equivalent_ml
speed
ramp_up_ms
hold_ms
ramp_down_ms
forced_stop_sent
stop_verified
```

### `outcomes`

One row per measured result after the action wait window.

Fields:

```text
action_id
cycle_id_before
cycle_id_after
measured_after_sec
expected_direction
success
failure_reason
before_sensors_json
after_sensors_json
deltas_json
notes
```

Examples:

```text
pH moved expected direction
TDS rose too much
water temp did not improve
CO2 did not fall after valve off
no measurable change
sensor missing during outcome window
```

### `alerts`

One row per alert or alert-worthy event.

Fields:

```text
alert_id
cycle_id
action_id
timestamp
severity
title
message
channel
sent
error
acknowledged
```

### `manual_events`

One row per user intervention.

Fields:

```text
event_id
timestamp
cycle_id
event_type
description
device_name
port
value
notes
```

Examples:

```text
changed_reservoir
added_water
added_nutrients_manual
calibrated_probe
changed_solution_strength
moved_sensor
changed_port_mapping
pruned_plants
```

## Recent Action Summary

The runtime should expose a recent-action summary for the AI prompt and user display.

Example:

```json
"recent_actions": [
  {
    "age_minutes": 12,
    "playbook": "timed_ph_down_microdose",
    "device": "RDWC Control",
    "port": 4,
    "estimated_actual_ml": 0.5,
    "result": "pending"
  },
  {
    "age_minutes": 45,
    "playbook": "disable_co2",
    "result": "success",
    "evidence": "co2_ppm fell from 1500 to 1180"
  }
]
```

The AI should see recent actions so it does not repeat corrections blindly.

## Logger API

Create a small logger module, for example:

```text
event_log.py
```

Initial functions:

```python
start_cycle(snapshot) -> cycle_id
log_sensor_readings(cycle_id, snapshot)
log_port_states(cycle_id, snapshot)
log_stressors(cycle_id, diagnostics)
log_ai_decision(cycle_id, result, raw_response=None, latency_sec=None)
log_action_request(cycle_id, action, source) -> action_id
log_action_validation(action_id, valid, reason=None, effective_action=None)
log_action_execution(action_id, result)
log_action_outcome(action_id, before_snapshot, after_snapshot, deltas, success)
log_alert(...)
log_manual_event(...)
recent_actions(limit=10, window_hours=24)
```

## Integration Points

### Poll cycle start

After `build_snapshot(devices)`:

```text
cycle_id = event_log.start_cycle(snapshot)
event_log.log_sensor_readings(cycle_id, snapshot)
event_log.log_port_states(cycle_id, snapshot)
event_log.log_stressors(cycle_id, snapshot.diagnostics)
```

### AI response

After `ask_ai(snapshot)`:

```text
event_log.log_ai_decision(cycle_id, result, latency_sec=...)
```

### Validation

When filtering or validating actions:

```text
event_log.log_action_request(...)
event_log.log_action_validation(...)
```

### Execution

When hardware is called:

```text
event_log.log_action_execution(...)
```

### Outcome

When delayed outcome measurement is settled:

```text
event_log.log_action_outcome(...)
```

## Conflict Checklist For Later Review

Compare this plan against:

- Away-mode AI triage/playbook plan.
- Timed dosing plan.
- Strain/pheno learning plan.
- Reservoir gate enforcement.
- Action/playbook schema validation.
- Actual executed action return values.
- Persistent lockouts.
- Read-after-write verification.
- Alerting system.

The logging system should observe all of these but should not bypass their safety logic.

## Rollout Plan

### v1 rollout

1. Add SQLite schema and `event_log.py`.
2. Log cycle snapshots, sensor readings, and port states only.
3. Add AI decision summary logging.
4. Add action request and validation/preflight logging.
5. Add hardware command and verification logging.
6. Add basic pending/success/failure outcome status.
7. Add `recent_actions()` query for the AI snapshot and HUD/debug output.

### v1.1 rollout

1. Add manual event CLI helper.
2. Add export scripts for review and analysis.
3. Add richer delayed outcome analytics.
4. Add retention/downsampling policy.
5. Use logged data to feed strain/pheno learning summaries.
6. Add dashboard or TUI if useful.

## Open Decisions

- Should SQLite live in project root or `data/`?
- How long should raw AI responses be retained?
- Should sensor readings be logged every cycle forever, or downsample old stable data?
- What should count as a manual event that must be logged?
- Should manual events be entered through CLI flags, a small TUI, or a web dashboard later?
- Should logs be backed up automatically?
