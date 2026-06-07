"""
Persistent safety state for the grow controller.

Two independent safety concepts, deliberately kept separate:

1. **dosing_disabled** -- a CHEMICAL-ONLY freeze. Blocks doser + pH ports while
   leaving climate (lights, fans, exhaust, CO2) running normally. This is the
   trip used for routine safety failures (a failed pump-stop verification, a
   manual "stop dosing while I work on the res"). Killing ventilation or lighting
   is itself a hazard, so a chemical fault must NEVER cascade into a climate kill.

2. **Reservoir-burst response (WATER/CHEMICAL ONLY)** -- lives in the res-burst path
   (`ai_advisor.compute_res_burst` + `poller.enforce_res_burst`), NOT here. On a
   confirmed leak it stops dosers + closes the CO2 valve (and trips the dosing freeze
   below), plus an optional evac pump (`compute_evac_pump`). Lights and ventilation
   are NEVER cut -- there is deliberately no "full power kill" anywhere in the system.

State file: profiles/.safety_state.json   (atomic tmp+replace, corrupt-tolerant)
  {
    "dosing_disabled": false,
    "dosing_disabled_reason": null,
    "dosing_disabled_at": null          # unix ts when it tripped
  }

The env var DOSING_DISABLED=true is a coarse manual override that forces dosing
off regardless of the file (OR'd with the persisted flag). Clearing dosing
requires both: env unset AND clear_dosing_disable() / file flag false.
"""

import json
import os
import time
from pathlib import Path

_STATE_FILE = Path(__file__).parent / "profiles" / ".safety_state.json"

_DEFAULT = {
    "dosing_disabled": False,
    "dosing_disabled_reason": None,
    "dosing_disabled_at": None,
}


def _load() -> dict:
    """Read state from disk. Missing or corrupt file -> safe default (not disabled).
    A corrupt file does NOT auto-disable dosing -- it would be indistinguishable
    from a fresh install, and the env override / explicit trips remain available."""
    if not _STATE_FILE.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(_STATE_FILE.read_text())
        if not isinstance(data, dict):
            return dict(_DEFAULT)
    except Exception as e:
        print(f"[SAFETY] Could not read {_STATE_FILE.name} ({e}) -- assuming not disabled")
        return dict(_DEFAULT)
    merged = dict(_DEFAULT)
    merged.update({k: data[k] for k in _DEFAULT if k in data})
    return merged


def _save(state: dict) -> None:
    """Atomic write. Failure is logged but not fatal (in-memory caller continues)."""
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(_STATE_FILE)
    except Exception as e:
        print(f"[SAFETY] Could not write {_STATE_FILE.name}: {e}")


def _env_dosing_disabled() -> bool:
    return os.getenv("DOSING_DISABLED", "").strip().lower() == "true"


def dosing_disable_status() -> tuple[bool, str | None]:
    """Return (disabled, reason). True if EITHER the env override or the persisted
    flag is set. Env override reports a fixed reason; otherwise the stored reason."""
    if _env_dosing_disabled():
        return True, "DOSING_DISABLED=true (env override)"
    state = _load()
    if state.get("dosing_disabled"):
        return True, state.get("dosing_disabled_reason") or "dosing_disabled (persisted)"
    return False, None


def is_dosing_disabled() -> bool:
    return dosing_disable_status()[0]


def disable_dosing(reason: str) -> None:
    """Trip the chemical-only freeze and persist it. Idempotent; refreshes reason.
    Use for fail-safe auto-trips (e.g. a doser stop that could not be verified) and
    for manual stops. Requires an explicit clear_dosing_disable() to lift."""
    state = _load()
    already = state.get("dosing_disabled")
    state["dosing_disabled"] = True
    state["dosing_disabled_reason"] = reason
    if not already:
        state["dosing_disabled_at"] = time.time()
    _save(state)
    print(f"[SAFETY] Dosing DISABLED -- {reason}. Clear with clear_dosing_disable() "
          "after inspection. (Climate/ventilation unaffected.)")


def clear_dosing_disable() -> None:
    """Manually lift the persisted chemical freeze after inspection. Note: an active
    DOSING_DISABLED=true env override still keeps dosing off until that is unset."""
    state = _load()
    state["dosing_disabled"] = False
    state["dosing_disabled_reason"] = None
    state["dosing_disabled_at"] = None
    _save(state)
    if _env_dosing_disabled():
        print("[SAFETY] Persisted dosing freeze cleared, but DOSING_DISABLED=true is "
              "still set in env -- dosing remains blocked until you unset it.")
    else:
        print("[SAFETY] Dosing re-enabled.")
