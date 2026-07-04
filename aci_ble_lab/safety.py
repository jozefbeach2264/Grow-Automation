"""Port classification + chemical-write guard for the BLE channel.

The BLE channel is just another transport, never a bypass: it is
telemetry/climate only. Chemicals move SOLELY via the bounded dose verb on
the cloud path (dosing.timed_dose) -- so a chemical START through this layer
is rejected outright, freeze or no freeze. STOPS (speed 0) are always
allowed: halting a pump must never be blocked by the safety system itself.

A "chemical" port is anything in DOSER_PORTS_<SLUG>, PH_PORTS_<SLUG>, or the
CO2_VALVE outlet. The classification is recomputed every call so .env edits
take effect mid-run (matches the rest of the codebase's dynamic-config model).
"""

import os
from utils import name_slug
import safety_state


class SafetyBlocked(Exception):
    """A chemical write was rejected (raw chemical start on the BLE layer --
    chemicals move only via the bounded dose verb on the cloud path)."""


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


def guard_chemical_write(device: str, port: int,
                         work_type: int | None = None,
                         speed: int | None = None) -> None:
    """Gate a BLE write to (device, port). No-op for climate ports.

    For chemical channels (doser / pH / CO2 valve):
      * STOPS (speed == 0) ALWAYS pass -- freeze or not. Only speed 0 counts
        as a stop: an off-type work_type still programs level_off, and the
        port RUNS at level_off while "off", so a nonzero speed is never a
        stop regardless of work_type.
      * Everything else raises SafetyBlocked, ALWAYS: the BLE layer is
        telemetry/climate only; chemicals move solely via the bounded dose
        verb on the cloud path (dosing.timed_dose). Callers that can't say
        what they're writing (speed=None, the legacy signature) are treated
        as starts -- the conservative reading.

    Call at every write entry point: enqueue, executor, ctl.py."""
    if not is_chemical_port(device, port):
        return
    if speed == 0:
        return  # a stop is always allowed, in any state
    disabled, reason = safety_state.dosing_disable_status()
    frozen = f" (dosing freeze also active: {reason})" if disabled else ""
    raise SafetyBlocked(
        f"chemical START to {device!r}:{port} (work_type={work_type}, "
        f"speed={speed}) rejected -- the BLE layer never moves chemicals; "
        f"use the bounded dose verb (dosing.timed_dose) on the cloud path."
        f"{frozen}"
    )
