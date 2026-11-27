"""OWNER: Varuni
Reading ISO times the same way everywhere, on every Python version.

Google and other tools often write UTC times with a trailing "Z"
("2026-03-04T15:00:00Z"). Python 3.11+ reads that, but Python 3.10 raises
ValueError. The undo router used to skip such an event when checking for
conflicts, decide the slot was free, and restore a meeting right on top of it.
"""
from __future__ import annotations

from datetime import datetime


def parse_iso(value: str) -> datetime:
    """datetime.fromisoformat that also accepts a trailing Z / z for UTC.
    Raises ValueError/TypeError like fromisoformat for anything else invalid."""
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
