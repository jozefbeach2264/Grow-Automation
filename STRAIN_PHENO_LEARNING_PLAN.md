# Strain And Pheno Learning Plan

Status: draft for review

## Goal

Build enough clean grow data to predict the best operating parameters for each
specific strain and pheno, eventually moving toward a nearly set-it-and-forget-it
system.

The learning layer should not replace the safety controller. It should recommend
better targets and predictions while deterministic code continues to enforce hard
safety limits.

Target architecture:

```text
Safety controller = hard rules and execution limits
Learning layer = predicts best targets and expected responses
AI = explains, ranks issues, and chooses from safe options/playbooks
```

## Development Layers

### 1. Safe control first

Before learning can be trusted, control must be safe and bounded:

- Hard reservoir gate enforcement.
- Timed dosing and forced pump stop.
- Strict action/playbook schema validation.
- Read-after-write verification.
- Persistent lockouts.
- Clear alerting.
- Accurate executed-action tracking.

Bad control creates bad data, and bad data produces bad recommendations.

### 2. Accurate event history

Every meaningful event should record both request and outcome.

For each action:

- Requested action or playbook.
- Validated action selected by code.
- Actual executed hardware command.
- Start timestamp.
- Stop timestamp.
- Speed.
- Duration.
- Estimated mL delivered.
- Strength factor.
- Full-strength-equivalent mL.
- Reservoir state before action.
- Reservoir state after action.
- Relevant environmental state before and after.
- Whether the result matched expectation.
- Whether any fallback, alert, or lockout was triggered.

For each cycle:

- Sensor snapshot.
- Trends.
- Reservoir health state.
- Deterministic stressors.
- Active targets.
- AI assessment.
- AI-selected playbook, if any.
- User intervention, if any.

### 3. Strain and pheno identity

Profiles need stable identity fields so data is not mixed across different genetics
or setups.

Recommended fields:

```text
strain_name
pheno_id
run_id
plant_id
clone_or_seed
mother_id
medium
system_type
reservoir_volume_gal
nutrient_line
water_source
light_model
tent_size
controller_layout_version
```

The minimum useful identity should be:

```text
strain_name
pheno_id
run_id
reservoir_volume_gal
nutrient_line
```

### 4. Response models

Once the data is clean, build per-strain and per-pheno response summaries.

Examples:

- pH response per full-strength-equivalent mL of pH Up/Down.
- TDS/EC response per mL of nutrient A/B.
- Typical daily water consumption by stage/week.
- Typical EC drift by stage/week.
- Typical pH drift by stage/week.
- Water temperature threshold where uptake slows.
- VPD range where the pheno stays most active.
- CO2 range where the pheno continues eating and drinking.
- Light/heat sensitivity by stage.
- Recovery time after stress events.

The model should distinguish:

- Generic strain behavior.
- Specific pheno behavior.
- Current run behavior.
- Equipment/system behavior.

### 5. Predictive targets

After enough history, the system can recommend targets instead of blindly following
generic schedules.

Examples:

- `target_tds_ppm` for this pheno, stage, and week.
- `target_ph_min` / `target_ph_max`.
- `target_vpd_min` / `target_vpd_max`.
- `target_co2_ppm`.
- `reference_water_temp_f` as context only unless the hardware plan changes.
- Expected water-level drop over the next interval.
- Expected EC/TDS drift over the next interval.
- Expected pH drift over the next interval.

Predictions should include confidence:

```json
{
  "target_tds_ppm": 620,
  "confidence": "medium",
  "basis": "3 previous runs of this pheno, veg week 2"
}
```

### 6. Prescriptive control

Only after enough clean data should the system start acting ahead of problems.

Examples:

- Lower EC before the pheno typically stalls.
- Delay CO2 increase until water uptake confirms readiness.
- Keep water temp under a tighter limit for a root-sensitive pheno.
- Adjust pH target window for the pheno's natural drift pattern.

Prescriptive control must still pass through:

- Safety gates.
- Playbook validation.
- Execution bounds.
- Verification and alerting.

## Data Quality Rules

Learning data should be marked or excluded when:

- Sensor was missing or implausible.
- Action failed or only partially executed.
- Stop verification failed.
- User manually intervened but did not log what changed.
- Reservoir volume changed.
- Nutrient concentration/strength factor changed without being recorded.
- Calibration solution was diluted and not normalized.
- Plant count changed.
- Hardware layout changed.
- The system was in `STRESS` or `PROBLEM` and the response was not normal uptake behavior.

Do not average incompatible data unless normalized first.

## Profile Storage Direction

The current JSON profile is fine for early work. Long term, consider moving event
history into SQLite for queryable time-series/event analysis.

Possible tables:

```text
runs
plants
cycles
sensors
actions_requested
actions_executed
outcomes
stressors
targets
alerts
hardware_config
```

Keep the JSON profile as a summary/cache if useful, but use structured event storage
as the source of truth when the dataset grows.

## Conflict Checklist For Later Review

Before implementing, compare this plan against:

- Away-mode AI triage/playbook plan.
- Timed dosing plan.
- Reservoir gate enforcement.
- Action/playbook schema validation.
- Actual executed action return values.
- Persistent lockouts.
- Read-after-write verification.
- `.env` permissions and secret handling.

The learning layer must never bypass safety and execution validation.

## Open Decisions

- What should the first `pheno_id` format be?
- Should reservoir volume be required before chemical dosing is enabled?
- Should profile storage remain JSON initially or move to SQLite sooner?
- How should manual user interventions be logged quickly from the terminal?
- How many observations are required before a recommendation can become predictive?
- Which metrics define "best possible parameters": yield, stability, uptake, growth rate, quality, or a weighted mix?
