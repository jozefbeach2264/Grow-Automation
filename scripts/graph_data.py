#!/usr/bin/env python3
"""
graph_data.py — Collect live status stream from AC Infinity controller and graph it.

Collects for --duration seconds (default 90), then shows a matplotlib plot with:
  - Temperature, Humidity, VPD (from 1EFF status packet)
  - Per-port work_type and speed (polled via get_model_data every 5s)
  - Raw sensor tail bytes (115-126) for any attached probes

Usage:
  python scripts/graph_data.py
  python scripts/graph_data.py --duration 120
  python scripts/graph_data.py --ports 1 2 3  # poll specific ports
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, r"C:\Users\Ziggs\aci-ble-lab\.venv\Lib\site-packages")

from bleak import BleakClient, BleakScanner
from ac_infinity_ble.protocol import Protocol
from ac_infinity_ble.util import get_bits, get_short

ADDRESS    = "50:78:7D:C5:0C:6E"
CHAR_WRITE = "70d51001-2c7f-4e75-ae8a-d758951ce4e0"
CHAR_READ  = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"

proto = Protocol()
TYPE_MULTIPORT = 9
TYPE_GLOBAL    = 20


def decode_1eff(data: bytes) -> dict | None:
    """Decode 127-byte 1EFF status packet using library offsets."""
    if len(data) < 18 or data[0] != 0x1E or data[1] != 0xFF:
        return None
    return {
        "hw_type":    data[3],
        "is_degree":  bool(data[6] & 0x01),
        "tmp_state":  get_bits(data[6], 1, 2),
        "hum_state":  get_bits(data[6], 3, 2),
        "vpd_state":  get_bits(data[6], 5, 2),
        "choose_port": get_bits(data[7], 4, 4),
        "tmp":        get_short(data, 8)  / 100.0,
        "hum":        get_short(data, 10) / 100.0,
        "vpd":        get_short(data, 12) / 100.0,
        "fan_type":   get_short(data, 14),
        "fan_state":  get_bits(data[16], 0, 2),
        "work_type":  get_bits(data[17], 4, 4),
    }


def decode_sensor_tail(data: bytes) -> list[dict]:
    """Decode bytes 115-126 sensor probe groups [port_id, 0x67, val1, val2]."""
    sensors = []
    for base in (115, 119, 123):
        if base + 3 >= len(data):
            break
        port_id  = data[base]
        type_code = data[base + 1]
        val1     = data[base + 2]
        val2     = data[base + 3]
        if port_id == 0 and type_code == 0:
            continue
        sensors.append({"port": port_id, "type": type_code, "v1": val1, "v2": val2})
    return sensors


def decode_port_response(data: bytes) -> dict | None:
    """Decode A5-1C get_model_data (cmd_type=1) response into port state."""
    if len(data) < 12 or data[0] != 0xA5 or data[1] != 0x1C:
        return None
    data_len = (data[2] << 8) | data[3]
    cmd_type = data[9]
    if cmd_type != 1:
        return None
    payload = data[10:10 + data_len]
    result = {}
    i = 0
    while i + 2 < len(payload):
        tag = payload[i]
        length = payload[i + 1]
        if tag == 0xFF:
            result["port_num"] = payload[i + 1] if i + 1 < len(payload) else None
            break
        value = payload[i + 2:i + 2 + length]
        if tag == 0x10 and length == 1: result["work_type"]  = value[0]
        if tag == 0x11 and length == 1: result["level_off"]  = value[0]
        if tag == 0x12 and length == 1: result["level_on"]   = value[0]
        i += 2 + length
    return result


async def _scan_until_found(address: str) -> bool:
    found = asyncio.Event()
    def cb(dev, _):
        if dev.address.upper() == address.upper():
            found.set()
    async with BleakScanner(detection_callback=cb):
        try:
            await asyncio.wait_for(found.wait(), 60)
            return True
        except asyncio.TimeoutError:
            return False


async def collect(duration: int, poll_ports: list[int]) -> dict:
    """Connect, collect status stream, poll port states, return all data."""
    print("Scanning...")
    if not await _scan_until_found(ADDRESS):
        print("Controller not found.")
        return {}

    print("Connecting...")
    async with BleakClient(ADDRESS) as client:
        print(f"Connected (MTU={client.mtu_size}B)")

        a5_queue: asyncio.Queue = asyncio.Queue()
        status_ts   = []   # timestamps
        status_rows = []   # decoded dicts
        sensor_rows = []   # (ts, list[dict])
        port_states: dict[int, list] = defaultdict(list)  # port -> [(ts, state_dict)]
        seq = 1

        def on_notif(_, raw):
            d = bytes(raw)
            if d[:2] == b'\xa5\x1c':
                a5_queue.put_nowait(d)
            elif d[:2] == b'\x1e\xff' and len(d) == 127:
                ts = time.time()
                decoded = decode_1eff(d)
                sensors = decode_sensor_tail(d)
                if decoded:
                    status_ts.append(ts)
                    status_rows.append(decoded)
                if sensors:
                    sensor_rows.append((ts, sensors))

        await asyncio.sleep(0.3)
        for attempt in range(3):
            try:
                warm = proto.get_model_data(TYPE_GLOBAL, 0, 0)
                await client.write_gatt_char(CHAR_WRITE, warm, response=False)
                await asyncio.sleep(0.4 + attempt * 0.3)
                await client.start_notify(CHAR_READ, on_notif)
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                print("[warn] Could not subscribe to notifications.")

        # Drain startup noise
        await asyncio.sleep(2)
        while not a5_queue.empty():
            a5_queue.get_nowait()
        status_ts.clear()
        status_rows.clear()
        sensor_rows.clear()

        t_start = time.time()
        t_last_poll = 0.0
        POLL_INTERVAL = 5.0

        print(f"\nCollecting for {duration}s  (ports polled: {poll_ports or 'none'})...\n")
        print(f"{'Time':>6}  {'Temp':>6}  {'Hum':>6}  {'VPD':>5}  {'WkType':>6}  {'FanSt':>5}")
        print("-" * 50)

        while time.time() - t_start < duration:
            elapsed = time.time() - t_start

            # Periodic per-port polling
            if poll_ports and (elapsed - t_last_poll) >= POLL_INTERVAL:
                t_last_poll = elapsed
                while not a5_queue.empty():
                    a5_queue.get_nowait()
                for port in poll_ports:
                    seq += 1
                    cmd = proto.get_model_data(TYPE_MULTIPORT, port, seq)
                    await client.write_gatt_char(CHAR_WRITE, cmd, response=False)
                    try:
                        resp = await asyncio.wait_for(a5_queue.get(), 3.0)
                        state = decode_port_response(resp)
                        if state:
                            port_states[port].append((time.time(), state))
                    except asyncio.TimeoutError:
                        pass

            # Live print of latest status row
            if status_rows:
                r = status_rows[-1]
                t = r["tmp"]
                h = r["hum"]
                v = r["vpd"]
                wt = {1: "OFF", 2: "ON"}.get(r["work_type"], str(r["work_type"]))
                fs = r["fan_state"]
                print(f"{elapsed:>6.1f}  {t:>6.2f}  {h:>6.2f}  {v:>5.3f}  {wt:>6}  {fs:>5}", end="\r")

            await asyncio.sleep(1.0)

        print()
        try:
            await client.stop_notify(CHAR_READ)
        except Exception:
            pass

        return {
            "ts":           status_ts,
            "status":       status_rows,
            "sensors":      sensor_rows,
            "port_states":  dict(port_states),
        }


def plot(data: dict, duration: int):
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime

    if not data.get("ts"):
        print("No data collected.")
        return

    ts = data["ts"]
    rows = data["status"]
    t0 = ts[0]
    elapsed = [t - t0 for t in ts]
    dts = [datetime.fromtimestamp(t) for t in ts]

    tmp  = [r["tmp"]       for r in rows]
    hum  = [r["hum"]       for r in rows]
    vpd  = [r["vpd"]       for r in rows]
    wt   = [r["work_type"] for r in rows]
    fs   = [r["fan_state"] for r in rows]

    port_states = data.get("port_states", {})
    sensor_rows = data.get("sensors", [])

    # Build per-probe time series from sensor tail
    probe_data: dict[int, tuple[list, list, list]] = {}
    for ts_s, sensors in sensor_rows:
        for s in sensors:
            pid = s["port"]
            if pid not in probe_data:
                probe_data[pid] = ([], [], [])
            probe_data[pid][0].append(ts_s - t0)
            probe_data[pid][1].append(s["v1"])
            probe_data[pid][2].append(s["v2"])

    n_panels = 3
    if port_states:     n_panels += 1
    if probe_data:      n_panels += 1

    fig, axes = plt.subplots(n_panels, 1, figsize=(13, 3.5 * n_panels), sharex=True)
    fig.suptitle(f"AC Infinity ACI_V3.5_CTRLER — {len(rows)} samples over {duration}s",
                 fontsize=13, fontweight="bold")

    ax_idx = 0

    # Panel 1: Temperature
    ax = axes[ax_idx]; ax_idx += 1
    ax.plot(elapsed, tmp, color="#e05c2e", linewidth=1.5, label="Temp (°C)" if rows[0]["is_degree"] else "Temp (°F)")
    ax.set_ylabel("Temperature")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("Temperature")

    # Panel 2: Humidity
    ax = axes[ax_idx]; ax_idx += 1
    ax.plot(elapsed, hum, color="#4da6e8", linewidth=1.5, label="Humidity (%RH)")
    ax.set_ylabel("Humidity (%)")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("Humidity")

    # Panel 3: VPD + fan state
    ax = axes[ax_idx]; ax_idx += 1
    ax2 = ax.twinx()
    ax.plot(elapsed, vpd, color="#7ec87e", linewidth=1.5, label="VPD (kPa)")
    ax2.step(elapsed, wt, color="#aaaaaa", linewidth=1, where="post", alpha=0.6, label="work_type")
    ax2.step(elapsed, fs, color="#cc88cc", linewidth=1, where="post", alpha=0.6, label="fan_state")
    ax.set_ylabel("VPD (kPa)")
    ax2.set_ylabel("State (enum)")
    ax2.set_ylim(-0.5, 5.5)
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title("VPD + Fan State")

    # Panel 4: Per-port state (polled)
    if port_states:
        ax = axes[ax_idx]; ax_idx += 1
        colors = ["#e05c2e", "#4da6e8", "#7ec87e", "#cc88cc",
                  "#f0a030", "#60c0c0", "#c060c0", "#80a0e0"]
        for i, (port, readings) in enumerate(sorted(port_states.items())):
            pts = [r - t0 for r, _ in readings]
            levels = [s.get("level_on", 0) if s.get("work_type") == 2 else 0 for _, s in readings]
            c = colors[i % len(colors)]
            ax.step(pts, levels, where="post", linewidth=2, label=f"Port {port} speed",
                    color=c, marker="o", markersize=4)
        ax.set_ylabel("Speed (0-10)")
        ax.set_ylim(-0.5, 11)
        ax.legend(loc="upper right", fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_title("Per-Port Speed (polled)")

    # Panel 5: Probe sensor tail values
    if probe_data:
        ax = axes[ax_idx]; ax_idx += 1
        colors = ["#e05c2e", "#4da6e8", "#7ec87e", "#cc88cc",
                  "#f0a030", "#60c0c0", "#c060c0"]
        for i, (pid, (ets, v1s, v2s)) in enumerate(sorted(probe_data.items())):
            c = colors[i % len(colors)]
            ax.plot(ets, v1s, color=c, linewidth=1.5, label=f"Probe port={pid} v1")
            ax.plot(ets, v2s, color=c, linewidth=1,   linestyle="--", label=f"Probe port={pid} v2", alpha=0.7)
        ax.set_ylabel("Raw value")
        ax.legend(loc="upper right", fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_title("Sensor Tail Probes (bytes 115-126)")

    axes[-1].set_xlabel("Time (seconds)")
    plt.tight_layout()
    out_path = Path(__file__).parent.parent / "controller_data.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nPlot saved: {out_path}")
    import os
    os.startfile(out_path)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Collect and graph AC Infinity controller data")
    p.add_argument("--address",  default=ADDRESS,    help="Controller MAC address")
    p.add_argument("--duration", type=int, default=90, help="Collection time in seconds (default 90)")
    p.add_argument("--ports",    type=int, nargs="*", default=[1],
                   help="Ports to poll for speed/state (default: 1). Pass 0 for none.")
    args = p.parse_args()

    poll_ports = [p for p in (args.ports or []) if p > 0]

    data = asyncio.run(collect(args.duration, poll_ports))
    if data:
        plot(data, args.duration)
