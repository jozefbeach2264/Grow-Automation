# Execution Records And Device Preflight Plan

Status: DONE -- validation + preflight (#3/#4) + unified per-action lifecycle records (2026-06-16)

## Update (2026-06-16)

The remaining item below -- a single per-action lifecycle record threading requested ->
validated -> executed -> verified under one id -- is now SHIPPED via `event_log.py`.
`ai_advisor.execute_actions(..., cycle_id=)` emits an `action_request` (with a fresh
action_id), an `action_validation` at the deciding stage (schema / safety_gate / passed)
carrying the precise reject reason (from the opt-in `reasons` collector on
`validate_actions` / `filter_actions`), and an `action_execution` (sent / success /
verified / error) for actions that ran. See `EVENT_LOGGING_PLAN.md` for the ledger.
Still deferred (needs hardware): linking the delayed `action_outcome` back to its
action_id from `profile_manager.record_outcomes`.

## Implementation status (2026-06-02)

DONE:
- Schema validation (#3) -- `ai_advisor.validate_actions()` runs before `filter_actions()`:
  catches bad verbs, wrong value types, out-of-range values, port-type/verb mismatches,
  port name used as device name.
- Device/port preflight (#4) -- folded into `validate_actions` (snapshot lookup confirms
  device exists, port exists on device, port type matches verb).
- Read-after-write verification (`hardware_verified` step) -- see docs/done/READBACK_VERIFICATION_PLAN.md.
- Execution-record seed -- `runtime_state.record_event()` appends to `profiles/events.jsonl`
  (active_dose_*, stop_recovery_*, process_*, high_alert_*, estimated_overdose_window).

REMAINING:
- A single per-action lifecycle record that threads requested -> validated -> preflight ->
  sent -> verified -> outcome through one stable action_id (today the lifecycle is split
  across lockout state, pending-outcomes queue, and the event log). Lands with the Layer 2
  action ledger (EVENT_LOGGING_PLAN v1).

## Goal

Track every requested action through its full lifecycle:

```text
requested -> schema_validated -> preflight_checked -> command_sent -> hardware_verified -> outcome_pending -> outcome_verified
```

Before commands are sent, the target device and port must be verified visible and usable.
After commands are sent, the execution and resulting state must be verified.

## Why This Matters

The system must not treat an AI suggestion as an executed action.

An action may fail at several points:

- Bad schema.
- Unknown device.
- Device not visible in current poll.
- Device offline.
- Port missing.
- Port offline.
- Wrong port type.
- Port already in unsafe state.
- Reservoir gate blocks action.
- Lockout blocks action.
- API write fails.
- API write succeeds but readback does not change.
- Stop command fails.
- Expected model verification fails later.

Each stage needs to be recorded separately so logs, calibration, and AI context reflect reality.

## Execution Lifecycle

### 1. Requested

An action is requested by:

```text
ai
deterministic_emergency
manual_user
scheduled_task
```

The original request should be preserved exactly.

### 2. Schema validated

Validate that the action shape is structurally correct.

Checks:

- `device` is a non-empty string.
- `port` is an integer.
- `action` is one of the allowed commands/playbooks.
- `value` has the correct type.
- `set_outlet.value` is a real boolean.
- `set_speed.value` is an integer in `0..10`.
- Chemical dosing uses a timed-dose/playbook action, not raw generic speed.

### 3. Preflight checked

Before sending any command, use the current snapshot/readback to verify target usability.

Checks:

- Device exists in current snapshot.
- Device has a stable `dev_id`.
- Device is online.
- Port exists.
- Port is online.
- Port type matches action.
- Doser action targets a configured doser port.
- pH action targets a configured pH port.
- Outlet action targets an outlet port.
- Speed action targets a variable-speed port.
- For timed dosing, target port starts at speed `0`.
- For paired nutrient dosing, both ports are visible, online, and start at speed `0`.
- Required sensors are present and plausible.
- Reservoir gates allow the action.
- Lockouts allow the action.

Preflight failure should stop the action before any API write.

### 4. Command sent

The API write is attempted.

Record:

- Endpoint/function used.
- Value sent.
- Timestamp.
- Error, if any.
- API response where safe to store.

### 5. Hardware verified

Readback confirms the target hardware state.

Examples:

- Outlet reports `powered=True`.
- Outlet reports `powered=False`.
- Speed port reports expected target or acceptable tolerance.
- Doser stop reports `speed_actual=0`.

For timed dosing, stop verification is mandatory.

### 6. Outcome pending

If the action is expected to affect the grow state, create an outcome expectation.

Examples:

- pH Down should make pH fall within expected bounds.
- Nutrient dose should make TDS/EC rise within expected bounds.
- CO2 off should make CO2 fall.
- Exhaust increase should reduce temp/CO2/VPD pressure depending on stressor.

### 7. Outcome verified

After the verification window, compare the later snapshot against the expected model.

Outcomes:

```text
matched_expected_model
improved_but_not_enough
no_measurable_change
moved_wrong_direction
overshot_safe_range
missing_sensor_for_verification
```

## Execution Record Shape

Initial record:

```json
{
  "action_id": "uuid-or-db-id",
  "cycle_id": "cycle-id",
  "source": "ai",
  "requested": {},
  "schema_valid": null,
  "schema_error": null,
  "preflight_ok": null,
  "preflight_error": null,
  "command_sent": false,
  "write_ok": null,
  "hardware_verified": null,
  "outcome_status": "not_applicable",
  "success": null
}
```

After execution:

```json
{
  "action_id": "abc123",
  "source": "ai",
  "requested": {
    "device": "Auxiliary Outputs",
    "port": 2,
    "action": "set_outlet",
    "value": false
  },
  "effective": {
    "device": "Auxiliary Outputs",
    "dev_id": "...",
    "port": 2,
    "action": "set_outlet",
    "value": false
  },
  "schema_valid": true,
  "preflight_ok": true,
  "command_sent": true,
  "write_ok": true,
  "hardware_verified": true,
  "outcome_status": "pending",
  "success": null
}
```

For blocked actions:

```json
{
  "action_id": "abc124",
  "requested": {
    "device": "RDWC Control",
    "port": 3,
    "action": "timed_ph_up_microdose"
  },
  "schema_valid": true,
  "preflight_ok": false,
  "preflight_error": "ph_gate_hold",
  "command_sent": false,
  "success": false
}
```

## Preflight Snapshot Rules

Preflight should use the freshest available data.

Recommended flow:

```text
1. Poll devices.
2. Build snapshot.
3. Validate AI/deterministic actions against snapshot.
4. For high-risk actions, optionally re-read target device/port immediately before write.
5. Send command only if preflight still passes.
```

High-risk actions:

- Any chemical dosing.
- CO2 valve changes.
- Light intensity changes.
- Emergency outlet shutoff.

For low-risk fan/exhaust changes, current cycle snapshot may be enough initially.

## Interaction With `execute_actions()`

Short-term change:

```python
records = execute_actions(...)
```

Return records, not raw actions.

The caller should:

```python
successful_records = [r for r in records if r["success"] or r["outcome_status"] == "pending"]
```

Only successful or pending verified actions should be passed to outcome tracking.

Long-term:

```text
execute_actions() should become execute_playbook() for AI-selected playbooks.
```

## Interaction With Event Logging

Every lifecycle stage should write to the event log:

- action request
- schema validation
- preflight result
- command execution
- hardware verification
- expected model
- delayed outcome

The event log should preserve failed actions too. Failed actions are useful for debugging,
but must not be used as successful calibration data.

## Simulation And Tests

Add tests for:

- Unknown device blocked at preflight.
- Offline device blocked at preflight.
- Missing port blocked at preflight.
- Offline port blocked at preflight.
- Outlet command rejected on speed port.
- Speed command rejected on outlet port.
- pH command rejected when pH sensor missing.
- pH command rejected when `ph_gate=HOLD`.
- Nutrient command rejected when `dose_gate=HOLD` or `NONE`.
- Timed dosing rejected if port starts nonzero.
- Paired nutrient dose rejected if either port is unavailable.
- API write success but readback failure produces `hardware_verified=false`.
- Stop verification failure produces critical failure record.

## Rollout Plan

1. Define execution record structure.
2. Add schema validation function.
3. Add preflight validation function using the current snapshot.
4. Make `execute_actions()` return execution records.
5. Update `poller.py` to use returned records instead of proposed AI actions.
6. Send only verified successful/pending records to outcome tracking.
7. Log all records once event logging exists.
8. Extend records for timed dosing and expected model verification.

## Conflict Checklist For Later Review

Compare this plan against:

- Readback verification plan.
- Event logging plan.
- Timed dosing plan.
- Away-mode AI triage/playbook plan.
- Reservoir gate enforcement.
- Persistent lockouts.

Preflight decides whether a command may be sent. Readback verifies what happened after
the command. Expected model verification evaluates whether the system response matched
the intended effect.
