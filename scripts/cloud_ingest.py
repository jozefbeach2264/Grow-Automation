#!/usr/bin/env python3
"""
cloud_ingest.py — Poll AC Infinity cloud API and write sensor readings to controller.db.

Run alongside logger.py. The BLE logger owns the live 1Hz local readings; this script
fills in sensors only reachable via cloud: multi-controller setups, doser port states,
and any device without BLE.

The unified snapshot (build_unified_snapshot in db.py) prefers BLE when fresh, and
falls back to these cloud readings for everything else.

Usage:
  python scripts/cloud_ingest.py            # reads .env from repo root
  python scripts/cloud_ingest.py --interval 120

Requires in .env:
  AC_INFINITY_EMAIL=...
  AC_INFINITY_PASSWORD=...
  AC_INFINITY_TOKEN=...   (auto-written after first login)
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "labels.env")

from ac_infinity_client import (
    ACInfinityAuthError,
    SENSOR_TYPE,
    fetch_all_devices,
    get_or_refresh_token,
    parse_device,
)
from aci_ble_lab.db import add_cloud_readings, init_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cloud_ingest")

# sensor_ids where cloud API returns raw units (not *100) — must divide by 100
# so they land in the same unit space as BLE sensor_readings.
# All other sensor_ids already use divisor=100.0 and arrive pre-scaled.
_RAW_IDS = {11, 12, 20}   # CO2 ppm, light, water level


def _extract_readings(device: dict) -> dict[int, float]:
    """
    Pull all numeric sensor values from a parsed device dict.
    Returns {sensor_id: value} in /100 units (matching BLE sensor_readings).
    """
    # Map parsed device field names back to sensor_id integers
    _field_to_id: dict[str, int] = {label: sid for sid, (label, _) in SENSOR_TYPE.items()}

    readings: dict[int, float] = {}
    for field, sensor_id in _field_to_id.items():
        val = device.get(field)
        if val is None:
            continue
        # CO2, light, water_level arrive as display units from parse_device —
        # divide by 100 to match BLE storage format.
        if sensor_id in _RAW_IDS:
            val = val / 100.0
        readings[sensor_id] = val

    return readings


def poll_once(token: str) -> tuple[int, int]:
    """Fetch all devices, write readings to DB. Returns (devices, sensor_rows)."""
    raw_list = fetch_all_devices(token)
    ts = time.time()
    total_rows = 0

    for raw in raw_list:
        device = parse_device(raw)
        if not device.get("online"):
            continue
        readings = _extract_readings(device)
        if readings:
            add_cloud_readings(ts, device["dev_id"], device["name"], readings)
            total_rows += len(readings)
            log.debug("  %-30s  %d sensors", device["name"], len(readings))

    return len(raw_list), total_rows


def main(interval: int) -> None:
    init_schema()

    email    = os.getenv("AC_INFINITY_EMAIL", "")
    password = os.getenv("AC_INFINITY_PASSWORD", "")
    env_path = str(ROOT / ".env")

    if not email or not password:
        log.error("AC_INFINITY_EMAIL and AC_INFINITY_PASSWORD must be set in .env")
        sys.exit(1)

    log.info("Cloud ingest started. Poll interval: %ds", interval)

    token = get_or_refresh_token(email, password, env_path)
    consecutive_errors = 0

    while True:
        try:
            devices, rows = poll_once(token)
            log.info("Polled %d devices → %d sensor rows written", devices, rows)
            consecutive_errors = 0

        except ACInfinityAuthError:
            log.warning("Auth error — refreshing token")
            try:
                # Force re-login by clearing the env token
                os.environ.pop("AC_INFINITY_TOKEN", None)
                token = get_or_refresh_token(email, password, env_path)
            except Exception as e:
                log.error("Re-auth failed: %s", e)

        except Exception as e:
            consecutive_errors += 1
            backoff = min(30 * consecutive_errors, 600)
            log.warning("Poll error (%s) — retry in %ds", e, backoff)
            time.sleep(backoff)
            continue

        time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="AC Infinity cloud → DB ingest")
    p.add_argument("--interval", type=int, default=60,
                   help="Seconds between cloud polls (default: 60)")
    args = p.parse_args()
    try:
        main(args.interval)
    except KeyboardInterrupt:
        log.info("Stopped.")
