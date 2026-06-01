#!/usr/bin/env python3
"""
Diagnostic: dump raw getdevModeSettingList and show the exact modeAndSetting
payload we build, then send it and print the full response.

Targets:
  - "4 x 4"  port 2  (Exhaust Fan -- type 20, CTR89Q)
  - "4 x 4"  port 2  ON then immediately OFF

Run:
    python3 diag_control.py
"""

import json
import os
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(".env"))
load_dotenv(Path("labels.env"))

os.environ["AC_INFINITY_TOKEN"] = ""          # force fresh login

from ac_infinity_client import (
    get_or_refresh_token,
    fetch_all_devices,
    parse_device,
    _get_port_mode_settings,
    _flatten_for_mode_and_setting,
    API_HOST,
    HEADERS,
)

email    = os.getenv("AC_INFINITY_EMAIL", "")
password = os.getenv("AC_INFINITY_PASSWORD", "")
token    = get_or_refresh_token(email, password, ".env")
print(f"Token: {token[:16]}...")

# --------------------------------------------------------------------------
# Find devices
# --------------------------------------------------------------------------
raw_devices = fetch_all_devices(token)
devs = {d["name"]: d for d in [parse_device(r) for r in raw_devices]}
raw_by_name = {r.get("devName"): r for r in raw_devices}

TARGET_DEVICE = "4 x 4"
TARGET_PORT   = 2

dev  = devs.get(TARGET_DEVICE)
if not dev:
    print(f"Device '{TARGET_DEVICE}' not found. Available: {list(devs.keys())}")
    exit(1)

dev_id   = dev["dev_id"]
dev_type = dev["type"]
print(f"\nDevice: {dev['name']}  id={dev_id}  type={dev_type}")

# --------------------------------------------------------------------------
# Dump raw getdevModeSettingList
# --------------------------------------------------------------------------
print(f"\n--- getdevModeSettingList  port {TARGET_PORT} ---")
settings = _get_port_mode_settings(token, dev_id, TARGET_PORT)
print(json.dumps(settings, indent=2))

# --------------------------------------------------------------------------
# Build and show ON payload
# --------------------------------------------------------------------------
id_str_on = "[16,18]"
payload_on = _flatten_for_mode_and_setting(settings, dev_id, TARGET_PORT, 10, 2, id_str_on)
print(f"\n--- modeAndSetting ON payload ({len(payload_on)} keys) ---")
for k, v in sorted(payload_on.items()):
    print(f"  {k:30s} = {v!r}")

# --------------------------------------------------------------------------
# Send PUT modeAndSetting -- ON
# --------------------------------------------------------------------------
headers = HEADERS.copy()
headers["token"]      = token
headers["minversion"] = "3.5"

print(f"\n--- PUT {API_HOST}/api/dev/modeAndSetting  (ON) ---")
resp = requests.put(f"{API_HOST}/api/dev/modeAndSetting",
                    params=payload_on, headers=headers, timeout=15)
print(f"HTTP {resp.status_code}")
try:
    body = resp.json()
    print(json.dumps(body, indent=2))
except Exception:
    print(resp.text[:400])

# --------------------------------------------------------------------------
# If 200 -- check speak after a moment, then turn OFF
# --------------------------------------------------------------------------
if resp.json().get("code") == 200:
    import time
    time.sleep(3)
    fresh = fetch_all_devices(token)
    for r in fresh:
        if r.get("devName") == TARGET_DEVICE:
            for p in (r.get("deviceInfo") or {}).get("ports", []):
                if p.get("port") == TARGET_PORT:
                    print(f"\nAfter ON: speak={p.get('speak')}  onSpead={p.get('onSpead')}  atType={p.get('atType')}")

    payload_off = _flatten_for_mode_and_setting(settings, dev_id, TARGET_PORT, 0, 1, "[16,17]")
    print(f"\n--- PUT modeAndSetting  (OFF) ---")
    resp2 = requests.put(f"{API_HOST}/api/dev/modeAndSetting",
                         params=payload_off, headers=headers, timeout=15)
    print(f"HTTP {resp2.status_code}  code={resp2.json().get('code')}")
else:
    print("\nmodeAndSetting returned non-200 -- checking addDevMode fallback")
    minimal = {
        "devId":         dev_id,
        "externalPort":  TARGET_PORT,
        "onSpead":       10,
        "atType":        2,
    }
    if settings.get("modeSetid"):
        minimal["modeSetid"] = settings["modeSetid"]
    print("addDevMode payload:", minimal)
    resp3 = requests.post(f"{API_HOST}/api/dev/addDevMode",
                          params=minimal, headers=headers, timeout=15)
    print(f"HTTP {resp3.status_code}")
    try:
        print(json.dumps(resp3.json(), indent=2))
    except Exception:
        print(resp3.text[:400])

    import time
    time.sleep(5)
    fresh = fetch_all_devices(token)
    for r in fresh:
        if r.get("devName") == TARGET_DEVICE:
            for p in (r.get("deviceInfo") or {}).get("ports", []):
                if p.get("port") == TARGET_PORT:
                    print(f"\nAfter addDevMode: speak={p.get('speak')}  onSpead={p.get('onSpead')}  atType={p.get('atType')}")

    # stop it
    stop = {**minimal, "onSpead": 0, "atType": 1}
    requests.post(f"{API_HOST}/api/dev/addDevMode",
                  params=stop, headers=headers, timeout=15)
    print("Stop sent.")
