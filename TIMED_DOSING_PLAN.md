# Timed Dosing And Forced Stop Plan

Status: NOT STARTED -- prerequisites now in place (2026-06-02)

## Implementation status (2026-06-02)

NOT STARTED, but the scaffolding it depends on now exists:
- Stop verification (`verify_port_state`, retry-then-freeze) -- DONE, see
  READBACK_VERIFICATION_PLAN. The "always command 0 in finally, verify it reached 0"
  requirement is satisfiable today.
- Crash-safe active-dose record -- `runtime_state.begin_active_dose()` /
  `mark_active_dose_stopped()` / `clear_active_dose()` are wired; `timed_dose()` only needs
  to write the record before starting the pump and clear it after the verified stop.
- The watchdog reads that record via `active_dose_window_port()`, so a legitimate in-window
  dose is left alone while orphans are killed.
- Ramp model (`ramp_seconds`, 1 speed/sec) is measured and available for run-time math.

BLOCKED ON HARDWARE: real dosing needs the HDS3 probe reading + `RESERVOIR_VOLUME_GAL`
(for mL->ppm/pH math) and calibrated per-pump flow. Logic + sim tests can be written now;
live validation waits on the bucket/probe test.

## Goal

Replace open-ended doser speed commands with bounded dosing actions.

Current risk:

```text
set_port_speed(port, speed)
```

can leave a doser running until another command stops it.

Target behavior:

```text
timed_dose(port, speed, target_ml)
  verify pump starts from 0
  compute run time from calibrated flow rate
  account for ramp-up and ramp-down delivered volume
  start pump
  hold only as long as needed
  always command speed 0 in finally
  verify pump reaches 0
  record actual estimated dose
```

## Manufacturer Flow Model

AC Infinity peristaltic pump spec:

```text
21 mL/min per speed level
```

Assuming linear flow:

```text
flow_ml_sec = speed * 21 / 60
            = speed * 0.35 mL/sec
```

Examples:

```text
speed 1 = 21 mL/min  = 0.35 mL/sec
speed 2 = 42 mL/min  = 0.70 mL/sec
speed 5 = 105 mL/min = 1.75 mL/sec
```

The executor should time dose holds in milliseconds using `time.monotonic_ns()` or
another monotonic clock source. Calculations may use seconds internally for readability,
but execution records and control waits should store millisecond fields.

Millisecond equivalents:

```text
flow_ml_ms = speed * 21 / 60000
           = speed * 0.00035 mL/ms

speed 1 = 0.00035 mL/ms
speed 2 = 0.00070 mL/ms
speed 5 = 0.00175 mL/ms
```

This should be the default model, with future per-port calibration overrides:

```text
FLOW_ML_MIN_RDWC_CONTROL_1=21
FLOW_ML_MIN_RDWC_CONTROL_2=21
FLOW_ML_MIN_RDWC_CONTROL_3=21
FLOW_ML_MIN_RDWC_CONTROL_4=21
```

## Ramp Compensation

CTR89Q ports have measured ramp behavior of roughly:

```text
1 speed unit per second
```

This matters because a pH pump continues dosing while ramping down after the stop command.

For a timed dose starting from speed `0`, targeting speed `S`, with ramp rate `R` speed units/sec:

```text
ramp_up_sec   = S / R
ramp_down_sec = S / R
```

In executor records, store:

```text
ramp_up_ms
ramp_down_ms
hold_ms
```

During each ramp, average speed is approximately `S / 2`.

Delivered volume:

```text
k = 21 / 60 = 0.35 mL/sec/speed

ramp_up_ml   = k * (S / 2) * ramp_up_sec
ramp_down_ml = k * (S / 2) * ramp_down_sec
hold_ml      = k * S * hold_sec

total_ml = ramp_up_ml + hold_ml + ramp_down_ml
```

With `R = 1`, that simplifies to:

```text
ramp_up_ml   = 0.175 * S^2
ramp_down_ml = 0.175 * S^2
ramp_total   = 0.35  * S^2
```

Examples:

```text
speed 1 minimum full pulse ~= 0.35 mL
  ramp up   ~= 0.175 mL
  ramp down ~= 0.175 mL

speed 2 minimum full pulse ~= 1.40 mL
  ramp up   ~= 0.70 mL
  ramp down ~= 0.70 mL
```

For pH pumps, ramp-down volume is especially important because it happens after the stop command. The planner must subtract both ramp-up and ramp-down volume from the requested target dose before calculating hold time.

If requested `target_ml` is less than the minimum ramp-only dose, do not run the pump. Report that the requested dose is below hardware resolution.

## Dose Calculation

Function shape:

```text
calculate_timed_dose(port, speed, target_ml)
  flow_per_speed_ml_ms = configured_flow_ml_min / 60000
  ramp_rate = configured_ramp_speed_per_sec
  ramp_up_ms = round((speed / ramp_rate) * 1000)
  ramp_down_ms = same
  ramp_up_ml = flow_per_speed_ml_ms * (speed / 2) * ramp_up_ms
  ramp_down_ml = same
  hold_ml = target_ml - ramp_up_ml - ramp_down_ml
  if hold_ml < 0: reject or raise to minimum deliverable dose
  hold_ms = round(hold_ml / (flow_per_speed_ml_ms * speed))
```

The executor should log:

```text
target_ml
speed
ramp_up_ms
hold_ms
ramp_down_ms
estimated_actual_ml
```

## Execution Contract

Add a dedicated function for dosers instead of using raw `set_port_speed()` directly:

```python
def timed_dose(token, dev, port, speed, target_ml):
    set_port_speed(token, dev_id, port, speed, dev_type)
    try:
        sleep_ms(hold_ms)
    finally:
        set_port_speed(token, dev_id, port, 0, dev_type)
        verify_port_stopped(...)
```

Important rules:

- Start only if readback says the port is already at speed `0`.
- If start command fails, do not record an executed dose.
- Always command speed `0` in `finally`.
- After stop command, wait expected ramp-down time plus buffer.
- Verify readback reaches `0`.
- If stop verification fails, alert immediately and enter dosing lockout / alert-only mode.

## pH Pump Rules

pH ports should be the strictest path:

- Speed must be `1`.
- One pH action per cycle.
- pH lockout applies before and after the action.
- Target dose should be code-owned, not AI-chosen free-form.
- Minimum hardware dose must account for ramp-up and ramp-down.
- Verification should require pH movement in the expected direction.
- If pH moves the wrong direction or overshoots, block further pH dosing and alert.

Initial pH dose sizes:

```text
PH_MICRODOSE_ML=0.5
PH_SMALL_DOSE_ML=1.0
```

If the calculated minimum speed-1 pulse is close to or above the configured microdose, prefer the hardware minimum and document the actual estimated dose.

## Diluted First Live Test

First live chemical tests should use diluted pH solution and/or diluted nutrients.
This lowers the consequence of a timing, ramp, or API-control mistake while collecting
real reservoir response data.

Important calibration rule:

The AI does not need to know the dilution percentage, but the deterministic controller
and calibration logger must know it. Otherwise, diluted observations will make the
system believe full-strength solution is weaker than it really is, and later full-strength
doses could overshoot.

Recommended env/config fields:

```text
PH_UP_STRENGTH_FACTOR=0.25
PH_DOWN_STRENGTH_FACTOR=0.25
NUTE_A_STRENGTH_FACTOR=0.50
NUTE_B_STRENGTH_FACTOR=0.50
```

Where:

```text
1.00 = full strength
0.50 = half strength
0.25 = quarter strength
```

Calibration records should store both:

```text
actual_ml_delivered
strength_factor
full_strength_equivalent_ml = actual_ml_delivered * strength_factor
```

The prompt can still receive only the normalized result, for example:

```text
Observed pH UP response: +0.08 pH per 1.0 mL full-strength-equivalent
```

Do not mix raw diluted and full-strength observations in the same average unless they
are normalized to full-strength-equivalent units first.

Suggested rollout:

1. Start with diluted pH solution.
2. Run only advisory mode until calculations look correct.
3. Run one supervised live microdose.
4. Verify forced stop and readback.
5. Wait for reservoir mixing and sensor response.
6. Store response with strength factor.
7. Repeat enough times to establish a conservative response table.
8. Only then move toward full-strength solution, and keep the old diluted observations normalized.

## Nutrient Pump Rules

Nutrient ports 1 and 2 must run as a paired timed dose.

Rules:

- Ports 1 and 2 start from `0`.
- Both ports use the same dose window unless ratio adjustment requires per-port target volume.
- If one port fails to start, stop both immediately.
- If one port fails to stop, alert immediately.
- Record both actual estimated doses.

Initial nutrient dose sizes:

```text
NUTE_MICRODOSE_ML_EACH=5
NUTE_SMALL_DOSE_ML_EACH=10
```

The existing `NUTRIENT_RATIO_1` / `NUTRIENT_RATIO_2` can later convert a total target into per-port target mL rather than altering speed.

## AI Contract

The AI should not choose raw pump duration.

Allowed chemical action shape should become:

```json
{
  "selected_playbook": "timed_ph_up_microdose",
  "reason": "pH is below the current target range and pH gate allows correction."
}
```

or:

```json
{
  "selected_playbook": "timed_nutrient_microdose",
  "reason": "TDS is below target and reservoir dose gate is NORMAL."
}
```

Code maps the playbook to speed and target mL.

## Required Safety Gates

Timed dosing must depend on these other fixes:

- Reservoir gate enforcement in code.
- Strict action/playbook schema validation.
- Actual executed action return values.
- Persistent lockouts.
- Read-after-write verification.

Do not enable live chemical dosing until those are in place.

## Simulation And Test Cases

Add tests or simulation probes for:

- pH microdose calculation at speed 1 including ramp-down volume.
- Nutrient paired dose calculation for ports 1 and 2.
- Requested dose below minimum hardware deliverable.
- Start failure prevents execution record.
- Stop command runs in `finally` after simulated exception.
- Stop readback failure triggers alert-only state.
- One nutrient port starts and the other fails: both stop.
- pH lockout blocks second pH dose.
- Reservoir `ph_gate=HOLD` blocks pH dose.
- Reservoir `dose_gate=HOLD/NONE` blocks nutrient dose.

## Open Decisions

- Confirm whether ramp rate is identical on RDWC doser ports and 4 x 4 ports.
- Confirm if pH pump tubing/head behaves exactly like nutrient pump tubing/head.
- Pick initial pH microdose and small-dose mL values.
- Pick initial nutrient microdose and small-dose mL values.
- Decide whether to physically calibrate each pump by pumping water into a measuring cup.
- Decide first alert channel for failed stop verification.
