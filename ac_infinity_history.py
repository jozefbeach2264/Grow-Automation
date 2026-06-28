"""Loader for AC Infinity app CSV exports ("Device Data" export) -- 1-min trend history.

The trend series is NOT reachable from the cloud API (there is no history endpoint --
confirmed against the reverse-engineered API and 24 probed endpoint names). Instead the
user exports "Device Data" to CSV from the app; this module ingests those files into a
clean time-series the rest of the system can learn from -- dense dose-response curves the
single before/after API reads can't see -- and cross-reference against dose events.

Format quirks handled:
  - UTF-8 BOM, CRLF line endings
  - a blank line between every data row
  - a U+202F narrow no-break space before AM/PM in every timestamp
  - a metadata header block (Device ID, Sample Frequency, Start/End Time, ...) then a
    blank line, then the column header row beginning with "Time"
  - columns: Time, PH (Sensor 1), TDS (Sensor 1), WATER TEMP (Sensor 1),
    Water Detect (Sensor 2), Temperature (Outside), Relative Humidity (Outside),
    VPD (Outside). Export carries TDS (ppm) + pH + water temp + leak + outside air.
    There is NO EC column -- TDS only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NBSP = " "                       # narrow no-break space the app injects before AM/PM
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
EXPORT_GLOB = "AC INFINITY Data*.csv"
_TS_FORMATS = ("%m/%d/%Y, %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p")

# Phone exports land in the "incoming" dir (KDE Connect / Taildrop / Syncthing /
# manual copy -- the transport is interchangeable). They accrete into one merged,
# deduped trend store under trend_data/ so overlapping or re-exported windows build a
# single continuous per-device series instead of fragmenting across files.
REPO_DIR = Path(__file__).resolve().parent
TREND_DIR = REPO_DIR / "trend_data"
ARCHIVE_DIR = TREND_DIR / "acinfinity"               # raw exports, content-hash deduped
STORE_PATH = TREND_DIR / "acinfinity_history.jsonl"  # merged sample ledger


def incoming_dir() -> Path:
    """Directory phone exports arrive in. Override with ACI_EXPORT_DIR; default ~/Downloads."""
    return Path(os.environ.get("ACI_EXPORT_DIR") or DEFAULT_DOWNLOADS).expanduser()


@dataclass
class Sample:
    ts: datetime
    ph: float | None
    tds_ppm: float | None
    water_temp_f: float | None
    water_leak: bool | None
    out_temp_f: float | None
    out_humidity: float | None
    out_vpd: float | None


@dataclass
class Export:
    device: str
    sample_seconds: int
    start: datetime | None
    end: datetime | None
    samples: list[Sample]
    path: Path

    @property
    def span_minutes(self) -> float:
        if not self.samples:
            return 0.0
        return (self.samples[-1].ts - self.samples[0].ts).total_seconds() / 60.0

    def window(self, start: datetime, end: datetime) -> list[Sample]:
        """Samples with start <= ts <= end (inclusive). Useful to pull the settling
        curve around a dose event."""
        return [s for s in self.samples if start <= s.ts <= end]

    def around(self, ts: datetime, before_min: float = 1.0, after_min: float = 6.0) -> list[Sample]:
        """Samples in [ts - before_min, ts + after_min] -- the dose-response window."""
        from datetime import timedelta
        return self.window(ts - timedelta(minutes=before_min), ts + timedelta(minutes=after_min))


def _f(s: str | None) -> float | None:
    s = (s or "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_ts(s: str | None) -> datetime | None:
    s = (s or "").replace(NBSP, " ").strip().strip('"').strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _freq_seconds(raw: str) -> int:
    raw = (raw or "").strip().upper()
    digits = "".join(c for c in raw if c.isdigit())
    n = int(digits) if digits else 1
    if "MIN" in raw:
        return n * 60
    if "HOUR" in raw or "HR" in raw:
        return n * 3600
    if "SEC" in raw:
        return n
    return n * 60                     # app default is minutes


def parse_export(path: str | Path) -> Export:
    path = Path(path)
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    meta = {r[0].strip(): r[1].strip()
            for r in rows if len(r) >= 2 and r[0].strip() and r[1].strip()}
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Time")
    header = [h.strip().lower() for h in rows[hdr_i]]

    def find(substr: str) -> int | None:
        return next((i for i, h in enumerate(header) if substr in h), None)

    idx = {
        "ph": find("ph"), "tds": find("tds"), "wt": find("water temp"),
        "leak": find("water detect"), "ot": find("temperature (outside"),
        "orh": find("relative humidity"), "ovpd": find("vpd"),
    }

    samples: list[Sample] = []
    for r in rows[hdr_i + 1:]:
        if not r or not r[0].strip():
            continue                  # blank separator row
        ts = _parse_ts(r[0])
        if ts is None:
            continue

        def g(key: str) -> str:
            i = idx[key]
            return r[i] if (i is not None and i < len(r)) else ""

        leak = g("leak").strip().upper()
        samples.append(Sample(
            ts=ts,
            ph=_f(g("ph")),
            tds_ppm=_f(g("tds")),
            water_temp_f=_f(g("wt")),
            water_leak=(leak == "YES") if leak in ("YES", "NO") else None,
            out_temp_f=_f(g("ot")),
            out_humidity=_f(g("orh")),
            out_vpd=_f(g("ovpd")),
        ))

    return Export(
        device=meta.get("Device ID", "?"),
        sample_seconds=_freq_seconds(meta.get("Sample Frequency", "1 MIN")),
        start=_parse_ts(meta.get("Start Time")),
        end=_parse_ts(meta.get("End Time")),
        samples=samples,
        path=path,
    )


def latest_export(downloads: str | Path | None = None) -> Path | None:
    """Newest 'AC INFINITY Data*.csv' in the incoming dir, or None."""
    base = Path(downloads).expanduser() if downloads else incoming_dir()
    files = sorted(base.glob(EXPORT_GLOB), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# --- merged trend store (transport-agnostic ingest) -----------------------------

def _sample_row(device: str, s: Sample) -> dict:
    return {
        "device": device, "ts": s.ts.isoformat(),
        "ph": s.ph, "tds_ppm": s.tds_ppm, "water_temp_f": s.water_temp_f,
        "water_leak": s.water_leak, "out_temp_f": s.out_temp_f,
        "out_humidity": s.out_humidity, "out_vpd": s.out_vpd,
    }


def _row_sample(row: dict) -> Sample:
    return Sample(
        ts=datetime.fromisoformat(row["ts"]),
        ph=row.get("ph"), tds_ppm=row.get("tds_ppm"),
        water_temp_f=row.get("water_temp_f"), water_leak=row.get("water_leak"),
        out_temp_f=row.get("out_temp_f"), out_humidity=row.get("out_humidity"),
        out_vpd=row.get("out_vpd"),
    )


def _read_store(store: Path) -> list[dict]:
    if not store.exists():
        return []
    rows = []
    for line in store.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def ingest(src: str | Path | None = None, *, archive: bool = True,
           store: str | Path = STORE_PATH, archive_dir: str | Path = ARCHIVE_DIR) -> dict:
    """Fold every 'AC INFINITY Data*.csv' in the incoming dir into the merged trend
    store, deduping samples by (device, timestamp). Overlapping or re-exported windows
    accrete into one continuous per-device series instead of fragmenting; byte-identical
    re-exports are skipped via content hash. Returns a summary dict.

    Transport is irrelevant here -- files reach `src` however you move them off the
    phone (KDE Connect, Taildrop, Syncthing, USB, manual copy).
    """
    src = Path(src).expanduser() if src else incoming_dir()
    store, archive_dir = Path(store), Path(archive_dir)
    store.parent.mkdir(parents=True, exist_ok=True)
    if archive:
        archive_dir.mkdir(parents=True, exist_ok=True)

    seen = {(r.get("device", "?"), r.get("ts", "")) for r in _read_store(store)}
    files = sorted(src.glob(EXPORT_GLOB), key=lambda p: p.stat().st_mtime)
    archived, added, devices, new_rows = 0, 0, set(), []

    for f in files:
        if archive:
            digest = hashlib.sha1(f.read_bytes()).hexdigest()[:8]
            if not list(archive_dir.glob(f"*__{digest}.csv")):
                (archive_dir / f"{f.stem}__{digest}.csv").write_bytes(f.read_bytes())
                archived += 1
        try:
            exp = parse_export(f)
        except Exception as e:                         # one malformed file never aborts ingest
            print(f"  skip {f.name}: {e}")
            continue
        for s in exp.samples:
            key = (exp.device, s.ts.isoformat())
            if key in seen:
                continue
            seen.add(key)
            new_rows.append(json.dumps(_sample_row(exp.device, s)))
            added += 1
            devices.add(exp.device)
        _trend_db_write(lambda db, e=exp: db.ingest_samples_db(e.device, e.samples, "csv"))

    if new_rows:
        with store.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(new_rows) + "\n")

    return {
        "incoming": str(src), "files_seen": len(files), "files_archived": archived,
        "samples_added": added, "samples_total": len(seen),
        "devices": sorted(devices), "store": str(store),
    }


def load_history(device: str | None = None, *, store: str | Path = STORE_PATH) -> Export | None:
    """The merged trend as one sorted, deduped Export (None if the store is empty).
    `device` filters to matching device IDs by case-insensitive substring."""
    rows = _read_store(Path(store))
    if device:
        rows = [r for r in rows if device.lower() in str(r.get("device", "")).lower()]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("ts", ""))
    samples = [_row_sample(r) for r in rows]
    devs = sorted({r.get("device", "?") for r in rows})
    gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(samples, samples[1:])]
    gaps = [g for g in gaps if g > 0]
    return Export(
        device=devs[0] if len(devs) == 1 else f"merged({len(devs)} devices)",
        sample_seconds=int(min(gaps)) if gaps else 60,
        start=samples[0].ts, end=samples[-1].ts,
        samples=samples, path=Path(store),
    )


def _numf(v) -> float | None:
    """Coerce a live snapshot sensor value (already numeric, or a string) to float."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_snapshot_ts(s: str | None) -> datetime:
    """build_snapshot stamps '%Y-%m-%d %H:%M:%S'; fall back to now() if absent/odd."""
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.now().replace(microsecond=0)


# A device carrying any of these is the reservoir/hydro source worth logging.
_HYDRO_KEYS = ("ph", "tds_ppm", "water_temp_f", "water_level", "ec_us", "ec_ms")


def _trend_db_write(call) -> None:
    """Best-effort mirror to the TimescaleDB trend store (trend_db.py). Never raises
    -- the JSONL store is the source of truth and the control loop must not depend on
    Postgres. Disable with TREND_DB_ENABLED=false."""
    if os.environ.get("TREND_DB_ENABLED", "true").strip().lower() == "false":
        return
    try:
        import trend_db
        if trend_db.available():
            call(trend_db)
    except Exception:
        pass


def record_snapshot(snapshot: dict, *, store: str | Path = STORE_PATH,
                    seen: set | None = None) -> int:
    """Append the reservoir reading(s) from a live poller snapshot to the merged
    trend store -- the phone-free path. Instead of importing the app's CSV, the
    automation logs the sensors it already reads each cycle, in the SAME schema as
    `ingest()`, so `load_history()` / `dose_align` read self-logged and CSV-imported
    history transparently. Deduped by (device, timestamp). Returns rows appended.

    A long-running logger can pass a persistent `seen` set to skip re-reading the
    store every cycle; omit it and the store is read once per call for the dedup set.
    """
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    if seen is None:
        seen = {(r.get("device", "?"), r.get("ts", "")) for r in _read_store(store)}

    ts = _parse_snapshot_ts(snapshot.get("timestamp"))
    rows = []
    for dev in snapshot.get("devices", []):
        sensors = dev.get("sensors", {})
        if not any(k in sensors for k in _HYDRO_KEYS):
            continue                                   # skip air/outlet-only devices
        device = dev.get("name", "?")
        key = (device, ts.isoformat())
        if key in seen:
            continue
        seen.add(key)
        leak = sensors.get("water_leak")
        rows.append(json.dumps(_sample_row(device, Sample(
            ts=ts,
            ph=_numf(sensors.get("ph")),
            tds_ppm=_numf(sensors.get("tds_ppm")),
            water_temp_f=_numf(sensors.get("water_temp_f")),
            water_leak=(bool(leak) if leak is not None else None),
            out_temp_f=None, out_humidity=None, out_vpd=None,
        ))))
    if rows:
        with store.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
    _trend_db_write(lambda db: db.record_snapshot_db(snapshot))   # mirror to TimescaleDB
    return len(rows)


def _summary(exp: Export) -> str:
    ph = [s.ph for s in exp.samples if s.ph is not None]
    tds = [s.tds_ppm for s in exp.samples if s.tds_ppm is not None]
    lines = [
        f"file   : {exp.path.name}",
        f"device : {exp.device}   sample every {exp.sample_seconds}s",
        f"span   : {exp.samples[0].ts:%Y-%m-%d %H:%M} -> {exp.samples[-1].ts:%H:%M}"
        f"  ({exp.span_minutes:.0f} min, {len(exp.samples)} samples)" if exp.samples else "span   : (empty)",
    ]
    if ph:
        lines.append(f"pH     : {min(ph):.2f} .. {max(ph):.2f}")
    if tds:
        lines.append(f"TDS    : {min(tds):.0f} .. {max(tds):.0f} ppm")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    cmd = args[0] if args else ""

    if cmd == "ingest":                       # fold new phone exports into the merged store
        for k, v in ingest(args[1] if len(args) > 1 else None).items():
            print(f"  {k:14}: {v}")
    elif cmd == "history":                    # summarize the merged store
        exp = load_history(args[1] if len(args) > 1 else None)
        if not exp or not exp.samples:
            print("merged store empty -- run: python3 ac_infinity_history.py ingest")
            sys.exit(1)
        print(_summary(exp))
    else:                                     # summarize a single file (default: newest)
        target = args[0] if args else latest_export()
        if not target:
            print("no AC Infinity CSV export found in", incoming_dir())
            sys.exit(1)
        print(_summary(parse_export(target)))
