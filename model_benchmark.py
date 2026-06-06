#!/usr/bin/env python3
"""
Head-to-head LLM benchmark for the grow-automation set_speed schema.

For each model under test, fires the same 16 prompts (the 8 variable-speed
ports across both CTR89Q controllers, ramp-up then ramp-down) and scores:

  - schema_valid:  action == "set_speed" AND value is int 0-10 AND value matches
  - json_parsed:   response was parseable JSON at all
  - device_match:  AI echoed the right device name
  - port_match:    AI echoed the right port number
  - latency_sec:   wall time per request

NO hardware writes. This is pure prompt -> response measurement.
Repeats each prompt N_TRIALS times to smooth single-shot noise.

Results land in:  benchmark_results.json
Console prints a side-by-side summary table.
"""

import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from ac_infinity_client import (
    fetch_all_devices,
    get_or_refresh_token,
    parse_device,
)

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

EMAIL        = os.getenv("AC_INFINITY_EMAIL", "")
PASSWORD     = os.getenv("AC_INFINITY_PASSWORD", "")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Models under test. Add/remove freely.
MODELS_UNDER_TEST = [
    "deepseek-r1:1.5b",
    "qwen2.5:3b-instruct",
    "llama3.2:3b",
    "phi4-mini",
]

N_TRIALS         = 2     # repeat each prompt this many times
REQUEST_TIMEOUT  = 60    # per-request timeout in seconds
TARGET_DEV_TYPES = {20}  # CTR89Q only

OUT_FILE = Path(__file__).parent / "benchmark_results.json"


# ---------------------------------------------------------------------------
# Ollama plumbing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    depth, start = 0, -1
    for i, c in enumerate(cleaned):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def warmup(model: str):
    try:
        requests.post(f"{OLLAMA_HOST}/api/generate", json={
            "model":  model,
            "prompt": "ping",
            "stream": False,
        }, timeout=120)
    except Exception:
        pass


def ask(model: str, prompt: str) -> tuple[dict | None, float, str]:
    t0 = time.time()
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/generate", json={
            "model":  model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        raw = r.json().get("response", "")
    except Exception as e:
        return None, time.time() - t0, f"<request failed: {e}>"
    return _extract_json(raw), time.time() - t0, raw


# ---------------------------------------------------------------------------
# Prompt + scoring
# ---------------------------------------------------------------------------

def build_prompt(dev_name: str, port_num: int, port_label: str, target: int) -> str:
    return (
        "You are commanding hardware in a grow-tent test rig. "
        f"Set device \"{dev_name}\" port {port_num} (\"{port_label}\") to speed {target}. "
        "Respond with this JSON object only and no commentary:\n"
        f'{{"action": "set_speed", "device": "{dev_name}", '
        f'"port": {port_num}, "value": {target}}}'
    )


def score(action: dict | None, expected_dev: str, expected_port: int,
          expected_val: int) -> dict:
    out = {
        "json_parsed":  action is not None,
        "schema_valid": False,
        "device_match": False,
        "port_match":   False,
        "value_match":  False,
        "wrong_action": None,
    }
    if not isinstance(action, dict):
        return out
    if action.get("action") == "set_speed":
        out["schema_valid"] = True
    else:
        out["wrong_action"] = action.get("action")
    if action.get("device") == expected_dev:
        out["device_match"] = True
    if action.get("port") == expected_port:
        out["port_match"] = True
    val = action.get("value")
    if isinstance(val, int) and val == expected_val:
        out["value_match"] = True
    return out


# ---------------------------------------------------------------------------
# Run one model
# ---------------------------------------------------------------------------

def run_model(model: str, prompts: list[dict]) -> dict:
    print(f"\n{'=' * 72}")
    print(f"  MODEL: {model}")
    print(f"{'=' * 72}")
    print(f"  warming up...")
    warmup(model)

    results = []
    for trial in range(N_TRIALS):
        print(f"\n  --- trial {trial + 1}/{N_TRIALS} ---")
        for i, p in enumerate(prompts, 1):
            action, latency, raw = ask(model, p["prompt"])
            s = score(action, p["device"], p["port"], p["value"])
            tag = "PASS" if (s["schema_valid"] and s["value_match"]) else "FAIL"
            extra = ""
            if not s["json_parsed"]:
                extra = f"  [no JSON: {raw[:80]!r}]"
            elif s["wrong_action"]:
                extra = f"  [action={s['wrong_action']!r}]"
            elif not s["value_match"]:
                extra = f"  [value mismatch: {action.get('value')!r}]"
            print(f"    {tag:4}  {latency:5.1f}s  {p['label']:<35} {extra}")
            results.append({
                "trial":    trial + 1,
                "device":   p["device"],
                "port":     p["port"],
                "target":   p["value"],
                "direction": p["direction"],
                "label":    p["label"],
                "latency":  round(latency, 2),
                "action":   action,
                "raw":      raw[:300],
                "score":    s,
            })

    return summarize(model, results)


def summarize(model: str, results: list[dict]) -> dict:
    n = len(results)
    passes = [r for r in results if r["score"]["schema_valid"] and r["score"]["value_match"]]
    json_ok = [r for r in results if r["score"]["json_parsed"]]
    schema_ok = [r for r in results if r["score"]["schema_valid"]]
    dev_ok = [r for r in results if r["score"]["device_match"]]
    port_ok = [r for r in results if r["score"]["port_match"]]
    val_ok = [r for r in results if r["score"]["value_match"]]
    lats = [r["latency"] for r in results]
    return {
        "model":            model,
        "n":                n,
        "pass_rate":        len(passes) / n if n else 0,
        "json_rate":        len(json_ok) / n if n else 0,
        "schema_rate":      len(schema_ok) / n if n else 0,
        "device_rate":      len(dev_ok) / n if n else 0,
        "port_rate":        len(port_ok) / n if n else 0,
        "value_rate":       len(val_ok) / n if n else 0,
        "latency_median":   round(statistics.median(lats), 2) if lats else 0,
        "latency_p90":      round(sorted(lats)[int(len(lats) * 0.9) - 1], 2) if lats else 0,
        "latency_max":      round(max(lats), 2) if lats else 0,
        "latency_min":      round(min(lats), 2) if lats else 0,
        "results":          results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Authenticating to AC Infinity to fetch real device/port labels...")
    token = get_or_refresh_token(EMAIL, PASSWORD, str(ENV_PATH))
    raw_devs = fetch_all_devices(token)
    devices  = [parse_device(r) for r in raw_devs]
    targets  = [d for d in devices if d["type"] in TARGET_DEV_TYPES and d["online"]]

    prompts: list[dict] = []
    for dev in targets:
        for port in dev["ports"]:
            if port["is_outlet"] or not port["online"]:
                continue
            for direction, target in (("up", 10), ("down", 0)):
                prompts.append({
                    "device":    dev["name"],
                    "port":      port["port"],
                    "value":     target,
                    "direction": direction,
                    "label":     f"{dev['name'][:14]} p{port['port']} {direction}->{target}",
                    "prompt":    build_prompt(dev["name"], port["port"], port["name"], target),
                })

    print(f"Built {len(prompts)} prompts ({len(prompts) // 2} ports x 2 directions)")
    print(f"Will run each prompt {N_TRIALS}x against {len(MODELS_UNDER_TEST)} model(s)")
    print(f"Total inferences: {len(prompts) * N_TRIALS * len(MODELS_UNDER_TEST)}")

    summaries = []
    for model in MODELS_UNDER_TEST:
        try:
            summaries.append(run_model(model, prompts))
        except Exception as e:
            print(f"\n  [{model}] FATAL: {e}")
            summaries.append({"model": model, "fatal": str(e)})

    # Save full results
    OUT_FILE.write_text(json.dumps(summaries, indent=2))

    # Print summary table
    print(f"\n\n{'=' * 90}")
    print(f"  COMPARISON TABLE")
    print(f"{'=' * 90}")
    print(f"  {'model':<26}  {'pass%':>6} {'json%':>6} {'schema%':>8} "
          f"{'med s':>6} {'p90 s':>6} {'max s':>6}")
    print(f"  {'-' * 26}  {'-' * 6} {'-' * 6} {'-' * 8} "
          f"{'-' * 6} {'-' * 6} {'-' * 6}")
    for s in summaries:
        if "fatal" in s:
            print(f"  {s['model']:<26}  FATAL: {s['fatal'][:50]}")
            continue
        print(f"  {s['model']:<26}  "
              f"{s['pass_rate']*100:5.1f}% "
              f"{s['json_rate']*100:5.1f}% "
              f"{s['schema_rate']*100:7.1f}% "
              f"{s['latency_median']:6.1f} "
              f"{s['latency_p90']:6.1f} "
              f"{s['latency_max']:6.1f}")
    print()
    print(f"  Full results in: {OUT_FILE}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
