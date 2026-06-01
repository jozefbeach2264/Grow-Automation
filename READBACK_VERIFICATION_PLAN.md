# Read-After-Write And Stop Verification Plan

Status: draft for review

## Goal

After every hardware write, verify that the AC Infinity device actually reports the
expected state.

This is required before live unattended control, and it is especially important for
timed dosing. A command being accepted by the cloud API is not enough. The system
must verify that the physical port reached the expected state.

## Why This Matters

The API can report success while hardware behavior is delayed, rejected, or only
partially applied.

For chemical dosing, the most important verification is:

```text
after a timed dose, the doser port must reach speed 0
```

If stop verification fails, the system should assume the pump may still be running
and immediately enter alert/lockout behavior.

## Verification Levels

### Level 1: API write accepted

The HTTP request succeeded and the API returned code `200`.

This only proves the server accepted the request. It does not prove the controller
or port physically changed.

### Level 2: Device readback matches expected state

After a short delay, the system polls device state and confirms the relevant port
matches the expected state.

Examples:

```text
set outlet ON  -> readback powered == True
set outlet OFF -> readback powered == False
set speed 3    -> readback speed_actual approaches 3
set speed 0    -> readback speed_actual == 0
```

### Level 3: Outcome verification

After enough process time passes, sensor readings confirm the intended effect.

Examples:

```text
CO2 valve OFF   -> co2_ppm falling
pH DOWN dose    -> pH falling without overshoot
nutrient dose   -> TDS/EC rising within expected range
```

This plan focuses on Level 1 and Level 2. Outcome verification belongs in the event
logging and timed dosing plans.

## Expected Model Verification

Outcome verification should compare a later snapshot against an expected model or
expected parameter set. This is separate from command readback.

Command readback answers:

```text
Did the hardware report the commanded state?
```

Expected model verification answers:

```text
Did the grow system respond the way this action was supposed to make it respond?
```

Each playbook/action should define an expected model before execution. The model can
be simple at first and become learned/calibrated later.

Example for pH Down:

```json
{
  "action": "timed_ph_down_microdose",
  "before": {"ph": 6.7},
  "expected_after": {
    "ph": {
      "direction": "falling",
      "min_delta": -0.02,
      "max_delta": -0.25,
      "hard_min": 5.5,
      "hard_max": 6.5
    }
  },
  "verify_after_seconds": 600
}
```

Example for CO2 valve off:

```json
{
  "action": "disable_co2",
  "before": {"co2_ppm": 1500},
  "expected_after": {
    "co2_ppm": {
      "direction": "falling",
      "min_delta": -50
    }
  },
  "verify_after_seconds": 300
}
```

Result categories:

```text
matched_expected_model
improved_but_not_enough
no_measurable_change
moved_wrong_direction
overshot_safe_range
missing_sensor_for_verification
```

The expected model should be code-owned. The AI may explain the result, but it should
not redefine the success criteria after the action has already run.

## Core Readback Function

Add a function that fetches the current device list and returns the target port state:

```python
def read_port_state(token, device_name=None, dev_id=None, port=None) -> dict:
    ...
```

Returned normalized state should include:

```text
device_name
dev_id
dev_type
port
online
is_outlet
powered
speed_actual
speed_target
mode
raw_state
```

The function should support lookup by `dev_id` because device names can change.

## Generic Verification Function

Add a reusable verification helper:

```python
def verify_port_state(
    token,
    dev_id,
    port,
    expected,
    timeout_sec,
    poll_sec,
    tolerance=None,
) -> VerificationResult:
    ...
```

`VerificationResult` should include:

```text
ok
reason
expected
observed
attempts
elapsed_sec
```

Suggested expected formats:

```python
{"powered": True}
{"powered": False}
{"speed_actual": 0}
{"speed_actual": 3, "tolerance": 1}
```

## Delay And Retry Rules

AC Infinity writes can be delayed by cloud/API/controller latency and port ramping.

Use retries instead of a single immediate readback:

```text
initial_delay_sec = 2
poll_sec = 2
timeout_sec = depends on command
```

Recommended defaults:

```text
outlet verification timeout: 15 sec
speed increase verification timeout: ramp_seconds(target, current) + 10 sec
speed stop verification timeout: ramp_seconds(0, current) + 10 sec
```

The existing ramp model says CTR89Q ports ramp about 1 speed unit per second, plus
buffer. That should be used for speed verification timeouts.

## Stop Verification For Timed Dosing

Timed dosing must treat stop verification as mandatory.

Required flow:

```text
1. Read port before dose.
2. Confirm speed_actual == 0.
3. Start pump.
4. Verify pump is moving or command was accepted.
5. Hold calculated dose time.
6. Send stop command in finally.
7. Wait expected ramp-down time.
8. Poll until speed_actual == 0.
9. Only mark dose as successful if stop is verified.
```

If stop verification fails:

```text
1. Send stop command again.
2. Re-read.
3. If still not stopped, alert immediately.
4. Mark dosing subsystem unsafe.
5. Block all further doser actions.
6. Keep polling the port until stopped or user intervenes.
```

This should be treated as a critical event.

## Verification Outcomes

Every write should produce one of these states:

```text
accepted_unverified
verified
verification_timeout
verification_failed
write_failed
auth_failed
```

For live chemical dosing, `accepted_unverified` is not good enough. A dose should not
be logged as successfully executed unless the stop state is verified.

## Execution Return Shape

Hardware execution should return a structured record, not just print output.

Example:

```json
{
  "action_id": "optional-event-log-id",
  "device": "RDWC Control",
  "port": 4,
  "command": "set_speed",
  "requested_value": 1,
  "write_ok": true,
  "verified": true,
  "verification": {
    "expected": {"speed_actual": 1},
    "observed": {"speed_actual": 1},
    "elapsed_sec": 4
  }
}
```

For timed dosing:

```json
{
  "device": "RDWC Control",
  "port": 4,
  "playbook": "timed_ph_down_microdose",
  "start_write_ok": true,
  "start_verified": true,
  "stop_write_ok": true,
  "stop_verified": true,
  "estimated_actual_ml": 0.5,
  "success": true
}
```

## Interaction With Event Logging

The event logger should store:

```text
readback_before_json
write_response_json
readback_after_json
verification_status
verification_elapsed_sec
verification_attempts
failure_reason
```

For stop verification failures, also create an alert event.

## Interaction With Action Tracking

Only actions with the right verification level should be passed to outcome tracking.

Examples:

```text
outlet action: track if write accepted and readback verified
fan/light speed action: track if readback reaches target or acceptable tolerance
timed dose: track only if stop_verified == true
```

If an action is rejected, write-failed, or verification-failed, it should still be
logged, but it should not be used as successful calibration data.

## Simulation And Tests

Add tests or simulation probes for:

- Successful outlet ON verification.
- Successful outlet OFF verification.
- Successful speed set verification.
- Successful speed stop verification.
- Delayed readback that succeeds after retries.
- Readback timeout.
- Write accepted but readback never changes.
- Stop command fails first time, retry succeeds.
- Stop command fails repeatedly and triggers critical alert state.
- Auth failure during verification triggers token refresh path.

## Rollout Plan

1. Add `read_port_state()`.
2. Add `verify_port_state()`.
3. Use verification for non-doser outlet/speed actions in advisory test/sim first.
4. Return structured execution records from `execute_actions()`.
5. Log verification results once event logging exists.
6. Add mandatory stop verification to timed dosing.
7. Block live chemical dosing unless stop verification is available.
8. Add alert/lockout state for failed stop verification.

## Conflict Checklist For Later Review

Compare this plan against:

- Timed dosing plan.
- Event logging plan.
- Away-mode AI triage/playbook plan.
- Action/playbook schema validation.
- Actual executed action return values.
- Persistent lockouts.
- Reservoir gate enforcement.

Readback verification should support these systems, not duplicate their policy logic.

## Open Decisions

- What tolerance is acceptable for fan/light speed readback?
- Should speed verification require `speed_actual` or is `speed_target` enough for non-doser ports?
- How many stop retries should happen before critical alert?
- Should failed stop verification try a controller-level mode reset if normal stop fails?
- Should the system keep polling forever after a failed stop until user confirms safe state?
