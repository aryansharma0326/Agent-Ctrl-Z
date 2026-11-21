"""OWNER: Visista
CALLED BY: Varuni's MCP server (to act) and Aryan's group repair (to read the
calendar, check for conflicts and create makeup events)

PLACEHOLDERS: right names, inputs and output shapes, but they don't change anything.
TODO(Visista): build these for real (Google Calendar API).
"""

from google_tools.backend import get_backend


def calendar_get(event_id: str) -> dict | None:
    """Return the event as it is right now, or None if it doesn't exist."""
    return None  # PLACEHOLDER


def calendar_cancel(event_id: str) -> dict:
    """Save a copy of the event, then cancel it.
    Returns {"before": <event dict>, "after": None}"""
    return {"before": None, "after": None}  # PLACEHOLDER


def calendar_update(event_id: str, changes: dict) -> dict:
    """Save a copy of the event, then apply changes (e.g. new start/end).
    Returns {"before": <event dict>, "after": <updated event dict>}"""
    return {"before": None, "after": None}  # PLACEHOLDER


def calendar_list(start: str, end: str) -> list[dict]:
    """Return all events between start and end (ISO times, e.g. "2026-11-19T00:00"),
    sorted by start time. Used to check for conflicts and find free slots."""
    return []  # PLACEHOLDER


def calendar_create(event: dict) -> dict:
    """Create a new event (e.g. a makeup meeting). Google gives it a new id.
    Returns {"before": None, "after": <created event dict, including its id>}"""
    return {"before": None, "after": None}  # PLACEHOLDER
