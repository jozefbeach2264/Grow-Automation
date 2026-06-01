"""Grow calendar -- auto-compute current week and stage from elapsed days.

Set GROW_START_DATE (YYYY-MM-DD) in .env to enable auto-mode.
VEG_DAYS controls when the stage flips from veg to bloom.
FLOWER_DAYS is informational (planned bloom duration).

If GROW_START_DATE is unset or unparseable, falls back to manual
GROW_WEEK / GROW_STAGE values from .env.
"""
import os
from datetime import date


def current_grow_week_and_stage() -> tuple[int, str]:
    """
    Return (week, stage) computed from GROW_START_DATE + VEG_DAYS.

    Day 1 = start date. Days 1-7 = week 1, 8-14 = week 2, etc.
    After VEG_DAYS elapsed, stage flips to bloom and the week counter resets.
    """
    start_date = _start_date()
    if start_date is None:
        return _manual()

    days_elapsed = max(1, (date.today() - start_date).days + 1)
    veg_days = _int_env("VEG_DAYS", 28)

    if days_elapsed <= veg_days:
        return (((days_elapsed - 1) // 7) + 1, "veg")
    bloom_day = days_elapsed - veg_days
    return (((bloom_day - 1) // 7) + 1, "bloom")


def days_into_current_stage() -> tuple[int, int]:
    """
    Return (day_in_stage, planned_stage_days) for HUD display.
    Returns (0, 0) when GROW_START_DATE is unset.
    """
    start_date = _start_date()
    if start_date is None:
        return (0, 0)

    days_elapsed = max(1, (date.today() - start_date).days + 1)
    veg_days    = _int_env("VEG_DAYS",    28)
    flower_days = _int_env("FLOWER_DAYS", 63)

    if days_elapsed <= veg_days:
        return (days_elapsed, veg_days)
    return (days_elapsed - veg_days, flower_days)


def _start_date():
    raw = os.getenv("GROW_START_DATE", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _manual() -> tuple[int, str]:
    return (_int_env("GROW_WEEK", 1),
            os.getenv("GROW_STAGE", "veg").strip().lower())
