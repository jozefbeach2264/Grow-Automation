#!/usr/bin/env python3
"""
analyze_packets.py — Analyze BLE notification packets from notifications.jsonl.

Usage:
    # Analyze captured packets:
    python scripts/analyze_packets.py --in notifications.jsonl

    # Show annotated first packet only:
    python scripts/analyze_packets.py --in notifications.jsonl --first

    # Live mode: tail the file and highlight byte changes in real time:
    python scripts/analyze_packets.py --in notifications.jsonl --live

    # Focus on a specific characteristic:
    python scripts/analyze_packets.py --in notifications.jsonl --char 70d51002
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aci_ble_lab.common import bold, dim, green, cyan, yellow, red


def load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def hex_to_bytes(hexstr: str) -> list[int]:
    return [int(hexstr[i:i+2], 16) for i in range(0, len(hexstr), 2)]


def group_by_char(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        char = r.get("characteristic", "unknown")
        # Normalize to short UUID prefix for display
        grouped[char].append(r)
    return dict(grouped)


def short_uuid(uuid: str) -> str:
    """Return first 8 chars of UUID for compact display."""
    return uuid[:8] if len(uuid) >= 8 else uuid


def print_byte_diff_report(char_uuid: str, packets: list[dict]) -> None:
    if not packets:
        return

    all_bytes = [hex_to_bytes(p["hex"]) for p in packets]
    lengths = set(len(b) for b in all_bytes)

    print(f"\n{'='*72}")
    print(f"Characteristic: {cyan(char_uuid)}")
    print(f"  Packets     : {len(packets)}")
    print(f"  Lengths     : {sorted(lengths)} bytes")
    print(f"  Time range  : {packets[0]['timestamp']} → {packets[-1]['timestamp']}")

    if len(lengths) > 1:
        print(yellow("  WARNING: Variable-length packets — only analyzing fixed-length ones"))
        # Use most common length
        from collections import Counter
        common_len = Counter(len(b) for b in all_bytes).most_common(1)[0][0]
        all_bytes = [b for b in all_bytes if len(b) == common_len]
        print(f"  Using       : {common_len}-byte packets ({len(all_bytes)} of {len(packets)})")

    if len(all_bytes) < 2:
        print(yellow("  Only 1 packet — can't compute diff"))
        return

    n = len(all_bytes[0])

    # Per-byte analysis
    min_val = [255] * n
    max_val = [0] * n
    seen_vals: list[set] = [set() for _ in range(n)]

    for b in all_bytes:
        for i, v in enumerate(b):
            min_val[i] = min(min_val[i], v)
            max_val[i] = max(max_val[i], v)
            seen_vals[i].add(v)

    changing = [i for i in range(n) if max_val[i] != min_val[i]]
    stable   = [i for i in range(n) if max_val[i] == min_val[i]]

    print(f"\n  Stable bytes : {len(stable)}/{n}")
    print(f"  Changing bytes: {bold(str(len(changing)))}/{n}")

    if changing:
        print(f"\n  {'Byte':>4}  {'Min':>4}  {'Max':>4}  {'Delta':>5}  {'UniqueVals':>10}  Values")
        print(f"  {'----':>4}  {'---':>4}  {'---':>4}  {'-----':>5}  {'----------':>10}  ------")
        for i in changing:
            vals = sorted(seen_vals[i])
            delta = max_val[i] - min_val[i]
            vals_str = str(vals) if len(vals) <= 8 else f"[{vals[0]}..{vals[-1]}] ({len(vals)} unique)"
            print(f"  {i:>4}  {min_val[i]:>4}  {max_val[i]:>4}  {delta:>5}  {len(seen_vals[i]):>10}  {vals_str}")

    # First packet annotated hex dump
    print(f"\n  First packet hex dump ({n} bytes):")
    _print_hex_dump(all_bytes[0], changing_indices=set(changing))

    # Last packet hex dump if different
    if all_bytes[-1] != all_bytes[0]:
        print(f"\n  Last packet hex dump ({n} bytes):")
        _print_hex_dump(all_bytes[-1], ref=all_bytes[0], changing_indices=set(changing))


def _print_hex_dump(data: list[int], ref: list[int] | None = None, changing_indices: set[int] | None = None) -> None:
    """Print a formatted hex dump with optional diff highlighting."""
    col_width = 16
    for row_start in range(0, len(data), col_width):
        row = data[row_start:row_start + col_width]
        offset_str = f"  {row_start:04x}: "
        hex_parts = []
        ascii_parts = []
        for i, byte in enumerate(row):
            global_idx = row_start + i
            is_changing = changing_indices and global_idx in changing_indices
            is_changed = ref is not None and global_idx < len(ref) and ref[global_idx] != byte

            hex_byte = f"{byte:02x}"
            if is_changed:
                hex_byte = green(hex_byte)
            elif is_changing:
                hex_byte = yellow(hex_byte)

            hex_parts.append(hex_byte)
            c = chr(byte) if 32 <= byte < 127 else "."
            ascii_parts.append(c)

        hex_row = " ".join(hex_parts)
        ascii_row = "".join(ascii_parts)
        print(f"{offset_str}{hex_row}   {dim(ascii_row)}")


def live_mode(path: str, char_filter: str | None, interval: float = 0.5) -> None:
    """Tail the file and print diffs as new packets arrive."""
    print(bold(f"\nLive mode — watching {path}"))
    print(dim("Press Ctrl+C to stop.\n"))

    seen_count: dict[str, int] = defaultdict(int)
    last_bytes: dict[str, list[int]] = {}

    try:
        while True:
            records = load_jsonl(path)
            grouped = group_by_char(records)

            for char_uuid, pkts in grouped.items():
                if char_filter and char_filter.lower() not in char_uuid.lower():
                    continue

                prev_seen = seen_count[char_uuid]
                new_pkts = pkts[prev_seen:]
                if not new_pkts:
                    continue

                seen_count[char_uuid] = len(pkts)

                for pkt in new_pkts:
                    curr = hex_to_bytes(pkt["hex"])
                    prev = last_bytes.get(char_uuid)
                    last_bytes[char_uuid] = curr

                    ts = pkt["timestamp"]
                    n = len(curr)

                    if prev is None:
                        print(f"{dim(ts)}  {cyan(short_uuid(char_uuid))}  {n}B  (first packet)")
                        _print_hex_dump(curr)
                    else:
                        changed_idx = [i for i in range(min(len(prev), n)) if prev[i] != curr[i]]
                        if changed_idx:
                            print(f"{dim(ts)}  {cyan(short_uuid(char_uuid))}  {n}B  "
                                  f"{green(str(len(changed_idx)))} byte(s) changed: "
                                  f"idx={changed_idx[:10]}{'...' if len(changed_idx) > 10 else ''}")
                            for i in changed_idx[:20]:
                                print(f"  byte[{i:3d}]: {prev[i]:3d} (0x{prev[i]:02x}) → "
                                      f"{green(f'{curr[i]:3d}')} ({green(f'0x{curr[i]:02x}')})")
                        else:
                            print(f"{dim(ts)}  {cyan(short_uuid(char_uuid))}  {n}B  {dim('(no change)')}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nLive mode stopped.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Analyze BLE notification packets from a .jsonl capture file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--in", dest="input_path", required=True,
                   help="Path to .jsonl file (output of listen_ble.py)")
    p.add_argument("--char", default=None,
                   help="Filter to a specific characteristic UUID (partial match OK)")
    p.add_argument("--first", action="store_true",
                   help="Print annotated dump of first packet only")
    p.add_argument("--live", action="store_true",
                   help="Tail the file and print byte diffs in real time")
    args = p.parse_args()

    if not Path(args.input_path).exists():
        print(f"ERROR: File not found: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    if args.live:
        live_mode(args.input_path, args.char)
        return

    records = load_jsonl(args.input_path)
    if not records:
        print("No records found in file.")
        return

    grouped = group_by_char(records)

    print(bold(f"\nACI BLE Lab — Packet Analyzer"))
    print(f"File   : {args.input_path}")
    print(f"Records: {len(records)}")
    print(f"Chars  : {len(grouped)}")
    for char_uuid, pkts in sorted(grouped.items()):
        lengths = set(p["length"] for p in pkts)
        print(f"  {cyan(char_uuid)}  {len(pkts)} packets  lengths={sorted(lengths)}")

    for char_uuid, pkts in sorted(grouped.items()):
        if args.char and args.char.lower() not in char_uuid.lower():
            continue
        if args.first:
            print(f"\n{bold('First packet')} from {cyan(char_uuid)}:")
            _print_hex_dump(hex_to_bytes(pkts[0]["hex"]))
        else:
            print_byte_diff_report(char_uuid, pkts)


if __name__ == "__main__":
    main()
