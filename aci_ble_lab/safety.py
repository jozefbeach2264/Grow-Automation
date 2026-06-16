"""Port classification + chemical-write guard for the BLE channel.

The BLE channel is just another transport, never a bypass: every doser / pH /
CO2-valve write -- regardless of whether it came from poller, the AI advisor,
or ctl.py -- has to clear the same chemical freeze as a cloud write would.

A "chemical" port is anything in DOSER_PORTS_<SLUG>, PH_PORTS_<SLUG>, or the
CO2_VALVE outlet. The classification is recomputed every call so .env edits
take effect mid-run (matches the rest of the codebase's dynamic-config model).
"""

import os
from utils import name_slug
import safety_state


class SafetyBlocked(Exception):
    """A chemical write was rejected because the dosing freeze is active."""


def _parse_int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def _doser_ports(device: str) -> set[int]:
    return set(_parse_int_list(os.getenv(f"DOSER_PORTS_{name_slug(device)}", "")))


def _ph_ports(device: str) -> set[int]:
    return set(_parse_int_list(os.getenv(f"PH_PORTS_{name_slug(device)}", "")))


def _co2_valve() -> tuple[str, int] | None:
    raw = os.getenv("CO2_VALVE", "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        d, p = raw.rsplit(":", 1)
        return d.strip(), int(p)
    except (ValueError, AttributeError):
        return None


def chemical_ports(device: str) -> set[int]:
    """Union of doser + pH ports for a given device. Excludes the CO2 valve
    (which is keyed off device,port together via CO2_VALVE)."""
    return _doser_ports(device) | _ph_ports(device)


def is_chemical_port(device: str, port: int) -> bool:
    """True if (device, port) addresses a doser, pH pump, or the CO2 valve."""
    if port in chemical_ports(device):
        return True
    co2 = _co2_valve()
    return co2 is not None and co2[0] == device and co2[1] == port


def guard_chemical_write(device: str, port: int) -> None:
    """Raise SafetyBlocked if (device, port) is a chemical channel and the
    persistent dosing freeze is active. No-op for climate ports.

    Call at every write entry point: enqueue, executor, ctl.py."""
    if not is_chemical_port(device, port):
        return
    disabled, reason = safety_state.dosing_disable_status()
    if disabled:
        raise SafetyBlocked(
            f"chemical write to {device!r}:{port} rejected -- dosing freeze active "
            f"({reason}). Clear with safety_state.clear_dosing_disable()."
        )
