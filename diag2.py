#!/usr/bin/env python3
"""
diag2.py -- Find why modeAndSetting returns 999999.

Strategy: send increasingly minimal payloads until it works (or we find the
bad field), then cross-check against ADA4 (which works).

Also checks what modeType the ADA4 is in vs CTR89Q.
"""

import json
import os
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(".env"))
load_dotenv(Path("labels.env"))

os.environ["AC_INFINITY_TOKEN"] = ""

from ac_infinity_client import (
    get_or_refresh_token,
    fetch_all_devices,
    _get_port_mode_settings,
    API_HOST,
    HEADERS,
)

email    = os.getenv("AC_INFINITY_EMAIL", "")
password = os.getenv("AC_INFINITY_PASSWORD", "")
token    = get_or_refresh_token(email, password, ".env")

raw_devices = fetch_all_devices(token)

# Find our two test targets
targets = {}
for r in raw_devices:
    name = r.get("devName", "")
    dev_id = r.get("devId")
    dev_type = r.get("devType")
    if name in ("4 x 4", "Auxiliary Outputs"):
        targets[name] = {"dev_id": dev_id, "dev_type": dev_type}

print("Targets found:", list(targets.keys()))


def put_mode(dev_id, port, payload, label=""):
    headers = HEADERS.copy()
    headers["token"]      = token
    headers["minversion"] = "3.5"
    resp = requests.put(f"{API_HOST}/api/dev/modeAndSetting",
                        params=payload, headers=headers, timeout=15)
    body = resp.json()
    code = body.get("code")
    msg  = body.get("msg", "")
    print(f"  [{label}]  HTTP {resp.status_code}  code={code}  {msg}")
    return code == 200


def test_minimal(name, port, speed, at_type, label="minimal"):
    """Try the absolute minimum fields."""
    t = targets[name]
    settings = _get_port_mode_settings(token, t["dev_id"], port)
    id_str = "[16,17]" if at_type == 1 else "[16,18]"
    payload = {
        "devId":               t["dev_id"],
        "externalPort":        port,
        "port":                port,
        "masterPort":          port,
        "onSpead":             speed,
        "atType":              at_type,
        "modeAndSettingIdStr": id_str,
        "modeSetid":           settings.get("modeSetid", ""),
    }
    return put_mode(t["dev_id"], port, payload, label)


def test_with_extra(name, port, speed, at_type, extra_keys, label=""):
    """Minimal plus specific extra keys from settings."""
    t = targets[name]
    settings = _get_port_mode_settings(token, t["dev_id"], port)
    id_str = "[16,17]" if at_type == 1 else "[16,18]"
    payload = {
        "devId":               t["dev_id"],
        "externalPort":        port,
        "port":                port,
        "masterPort":          port,
        "onSpead":             speed,
        "atType":              at_type,
        "modeAndSettingIdStr": id_str,
        "modeSetid":           settings.get("modeSetid", ""),
    }
    for k in extra_keys:
        v = settings.get(k)
        if v is None:
            dev_s = settings.get("devSetting") or {}
            v = dev_s.get(k)
        if v is not None:
            payload[k] = v
    return put_mode(t["dev_id"], port, payload, label)


# -------------------------------------------------------------------------
print("\n=== ADA4 port 1 (outlet) ===")
s_ada4 = _get_port_mode_settings(token, targets["Auxiliary Outputs"]["dev_id"], 1)
print(f"  modeType={s_ada4.get('modeType')}  atType={s_ada4.get('atType')}  speak={s_ada4.get('speak')}")

print("\n=== CTR89Q port 2 (exhaust fan) ===")
s_ctr = _get_port_mode_settings(token, targets["4 x 4"]["dev_id"], 2)
print(f"  modeType={s_ctr.get('modeType')}  atType={s_ctr.get('atType')}  speak={s_ctr.get('speak')}")

# -------------------------------------------------------------------------
print("\n=== Test 1: minimal payload -- ADA4 port 1  ON ===")
test_minimal("Auxiliary Outputs", 1, 10, 2, "ada4-minimal-ON")

print("\n=== Test 2: minimal payload -- CTR89Q port 2  ON ===")
test_minimal("4 x 4", 2, 10, 2, "ctr89q-minimal-ON")

# -------------------------------------------------------------------------
print("\n=== Test 3: add modeType=1 (force manual) -- CTR89Q port 2  ON ===")
test_with_extra("4 x 4", 2, 10, 2, ["modeType"], "ctr89q+modeType1")
# Actually override modeType to 1 (manual)
t = targets["4 x 4"]
s = _get_port_mode_settings(token, t["dev_id"], 2)
payload_m1 = {
    "devId": t["dev_id"], "externalPort": 2, "port": 2, "masterPort": 2,
    "onSpead": 10, "atType": 2, "modeAndSettingIdStr": "[16,18]",
    "modeSetid": s.get("modeSetid", ""), "modeType": 1,
}
put_mode(t["dev_id"], 2, payload_m1, "ctr89q+modeType=1")

# -------------------------------------------------------------------------
print("\n=== Test 4: add offSpead + onSelfSpead -- CTR89Q port 2  ON ===")
test_with_extra("4 x 4", 2, 10, 2,
                ["offSpead", "onSelfSpead", "schedStartTime", "schedEndtTime"],
                "ctr89q+sched")

# -------------------------------------------------------------------------
print("\n=== Test 5: exclude portParamData, try rest of devSetting -- CTR89Q ===")
t = targets["4 x 4"]
s = _get_port_mode_settings(token, t["dev_id"], 2)
ds = s.get("devSetting") or {}
payload5 = {
    "devId": t["dev_id"], "externalPort": 2, "port": 2, "masterPort": 2,
    "onSpead": 10, "atType": 2, "modeAndSettingIdStr": "[16,18]",
    "modeSetid": s.get("modeSetid", ""),
}
# Add devSetting scalars except portParamData
for k, v in ds.items():
    if k == "portParamData":
        continue
    if isinstance(v, (dict, list)):
        continue
    if v is None:
        payload5[k] = 0
    elif isinstance(v, bool):
        payload5[k] = "true" if v else "false"
    else:
        payload5[k] = v
put_mode(t["dev_id"], 2, payload5, "ctr89q-noPortParam")

# -------------------------------------------------------------------------
print("\n=== ADA4 port 1 OFF (cleanup) ===")
test_minimal("Auxiliary Outputs", 1, 0, 1, "ada4-minimal-OFF")
print("\n=== CTR89Q port 2 OFF (cleanup) ===")
test_minimal("4 x 4", 2, 0, 1, "ctr89q-minimal-OFF")

print("\ndone.")
