"""
Timed dosing with forced stop -- Layer 1 #7 (docs/done/TIMED_DOSING_PLAN.md).

Replaces open-ended `set_port_speed(port, speed)` doses (which run until something
else stops them) with bounded `timed_dose()` / `timed_dose_pair()` calls that:

  1. verify the port is at speed 0 before starting,
  2. persist a crash-safe active-dose record (so the watchdog leaves it alone and a
     crash mid-dose is recoverable),
  3. run the pump for a computed time (ramp-up + hold), accounting for ramp-down
     volume delivered after the stop command,
  4. ALWAYS command speed 0 in `finally` and verify the port reaches 0,
  5. freeze dosing + raise high-alert if the stop cannot be verified.

Flow model: AC Infinity peristaltic pumps move 21 mL/min per speed level (linear).
Ramp: 1 speed unit/sec, symmetric (measured 2026-05-30). Both overridable in .env.

Diluted-test note: dose math is in ACTUAL mL delivered. `strength_factor` (<1.0 for a
diluted stock) converts to full-strength-equivalent mL for calibration, so diluted
observations never make the system think full-strength solution is weaker than it is.

This module is the execution primitive. Reservoir-gate / schema / lockout enforcement
stays in ai_advisor.filter_actions/validate_actions; callers should gate before dosing.
"""

import math
import os
import time

from utils import name_slug
from ac_infinity_client import set_port_speed, read_port_state, stop_and_verify
import proc_lock
import runtime_state
import safety_state

DEFAULT_FLOW_ML_MIN = 21.0          # AC Infinity peristaltic spec, per speed level
DEFAULT_RAMP_SPEED_PER_SEC = 1.0    # measured, symmetric
# Only attempt a mid-dose start-confirm readback for doses whose on-time is at least
# this long -- a sub-second pH pulse would finish before a readback returns, so it
# stays start-unverified (which only makes the crash estimate more conservative).
START_CONFIRM_MIN_MS = 3000

# Hard minimum settle after ANY doser/pH dose before the reservoir reading is trusted.
# pH especially keeps drifting ~5 min past the apparent quick-settle (observed 2026-06-02:
# pH-down read -0.81 at 15s but settled at -0.74 only after 5 min; pH-up the same). This is
# a HARD wait for every doser dose -- never read an outcome or recalibrate sooner. Both the
# test harness and the autonomous outcome-readback (profile_manager) honor it.
DOSE_SETTLE_SEC = 300


def dose_settle_seconds() -> float:
    """Canonical doser settle (seconds). Default 300 (5 min); override DOSE_SETTLE_MINUTES."""
    v = os.getenv("DOSE_SETTLE_MINUTES", "").strip()
    try:
        return max(0.0, float(v) * 60.0) if v else float(DOSE_SETTLE_SEC)
    except ValueError:
        return float(DOSE_SETTLE_SEC)

# Code-owned dose sizes (mL). The AI selects a playbook name; code maps it to a dose.
_DOSE_DEFAULTS = {
    "PH_MICRODOSE_ML":       0.5,
    "PH_SMALL_DOSE_ML":      1.0,
    "NUTE_MICRODOSE_ML_EACH": 5.0,
    "NUTE_SMALL_DOSE_ML_EACH": 10.0,
}

# Playbook registry -- the only chemical actions the AI may select. `ports` is resolved
# by the caller against the live device; speed + dose size are code-owned. pH is always
# speed 1 (strictest path).
PLAYBOOKS = {
    "timed_ph_up_microdose":   {"kind": "ph",   "speed": 1, "ml_env": "PH_MICRODOSE_ML"},
    "timed_ph_down_microdose": {"kind": "ph",   "speed": 1, "ml_env": "PH_MICRODOSE_ML"},
    "timed_ph_up_small":       {"kind": "ph",   "speed": 1, "ml_env": "PH_SMALL_DOSE_ML"},
    "timed_ph_down_small":     {"kind": "ph",   "speed": 1, "ml_env": "PH_SMALL_DOSE_ML"},
    "timed_nutrient_microdose": {"kind": "nute", "speed": 2, "ml_env": "NUTE_MICRODOSE_ML_EACH"},
    "timed_nutrient_small":     {"kind": "nute", "speed": 2, "ml_env": "NUTE_SMALL_DOSE_ML_EACH"},
}


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #
def _flow_ml_min(device: str, port: int) -> float:
    key = f"FLOW_ML_MIN_{name_slug(device)}_{port}"
    v = os.getenv(key, "").strip()
    if not v:
        return DEFAULT_FLOW_ML_MIN
    try:
        f = float(v)
    except ValueError:
        f = None
    if f is None or not math.isfinite(f) or f <= 0:
        # An EXPLICITLY-set but invalid flow (unparseable, NaN/inf, or <= 0) is an
        # operator error, never a case for the spec default: silently substituting
        # 21 mL/min under a pump whose true calibrated flow is e.g. ~40 mL/min would
        # run the dose ~2x long -- a silent chemical overdose. Abort loudly BEFORE
        # any pump starts (the callers plan doses before any client write). Only the
        # UNSET case falls back to the spec default.
        runtime_state.record_event("dose_config_error", device=device, port=port,
                                   env_key=key, value=v,
                                   reason="invalid FLOW_ML_MIN override")
        raise ValueError(f"{key}={v}: FLOW_ML_MIN override must be a finite number > 0 "
                         "(unset it to use the 21 mL/min spec default)")
    return f


def _ramp_rate() -> float:
    try:
        r = float(os.getenv("RAMP_SPEED_PER_SEC", "").strip() or DEFAULT_RAMP_SPEED_PER_SEC)
    except ValueError:
        return DEFAULT_RAMP_SPEED_PER_SEC
    return r if r > 0 else DEFAULT_RAMP_SPEED_PER_SEC


def strength_factor(device: str, port: int) -> float:
    """Concentration of the stock on this port relative to full feeding strength.
    1.0 = full strength, 0.25 = quarter (diluted), 4.0 = 4x (concentrated). Used only
    to convert ACTUAL mL delivered -> full-strength-equivalent mL for calibration, so a
    concentrated OR diluted stock is recorded correctly and later doses don't over/under
    shoot. Clamped to (0.01, 100] to catch fat-finger typos. Set
    STRENGTH_FACTOR_<SLUG>_<port> in .env."""
    v = os.getenv(f"STRENGTH_FACTOR_{name_slug(device)}_{port}", "").strip()
    try:
        f = float(v) if v else 1.0
    except ValueError:
        f = 1.0
    return min(max(f, 0.01), 100.0)


def dose_ml(env_key: str) -> float:
    v = os.getenv(env_key, "").strip()
    try:
        return float(v) if v else _DOSE_DEFAULTS.get(env_key, 0.0)
    except ValueError:
        return _DOSE_DEFAULTS.get(env_key, 0.0)


# --------------------------------------------------------------------------- #
# dose math (pure)
# --------------------------------------------------------------------------- #
def calculate_timed_dose(speed: int, target_ml: float,
                         flow_ml_min: float = DEFAULT_FLOW_ML_MIN,
                         ramp_rate: float = DEFAULT_RAMP_SPEED_PER_SEC) -> dict:
    """Compute timing + volumes for one bounded dose. Pure function (no IO).

    Ramp-up and ramp-down each deliver ~ (S/2) * flow over the ramp time; the hold
    delivers the remainder. If target_ml is below the minimum ramp-only pulse for this
    speed, the dose is not deliverable (the caller must not run the pump)."""
    if speed < 1 or speed > 10:
        raise ValueError("dose speed must be 1-10")
    if target_ml <= 0:
        raise ValueError("target_ml must be > 0")
    if flow_ml_min <= 0 or ramp_rate <= 0:
        # Defensive: the env helpers already clamp these, but a direct caller passing
        # a non-positive flow/ramp would otherwise hit a bare ZeroDivisionError below.
        raise ValueError("flow_ml_min and ramp_rate must be > 0")

    flow_ml_ms = flow_ml_min / 60000.0            # mL per ms per speed level
    ramp_up_ms = round((speed / ramp_rate) * 1000)
    ramp_down_ms = ramp_up_ms
    ramp_up_ml = flow_ml_ms * (speed / 2.0) * ramp_up_ms
    ramp_down_ml = ramp_up_ml
    ramp_total_ml = ramp_up_ml + ramp_down_ml

    if target_ml < ramp_total_ml:
        return {"deliverable": False, "speed": speed, "target_ml": target_ml,
                "min_ml": round(ramp_total_ml, 4),
                "reason": (f"target {target_ml} mL below minimum ramp-only pulse "
                           f"{round(ramp_total_ml, 4)} mL at speed {speed}")}

    hold_ml = target_ml - ramp_total_ml
    hold_ms = round(hold_ml / (flow_ml_ms * speed))
    on_ms = ramp_up_ms + hold_ms
    est_actual = ramp_up_ml + hold_ml + ramp_down_ml
    return {
        "deliverable": True, "speed": speed, "target_ml": target_ml,
        "flow_ml_min": flow_ml_min, "ramp_rate": ramp_rate,
        "ramp_up_ms": ramp_up_ms, "hold_ms": hold_ms, "ramp_down_ms": ramp_down_ms,
        "on_ms": on_ms,
        "ramp_up_ml": round(ramp_up_ml, 4), "hold_ml": round(hold_ml, 4),
        "ramp_down_ml": round(ramp_down_ml, 4),
        "estimated_actual_ml": round(est_actual, 4),
    }


# --------------------------------------------------------------------------- #
# stop helper (command 0 -> verify -> one retry). Freeze policy is the caller's.
# --------------------------------------------------------------------------- #
def _ble_fallback_enabled() -> bool:
    return os.getenv("BLE_STOP_FALLBACK", "true").strip().lower() != "false"


def _ble_configured(device: str) -> bool:
    """True when a BLE MAC is configured for this controller -- i.e. a `ble_logger`
    daemon could plausibly be draining the queue. Same resolution order the daemon
    itself uses (BLE_<SLUG>_MAC, then BLE_DEFAULT_MAC). Checked before falling back
    so that on a system with no BLE set up the fallback costs nothing: with no daemon
    the wait below could only ever time out, delaying the freeze during an emergency
    for no possible gain."""
    return bool(os.getenv(f"BLE_{name_slug(device)}_MAC", "").strip()
                or os.getenv("BLE_DEFAULT_MAC", "").strip())


def _ble_fallback_wait_s() -> float:
    """Budget for the BLE fallback to CONFIRM the port off. The daemon polls port
    state every 30s by default, so the default here must span a full sweep plus its
    3s response timeout or confirmation can never arrive. Override
    BLE_STOP_FALLBACK_WAIT_S (and shorten it if you run --poll-interval lower)."""
    try:
        return max(0.0, float(os.getenv("BLE_STOP_FALLBACK_WAIT_S", "").strip() or 35.0))
    except ValueError:
        return 35.0


def ble_stop_fallback(dev: dict, port: int) -> bool:
    """Independent LOCAL stop path, used only after the cloud stop failed to verify.

    The cloud is the sole stop transport on the normal path, so an AC Infinity outage
    (or an expired token, or the laptop's WAN dropping) mid-dose leaves a chemical
    pump running with no way to command it off -- the freeze that follows protects
    FUTURE doses but does nothing about the pump that is already spinning. The BLE
    channel does not depend on the cloud at all, and its chemical guard has always
    permitted stops (speed 0) unconditionally, precisely so it could be used here.

    Returns True only when a post-stop BLE readback PROVES the port off; a queued
    write that we cannot confirm still counts as an unverified stop, so the caller's
    freeze fires exactly as before."""
    device = dev["name"]
    if not _ble_fallback_enabled():
        return False
    if not _ble_configured(device):
        print(f"  [DOSE] no BLE MAC configured for {device} -- no local stop fallback")
        return False
    try:
        from aci_ble_lab import db as ble_db
    except Exception as e:      # BLE lab absent/unimportable -- never mask the real fault
        print(f"  [DOSE] BLE stop fallback unavailable ({e})")
        return False

    issued_ts = time.time()
    try:
        # work_type 0 + speed 0 = off-mode at level 0. guard_chemical_write lets every
        # stop through, freeze or not -- stopping a pump is never gated.
        cmd_id = ble_db.enqueue_command(device, port, 0, 0, source="dosing_stop_fallback")
    except Exception as e:
        print(f"  [DOSE] BLE stop fallback could not enqueue {device} p{port}: {e}")
        runtime_state.record_event("dose_stop_ble_enqueue_failed", device=device,
                                   port=port, reason=str(e))
        return False

    print(f"  [DOSE] cloud stop unverified -- queued BLE stop {device} p{port} "
          f"(cmd {cmd_id}); waiting up to {_ble_fallback_wait_s():.0f}s for confirmation")
    deadline = issued_ts + _ble_fallback_wait_s()
    status = None
    while time.time() < deadline:
        status = ble_db.command_status(cmd_id)
        if status in ("failed", "expired"):
            break
        # Confirmation is the readback, not the queue row: 'done' only means the GATT
        # write went out. Check every pass so a port poll landing mid-wait is caught.
        if ble_db.port_confirmed_off(device, port, issued_ts):
            print(f"  [DOSE] BLE fallback CONFIRMED {device} p{port} off "
                  f"({round(time.time() - issued_ts, 1)}s)")
            runtime_state.record_event("dose_stop_ble_fallback_ok", device=device,
                                       port=port, cmd_id=cmd_id,
                                       elapsed_sec=round(time.time() - issued_ts, 1))
            return True
        time.sleep(1.0)

    print(f"  [DOSE] BLE fallback did NOT confirm {device} p{port} off "
          f"(queue status {status or 'unknown'}) -- treating stop as unverified")
    runtime_state.record_event("dose_stop_ble_fallback_unconfirmed", device=device,
                               port=port, cmd_id=cmd_id, queue_status=status)
    return False


def _force_stop(token: str, dev: dict, port: int) -> bool:
    """Stop the pump and confirm it via the shared stop primitive. Returns True only
    when the port is confirmed at 0. Freeze policy stays with the caller -- timed_dose
    freezes dosing + raises high-alert on a False return.

    A cloud stop that will not verify falls back to the local BLE transport before
    giving up (see ble_stop_fallback), so a cloud outage mid-dose has a second,
    independent way to actually stop the pump rather than only freezing after it."""
    device = dev["name"]
    verify = os.getenv("VERIFY_WRITES", "true").strip().lower() != "false"
    res = stop_and_verify(token, dev, port, retries=1, verify=verify)
    if res["ok"]:
        if res["reason"] != "verify skipped":
            print(f"  [DOSE] verified stop {device} p{port} ({res['elapsed_sec']}s)")
        return True
    obs = (res.get("observed") or {}).get("speed_actual")
    print(f"  [DOSE] stop UNVERIFIED {device} p{port} (observed {obs}) -- {res['reason']}")
    if token == "SIM":
        return False        # sim_runner never touches the BLE queue
    if ble_stop_fallback(dev, port):
        # The pump is confirmed off by an independent transport, so the freeze would be
        # a false alarm -- but the cloud control path IS degraded, so raise the
        # high-alert window (faster reservoir polling) rather than continuing silently.
        runtime_state.start_high_alert(
            f"cloud stop unverified on {device} p{port}; stopped via BLE fallback")
        return True
    return False


def _fast_stop(token: str, dev: dict, port: int) -> None:
    """Issue the stop write ONLY (no read-after-write verify). Used by timed_dose_pair
    to fire each pump's stop on its own deadline; verifying inline would block the next
    port's stop by the verify latency (~2-4s) and over-run the later pump. The
    authoritative verify + retry + freeze runs once afterward via _force_stop."""
    try:
        set_port_speed(token, dev["dev_id"], port, 0, dev["type"])
    except Exception as e:
        print(f"  [DOSE] fast-stop write error {dev['name']} p{port}: {e} "
              "-- final verify will catch it")


def _sleep_ms(ms: float) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


def _freeze_after_failed_stop(device: str, ports) -> None:
    plist = ports if isinstance(ports, (list, tuple, set)) else [ports]
    safety_state.disable_dosing(
        f"timed dose stop NOT verified on {device} port(s) {list(plist)} -- pump may run")
    runtime_state.start_high_alert(f"timed dose stop failed on {device} port(s) {list(plist)}")


def _busy_result(device: str, ports, err: Exception) -> dict:
    """Uniform refusal when another process already holds the chemical-dose lock.
    Nothing was written and no pump was touched -- the caller can retry next cycle."""
    plist = ports if isinstance(ports, (list, tuple, set)) else [ports]
    print(f"  [DOSE] BLOCKED {device} port(s) {list(plist)}: another chemical dose is "
          f"in flight ({err})")
    runtime_state.record_event("dose_blocked_concurrent", device=device,
                               ports=list(plist), reason=str(err))
    return {"ok": False, "reason": f"another chemical dose is in flight ({err})"}


def _record_write_failure(device: str, ports, err: Exception) -> dict:
    """Uniform refusal when the crash-safe active-dose record would not persist.
    The record is what makes a dose recoverable; without it a crash leaves a pump
    running with nothing for startup recovery to reconcile against, so we refuse to
    start rather than dose blind."""
    plist = ports if isinstance(ports, (list, tuple, set)) else [ports]
    msg = (f"active-dose record could not be persisted ({err}) -- refusing to start "
           "the pump (a dose with no crash-recovery record is unrecoverable)")
    print(f"  [DOSE] ABORT {device} port(s) {list(plist)}: {msg}")
    runtime_state.record_event("dose_aborted_no_record", device=device,
                               ports=list(plist), reason=str(err))
    return {"ok": False, "reason": msg}


# --------------------------------------------------------------------------- #
# single-port timed dose (pH / single doser)
# --------------------------------------------------------------------------- #
def timed_dose(token: str, dev: dict, port: int, speed: int, target_ml: float, *,
               solution: str | None = None, strength: float | None = None,
               advisory: bool = False) -> dict:
    """Run one bounded dose on a doser/pH port. ALWAYS stops in finally. Persists a
    crash-safe active-dose record. advisory=True computes + logs but never actuates."""
    device, dev_id, dev_type = dev["name"], dev["dev_id"], dev["type"]
    if strength is None:
        strength = strength_factor(device, port)

    plan = calculate_timed_dose(speed, target_ml,
                                flow_ml_min=_flow_ml_min(device, port),
                                ramp_rate=_ramp_rate())
    if not plan["deliverable"]:
        print(f"  [DOSE] {device} p{port}: {plan['reason']} -- not dosing")
        runtime_state.record_event("dose_below_resolution", device=device, port=port,
                                   target_ml=target_ml, min_ml=plan["min_ml"], speed=speed)
        return {"ok": False, "deliverable": False, **plan}

    fse = round(plan["estimated_actual_ml"] * strength, 4)
    print(f"  [DOSE] {device} p{port} ({solution or 'doser'}): target {target_ml} mL @ spd "
          f"{speed} -> on {plan['on_ms']}ms (ramp {plan['ramp_up_ms']} + hold {plan['hold_ms']} "
          f"+ rampdn {plan['ramp_down_ms']}); strength {strength} -> {fse} mL full-strength-eq")
    if advisory:
        return {"ok": True, "advisory": True, "full_strength_equivalent_ml": fse, **plan}

    # Serialize chemical execution system-wide before touching anything. The poller
    # and the supervised bucket harnesses are SEPARATE PROCESSES that both dose
    # through here, nothing else stops them overlapping, and the crash-recovery
    # active-dose record is a single slot -- two concurrent doses overwrite each
    # other's record, so the watchdog vouches for the wrong port and whichever pump
    # finishes first clears the protection the other is still relying on.
    # Non-blocking on purpose: refuse the dose rather than park the control loop
    # behind another process's pump (it can retry next cycle).
    try:
        with proc_lock.ProcessLock(proc_lock.CHEMICAL_DOSE):
            return _run_timed_dose(token, dev, port, speed, plan,
                                   fse=fse, strength=strength, solution=solution)
    except proc_lock.LockBusy as e:
        return _busy_result(device, port, e)


def _run_timed_dose(token: str, dev: dict, port: int, speed: int, plan: dict, *,
                    fse: float, strength: float, solution: str | None) -> dict:
    """Actuating half of `timed_dose`, run with the chemical-dose lock HELD.

    Split out so the lock's scope is exactly the dangerous span -- freeze check,
    pre-check, active-dose record, pump, verified stop -- and no wider."""
    device, dev_id, dev_type = dev["name"], dev["dev_id"], dev["type"]

    # 0. Honor the chemical freeze at the execution primitive too. A freeze tripped
    # mid-cycle (e.g. by a prior action's failed stop) must block the NEXT dose in the
    # same batch -- those actions already cleared filter_actions before the freeze.
    if safety_state.is_dosing_disabled():
        reason = safety_state.dosing_disable_status()[1]
        print(f"  [DOSE] BLOCKED {device} p{port}: dosing frozen ({reason})")
        runtime_state.record_event("dose_blocked_frozen", device=device, port=port,
                                   reason=reason)
        return {"ok": False, "reason": f"dosing frozen: {reason}"}

    # 1. Pre-check: must be at 0.
    cur = read_port_state(token, dev_id, port)
    if cur is None:
        return {"ok": False, "reason": "port_not_found"}
    if (cur.get("speed_actual") or 0) != 0:
        msg = f"port not at 0 before dose (observed {cur.get('speed_actual')})"
        print(f"  [DOSE] ABORT {device} p{port}: {msg}")
        runtime_state.record_event("dose_aborted", device=device, port=port, reason=msg)
        return {"ok": False, "reason": msg}

    # 2. Persist active-dose record BEFORE the pump starts. The durable write is a
    # HARD PRECONDITION, not bookkeeping: it is the only thing that lets startup
    # recovery distinguish "a dose was in flight when we died" from "nothing was
    # running", so if it cannot be written we must not start a pump we would be
    # unable to account for afterwards.
    started_ts = time.time()
    planned_stop_ts = (started_ts + plan["on_ms"] / 1000.0
                       + plan["ramp_down_ms"] / 1000.0 + 5)
    try:
        runtime_state.begin_active_dose({
            "device": device, "dev_id": dev_id, "port": port, "speed": speed,
            "solution": solution, "target_ml": plan["estimated_actual_ml"],
            "strength_factor": strength, "start_verified": False,
            "started_wall_ts": started_ts, "planned_stop_wall_ts": planned_stop_ts,
            "planned_on_ms": plan["on_ms"], "flow_ml_min": plan["flow_ml_min"],
        })
    except runtime_state.StateWriteError as e:
        return _record_write_failure(device, port, e)

    start_verified = False
    stop_ok = False
    est = None
    try:
        set_port_speed(token, dev_id, port, speed, dev_type)
        # Time the hold from HERE -- the pump only begins ramping once the start write's
        # GET+PUT round-trip completes, so the clock must start AFTER set_port_speed
        # returns. Charging that round-trip against on_ms would truncate the dose (worst
        # for sub-second pH pulses, where the settings GET can eat most of the hold).
        pump_start_mono = time.monotonic()
        # Best-effort start confirm (long doses only). This readback happens while the
        # pump is already running, so its latency legitimately counts toward on_ms.
        if plan["on_ms"] >= START_CONFIRM_MIN_MS and token != "SIM":
            chk = read_port_state(token, dev_id, port)
            if chk and (chk.get("speed_actual") or 0) > 0:
                start_verified = True
                runtime_state.mark_active_dose_running()
        elapsed_ms = (time.monotonic() - pump_start_mono) * 1000.0
        _sleep_ms(plan["on_ms"] - elapsed_ms)
    finally:
        # ALWAYS stop, tear down the active-dose record, and freeze on an unverified
        # stop -- on EVERY exit path, including an exception mid-dose. A token expiry or
        # network drop that raises during the start-confirm read can also break the stop
        # write (stop_and_verify reports ok=False without raising); the freeze MUST still
        # fire. Keeping this whole block in the finally is the module's core guarantee:
        # a pump that may still be running never escapes the freeze.
        stop_ok = _force_stop(token, dev, port)
        stopped_ts = time.time()
        est = runtime_state.estimate_interrupted_dose(
            {"speed": speed, "start_verified": start_verified,
             "target_ml": plan["estimated_actual_ml"], "strength_factor": strength,
             "started_wall_ts": started_ts, "flow_ml_min": plan["flow_ml_min"]}, stopped_ts)
        runtime_state.mark_active_dose_stopped(verified=stop_ok)
        runtime_state.clear_active_dose()
        runtime_state.record_event("timed_dose" if stop_ok else "timed_dose_stop_failed",
                                   device=device, port=port, solution=solution, speed=speed,
                                   estimated_actual_ml=plan["estimated_actual_ml"],
                                   full_strength_equivalent_ml=fse, stop_verified=stop_ok)
        if not stop_ok:
            _freeze_after_failed_stop(device, port)

    # Reaching here means the pump ran (every no-start path returned earlier), so even
    # an ok=False result (unverified stop) delivered chem -- expose which port started
    # and the best delivered estimate so the caller can stamp lockouts / track outcome.
    return {"ok": stop_ok, "estimated_actual_ml": plan["estimated_actual_ml"],
            "full_strength_equivalent_ml": fse, "stop_verified": stop_ok,
            "start_verified": start_verified, "estimate": est,
            "started": [port],
            "delivered_ml_each": {port: plan["estimated_actual_ml"]}, **plan}


# --------------------------------------------------------------------------- #
# paired nutrient dose (ports 1 + 2 -- never run solo)
# --------------------------------------------------------------------------- #
def timed_dose_pair(token: str, dev: dict, ports: list, speed: int, target_ml_each: float, *,
                    solution: str = "nutrient", advisory: bool = False) -> dict:
    """Dose two ports together (FloraFlex V1+V2). Both start, both stop. If either
    fails to start or stop, BOTH are stopped immediately and dosing freezes."""
    device, dev_id, dev_type = dev["name"], dev["dev_id"], dev["type"]
    # target_ml_each may be a scalar (both ports equal) or a {port: mL} map (a V1/V2 split).
    ml_for = (lambda p: float(target_ml_each[p])) if isinstance(target_ml_each, dict) \
        else (lambda p: float(target_ml_each))
    plans = {}
    for p in ports:
        pl = calculate_timed_dose(speed, ml_for(p),
                                  flow_ml_min=_flow_ml_min(device, p), ramp_rate=_ramp_rate())
        if not pl["deliverable"]:
            print(f"  [DOSE] PAIR abort: p{p} {pl['reason']} -- not dosing")
            return {"ok": False, "deliverable": False, "port": p, **pl}
        plans[p] = pl

    on_ms = max(pl["on_ms"] for pl in plans.values())
    ml_desc = "/".join(f"p{p}:{ml_for(p):.1f}" for p in ports)
    print(f"  [DOSE] PAIR {device} ports {ports}: {ml_desc} mL @ spd {speed} -> on {on_ms}ms")
    if advisory:
        return {"ok": True, "advisory": True, "plans": plans, "on_ms": on_ms}

    # Same system-wide chemical-dose mutex as timed_dose -- a pair dose and a pH
    # dose are equally exclusive, and they share the one active-dose record slot.
    try:
        with proc_lock.ProcessLock(proc_lock.CHEMICAL_DOSE):
            return _run_timed_dose_pair(token, dev, ports, speed, plans,
                                        on_ms=on_ms, target_ml_each=target_ml_each,
                                        solution=solution)
    except proc_lock.LockBusy as e:
        return _busy_result(device, ports, e)


def _run_timed_dose_pair(token: str, dev: dict, ports: list, speed: int, plans: dict, *,
                         on_ms: float, target_ml_each, solution: str) -> dict:
    """Actuating half of `timed_dose_pair`, run with the chemical-dose lock HELD."""
    device, dev_id, dev_type = dev["name"], dev["dev_id"], dev["type"]

    # Honor the chemical freeze at the execution primitive (see timed_dose step 0).
    if safety_state.is_dosing_disabled():
        reason = safety_state.dosing_disable_status()[1]
        print(f"  [DOSE] PAIR BLOCKED {device} ports {ports}: dosing frozen ({reason})")
        runtime_state.record_event("dose_blocked_frozen", device=device,
                                   ports=list(ports), reason=reason)
        return {"ok": False, "reason": f"dosing frozen: {reason}"}

    # Pre-check: every port at 0.
    for p in ports:
        cur = read_port_state(token, dev_id, p)
        if cur is None or (cur.get("speed_actual") or 0) != 0:
            obs = None if cur is None else cur.get("speed_actual")
            print(f"  [DOSE] PAIR ABORT: p{p} not at 0 (observed {obs})")
            return {"ok": False, "reason": f"port {p} not at 0 before pair dose"}

    started_ts = time.time()
    planned_stop_ts = (started_ts + on_ms / 1000.0
                       + max(pl["ramp_down_ms"] for pl in plans.values()) / 1000.0 + 5)
    # Durable record is a hard precondition here too (see timed_dose step 2).
    try:
        runtime_state.begin_active_dose({
            "device": device, "dev_id": dev_id, "ports": list(ports), "speed": speed,
            "solution": solution, "target_ml_each": target_ml_each,
            "start_verified": False,
            "started_wall_ts": started_ts, "planned_stop_wall_ts": planned_stop_ts,
            "planned_on_ms": on_ms,
            # Scalar kept for backward compat (old readers); the per-port map is what the
            # crash estimate SUMS -- both pumps run concurrently, so max() alone models one
            # pump and under-counts a mid-pair power-loss estimate by ~2x.
            "flow_ml_min": max(plans[p]["flow_ml_min"] for p in ports),
            "flow_ml_min_by_port": {p: plans[p]["flow_ml_min"] for p in ports},
        })
    except runtime_state.StateWriteError as e:
        return _record_write_failure(device, ports, e)

    start_mono = {}            # port -> monotonic clock at its own start
    stop_mono = {}             # port -> monotonic clock at its stop WRITE (delivered est)
    started = []
    start_failed = None
    stop_results = {}
    try:
        for p in ports:
            try:
                set_port_speed(token, dev_id, p, speed, dev_type)
                start_mono[p] = time.monotonic()   # this port is now ramping
                started.append(p)
            except Exception as e:
                start_failed = (p, e)
                print(f"  [DOSE] PAIR start FAILED p{p}: {e} -- stopping both")
                break
        if start_failed is None:
            # Fire each port's STOP COMMAND at its own on_ms (soonest deadline first)
            # WITHOUT waiting on the read-after-write verify. Per-port flow can differ
            # (e.g. V2 ~16% faster), so equal target_ml means unequal run time and each
            # pump must stop on its own clock. Verifying inline would block the next
            # port's stop by the verify latency (~2-4s) and over-run the later pump --
            # the very imbalance this per-port timing exists to avoid. The authoritative
            # verify + retry + freeze runs once for every port in the finally below.
            for p in sorted(started,
                            key=lambda q: start_mono[q] + plans[q]["on_ms"] / 1000.0):
                deadline = start_mono[p] + plans[p]["on_ms"] / 1000.0
                _sleep_ms((deadline - time.monotonic()) * 1000.0)
                _fast_stop(token, dev, p)
                stop_mono[p] = time.monotonic()
    finally:
        # First fire the raw stop WRITES for every port still missing one (the
        # exception / failed-start paths) BEFORE any verification wait. Verifying port
        # 1's stop first would leave port 2 pumping through that whole verify + retry
        # (~4-40s, i.e. 3-30 mL of concentrate) -- the exact over-run the per-port fast
        # stop prevents on the normal path. On the normal path every started port was
        # already fast-stopped above, so this loop adds nothing.
        for p in ports:
            if p not in stop_mono:
                _fast_stop(token, dev, p)
                stop_mono[p] = time.monotonic()
        # Verify (with retry) EVERY port's stop now -- the ones fast-stopped above and
        # any never-started port -- and freeze on any unverified stop. This is also the
        # ONLY stop path on an exception. Runs on every exit path; _force_stop is
        # idempotent so re-stopping an already-stopped port is harmless.
        for p in ports:
            stop_results[p] = _force_stop(token, dev, p)
        all_stopped = all(stop_results.values())
        runtime_state.mark_active_dose_stopped(verified=all_stopped)
        runtime_state.clear_active_dose()
        runtime_state.record_event(
            "timed_dose_pair" if (all_stopped and start_failed is None)
            else "timed_dose_pair_failed",
            device=device, ports=list(ports), speed=speed,
            target_ml_each=target_ml_each, started=started, stop_results=stop_results,
            start_failed=(start_failed[0] if start_failed else None))
        if not all_stopped:
            _freeze_after_failed_stop(device, ports)

    ok = all(stop_results.values()) and start_failed is None
    per_port = {p: round(plans[p]["estimated_actual_ml"], 4) for p in ports}
    fse_each = {p: round(plans[p]["estimated_actual_ml"] * strength_factor(device, p), 4)
                for p in ports}
    # Best-available DELIVERED estimate per port so a partial pair (one pump ran, the
    # other never started) is visible to the caller's lockout/outcome tracking: 0 for a
    # never-started port, otherwise flow over the port's actual start->stop-write window,
    # capped at the plan (ramp-up is charged at full flow, so this never under-counts).
    delivered = {}
    for p in ports:
        if p not in start_mono:
            delivered[p] = 0.0
        else:
            elapsed = max(0.0, stop_mono.get(p, time.monotonic()) - start_mono[p])
            run_ml = plans[p]["flow_ml_min"] * speed * elapsed / 60.0
            delivered[p] = round(min(plans[p]["estimated_actual_ml"], run_ml), 4)
    return {"ok": ok, "stop_results": stop_results, "started": started,
            "estimated_actual_ml_each": per_port,
            "full_strength_equivalent_ml_each": fse_each,
            "delivered_ml_each": delivered,
            "start_failed": start_failed[0] if start_failed else None, "plans": plans}


# --------------------------------------------------------------------------- #
# playbook resolution (AI selects a name; code owns speed + dose size)
# --------------------------------------------------------------------------- #
def resolve_playbook(name: str) -> dict | None:
    """Map a playbook name to {kind, speed, target_ml}. Returns None for unknown
    names (the caller should reject). Port resolution is the caller's job."""
    pb = PLAYBOOKS.get(name)
    if not pb:
        return None
    return {"kind": pb["kind"], "speed": pb["speed"], "target_ml": dose_ml(pb["ml_env"]),
            "playbook": name}
