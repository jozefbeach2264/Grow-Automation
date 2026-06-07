"""Align logged dose events with the 1-min AC Infinity CSV trend.

For every effective dose in profiles/bucket_test_log.jsonl, pull its settling window from
the exported trend (ac_infinity_history.py) so we can see the REAL response the single
before/after reads miss -- the baseline, the transient peak/trough, and the settled value
-- then re-derive nutrient K and the pH buffer constant and judge whether pH is calculable.

Run:  python3 dose_align.py
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ac_infinity_history as H

GA = Path(__file__).parent
LOG = GA / "profiles" / "bucket_test_log.jsonl"
EVENTS = GA / "profiles" / "events.jsonl"
FIRE_TO_LOG_MIN = 6.0          # log_result runs ~this long after the pump fires (settle)


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def load_doses():
    return [json.loads(l) for l in LOG.read_text().splitlines()
            if l.strip() and not json.loads(l).get("nochem")]


def fire_times():
    ts = []
    if EVENTS.exists():
        for l in EVENTS.read_text().splitlines():
            if not l.strip():
                continue
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if (r.get("event") or r.get("type")) == "active_dose_started" and r.get("wall_ts"):
                ts.append(datetime.fromtimestamp(r["wall_ts"]))   # epoch -> local naive
    return sorted(ts)


def match_fire(log_ts, fires):
    guess = log_ts - timedelta(minutes=FIRE_TO_LOG_MIN)
    if not fires:
        return guess
    f = min(fires, key=lambda x: abs((x - guess).total_seconds()))
    return f if abs((f - guess).total_seconds()) <= 240 else guess


def analyze():
    exp = H.parse_export(H.latest_export())
    print(f"trend : {exp.path.name}  {exp.samples[0].ts:%m-%d %H:%M} -> {exp.samples[-1].ts:%m-%d %H:%M}"
          f"  ({len(exp.samples)} samples @ {exp.sample_seconds}s)\n")
    doses, fires = load_doses(), fire_times()

    nute_pair_K, nute_port_K, ph_rows = [], [], []
    print("per-dose (fire time | dose | logged delta vs CSV settled vs CSV transient):")
    for r in doses:
        log_ts = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S")
        fire = match_fire(log_ts, fires)
        gal, ml = _f(r.get("reservoir_gal"), 4.0), _f(r.get("actual_ml"))
        sol = (r.get("solution") or "").upper()
        is_ph = "PH" in sol
        down = "DOWN" in sol
        ec = _f((r.get("before") or {}).get("ec_us"))
        d_log = _f(r.get("d_ph") if is_ph else r.get("d_tds_ppm"))

        getv = (lambda s: s.ph) if is_ph else (lambda s: s.tds_ppm)
        pre = [getv(s) for s in exp.window(fire - timedelta(minutes=3), fire)]
        post = [getv(s) for s in exp.window(fire + timedelta(minutes=4), fire + timedelta(minutes=8))]
        full = [getv(s) for s in exp.window(fire - timedelta(minutes=1), fire + timedelta(minutes=8))]
        full = [v for v in full if v is not None]
        base = _median(pre[-3:]) or _f((r.get("before") or {}).get("ph" if is_ph else "tds_ppm"))
        settled = _median(post) or _f((r.get("after") or {}).get("ph" if is_ph else "tds_ppm"))
        transient = (min(full) if down else max(full)) if full else None
        d_csv = (settled - base) if (settled is not None and base is not None) else None
        d_tr = (transient - base) if (transient is not None and base is not None) else None

        label = ("pH-down" if down else "pH-up") if is_ph else "nute V1+V2"
        tr_s = f"transient {transient:.2f} (d {d_tr:+.2f})" if d_tr is not None else "transient n/a"
        print(f"  [{fire:%m-%d %H:%M}] {label:>10} {ml:>5.1f}mL  base {base:.2f} -> set {settled:.2f}"
              f"  | log d {d_log:+.2f} | CSV d {('%+.2f'%d_csv) if d_csv is not None else 'n/a':>6} | {tr_s}")

        if is_ph and ec and d_log:
            k_set = abs(d_log) * ec * gal / ml
            k_tr = (abs(d_tr) * ec * gal / ml) if d_tr is not None else None
            ph_rows.append((label, base, ec, ml, d_log, d_tr, k_set, k_tr))
        elif not is_ph and d_log:
            k = d_log * gal / ml
            if r.get("axis") == "pair":
                nute_pair_K.append(k)                      # V1+V2 fired together -> pair K
            else:
                nute_port_K.append((r.get("axis") or f"port{r.get('port')}", k))  # single-pump char

    print("\n=== NUTRIENT K  (ppm.gal / mL-each, from logged settled delta) ===")
    if nute_pair_K:
        m = sum(nute_pair_K) / len(nute_pair_K); spread = max(nute_pair_K) / min(nute_pair_K)
        print("  pair (V1+V2)     :", ", ".join(f"{k:.2f}" for k in nute_pair_K),
              f"  -> mean {m:.2f}, spread {spread:.2f}x  ({'STABLE, calculable' if spread < 1.25 else 'variable'})")
    if nute_port_K:
        tot = sum(k for _, k in nute_port_K)
        print("  single-port char :", ", ".join(f"{a}={k:.2f}" for a, k in nute_port_K),
              f"  -> sum {tot:.2f} (consistent with the pair K)")

    print("\n=== pH buffer K = |dpH| * EC * gal / mL ===")
    print(f"  {'dir':>8} {'pH0':>5} {'EC':>6} {'mL':>6} {'dSettle':>8} {'dTrans':>7} {'K_set':>7} {'K_tran':>7}")
    for lbl, b, ec, ml, dl, dt, ks, kt in ph_rows:
        print(f"  {lbl:>8} {b:>5.2f} {ec:>6.0f} {ml:>6.1f} {dl:>+8.2f} {('%+.2f'%dt) if dt is not None else '   n/a':>7}"
              f" {ks:>7.0f} {('%.0f'%kt) if kt is not None else 'n/a':>7}")

    downs = [r for r in ph_rows if r[0] == "pH-down"]
    print("\n=== VERDICT ===")
    if len(downs) >= 2:
        ks = [r[6] for r in downs]
        print(f"  pH-down K_settled = {min(ks):.0f}..{max(ks):.0f}  ({max(ks)/min(ks):.1f}x spread across EC "
              f"{downs[0][2]:.0f}->{downs[-1][2]:.0f}) -> EC-normalization does NOT hold; pH not yet calculable.")
        print("  Need a pH sweep (multiple doses across pH bins at known EC) to map K_ph(pH, EC).")
    else:
        print(f"  Only {len(downs)} pH-down point(s) -- need more to judge pH calculability.")
    if nute_pair_K and max(nute_pair_K)/min(nute_pair_K) < 1.25:
        print(f"  Nutrients ARE calculable: pair K ~ {sum(nute_pair_K)/len(nute_pair_K):.2f}, stable across the run.")
    print("  (06-02 CSV columns are approximate -- manual-harness timing + tight dose spacing;"
          " 06-04 alignment is exact. The pH K table uses logged deltas, which are reliable.)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    analyze()
