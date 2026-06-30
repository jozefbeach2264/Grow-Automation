#!/usr/bin/env python3
"""
Self-tests for the AC Infinity CSV export loader + merged trend store
(ac_infinity_history). No hardware and no real files: every test synthesizes
app-format CSVs (with the BOM / CRLF / blank-row / quoted-comma / narrow-NBSP
quirks) into a temp incoming dir and ingests into a temp store + archive, so the
real trend_data/ store is never touched.

Run: python3 ac_infinity_history_test.py
"""

import os
os.environ["TREND_DB_ENABLED"] = "false"   # these tests cover the JSONL path only; no Postgres
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ac_infinity_history as H

_PASS = 0
_FAIL = 0
NBSP = " "             # the narrow no-break space the app injects before AM/PM
_BASE = datetime(2026, 6, 4, 14, 0, 0)


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")


def make_csv(path: Path, device: str, start_min: int, n: int,
             ph0: float = 6.60, tds0: int = 420) -> Path:
    """Write an app-format export: metadata block, blank line, Time header, then
    1-min rows with a blank separator row between each. The timestamp carries a
    comma and a NBSP before AM/PM, so it is quoted exactly like the real files.
    Encoded utf-8-sig (BOM) with CRLF endings to mirror the real exports."""
    lines = [
        f"Device ID,{device}",
        "Sample Frequency,1 MIN",
        "",
        ("Time,PH (Sensor 1),TDS (Sensor 1),WATER TEMP (Sensor 1),"
         "Water Detect (Sensor 2),Temperature (Outside),"
         "Relative Humidity (Outside),VPD (Outside)"),
        "",
    ]
    for i in range(n):
        t = _BASE + timedelta(minutes=start_min + i)
        ts = t.strftime("%m/%d/%Y, %I:%M:%S") + NBSP + t.strftime("%p")
        lines.append(f'"{ts}",{ph0:.2f},{tds0 + i},68.00,NO,75.00,50.00,1.00')
        lines.append("")                                  # blank separator row
    path.write_text("\r\n".join(lines), encoding="utf-8-sig")
    return path


def env(incoming: Path):
    """Per-test isolated paths: incoming dir + temp store + temp archive."""
    store = incoming / "store.jsonl"
    archive = incoming / "_archive"
    return store, archive


def snap(ts="2026-06-04 14:05:00", device="Hydroponics Control",
         ph=6.6, tds=420, wt=68.0, air=False):
    """A minimal poller-shaped snapshot carrying one reservoir device."""
    sensors = {}
    if ph is not None:
        sensors["ph"] = ph
    if tds is not None:
        sensors["tds_ppm"] = tds
    if wt is not None:
        sensors["water_temp_f"] = wt
    devices = [{"name": device, "sensors": sensors, "ports": []}]
    if air:                       # an air-only device that must be ignored
        devices.append({"name": "4 x 4", "sensors": {"temp_f_tent": 80.0}, "ports": []})
    return {"timestamp": ts, "devices": devices}


# --- parsing -------------------------------------------------------------------

def test_parse_basic():
    with tempfile.TemporaryDirectory() as d:
        f = make_csv(Path(d) / "AC INFINITY Data.csv", "Hydroponics Control", 0, 5)
        exp = H.parse_export(f)
        check("parse: device id", exp.device == "Hydroponics Control")
        check("parse: sample count", len(exp.samples) == 5)
        check("parse: 1-min cadence", exp.sample_seconds == 60)
        check("parse: first pH", exp.samples[0].ph == 6.60)
        check("parse: first TDS", exp.samples[0].tds_ppm == 420)
        check("parse: leak NO -> False", exp.samples[0].water_leak is False)


def test_parse_nbsp_timestamps():
    # If the U+202F before AM/PM weren't handled, every row would drop -> 0 samples.
    with tempfile.TemporaryDirectory() as d:
        f = make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 6)
        exp = H.parse_export(f)
        check("parse: NBSP timestamps all parse", len(exp.samples) == 6)
        check("parse: ts is real datetime", isinstance(exp.samples[0].ts, datetime))


# --- ingest / merge ------------------------------------------------------------

def test_ingest_basic():
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 10)
        r = H.ingest(d, store=store, archive_dir=arch)
        check("ingest: samples_added", r["samples_added"] == 10)
        check("ingest: samples_total", r["samples_total"] == 10)
        check("ingest: files_seen", r["files_seen"] == 1)
        check("ingest: files_archived", r["files_archived"] == 1)
        check("ingest: store written", store.exists())


def test_ingest_idempotent():
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 10)
        H.ingest(d, store=store, archive_dir=arch)
        r2 = H.ingest(d, store=store, archive_dir=arch)        # same file again
        check("ingest idempotent: 0 new samples", r2["samples_added"] == 0)
        check("ingest idempotent: 0 re-archived", r2["files_archived"] == 0)
        check("ingest idempotent: total unchanged", r2["samples_total"] == 10)


def test_merge_overlapping_windows():
    # A = minutes 0..9, B = minutes 5..14 -> union 0..14 = 15 unique, NOT 20.
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 10)
        make_csv(Path(d) / "AC INFINITY Data (1).csv", "dev", 5, 10)
        r = H.ingest(d, store=store, archive_dir=arch)
        check("merge: overlap deduped to 15", r["samples_total"] == 15)
        exp = H.load_history(store=store)
        spans = (exp.samples[-1].ts - exp.samples[0].ts).total_seconds() / 60
        check("merge: continuous 14-min span", spans == 14)
        check("merge: both files archived", r["files_archived"] == 2)


def test_multidevice_separation():
    # Same timestamps, different devices -> kept apart (key is device+ts).
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "Hydroponics Control", 0, 5)
        make_csv(Path(d) / "AC INFINITY Data (1).csv", "4 x 4", 0, 5)
        r = H.ingest(d, store=store, archive_dir=arch)
        check("multidevice: both series kept", r["samples_total"] == 10)
        check("multidevice: two devices", len(r["devices"]) == 2)


def test_superset_reexport_adds_only_new():
    # Re-export a longer window (0..14) over an earlier short one (0..9):
    # different content hash -> re-archived, but only the 5 new minutes merge in.
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 10)
        H.ingest(d, store=store, archive_dir=arch)
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 15)   # overwrite, longer
        r = H.ingest(d, store=store, archive_dir=arch)
        check("superset: only new minutes added", r["samples_added"] == 5)
        check("superset: total now 15", r["samples_total"] == 15)
        check("superset: distinct hash archived", r["files_archived"] == 1)


# --- load_history --------------------------------------------------------------

def test_load_history_sorted():
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data (1).csv", "dev", 10, 5)   # later window first
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 5)
        H.ingest(d, store=store, archive_dir=arch)
        exp = H.load_history(store=store)
        ts = [s.ts for s in exp.samples]
        check("load: sorted ascending", ts == sorted(ts))
        check("load: single device label", exp.device == "dev")


def test_load_history_device_filter():
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "Hydroponics Control", 0, 5)
        make_csv(Path(d) / "AC INFINITY Data (1).csv", "4 x 4", 0, 5)
        H.ingest(d, store=store, archive_dir=arch)
        hydro = H.load_history("hydro", store=store)
        check("filter: only matching device", len(hydro.samples) == 5)
        check("filter: matched device label", hydro.device == "Hydroponics Control")
        check("filter: all merged when None", len(H.load_history(store=store).samples) == 10)


def test_load_history_empty_is_none():
    with tempfile.TemporaryDirectory() as d:
        check("load: missing store -> None", H.load_history(store=Path(d) / "nope.jsonl") is None)


# --- robustness ----------------------------------------------------------------

def test_ingest_skips_malformed():
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 8)
        (Path(d) / "AC INFINITY Data (bad).csv").write_text("garbage,no,header\n", encoding="utf-8")
        r = H.ingest(d, store=store, archive_dir=arch)
        check("malformed: good file still ingested", r["samples_added"] == 8)
        check("malformed: both files seen", r["files_seen"] == 2)


def test_latest_export_env_override():
    saved = os.environ.get("ACI_EXPORT_DIR")
    with tempfile.TemporaryDirectory() as d:
        make_csv(Path(d) / "AC INFINITY Data.csv", "dev", 0, 3)
        newer = make_csv(Path(d) / "AC INFINITY Data (1).csv", "dev", 5, 3)
        os.utime(Path(d) / "AC INFINITY Data.csv", (1, 1))            # force older mtime
        os.utime(newer, (10_000_000_000, 10_000_000_000))            # force newer mtime
        os.environ["ACI_EXPORT_DIR"] = d
        try:
            check("env: latest_export honors ACI_EXPORT_DIR", H.latest_export() == newer)
        finally:
            if saved is None:
                os.environ.pop("ACI_EXPORT_DIR", None)
            else:
                os.environ["ACI_EXPORT_DIR"] = saved


# --- record_snapshot (phone-free self-logging) ---------------------------------

def test_record_snapshot_basic():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "s.jsonl"
        n = H.record_snapshot(snap(air=True), store=store)
        check("record: one sample appended", n == 1)
        exp = H.load_history(store=store)
        check("record: ph stored", exp.samples[0].ph == 6.6)
        check("record: tds stored", exp.samples[0].tds_ppm == 420)
        check("record: water temp stored", exp.samples[0].water_temp_f == 68.0)
        check("record: snapshot ts parsed", exp.samples[0].ts == datetime(2026, 6, 4, 14, 5, 0))
        check("record: device label", exp.device == "Hydroponics Control")


def test_record_snapshot_dedup():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "s.jsonl"
        H.record_snapshot(snap(), store=store)
        n2 = H.record_snapshot(snap(), store=store)            # same device + ts
        check("record dedup: 0 on repeat", n2 == 0)


def test_record_snapshot_skips_non_hydro():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "s.jsonl"
        air_only = {"timestamp": "2026-06-04 14:05:00",
                    "devices": [{"name": "4 x 4", "sensors": {"temp_f_tent": 80.0}, "ports": []}]}
        check("record: skips air-only device", H.record_snapshot(air_only, store=store) == 0)


def test_record_snapshot_merges_with_csv():
    # Self-logged readings and CSV-imported readings share one device + store.
    with tempfile.TemporaryDirectory() as d:
        store, arch = env(Path(d))
        make_csv(Path(d) / "AC INFINITY Data.csv", "Hydroponics Control", 0, 5)
        H.ingest(d, store=store, archive_dir=arch)
        H.record_snapshot(snap(ts="2026-06-04 15:00:00"), store=store)   # later live reading
        exp = H.load_history(store=store)
        check("merge csv+live: 6 total", len(exp.samples) == 6)
        ts = [s.ts for s in exp.samples]
        check("merge csv+live: sorted", ts == sorted(ts))


def test_record_snapshot_seen_set_persists():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "s.jsonl"
        seen = set()
        H.record_snapshot(snap(), store=store, seen=seen)
        n = H.record_snapshot(snap(), store=store, seen=seen)   # dedup via shared set
        check("record: shared seen dedups without re-read", n == 0 and len(seen) == 1)


def test_load_seen_preseeds():
    # load_seen() pre-builds the dedup set from an existing store so a long-running
    # poller doesn't re-read the whole JSONL every cycle (regression #12).
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "s.jsonl"
        H.record_snapshot(snap(ts="2026-06-04 14:00:00"), store=store)
        H.record_snapshot(snap(ts="2026-06-04 14:01:00"), store=store)
        seen = H.load_seen(store=store)
        check("load_seen: picks up existing rows", len(seen) == 2)
        n = H.record_snapshot(snap(ts="2026-06-04 14:00:00"), store=store, seen=seen)
        check("load_seen: seeded set dedups an existing reading", n == 0)
        n2 = H.record_snapshot(snap(ts="2026-06-04 14:02:00"), store=store, seen=seen)
        check("load_seen: new reading still logged + tracked", n2 == 1 and len(seen) == 3)


def main():
    for fn in (
        test_parse_basic,
        test_parse_nbsp_timestamps,
        test_ingest_basic,
        test_ingest_idempotent,
        test_merge_overlapping_windows,
        test_multidevice_separation,
        test_superset_reexport_adds_only_new,
        test_load_history_sorted,
        test_load_history_device_filter,
        test_load_history_empty_is_none,
        test_ingest_skips_malformed,
        test_latest_export_env_override,
        test_record_snapshot_basic,
        test_record_snapshot_dedup,
        test_record_snapshot_skips_non_hydro,
        test_record_snapshot_merges_with_csv,
        test_record_snapshot_seen_set_persists,
        test_load_seen_preseeds,
    ):
        fn()
    print("=" * 44)
    print(f"  {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
