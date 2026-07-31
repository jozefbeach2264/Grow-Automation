"""Small utilities shared across the project."""
import re
from datetime import datetime, timezone
from pathlib import Path


def name_slug(name: str) -> str:
    """Normalize a device name to an env-var slug (uppercase, underscores)."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def preserve_corrupt(path, detail: str) -> str:
    """Copy an unparseable state file aside as `<name>.corrupt.<utc>` and return a
    human-readable note describing what happened.

    A COPY, not a move, and the distinction is the whole point: the caller is
    expected to overwrite the live path with a known-safe state right after this
    returns. If that write then fails, the still-corrupt original must remain in
    place so the NEXT load fails closed again -- moving the file would leave no file
    at all, which every loader reads as a clean fresh install (fail open).

    Never raises: failing to preserve evidence must not derail the fail-safe path."""
    p = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = p.with_name(f"{p.name}.corrupt.{stamp}")
    try:
        dest.write_bytes(p.read_bytes())
        return f"{detail}; a copy was preserved as {dest.name}"
    except Exception as e:
        return f"{detail}; a copy could NOT be preserved ({e})"
