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

State file: profiles/.safety_state.json   (atomic tmp+replace; missing -> not
disabled, corrupt -> DISABLED and quarantined -- see _load)
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

import utils

_STATE_FILE = Path(__file__).parent / "profiles" / ".safety_state.json"

_DEFAULT = {
    "dosing_disabled": False,
    "dosing_disabled_reason": None,
    "dosing_disabled_at": None,
}


def _load() -> dict:
    """Read state from disk.

    MISSING file -> not disabled. That is a fresh install: there has never been a
    trip to lose, and auto-freezing every new deployment would only train operators
    to clear the freeze reflexively.

    CORRUPT file -> DISABLED (fail closed). A file that EXISTS but will not parse
    means the state was lost, and the state we can least afford to lose is a trip --
    reading it as "not disabled" silently un-freezes chemicals after exactly the kind
    of event (power loss mid-write, disk fault) that most warrants a look. Corruption
    is distinguishable from a fresh install precisely because the file is there, so
    the fresh-install argument above does not apply. The bad file is preserved for
    inspection, the freeze is persisted so it survives restart, and
    clear_dosing_disable() still lifts it once a human has looked."""
    if not _STATE_FILE.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(_STATE_FILE.read_text())
        problem = None if isinstance(data, dict) else "state file is not a JSON object"
    except Exception as e:
        problem = f"state file is unreadable ({e})"
    if problem is not None:
        return _trip_on_corruption(problem)
    merged = dict(_DEFAULT)
    merged.update({k: data[k] for k in _DEFAULT if k in data})
    return merged


def _trip_on_corruption(problem: str) -> dict:
    """Fail-closed response to an unparseable state file: preserve the evidence,
    persist a tripped state over the live path, and return it.

    If the persist fails, the corrupt file is still there (preserve_corrupt copies
    rather than moves), so the next _load() trips again -- the freeze can degrade to
    "re-decided every read", never to "silently lifted"."""
    note = utils.preserve_corrupt(_STATE_FILE, problem)
    state = {
        "dosing_disabled": True,
        "dosing_disabled_reason": f"fail-closed: {note}",
        "dosing_disabled_at": time.time(),
    }
    print(f"[SAFETY] {note} -- chemical control DISABLED (fail closed). Inspect the "
          "preserved copy, then clear_dosing_disable(). (Climate/ventilation unaffected.)")
    _save(state)
    return state


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
