#!/usr/bin/env python3
"""
ppfd_build_map.py -- build a SMOOTHED, PREDICTIVE PPFD map for every intensity
level at every measured height, from the raw Apogee 510 readings.

RAW MEASUREMENTS (Growcraft X6 over the "4 x 4", back-left -> right, row by row):
  24in canopy: full 7x7 grids at L1, L3*, L7; center+corner only at L2, L4, L5, L6.
  16in canopy: full grids at L5, L6, L7* (front row not recorded -> 6x7).
  (*partial/ragged grids -- modeled from their center+corner anyway.)

MODEL
  1. SHAPE: one normalized beam shape (center=1, corner=0), 4-fold symmetric + lightly
     blurred, from the clean 7x7 grids. The *spatial* bump comes from the real fixture.
  2. MAGNITUDE: each level's CENTER PPFD. Measured where we have it. Levels measured at
     only one height get the other height PREDICTED via the center-ratio between heights
     (16in/24in ~1.19, derived from the overlap levels 5/6/7).
  3. UNIFORMITY: CORNER = center * (corner/center ratio for that height: ~0.74 @24in,
     ~0.62 @16in -- closer hangs are hotspottier). Measured corner used where available.
  4. RENDER: grid[i][j] = corner + (center - corner) * shape[i][j], a clean 7x7.

Every grid lands in ppfd_map.json with a `source` tag (measured / predicted) so the
control layer and a human can see what's real vs modeled. ppfd.py interpolates any
canopy distance between the measured heights. Levels 8-10 are intentionally left out --
they're the operator's "run it and read the plants" zone.

Re-run after any new readings:  python3 ppfd_build_map.py
"""

import json
import time
from pathlib import Path

_MAP = Path(__file__).parent / "ppfd_map.json"
_N = 7  # output grid is 7x7

# --------------------------------------------------------------------------- #
# RAW readings
# --------------------------------------------------------------------------- #
GRIDS_24 = {
    1: [[200, 210, 220, 230, 225, 230, 220], [205, 220, 230, 250, 245, 240, 215],
        [230, 235, 250, 265, 270, 265, 245], [240, 250, 270, 280, 285, 285, 260],
        [225, 240, 265, 280, 270, 260, 250], [229, 230, 250, 265, 250, 245, 230],
        [200, 215, 235, 250, 245, 260, 210]],
    3: [[390, 425, 440, 460, 465, 470, 440], [410, 440, 470, 490, 500, 480, 440],   # 6x7
        [470, 490, 525, 535, 535, 530, 500], [480, 500, 530, 550, 560, 550, 520],
        [490, 510, 530, 550, 550, 535, 500], [450, 480, 500, 515, 515, 500, 475]],
    7: [[740, 840, 880, 925, 915, 910, 850], [830, 870, 920, 960, 960, 940, 880],
        [960, 980, 1050, 1100, 1100, 1080, 980], [960, 1000, 1050, 1100, 1100, 1090, 1020],
        [875, 980, 1050, 1100, 1100, 1080, 920], [930, 1010, 1090, 1100, 1120, 1100, 950],
        [800, 880, 969, 980, 870, 840, 850]],
}
SUMM_24 = {2: (330, 420), 4: (550, 730), 5: (650, 850), 6: (800, 1000)}  # (corner_avg, center)

GRIDS_16 = {  # 6x7 -- front row not recorded
    5: [[620, 720, 730, 730, 730, 710, 650], [710, 820, 870, 900, 900, 870, 780],
        [850, 920, 990, 1010, 1020, 980, 940], [860, 920, 990, 1000, 1020, 1010, 970],
        [790, 800, 850, 910, 910, 880, 780], [560, 620, 700, 765, 750, 700, 640]],
    6: [[730, 830, 860, 850, 860, 850, 810], [870, 980, 1000, 1040, 1020, 1000, 930],
        [990, 1080, 1150, 1175, 1170, 1150, 1070], [1020, 1080, 1140, 1180, 1180, 1170, 1141],
        [830, 880, 990, 1030, 1030, 1030, 940], [670, 730, 740, 810, 860, 760, 750]],
}
SUMM_16 = {7: (822, 1350)}  # L7 grid came in ragged -> use its center + corner average

# --------------------------------------------------------------------------- #
# shape + render
# --------------------------------------------------------------------------- #
def _corners(g):
    return [g[0][0], g[0][-1], g[-1][0], g[-1][-1]]


def anchors(g):
    """(center_cell, corner_average) for any rectangular measured grid."""
    mid = g[len(g) // 2]
    return float(mid[len(mid) // 2]), sum(_corners(g)) / 4.0


def _normalize(g):
    c, k = anchors(g)
    span = (c - k) or 1.0
    return [[(v - k) / span for v in row] for row in g]


def _symmetrize(s):
    n, m = len(s), len(s[0])
    return [[(s[i][j] + s[i][m - 1 - j] + s[n - 1 - i][j] + s[n - 1 - i][m - 1 - j]) / 4.0
             for j in range(m)] for i in range(n)]


def _blur(s):
    n, m = len(s), len(s[0])
    out = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            tot = cnt = 0
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    tot += s[min(max(i + di, 0), n - 1)][min(max(j + dj, 0), m - 1)]
                    cnt += 1
            out[i][j] = tot / cnt
    return out


def master_shape():
    """Normalized (center=1, corner=0) beam shape from the clean 7x7 grids."""
    full = [g for g in GRIDS_24.values() if len(g) == _N and len(g[0]) == _N]
    norms = [_symmetrize(_normalize(g)) for g in full]
    s = [[sum(nm[i][j] for nm in norms) / len(norms) for j in range(_N)] for i in range(_N)]
    s = _blur(_symmetrize(s))
    c = s[_N // 2][_N // 2]
    k = sum(_corners(s)) / 4.0
    span = (c - k) or 1.0
    return [[(v - k) / span for v in row] for row in s]


def render(center, corner, shape):
    return [[round(corner + (center - corner) * shape[i][j], 1) for j in range(_N)]
            for i in range(_N)]


# --------------------------------------------------------------------------- #
# per-height level -> (center, corner, source)
# --------------------------------------------------------------------------- #
def _measured(grids, summ, partial=False):
    out = {}
    for L, g in grids.items():
        c, k = anchors(g)
        out[L] = (c, k, "measured:grid")
    for L, (k, c) in summ.items():
        out[L] = (c, k, "measured:center+corner" + ("(partial)" if partial else ""))
    return out


def build():
    shape = master_shape()
    A24 = _measured(GRIDS_24, SUMM_24)
    A16 = _measured(GRIDS_16, SUMM_16, partial=True)

    overlap = sorted(set(A16) & set(A24))
    fac = sum(A16[L][0] / A24[L][0] for L in overlap) / len(overlap)   # 16/24 center ratio
    r16 = sum(A16[L][1] / A16[L][0] for L in A16) / len(A16)           # 16in corner/center

    mp = json.loads(_MAP.read_text()) if _MAP.exists() else {}
    mp.setdefault("light", "Growcraft X6")
    mp.setdefault("controller", "4 x 4")
    mp.setdefault("meter", "Apogee 510")
    mp.setdefault("footprint_in", [48, 48])

    H = {}
    for hkey, A, base in (("24", A24, None), ("16", A16, A24)):
        node = {}
        for L in range(1, 8):
            if L in A:
                c, k, src = A[L]
            else:  # predict from the other height's measured curve
                c = base[L][0] * fac
                k = c * r16
                src = f"predicted:24in_x{fac:.3f}"
            node[str(L)] = {
                "grid": render(c, k, shape),
                "center": round(c, 1), "corner": round(k, 1),
                "source": src, "built": time.strftime("%Y-%m-%d"),
            }
        H[hkey] = node
    mp["heights_in"] = H
    mp["notes"] = [
        f"Predictive smoothed map (built {time.strftime('%Y-%m-%d')}). One fixture beam-shape "
        "from the clean 7x7 grids; per-level magnitude anchored to measurements.",
        f"16in/24in center-ratio = {fac:.3f} (overlap levels {overlap}); 16in corner/center ~ {r16:.2f}.",
        "source: measured = real grid or center+corner reading; predicted = modeled from the "
        "other height. Levels 8-10 intentionally omitted -- operator 'run-it-and-read' zone.",
    ]
    _MAP.write_text(json.dumps(mp, indent=2))

    # report
    print(f"16in/24in center-ratio = {fac:.3f}   16in corner/center = {r16:.2f}\n")
    for hkey in ("16", "24"):
        print(f"--- {hkey}in ---  level | avg | center | min | uni | DLI@12h | source")
        for L in range(1, 8):
            n = H[hkey][str(L)]
            cells = [v for row in n["grid"] for v in row]
            a = sum(cells) / len(cells)
            print(f"   L{L}: {a:6.0f} {n['center']:7.0f} {min(cells):6.0f} "
                  f"{min(cells)/a:5.2f} {a*0.0432:8.1f}   [{n['source']}]")


if __name__ == "__main__":
    build()
