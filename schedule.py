"""
Schedule-driven hardware state.

Computes what each schedule-controlled output should be doing RIGHT NOW based
on .env config. The poller compares this against the live snapshot, exposes
the deltas to the AI, and (after the AI cycle) deterministically corrects any
deltas the AI failed to handle.

All config is dynamic -- re-read every call so .env edits take effect mid-run.

Env reference:
  LIGHT_HOURS_ON      hours per cycle the light is on   (0-24, default 24)
  LIGHT_HOURS_OFF     hours per cycle the light is off  (0-24, default 0)
  LIGHT_CYCLE_START   local time when on-period starts  (HH:MM, default 06:00)
  LIGHT_INTENSITY     speed 0-10 when on                (default 10)
  LIGHT_SUNRISE_MIN   minutes ramping 0 -> intensity at start of on-period
                      (0 = no fade, default 0; stepped, X6 only takes int 0-10)
  LIGHT_SUNSET_MIN    minutes ramping intensity -> 0 at end of on-period
                      (0 = no fade, default 0)
  ROLE_LIGHT          "<device>:<port>"                 (default "4 x 4:1")
  OSC_FAN_SPEED       speed 0-10 for oscillating fans   (default 10)
  ROLE_OSC_FANS       comma-separated "<device>:<port>" (default "4 x 4:3,4 x 4:4")
"""

import os
from datetime import datetime, time as dtime


def _parse_role(env_key: str, default: tuple[str, int]) -> tuple[str, int]:
    raw = os.getenv(env_key, "").strip()
    if not raw or ":" not in raw:
        return default
    device, port_s = raw.rsplit(":", 1)
    try:
        return (device.strip(), int(port_s))
    except ValueError:
        return default


def _parse_role_list(env_key: str, default: str) -> list[tuple[str, int]]:
    raw = os.getenv(env_key, default).strip()
    out: list[tuple[str, int]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        device, port_s = entry.rsplit(":", 1)
        try:
            out.append((device.strip(), int(port_s)))
        except ValueError:
            continue
    return out


def _parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = s.strip().split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def expected_light_state(now: datetime | None = None) -> dict:
    """
    {'on': bool, 'speed': int, 'device': str, 'port': int, 'reason': str}
    """
    now = now or datetime.now()
    hours_on  = max(0, min(24, int(os.getenv("LIGHT_HOURS_ON",  "24"))))
    hours_off = max(0, min(24, int(os.getenv("LIGHT_HOURS_OFF", "0"))))
    intensity = max(0, min(10, int(os.getenv("LIGHT_INTENSITY", "10"))))
    device, port = _parse_role("ROLE_LIGHT", default=("4 x 4", 1))

    if hours_on == 0:
        return {"on": False, "speed": 0, "device": device, "port": port,
                "reason": "LIGHT_HOURS_ON=0 (lights disabled)"}
    if hours_off == 0 or hours_on >= 24:
        return {"on": True, "speed": intensity, "device": device, "port": port,
                "reason": "24h photoperiod"}

    cycle_start  = _parse_hhmm(os.getenv("LIGHT_CYCLE_START", "06:00"), dtime(6, 0))
    cycle_total  = hours_on + hours_off  # normally 24
    minutes_now  = now.hour * 60 + now.minute
    minutes_zero = cycle_start.hour * 60 + cycle_start.minute
    offset_min   = (minutes_now - minutes_zero) % (cycle_total * 60)
    on_total     = hours_on * 60
    cycle_tag    = (f"{hours_on}/{hours_off} cycle starting "
                    f"{cycle_start.strftime('%H:%M')}")
    elapsed_tag  = f"t+{offset_min // 60}h{offset_min % 60:02d}m"

    if offset_min >= on_total:
        return {"on": False, "speed": 0, "device": device, "port": port,
                "reason": f"{cycle_tag} -> currently OFF ({elapsed_tag} into cycle)"}

    sunrise_min = max(0, int(os.getenv("LIGHT_SUNRISE_MIN", "0")))
    sunset_min  = max(0, int(os.getenv("LIGHT_SUNSET_MIN",  "0")))
    fade_in     = min(sunrise_min, on_total)
    fade_out    = min(sunset_min,  on_total - fade_in)  # never overlap sunrise

    if fade_in > 0 and offset_min < fade_in:
        speed = round(intensity * offset_min / fade_in)
        phase = f"sunrise {offset_min}/{fade_in}m"
    elif fade_out > 0 and offset_min >= on_total - fade_out:
        rem   = on_total - offset_min                  # 1..fade_out
        speed = round(intensity * rem / fade_out)
        phase = f"sunset {fade_out - rem}/{fade_out}m"
    else:
        speed = intensity
        phase = "plateau"

    return {
        "on":     True,
        "speed":  speed,
        "device": device,
        "port":   port,
        "reason": f"{cycle_tag} -> ON {phase} ({elapsed_tag})",
    }


def expected_osc_fan_state() -> list[dict]:
    """
    [{'speed': int, 'device': str, 'port': int, 'reason': str}, ...]
    """
    speed = max(0, min(10, int(os.getenv("OSC_FAN_SPEED", "10"))))
    fans  = _parse_role_list("ROLE_OSC_FANS", default="4 x 4:3,4 x 4:4")
    return [
        {"speed": speed, "device": d, "port": p,
         "reason": f"oscillating fan always at speed {speed}"}
        for d, p in fans
    ]


def _find_port(snapshot: dict, device_name: str, port_num: int) -> dict | None:
    for dev in snapshot.get("devices", []):
        if dev.get("name") == device_name:
            for p in dev.get("ports", []):
                if p.get("port") == port_num:
                    return p
    return None


# ---------------------------------------------------------------------------
# CO2 emergency dump
# ---------------------------------------------------------------------------

# Stateful: once the trigger fires, stay ACTIVE until CO2 drops below the clear
# threshold. Module-level state -- if the poller restarts during an active
# emergency, the next cycle re-evaluates against the current reading and
# re-enters emergency if CO2 is still above trigger.
_co2_emergency_active: bool = False


def _co2_outlet() -> tuple[str, int] | None:
    raw = os.getenv("CO2_VALVE", "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        d, p = raw.rsplit(":", 1)
        return (d.strip(), int(p))
    except (ValueError, AttributeError):
        return None


def _read_co2(snapshot: dict) -> float | None:
    for dev in snapshot.get("devices", []):
        v = dev.get("sensors", {}).get("co2_ppm")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def compute_co2_emergency(snapshot: dict) -> dict | None:
    """
    Deterministic CO2 dump trigger. Returns an action block when emergency is
    active, or None when no action is required.

    Trigger:  co2_ppm >= CO2_EMERGENCY_PPM
    Clear:    co2_ppm <  CO2_DUMP_CLEAR_PPM   (between the two = hold prior state)

    Block shape:
      {
        "active":   True,
        "co2_ppm":  <reading>,
        "trigger":  <CO2_EMERGENCY_PPM>,
        "clear":    <CO2_DUMP_CLEAR_PPM>,
        "actions":  [...]    # always at least the exhaust ramp; valve OFF if configured
      }
    """
    global _co2_emergency_active

    try:
        trigger = int(os.getenv("CO2_EMERGENCY_PPM", "0"))
    except ValueError:
        return None
    if trigger <= 0:
        return None  # disabled

    try:
        clear = int(os.getenv("CO2_DUMP_CLEAR_PPM", str(trigger - 500)))
    except ValueError:
        clear = trigger - 500
    if clear >= trigger:
        clear = trigger - 200  # enforce real hysteresis gap

    co2 = _read_co2(snapshot)
    if co2 is None:
        # No reading -> we cannot evaluate safely. Leave prior state alone.
        # If a previous cycle entered emergency, the actions still get issued.
        if not _co2_emergency_active:
            return None
    else:
        if co2 >= trigger:
            _co2_emergency_active = True
        elif co2 < clear:
            _co2_emergency_active = False
        # else: hold previous state (hysteresis zone)

    if not _co2_emergency_active:
        return None

    actions: list[dict] = []

    valve = _co2_outlet()
    if valve:
        actions.append({
            "device": valve[0], "port": valve[1],
            "action": "set_outlet", "value": False,
            "reason": f"CO2 emergency: force valve OFF (co2={co2} ppm, trigger={trigger})",
        })

    exhaust_dev, exhaust_port = _parse_role("ROLE_EXHAUST", default=("4 x 4", 2))
    actions.append({
        "device": exhaust_dev, "port": exhaust_port,
        "action": "set_speed", "value": 10,
        "reason": f"CO2 emergency dump: exhaust to max until co2 < {clear} ppm",
    })

    return {
        "active":  True,
        "co2_ppm": co2,
        "trigger": trigger,
        "clear":   clear,
        "actions": actions,
    }


def compute_schedule_deltas(snapshot: dict, now: datetime | None = None) -> list[dict]:
    """
    Returns one delta per schedule-controlled output whose actual state
    doesn't match expected. Empty list means everything is in sync.

    Delta shape:
      {kind, device, port, action, expected_value, actual_value, reason}
        action == "set_speed"  -> values are int 0-10
        action == "set_outlet" -> values are bool

    The CO2 pulse delta is appended by build_snapshot() after this runs,
    since it depends on snapshot fields that the AI advisor populates.
    """
    deltas: list[dict] = []

    light = expected_light_state(now)
    lp = _find_port(snapshot, light["device"], light["port"])
    if lp is not None:
        actual = lp.get("speed", 0) or 0
        if actual != light["speed"]:
            deltas.append({
                "kind":           "light",
                "device":         light["device"],
                "port":           light["port"],
                "action":         "set_speed",
                "expected_value": light["speed"],
                "actual_value":   actual,
                "reason":         light["reason"],
            })

    for fan in expected_osc_fan_state():
        fp = _find_port(snapshot, fan["device"], fan["port"])
        if fp is None:
            continue
        actual = fp.get("speed", 0) or 0
        if actual != fan["speed"]:
            deltas.append({
                "kind":           "osc_fan",
                "device":         fan["device"],
                "port":           fan["port"],
                "action":         "set_speed",
                "expected_value": fan["speed"],
                "actual_value":   actual,
                "reason":         fan["reason"],
            })

    return deltas


# ---------------------------------------------------------------------------
# CO2 pulse modulator
# ---------------------------------------------------------------------------

_co2_pulse_state: bool | None = None   # None = no prior cycle


def compute_co2_pulse(snapshot: dict) -> dict | None:
    """
    Deterministic CO2 valve pulse modulator. Holds co2_ppm within
    target +/- CO2_PULSE_BAND_PPM using a hysteresis state machine.

    Reads (from snapshot):
      co2_target          int -- per-week target ppm (populated by build_snapshot)
      devices[].sensors.co2_ppm
      devices[].ports[].powered (CO2 valve outlet)
      res_health.co2_gate

    Returns a delta (same shape as compute_schedule_deltas) when the current
    valve state doesn't match expected, else None.

    Force-OFF cases (regardless of CO2 reading):
      - co2_gate is HOLD or REDUCE
      - CO2 emergency is active (caller skips this function when emergency set)
      - CO2 reading missing (don't dose blind)
      - CO2_VALVE not configured
    """
    global _co2_pulse_state

    valve = _co2_outlet()
    if valve is None:
        return None

    target = snapshot.get("co2_target")
    if not isinstance(target, (int, float)) or target <= 0:
        return None

    try:
        band = int(os.getenv("CO2_PULSE_BAND_PPM", "250"))
    except ValueError:
        band = 250

    co2        = _read_co2(snapshot)
    res_health = snapshot.get("res_health") or {}
    co2_gate   = res_health.get("co2_gate", "HOLD")

    # Decide expected valve state
    if co2_gate in ("HOLD", "REDUCE"):
        expected = False
        decision = f"gate={co2_gate}"
    elif co2 is None:
        expected = False
        decision = "no CO2 reading"
    else:
        low  = target - band
        high = target + band
        if co2 < low:
            _co2_pulse_state = True
        elif co2 > high:
            _co2_pulse_state = False
        # else hold previous state
        if _co2_pulse_state is None:
            _co2_pulse_state = co2 < target
        expected = _co2_pulse_state
        decision = f"co2={co2} vs target {int(target)}+/-{band}"

    # Read actual valve state
    actual = None
    for dev in snapshot.get("devices", []):
        if dev.get("name") == valve[0]:
            for p in dev.get("ports", []):
                if p.get("port") == valve[1]:
                    actual = p.get("powered")
                    break

    if actual is None:
        return None

    if bool(actual) == bool(expected):
        return None

    return {
        "kind":           "co2_pulse",
        "device":         valve[0],
        "port":           valve[1],
        "action":         "set_outlet",
        "expected_value": bool(expected),
        "actual_value":   bool(actual),
        "reason":         f"CO2 pulse: target {int(target)}+/-{band} ({decision})",
    }
