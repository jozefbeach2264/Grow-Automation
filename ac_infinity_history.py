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
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NBSP = " "                       # narrow no-break space the app injects before AM/PM
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
EXPORT_GLOB = "AC INFINITY Data*.csv"
_TS_FORMATS = ("%m/%d/%Y, %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p")


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


def latest_export(downloads: str | Path = DEFAULT_DOWNLOADS) -> Path | None:
    """Newest 'AC INFINITY Data*.csv' in the downloads dir, or None."""
    files = sorted(Path(downloads).glob(EXPORT_GLOB), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


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
    target = sys.argv[1] if len(sys.argv) > 1 else latest_export()
    if not target:
        print("no AC Infinity CSV export found in", DEFAULT_DOWNLOADS)
        sys.exit(1)
    exp = parse_export(target)
    print(_summary(exp))
