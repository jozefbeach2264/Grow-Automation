"""Small text utilities shared across the project."""
import re


def name_slug(name: str) -> str:
    """Normalize a device name to an env-var slug (uppercase, underscores)."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
