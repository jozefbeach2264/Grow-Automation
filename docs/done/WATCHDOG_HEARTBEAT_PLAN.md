# Watchdog And Heartbeat Plan

Status: FOUNDATION IMPLEMENTED -- Layer 1 #8 (2026-06-02)

## Implementation status (2026-06-02)

Implemented in `runtime_state.py` + `poller.py` (rollout steps 1-4, 6, 7); 34/34 tests
in `watchdog_test.py`.

DONE:
- Heartbeat file `profiles/.runtime_state.json` (atomic, corrupt-tolerant): phase, pid,
  `boot_id`, wall + monotonic clocks, last poll/api/readback ok. `HEARTBEAT_ENABLED`.
  Clean exit writes phase `shutdown`; `diagnose_restart()` distinguishes clean / crash /
  reboot (boot_id change) / mid-dose.
- Active-dose record (`begin_active_dose` / `active_dose_window_port` / `clear_active_dose`)
  and the interrupted-dose estimator (`estimate_interrupted_dose`) -- structures wired now;
  planned fields populated by #7 timed dosing.
- Startup recovery (`poller.recover_on_startup`): runs before AI/polling, stops any running
  chemical pump, freezes dosing + opens high-alert.
- Nonzero-doser watchdog (`poller.doser_watchdog`): orphan pump -> stop + verify + retry +
  freeze + high-alert. Detect-always / actuate-in-LIVE. `DOSER_WATCHDOG_ENABLED`.
- High-alert reservoir polling window (persisted, auto-expires): `HIGH_ALERT_POLL_INTERVAL`,
  `HIGH_ALERT_DURATION_MINUTES`.
- Event log `profiles/events.jsonl` (`record_event`) with the event types below.

DEFERRED:
- Precise interrupted-dose math (needs #7's planned fields), best-estimate from last
  confirmed-running timestamp.
- API freshness + sensor freshness watchdogs (need HDS3 reading to be meaningful).
- systemd `Restart=always` / `WatchdogSec` integration (rollout step 8).
- High-alert danger comparison against safe ranges (needs HDS3).

## Goal

Add a heartbeat/watchdog system that detects process crashes, laptop interruptions,
API disconnects, stale sensors, and unsafe hardware states.

The system must be able to recover from a failure during dosing, estimate the possible
delivered dose, log the disconnect window, alert at high severity, and temporarily
poll reservoir conditions more often.

## Clock Model

Use two clocks for different jobs.

### Wall clock

Use for:

- Event timestamps.
- Logs.
- Cross-restart lockouts.
- Human-readable start/end times.
- Estimating disconnect windows after restart.

Examples:

```python
time.time()
datetime.now(timezone.utc)
```

### Monotonic clock

Use for:

- Dosing hold duration.
- API timeout duration.
- In-process heartbeat age.
- Retry/backoff timing.

Example:

```python
time.monotonic()
time.monotonic_ns()
```

Do not use wall clock to time a dose duration. Wall clock can jump if NTP adjusts time.
Timed dosing should use millisecond fields and a monotonic clock.

## Heartbeat State

Persist a heartbeat file or database row frequently.

Suggested short-term file:

```text
grow_runtime_state.json
```

Suggested heartbeat fields:

```json
{
  "wall_time_utc": "2026-05-30T05:45:00Z",
  "monotonic": 123456.7,
  "pid": 1234,
  "boot_id": "...",
  "phase": "executing_timed_dose",
  "cycle_id": "...",
  "active_action_id": "...",
  "last_poll_ok": true,
  "last_api_ok": true,
  "last_readback_ok": true
}
```

Possible phases:

```text
starting
polling_devices
building_snapshot
asking_ai
validating_actions
preflight_checking
executing_timed_dose
verifying_readback
waiting_for_outcome
sleeping
error_backoff
shutdown
```

## Active Dose State

Before starting any pump, persist an active dose record.

Example:

```json
{
  "active_dose": {
    "action_id": "abc123",
    "device": "RDWC Control",
    "dev_id": "...",
    "port": 4,
    "solution": "ph_down",
    "speed": 1,
    "target_ml": 0.5,
    "strength_factor": 0.25,
    "started_wall_time_utc": "2026-05-30T05:45:10Z",
    "started_monotonic": 123466.7,
    "planned_stop_wall_time_utc": "2026-05-30T05:45:12Z",
    "planned_hold_ms": 1400,
    "ramp_down_ms": 3000,
    "status": "pump_running"
  }
}
```

After the stop is verified, clear or mark the active dose:

```json
{
  "status": "stopped_verified",
  "stopped_wall_time_utc": "...",
  "stop_verified": true
}
```

## Crash Or Disconnect During Dosing

On startup, before doing anything else:

1. Load runtime state.
2. If `active_dose.status == pump_running`, assume an interrupted dose.
3. Poll current device/port state immediately.
4. Send stop command to the doser port.
5. Verify the doser reaches speed `0`.
6. Log the disconnect interval.
7. Estimate possible delivered dose.
8. Mark dosing disabled until user review or safe recovery policy clears it.
9. Raise high/critical alert.
10. Temporarily poll reservoir conditions more often.

## Disconnect Interval Logging

Record:

```text
last_heartbeat_wall_time_utc
restart_wall_time_utc
planned_stop_wall_time_utc
first_successful_readback_wall_time_utc
stop_command_wall_time_utc
stop_verified_wall_time_utc
disconnect_duration_sec
overrun_duration_sec
```

Where:

```text
disconnect_duration_sec = restart_wall_time - last_heartbeat_wall_time
overrun_duration_sec = stop_verified_wall_time - planned_stop_wall_time
```

If the laptop crashed, actual pump state during the disconnect may be unknown. The system
should estimate worst-case delivery from planned start until verified stop, not only until
restart.

## Estimated Dose After Interruption

For interrupted dosing, calculate:

```text
minimum_estimated_ml
maximum_estimated_ml
best_estimated_ml
```

Suggested conservative model:

```text
minimum_estimated_ml = planned target delivered before crash if start was verified
maximum_estimated_ml = flow_at_commanded_speed * elapsed_until_verified_stop
                    + ramp_up/ramp_down allowance
best_estimated_ml = based on last known heartbeat phase and readback evidence
```

If readback never verified the pump started:

```text
minimum_estimated_ml = 0
maximum_estimated_ml = worst-case from command-sent time to verified stop
```

If readback verified the pump was running:

```text
minimum_estimated_ml = dose delivered until last confirmed running timestamp
maximum_estimated_ml = dose delivered until verified stop timestamp
```

Store strength-adjusted values:

```text
estimated_actual_ml_min
estimated_actual_ml_max
estimated_full_strength_equivalent_min
estimated_full_strength_equivalent_max
```

This estimate must be logged and shown in the alert.

## Temporary High-Alert Reservoir Polling

After interrupted dosing or stop verification failure, enter high-alert monitoring.

Suggested behavior:

```text
HIGH_ALERT_POLL_INTERVAL=30
HIGH_ALERT_DURATION_MINUTES=30
```

During high-alert mode:

- Poll reservoir sensors more often.
- Keep chemical dosing disabled.
- Track pH, TDS/EC, water level, water temp.
- Compare readings against expected/safe ranges.
- Alert again if readings move in a dangerous direction.
- Exit high-alert only after duration expires and readings are stable, or after user acknowledgement.

High-alert state should be persisted so it survives another restart.

## Critical Device Disconnects

Define critical devices/ports:

```text
RDWC Control ports 1-4 = chemical dosing
HDS3 pH/TDS/EC sensors = chemical decision sensors
Auxiliary Outputs CO2 valve = environment safety
```

Critical disconnect rules:

- If RDWC Control disappears during active dosing, treat as critical.
- If pH sensor disappears after pH dosing, treat outcome verification as failed/unknown.
- If TDS/EC sensors disappear after nutrient dosing, treat outcome verification as failed/unknown.
- If API readback is unavailable during active dosing, issue stop command when API recovers and estimate worst-case dose.

## Hardware Safety Watchdog

Doser ports should normally be speed `0`.

If any doser port is nonzero outside an active timed-dose window:

1. Send stop command.
2. Verify stop.
3. Log event.
4. Raise alert.
5. Disable further dosing until reviewed.

## API Freshness Watchdog

Track:

```text
last_successful_api_poll
last_successful_api_write
last_successful_readback
consecutive_api_failures
```

Rules:

- No API readback available: block live chemical actions.
- Repeated auth failures: refresh token, then alert/backoff if unresolved.
- API stale beyond threshold during active dose: treat as critical unknown until readback returns.

## Sensor Freshness Watchdog

Each critical sensor should have freshness/validity status.

Rules:

- pH missing/stale: block pH dosing.
- TDS/EC missing/stale: block nutrient dosing.
- Water level missing: reservoir gate is `UNKNOWN` or uses manual override.
- CO2 missing/stale: block CO2 automation.

## Process Watchdog

Long term, run under `systemd` with restart behavior.

On restart:

1. Load runtime state.
2. Run dosing recovery check before AI or normal polling.
3. Stop any nonzero doser ports.
4. Verify stop.
5. Log interruption/recovery.
6. Enter high-alert mode if needed.

Potential systemd settings later:

```text
Restart=always
RestartSec=5
WatchdogSec=30
```

## Interaction With Event Logging

Watchdog events should be logged as first-class events:

```text
heartbeat_missed
process_restarted
active_dose_recovered
stop_recovery_sent
stop_recovery_verified
stop_recovery_failed
estimated_overdose_window
high_alert_started
high_alert_ended
critical_device_missing
api_stale
sensor_stale
```

## Interaction With Persistent Lockouts

After any interrupted dose, stop verification failure, or unknown dosing window:

```text
dosing_disabled = true
dosing_disabled_reason = interrupted_dose_unknown_volume
```

User should manually clear this after reviewing reservoir conditions.

## Rollout Plan

1. Add runtime state file with heartbeat writes.
2. Add active dose state before timed dosing exists.
3. Add startup recovery check for active dose state.
4. Add nonzero doser watchdog check.
5. Add interrupted-dose estimate calculation.
6. Add high-alert reservoir polling mode.
7. Add event logging hooks.
8. Add systemd watchdog/restart integration later.

## Open Decisions

- How often should heartbeat write during normal polling?
- How often should heartbeat write during active dosing?
- What high-alert poll interval and duration should be used first?
- Should high-alert mode require manual acknowledgement to exit?
- What should be the first alert channel?
- How conservative should overdose estimates be when readback is missing?
