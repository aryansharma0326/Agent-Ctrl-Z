"""OWNER: Visista
CALLED BY: Varuni's MCP server (to act) and Aryan's group repair (to read current state)

PLACEHOLDERS: right names, inputs and output shapes, but they don't change anything.
TODO(Visista): build these for real (Google Calendar API).
"""


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
