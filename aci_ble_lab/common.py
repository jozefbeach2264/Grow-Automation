"""Shared utilities: ACI device matching, JSON I/O, terminal colours.

Forked from PR #2 (sethmblack) unchanged.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import colorama
    colorama.init(autoreset=True)
    _COLORS = True
except ImportError:
    _COLORS = False

_GREEN  = "\033[92m" if _COLORS else ""
_YELLOW = "\033[93m" if _COLORS else ""
_RED    = "\033[91m" if _COLORS else ""
_CYAN   = "\033[96m" if _COLORS else ""
_BOLD   = "\033[1m"  if _COLORS else ""
_DIM    = "\033[2m"  if _COLORS else ""
_RESET  = "\033[0m"  if _COLORS else ""

ACI_NAME_PATTERNS = [
    "aci_v", "aci", "ctrler", "ctrl", "ac infinity", "acinfinity", "uis",
]


def is_aci_device(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(p in lower for p in ACI_NAME_PATTERNS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json(data: object, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_jsonl(record: dict, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_ok(msg: str) -> None:   print(f"{_GREEN}[OK]{_RESET}   {msg}")
def print_warn(msg: str) -> None: print(f"{_YELLOW}[WARN]{_RESET} {msg}", file=sys.stderr)
def print_err(msg: str) -> None:  print(f"{_RED}[ERR]{_RESET}   {msg}", file=sys.stderr)
def green(text: str) -> str:  return f"{_GREEN}{text}{_RESET}"
def yellow(text: str) -> str: return f"{_YELLOW}{text}{_RESET}"
def cyan(text: str) -> str:   return f"{_CYAN}{text}{_RESET}"
def bold(text: str) -> str:   return f"{_BOLD}{text}{_RESET}"
def dim(text: str) -> str:    return f"{_DIM}{text}{_RESET}"
def aci_badge() -> str:       return f"{_GREEN}{_BOLD}[ACI]{_RESET}"
