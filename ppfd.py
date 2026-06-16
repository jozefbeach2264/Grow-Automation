"""
ppfd.py -- light / PPFD framework for the Growcraft X6 over the "4 x 4" tent.

Turns a measured PPFD map (Apogee quantum meter, full 6-inch grid across the
48x48 footprint, at each AC Infinity intensity level 1-10 and each canopy
distance) into something the control layer can use:

  - current canopy PPFD + DLI for the live light level / canopy distance
  - a per-stage DLI target and the level that would hit it
  - canopy UNIFORMITY (min/avg) -- the whole point of mapping a grid, not a point

Phase: "recommend now, control later." The advisory block (snapshot["ppfd"]) and
the level recommendation are live; actually auto-setting the level is gated behind
PPFD_CONTROL (default off) and is NOT yet wired into schedule.py, so it never
fights the schedule enforcer. Flip PPFD_CONTROL on after the map is validated.

Map file: ppfd_map.json at the repo root (committed -- it's a stable hardware
characterization, not per-grow runtime data). See ppfd_map.example.json for the schema.
Shape, position-agnostic so the exact grid count doesn't lock the schema:

  {
    "footprint_in": [48, 48], "grid_spacing_in": 6, "meter": "Apogee 510",
    "heights_in": {
      "18": { "1": {"grid": [[..],[..],..]}, ... "10": {...} },
      "24": { "1": {...}, ... }
    }
  }

`grid` is a rectangular matrix of PPFD readings (umol/m2/s). Stats are computed
over ALL cells, so a 9x9 or 8x8 grid both work.
"""

import json
import os
from pathlib import Path

_MAP_PATH = Path(__file__).parent / "ppfd_map.json"

# Per-stage DLI targets (mol/m2/day) -- conservative cannabis defaults, overridable
# via DLI_TARGET_<STAGE> in .env. Used to recommend an intensity level.
_DLI_DEFAULTS = {"seedling": 15.0, "veg": 35.0, "bloom": 45.0}


# --------------------------------------------------------------------------- #
# grid stats
# --------------------------------------------------------------------------- #
def _cells(grid) -> list[float]:
    out: list[float] = []
    for row in grid or []:
        if isinstance(row, (list, tuple)):
            out.extend(float(v) for v in row if isinstance(v, (int, float)))
        elif isinstance(row, (int, float)):  # tolerate a flat list
            out.append(float(row))
    return out


def grid_stats(grid) -> dict | None:
    """avg / min / max / center / uniformity (min/avg, 0..1) over all grid cells.
    Returns None for an empty/invalid grid."""
    cells = _cells(grid)
    if not cells:
        return None
    avg = sum(cells) / len(cells)
    mn, mx = min(cells), max(cells)
    # center cell of the matrix when rectangular; else the median-ish middle cell.
    center = avg
    if grid and isinstance(grid[0], (list, tuple)):
        r = grid[len(grid) // 2]
        if r:
            center = float(r[len(r) // 2])
    return {
        "avg": round(avg, 1),
        "min": round(mn, 1),
        "max": round(mx, 1),
        "center": round(center, 1),
        "uniformity": round(mn / avg, 3) if avg else 0.0,
        "n": len(cells),
    }


# --------------------------------------------------------------------------- #
# map loading
# --------------------------------------------------------------------------- #
def load_map(path: Path | None = None) -> dict | None:
    """Load + parse the PPFD map. Returns None when the file is missing/invalid
    (the framework then stays advisory-silent instead of crashing the loop)."""
    p = path or _MAP_PATH
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("heights_in"), dict):
        return None
    return raw


def _metric() -> str:
    m = os.getenv("PPFD_METRIC", "avg").strip().lower()
    return m if m in ("avg", "min", "center", "max") else "avg"


def _measured_heights(mp: dict) -> list[float]:
    hs = []
    for k in mp.get("heights_in", {}):
        try:
            hs.append(float(k))
        except (TypeError, ValueError):
            continue
    return sorted(hs)


def _level_stats(mp: dict, height_key: str, level: int) -> dict | None:
    block = mp.get("heights_in", {}).get(height_key, {}).get(str(level))
    if not isinstance(block, dict):
        return None
    return grid_stats(block.get("grid"))


def ppfd_for(level: int, distance_in: float, mp: dict | None = None) -> dict | None:
    """Stats for a given intensity level at a canopy distance, linearly
    interpolating between the two nearest MEASURED heights (clamped outside the
    measured range). Level 0 (light off) -> all zeros. None if no map / no data."""
    if mp is None:
        mp = load_map()
    if mp is None:
        return None
    if level <= 0:
        return {"avg": 0.0, "min": 0.0, "max": 0.0, "center": 0.0,
                "uniformity": 0.0, "n": 0, "level": 0, "distance_in": distance_in}

    heights = _measured_heights(mp)
    if not heights:
        return None
    # Clamp distance to the measured range, then pick bracketing heights.
    d = max(heights[0], min(distance_in, heights[-1]))
    lo = max([h for h in heights if h <= d], default=heights[0])
    hi = min([h for h in heights if h >= d], default=heights[-1])

    s_lo = _level_stats(mp, _fmt_h(lo, mp), level)
    s_hi = _level_stats(mp, _fmt_h(hi, mp), level)
    if s_lo is None and s_hi is None:
        return None
    if s_lo is None:
        s_lo = s_hi
    if s_hi is None:
        s_hi = s_lo

    if hi == lo:
        out = dict(s_lo)
    else:
        f = (d - lo) / (hi - lo)
        out = {}
        for k in ("avg", "min", "max", "center", "uniformity"):
            out[k] = round(s_lo[k] + (s_hi[k] - s_lo[k]) * f, 3)
        out["n"] = min(s_lo["n"], s_hi["n"])
    out["level"] = level
    out["distance_in"] = round(d, 1)
    return out


def _fmt_h(h: float, mp: dict) -> str:
    """Match a numeric height back to its exact string key in the map."""
    for k in mp.get("heights_in", {}):
        try:
            if float(k) == h:
                return k
        except (TypeError, ValueError):
            continue
    return str(int(h)) if h == int(h) else str(h)


# --------------------------------------------------------------------------- #
# DLI + level recommendation
# --------------------------------------------------------------------------- #
def dli(ppfd: float, photoperiod_hours: float) -> float:
    """Daily Light Integral (mol/m2/day) from an average PPFD + photoperiod."""
    return round(ppfd * photoperiod_hours * 3600.0 / 1e6, 2)


def stage_dli_target(stage: str) -> float:
    """Per-stage target DLI; DLI_TARGET_<STAGE> in .env overrides the default."""
    default = _DLI_DEFAULTS.get(stage, _DLI_DEFAULTS["veg"])
    try:
        return float(os.getenv(f"DLI_TARGET_{stage.upper()}", str(default)))
    except (TypeError, ValueError):
        return default


def recommend_level(target_dli: float, distance_in: float, photoperiod_hours: float,
                    mp: dict | None = None) -> dict | None:
    """Pick the intensity level (1-10) whose DLI lands closest to target_dli at
    the given canopy distance/photoperiod, using the configured metric. Returns
    {recommended_level, recommended_dli, table:[{level,ppfd,dli}], target_dli}."""
    if mp is None:
        mp = load_map()
    if mp is None:
        return None
    metric = _metric()
    table = []
    for lvl in range(1, 11):
        st = ppfd_for(lvl, distance_in, mp)
        if not st or not st.get("n"):
            continue
        ppfd = st[metric]
        table.append({"level": lvl, "ppfd": round(ppfd, 1),
                      "dli": dli(ppfd, photoperiod_hours)})
    if not table:
        return None
    best = min(table, key=lambda r: abs(r["dli"] - target_dli))
    return {
        "recommended_level": best["level"],
        "recommended_dli": best["dli"],
        "target_dli": round(target_dli, 2),
        "metric": metric,
        "table": table,
    }


# --------------------------------------------------------------------------- #
# snapshot integration (advisory)
# --------------------------------------------------------------------------- #
def _canopy_distance_in() -> float:
    try:
        return float(os.getenv("CANOPY_DISTANCE_IN", "18"))
    except (TypeError, ValueError):
        return 18.0


def _photoperiod_hours() -> float:
    try:
        return max(0.0, min(24.0, float(os.getenv("LIGHT_HOURS_ON", "18"))))
    except (TypeError, ValueError):
        return 18.0


def control_enabled() -> bool:
    return os.getenv("PPFD_CONTROL", "false").strip().lower() == "true"


def controlled_level(stage: str | None = None) -> dict | None:
    """When PPFD_CONTROL is armed AND a usable map exists, return the level the
    light should run at to hit the stage DLI target, plus context:
    {recommended_level, recommended_dli, target_dli, distance_in, stage, metric}.
    Returns None when control is disabled, no map is loaded, or the current
    level/distance isn't covered -- callers then fall back to LIGHT_INTENSITY so
    lighting NEVER breaks because the map is missing a cell."""
    if not control_enabled():
        return None
    mp = load_map()
    if mp is None:
        return None
    dist = _canopy_distance_in()
    hours = _photoperiod_hours()
    stage = stage or "veg"
    target = stage_dli_target(stage)
    rec = recommend_level(target, dist, hours, mp)
    if not rec:
        return None
    return {
        "recommended_level": rec["recommended_level"],
        "recommended_dli": rec["recommended_dli"],
        "target_dli": rec["target_dli"],
        "distance_in": dist,
        "stage": stage,
        "metric": rec["metric"],
    }


def build_ppfd_block(snapshot: dict, current_level: int | None,
                     stage: str | None = None) -> dict | None:
    """Advisory PPFD/DLI block for snapshot["ppfd"]. Reports the canopy PPFD +
    DLI for the live level/distance, the per-stage DLI target, the recommended
    level, and whether control is armed. None when no map is loaded.

    `current_level` is the live ROLE_LIGHT speed (0-10). READ-ONLY: this never
    sets the light -- auto-control is gated by PPFD_CONTROL and intentionally
    left unwired from schedule.py for now."""
    mp = load_map()
    if mp is None:
        return None
    dist = _canopy_distance_in()
    hours = _photoperiod_hours()
    stage = stage or (snapshot.get("grow_stage") if snapshot else None) or "veg"
    metric = _metric()

    cur = ppfd_for(current_level or 0, dist, mp)
    target = stage_dli_target(stage)
    rec = recommend_level(target, dist, hours, mp)

    block = {
        "level": current_level,
        "distance_in": dist,
        "photoperiod_hours": hours,
        "metric": metric,
        "stage": stage,
        "target_dli": round(target, 2),
        "control_armed": control_enabled(),
    }
    if cur:
        block["ppfd"] = cur.get(metric)
        block["ppfd_avg"] = cur.get("avg")
        block["ppfd_min"] = cur.get("min")
        block["uniformity"] = cur.get("uniformity")
        block["dli"] = dli(cur.get(metric, 0.0), hours)
    if rec:
        block["recommended_level"] = rec["recommended_level"]
        block["recommended_dli"] = rec["recommended_dli"]
        block["level_table"] = rec["table"]
    return block
