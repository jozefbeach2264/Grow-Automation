#!/usr/bin/env python3
"""
ppfd_capture.py -- interactive ingest for the PPFD map (Apogee 510).

Walks you through recording a PPFD grid for one intensity level at one canopy
height and writes it into ppfd_map.json at the repo root (creating/merging as needed).
Re-run per level/height; partial maps are fine -- ppfd.py skips missing levels.

Usage:
  python3 ppfd_capture.py                 # interactive
  python3 ppfd_capture.py --height 18 --level 7   # jump straight in

Enter the grid row by row (space-separated PPFD readings), one line per row,
blank line to finish the grid. Example for a 9x9 at 6in over 48x48:
  720 690 ... (9 numbers)
  ... (9 rows)
"""

import argparse
import json
import sys
from pathlib import Path

_MAP_PATH = Path(__file__).parent / "ppfd_map.json"


def parse_grid_rows(lines: list[str]) -> list[list[float]]:
    """Parse space/comma-separated numeric rows into a rectangular grid.
    Raises ValueError on non-numeric tokens or ragged rows."""
    grid: list[list[float]] = []
    for ln in lines:
        ln = ln.strip().replace(",", " ")
        if not ln:
            continue
        row = [float(tok) for tok in ln.split()]
        grid.append(row)
    if not grid:
        raise ValueError("empty grid")
    width = len(grid[0])
    if any(len(r) != width for r in grid):
        raise ValueError(f"ragged grid -- every row must have {width} readings")
    return grid


def _load() -> dict:
    try:
        return json.loads(_MAP_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "light": "Growcraft X6", "controller": "4 x 4", "meter": "Apogee 510",
            "footprint_in": [48, 48], "grid_spacing_in": 6, "heights_in": {},
        }


def _save(mp: dict) -> None:
    _MAP_PATH.parent.mkdir(exist_ok=True)
    _MAP_PATH.write_text(json.dumps(mp, indent=2))


def _stats(grid):
    # Local copy of ppfd.grid_stats to keep this tool standalone.
    cells = [float(v) for row in grid for v in row]
    avg = sum(cells) / len(cells)
    return (round(avg, 1), round(min(cells), 1), round(max(cells), 1),
            round(min(cells) / avg, 3) if avg else 0.0)


def main():
    ap = argparse.ArgumentParser(description="Record a PPFD grid into the map.")
    ap.add_argument("--height", type=str, help="canopy distance in inches (e.g. 18)")
    ap.add_argument("--level", type=int, help="AC Infinity intensity level 1-10")
    args = ap.parse_args()

    mp = _load()
    import time

    while True:
        height = args.height or input("\nCanopy distance (inches, e.g. 18) [blank=quit]: ").strip()
        if not height:
            break
        level = args.level or input("Intensity level (1-10): ").strip()
        try:
            level_i = int(level)
            assert 1 <= level_i <= 10
        except (ValueError, AssertionError):
            print("  level must be an integer 1-10")
            args.level = None
            continue

        print(f"Enter the grid for height={height}in level={level_i}, "
              "one row per line (space-separated), blank line to finish:")
        lines = []
        while True:
            try:
                ln = input()
            except EOFError:
                break
            if not ln.strip():
                break
            lines.append(ln)
        try:
            grid = parse_grid_rows(lines)
        except ValueError as e:
            print(f"  rejected: {e}")
            args.height = args.level = None
            continue

        avg, mn, mx, uni = _stats(grid)
        print(f"  -> {len(grid)}x{len(grid[0])} grid | avg={avg} min={mn} max={mx} "
              f"uniformity(min/avg)={uni}")
        if input("  save this? [Y/n]: ").strip().lower() in ("n", "no"):
            args.height = args.level = None
            continue

        mp.setdefault("heights_in", {}).setdefault(str(height), {})[str(level_i)] = {
            "grid": grid, "captured": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(mp)
        print(f"  saved to {_MAP_PATH}")
        args.height = args.level = None   # next loop prompts fresh

    print("done.")


if __name__ == "__main__":
    main()
