#!/usr/bin/env python3
"""
Self-tests for the PPFD/light framework (ppfd.py + ppfd_capture parsing). No
hardware: a synthetic map is written to a temp file. Run: python3 ppfd_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ppfd
import ppfd_capture

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")


_TMP = Path(tempfile.mkdtemp(prefix="ppfd_test_"))


def write_map(heights):
    """heights = {dist: {level: grid}} -> write a map file, point ppfd at it."""
    mp = {"footprint_in": [48, 48], "grid_spacing_in": 6, "heights_in": {}}
    for dist, levels in heights.items():
        mp["heights_in"][str(dist)] = {
            str(lvl): {"grid": grid} for lvl, grid in levels.items()}
    p = _TMP / "ppfd_map.json"
    p.write_text(json.dumps(mp))
    ppfd._MAP_PATH = p
    return ppfd.load_map(p)


def reset_env(**env):
    for k in ("PPFD_METRIC", "CANOPY_DISTANCE_IN", "LIGHT_HOURS_ON",
              "PPFD_CONTROL", "DLI_TARGET_VEG", "DLI_TARGET_BLOOM"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


# A simple uniform grid helper (all cells == value).
def uni(value, n=3):
    return [[value] * n for _ in range(n)]


# --- grid_stats --------------------------------------------------------------

def test_grid_stats():
    s = ppfd.grid_stats([[100, 200, 300], [200, 400, 200], [300, 200, 100]])
    check("avg computed", s["avg"] == round(2000 / 9, 1))
    check("min/max", s["min"] == 100 and s["max"] == 400)
    check("center cell", s["center"] == 400.0)
    check("uniformity = min/avg", s["uniformity"] == round(100 / (2000 / 9), 3))
    check("cell count", s["n"] == 9)


def test_grid_stats_empty():
    check("empty grid -> None", ppfd.grid_stats([]) is None)
    check("None grid -> None", ppfd.grid_stats(None) is None)


# --- load_map ----------------------------------------------------------------

def test_load_missing():
    check("missing file -> None", ppfd.load_map(_TMP / "nope.json") is None)


def test_load_valid():
    mp = write_map({18: {5: uni(500)}})
    check("valid map loads", mp is not None and "heights_in" in mp)


# --- ppfd_for + interpolation -----------------------------------------------

def test_ppfd_exact_height():
    mp = write_map({18: {5: uni(500)}, 24: {5: uni(300)}})
    s = ppfd.ppfd_for(5, 18, mp)
    check("exact 18in returns measured avg", s["avg"] == 500.0)
    check("distance echoed", s["distance_in"] == 18)


def test_ppfd_interpolation():
    mp = write_map({18: {5: uni(500)}, 24: {5: uni(300)}})
    s = ppfd.ppfd_for(5, 21, mp)   # midpoint -> 400
    check("21in interpolates to midpoint avg 400", s["avg"] == 400.0)


def test_ppfd_clamps_outside_range():
    mp = write_map({18: {5: uni(500)}, 24: {5: uni(300)}})
    check("below range clamps to nearest (18->500)", ppfd.ppfd_for(5, 12, mp)["avg"] == 500.0)
    check("above range clamps to nearest (24->300)", ppfd.ppfd_for(5, 30, mp)["avg"] == 300.0)


def test_ppfd_level_off():
    mp = write_map({18: {5: uni(500)}})
    s = ppfd.ppfd_for(0, 18, mp)
    check("level 0 (off) -> zero PPFD", s["avg"] == 0.0)


def test_ppfd_missing_level():
    mp = write_map({18: {5: uni(500)}})
    check("unmeasured level -> None", ppfd.ppfd_for(7, 18, mp) is None)


# --- DLI ---------------------------------------------------------------------

def test_dli_math():
    # 400 PPFD * 18h * 3600 / 1e6 = 25.92
    check("DLI formula", ppfd.dli(400, 18) == 25.92)
    check("zero PPFD -> 0 DLI", ppfd.dli(0, 18) == 0.0)


def test_stage_target_override():
    reset_env(DLI_TARGET_VEG="40")
    check("env overrides stage DLI target", ppfd.stage_dli_target("veg") == 40.0)
    reset_env()
    check("default veg DLI target", ppfd.stage_dli_target("veg") == 35.0)


# --- recommend_level ---------------------------------------------------------

def test_recommend_level():
    reset_env(PPFD_METRIC="avg")
    # PPFD scales with level: level L -> 100*L avg. At 18h photoperiod.
    mp = write_map({18: {lvl: uni(100 * lvl) for lvl in range(1, 11)}})
    # target 25.92 DLI == 400 PPFD == level 4
    rec = ppfd.recommend_level(25.92, 18, 18, mp)
    check("recommends the level closest to target DLI", rec["recommended_level"] == 4)
    check("table covers all measured levels", len(rec["table"]) == 10)
    check("table carries level/ppfd/dli", set(rec["table"][0]) == {"level", "ppfd", "dli"})


def test_recommend_metric_min():
    reset_env(PPFD_METRIC="min")
    # Non-uniform grids: min is much lower than avg -> recommendation uses min.
    grids = {lvl: [[100 * lvl, 100 * lvl, 0]] for lvl in range(1, 11)}  # min=0 for all
    mp = write_map({18: grids})
    rec = ppfd.recommend_level(10, 18, 18, mp)
    check("metric=min honored (all min=0 -> level 1 closest)", rec["metric"] == "min")


# --- build_ppfd_block (advisory) --------------------------------------------

def test_build_block():
    reset_env(PPFD_METRIC="avg", CANOPY_DISTANCE_IN="18", LIGHT_HOURS_ON="18", PPFD_CONTROL="false")
    write_map({18: {lvl: uni(100 * lvl) for lvl in range(1, 11)},
               24: {lvl: uni(80 * lvl) for lvl in range(1, 11)}})
    block = ppfd.build_ppfd_block({"grow_stage": "veg"}, current_level=7)
    check("block reports current level", block["level"] == 7)
    check("block PPFD for level 7 @18in", block["ppfd"] == 700.0)
    check("block DLI computed", block["dli"] == ppfd.dli(700, 18))
    check("block has stage target", block["target_dli"] == 35.0)
    check("block recommends a level", "recommended_level" in block)
    check("control not armed by default", block["control_armed"] is False)
    check("uniformity surfaced", "uniformity" in block)


def test_build_block_no_map():
    ppfd._MAP_PATH = _TMP / "absent.json"
    check("no map -> None block", ppfd.build_ppfd_block({"grow_stage": "veg"}, 5) is None)


def test_control_gate():
    reset_env(PPFD_CONTROL="true")
    check("PPFD_CONTROL=true arms control", ppfd.control_enabled() is True)
    reset_env()
    check("control off by default", ppfd.control_enabled() is False)


def test_controlled_level():
    # level L -> 100*L avg @18in; veg target 35 DLI @18h == 25.92->.. closest is level 4 (400 PPFD=25.92).
    write_map({18: {lvl: uni(100 * lvl) for lvl in range(1, 11)}})
    reset_env(PPFD_CONTROL="false")
    check("controlled_level None when disarmed", ppfd.controlled_level("veg") is None)
    reset_env(PPFD_CONTROL="true", PPFD_METRIC="avg", CANOPY_DISTANCE_IN="18",
              LIGHT_HOURS_ON="18", DLI_TARGET_VEG="25.92")
    rec = ppfd.controlled_level("veg")
    check("controlled_level returns recommendation when armed", rec is not None)
    check("controlled_level picks level for target DLI", rec and rec["recommended_level"] == 4)
    check("controlled_level carries stage + distance", rec["stage"] == "veg" and rec["distance_in"] == 18.0)


def test_controlled_level_no_map():
    ppfd._MAP_PATH = _TMP / "absent.json"
    reset_env(PPFD_CONTROL="true")
    check("controlled_level None when no map (lighting falls back)", ppfd.controlled_level("veg") is None)


# --- capture parsing ---------------------------------------------------------

def test_parse_grid_rows():
    g = ppfd_capture.parse_grid_rows(["100 200 300", "400 500 600"])
    check("parses rows", g == [[100, 200, 300], [400, 500, 600]])
    g2 = ppfd_capture.parse_grid_rows(["100, 200, 300"])
    check("tolerates commas", g2 == [[100, 200, 300]])


def test_parse_grid_ragged():
    try:
        ppfd_capture.parse_grid_rows(["1 2 3", "4 5"])
        check("ragged grid rejected", False)
    except ValueError:
        check("ragged grid rejected", True)


def main():
    print("PPFD / light framework self-tests")
    print("=" * 44)
    for fn in (
        test_grid_stats,
        test_grid_stats_empty,
        test_load_missing,
        test_load_valid,
        test_ppfd_exact_height,
        test_ppfd_interpolation,
        test_ppfd_clamps_outside_range,
        test_ppfd_level_off,
        test_ppfd_missing_level,
        test_dli_math,
        test_stage_target_override,
        test_recommend_level,
        test_recommend_metric_min,
        test_build_block,
        test_build_block_no_map,
        test_control_gate,
        test_controlled_level,
        test_controlled_level_no_map,
        test_parse_grid_rows,
        test_parse_grid_ragged,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
