"""AC Infinity cloud API client for UIS controllers (CTR89Q / AI power strip)."""

import os
import re
import time
import requests

API_HOST = "http://www.acinfinityserver.com"


class ACInfinityAuthError(Exception):
    """Token is invalid/expired -- caller should drop the token and re-authenticate."""
HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Content-Type": "application/x-www-form-urlencoded",
    "appVersion": "1.9.7",
    "phoneType": "1",
}

DEVICE_TYPE_LABELS = {
    11: "Controller 69 Pro",
    18: "Controller 69 Pro+",
    20: "Controller AI+ (CTR89Q)",
    21: "Outlet AI (ADA4)",
    22: "Outlet AI+",
}

AI_DEVICE_TYPES = {20, 21, 22}

# Outlet-type devices have on/off ports, not variable-speed ports
OUTLET_DEVICE_TYPES = {21, 22}

# sensorType integers returned in deviceInfo.sensors[]
# Types 0/2/3 = external probe (temp°F, humidity, VPD)
# Types 4/6/7 = built-in sensor hub (temp°F, humidity, VPD) on accessPort 7
# Types 11/12/20 = CO2, light, water-level probes
# Types 13-19 = hydro probe (HDS3): pH, EC, TDS, water temp
SENSOR_TYPE = {
    0:  ("temp_f_ext",      100.0),   # external temp probe, °F*100
    2:  ("humidity_ext",    100.0),   # external humidity probe, %*100
    3:  ("vpd_ext",         100.0),   # external VPD probe, kPa*100
    4:  ("temp_f",          100.0),   # built-in temp, °F*100
    6:  ("humidity",        100.0),   # built-in humidity, %*100
    7:  ("vpd",             100.0),   # built-in VPD, kPa*100
    11: ("co2_ppm",           1.0),   # CO2, raw ppm
    12: ("light",             1.0),   # light sensor, raw value
    13: ("ph",              100.0),   # HDS3 pH*100
    14: ("ec_us",           100.0),   # HDS3 EC µS/cm*100
    15: ("ec_ms",           100.0),   # HDS3 EC mS/cm*100
    16: ("tds_ppm",         100.0),   # HDS3 TDS ppm*100
    17: ("tds_ppt",         100.0),   # HDS3 TDS ppt*100
    18: ("water_temp_f",    100.0),   # HDS3 water temp°F*100
    19: ("water_temp_c",    100.0),   # HDS3 water temp°C*100
    20: ("water_level",       1.0),   # water level sensor, raw
}


def _post(endpoint: str, payload: dict, token: str = None, ai_control: bool = False,
          as_params: bool = False) -> dict:
    headers = HEADERS.copy()
    if token:
        headers["token"] = token
    if ai_control:
        headers["minversion"] = "3.5"
    if as_params:
        resp = requests.post(f"{API_HOST}{endpoint}", params=payload, headers=headers, timeout=15)
    else:
        resp = requests.post(f"{API_HOST}{endpoint}", data=payload, headers=headers, timeout=15)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        if resp.status_code == 401:
            raise ACInfinityAuthError(f"401 on {endpoint}") from e
        raise
    body = resp.json()
    code = body.get("code")
    if code != 200:
        # 999999 = generic auth/session failure on this API
        if code == 999999 or "appid" in str(body).lower():
            raise ACInfinityAuthError(f"Auth failure on {endpoint}: {body}")
        raise RuntimeError(f"API error on {endpoint}: {body}")
    return body.get("data", {})


def _get_port_mode_settings(token: str, dev_id: str, port: int) -> dict:
    """Fetch current mode settings for a port (required before any write)."""
    return _post("/api/dev/getdevModeSettingList",
                 {"devId": dev_id, "port": port},
                 token=token)


def login(email: str, password: str) -> str:
    """Authenticate and return token (appId)."""
    data = _post("/api/user/appUserLogin", {
        "appEmail": email,
        "appPasswordl": password[:25],  # API only reads first 25 chars
    })
    return data["appId"]


def _write_token_to_env(token: str, env_path: str):
    """Update AC_INFINITY_TOKEN in .env without disturbing other values."""
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()
    if re.search(r"^AC_INFINITY_TOKEN=.*$", content, re.MULTILINE):
        content = re.sub(r"^AC_INFINITY_TOKEN=.*$", f"AC_INFINITY_TOKEN={token}", content, flags=re.MULTILINE)
    else:
        content += f"\nAC_INFINITY_TOKEN={token}\n"
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)


def get_or_refresh_token(email: str, password: str, env_path: str) -> str:
    """Return cached token from env or login fresh and cache it."""
    token = os.getenv("AC_INFINITY_TOKEN", "").strip()
    if token:
        return token
    token = login(email, password)
    _write_token_to_env(token, env_path)
    return token


def fetch_all_devices(token: str) -> list[dict]:
    """Return raw device list from the API."""
    data = _post("/api/user/devInfoListAll", {"userId": token}, token=token)
    return data if isinstance(data, list) else []


def _parse_sensors(info: dict) -> dict:
    """
    Parse deviceInfo.sensors[] into a flat dict of {label: value}.
    Each entry uses the divisor from SENSOR_TYPE to scale the raw integer.

    Zero-value handling:
      - HDS3 hydro probe types (13-19): 0 means "probe disconnected/unsubmerged" — skip.
      - Air sensors (0,2,3,4,6,7) and CO2 (11): 0 is implausible (room temp/humidity/CO2
        never read 0), so 0 means "sensor not connected" — skip.
      - Light (12) and water level (20): 0 is a LEGITIMATE reading (lights off, empty
        reservoir) — pass through.

    Always skips the -327.68 (INT16_MIN/100) HDS3 sentinel.

    NOTE: Multiple sensors of the same type on different access ports collide here
    (last write wins). When a second water-level sensor is wired, this function will
    need to key by (sensorType, accessPort) instead of label. Not yet implemented.
    """
    _allow_zero = {12, 20}  # light, water_level

    result = {}
    for s in info.get("sensors") or []:
        s_type = s.get("sensorType")
        mapping = SENSOR_TYPE.get(s_type)
        if not mapping:
            continue
        label, divisor = mapping
        raw_val = s.get("sensorData")
        if raw_val is None or raw_val <= -32768:
            continue
        if raw_val == 0 and s_type not in _allow_zero:
            continue
        result[label] = round(raw_val / divisor, 2)
    return result


def parse_device(raw: dict) -> dict:
    """Normalize a single device entry into a clean dict."""
    dev_type = raw.get("devType")
    is_outlet = dev_type in OUTLET_DEVICE_TYPES
    info = raw.get("deviceInfo") or {}

    ports = []
    for p in info.get("ports") or []:
        port_entry = {
            "port": p.get("port"),
            "name": p.get("portName") or f"Port {p.get('port')}",
            "online": p.get("online") == 1,
            "mode": p.get("curMode"),
            "load_state": p.get("loadState"),
            "is_outlet": is_outlet,
        }
        speak = p.get("speak", 0)
        if is_outlet:
            port_entry["powered"] = speak > 0
        else:
            port_entry["speed_actual"] = speak
            port_entry["speed_target"] = p.get("onSpead")
        ports.append(port_entry)

    sensors = _parse_sensors(info)

    def f_to_c(f):
        return round((f - 32) * 5 / 9, 2) if f is not None else None

    device = {
        "dev_id":   raw.get("devId"),
        "dev_code": raw.get("devCode"),
        "name":     raw.get("devName"),
        "type":     dev_type,
        "type_label": DEVICE_TYPE_LABELS.get(dev_type, f"Unknown ({dev_type})"),
        "online": raw.get("online") == 1,
        "is_ai": dev_type in AI_DEVICE_TYPES,
        "is_outlet": is_outlet,
        "ports": ports,
        # Built-in sensor hub (accessPort 7)
        "temp_f":        sensors.get("temp_f"),
        "temp_c":        f_to_c(sensors.get("temp_f")),
        "humidity_pct":  sensors.get("humidity"),
        "vpd_kpa":       sensors.get("vpd"),
        # External air probe (accessPort varies)
        "temp_f_ext":    sensors.get("temp_f_ext"),
        "temp_c_ext":    f_to_c(sensors.get("temp_f_ext")),
        "humidity_ext":  sensors.get("humidity_ext"),
        "vpd_ext":       sensors.get("vpd_ext"),
        # Environment probes
        "co2_ppm":       sensors.get("co2_ppm"),
        "light":         sensors.get("light"),
        "water_level":   sensors.get("water_level"),
        # HDS3 hydro probe (present only when connected)
        "ph":            sensors.get("ph"),
        "tds_ppm":       sensors.get("tds_ppm"),
        "ec_us":         sensors.get("ec_us"),
        "ec_ms":         sensors.get("ec_ms"),
        "water_temp_f":  sensors.get("water_temp_f"),
        "water_temp_c":  sensors.get("water_temp_c") or f_to_c(sensors.get("water_temp_f")),
    }
    return device


def _control_port(token: str, dev_id: str, port: int, speed: int, at_type: int, dev_type: int = 0):
    """
    Write speed to a port via PUT /api/dev/modeAndSetting.

    Protocol findings from app traffic capture (2026-05-30, CTR89Q light 0->5):
    - onSelfSpead = new target speed  (NOT onSpead -- we were sending the wrong field)
    - onSpead     = current/old speed from port state  (readback, not the command)
    - devType header required for CTR89Q writes (= device type int, e.g. 20)
    - Full devSetting state dump must be included in the payload
    - restore=false, onlyUpdateSpeed=0 required
    """
    try:
        settings = _get_port_mode_settings(token, dev_id, port)
    except Exception as e:
        print(f"  [WARN] Could not fetch port {port} settings ({e}) -- writing minimal payload")
        settings = {}

    mode_set_id = settings.get("modeSetid", "")

    # Build payload from TOP-LEVEL settings (not devSetting — that's device config, not port state).
    # App traffic capture (2026-05-30) showed the app sends ~125 top-level scalar fields.
    # Exclude nested objects; convert None → 0 (app sends null fields as 0).
    _skip = {"devSetting", "fieldSet", "ipcSetting", "devTimeZone", "devMacAddr",
             "reportSeq", "timestamp"}
    payload: dict = {}
    for k, v in settings.items():
        if k in _skip or isinstance(v, (dict, list)):
            continue
        payload[k] = 0 if v is None else ("true" if v is True else ("false" if v is False else v))

    # Override with the actual control fields
    id_str = "[16,17]" if at_type == 1 else "[16,18]"
    payload.update({
        "devId":               dev_id,
        "externalPort":        port,
        "port":                port,
        "masterPort":          port,
        "atType":              at_type,
        "onSelfSpead":         speed,      # new target speed (NOT onSpead — confirmed from capture)
        "modeAndSettingIdStr": id_str,
        "modeSetid":           mode_set_id,
        "restore":             "false",
        "onlyUpdateSpeed":     0,
    })

    headers = HEADERS.copy()
    headers["token"]      = token
    headers["minversion"] = "3.5"
    if dev_type:
        headers["devType"] = str(dev_type)

    resp = requests.put(f"{API_HOST}/api/dev/modeAndSetting",
                        params=payload, headers=headers, timeout=15)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        if resp.status_code == 401:
            raise ACInfinityAuthError("401 on PUT /api/dev/modeAndSetting") from e
        raise
    body = resp.json()
    if body.get("code") == 200:
        return

    # Fallback: addDevMode (works for ADA4; may help if modeAndSetting still rejects)
    minimal = {
        "devId":        dev_id,
        "externalPort": port,
        "onSpead":      speed,
        "atType":       at_type,
    }
    if mode_set_id:
        minimal["modeSetid"] = mode_set_id
    _post("/api/dev/addDevMode", minimal, token=token, as_params=True, ai_control=True)


def set_port_speed(token: str, dev_id: str, port: int, speed: int, dev_type: int):
    """Set a fan/pump port to manual speed (0-10). 0 stops the port."""
    if speed < 0 or speed > 10:
        raise ValueError("speed must be 0-10")
    _control_port(token, dev_id, port, speed, at_type=1 if speed == 0 else 2, dev_type=dev_type)


def set_outlet(token: str, dev_id: str, port: int, on: bool, dev_type: int = 0):
    """Turn an outlet port on or off (ADA4 / Outlet AI devices)."""
    _control_port(token, dev_id, port, speed=10 if on else 0, at_type=2 if on else 1, dev_type=dev_type)


def ramp_seconds(target_speed: int, current_speed: int = 0, buffer: float = 2.0) -> float:
    """
    Seconds to wait for a CTR89Q port to reach target speed.
    Ramps linearly at 1 speed unit per second, both directions (measured 2026-05-30).
    Add `buffer` (default 2s) for API latency and settling.
    """
    return abs(int(target_speed) - int(current_speed)) + buffer
