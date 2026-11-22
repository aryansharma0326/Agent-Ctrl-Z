"""OWNER: Visista
CALLED BY: Varuni's MCP server (to act) and Aryan's group repair (to read the
calendar, check for conflicts and create makeup events)

Step 3 (built): calendar_get and calendar_list read the calendar.
Step 4 (still PLACEHOLDERS): calendar_cancel, calendar_update, calendar_create.
Everything goes through get_backend(), so it works on the fake calendar and real Google.

Every event comes back in the agreed flat shape: id, summary, start, end, attendees,
description, location, organizer, responses (+ raw on real Google).
Times without a timezone mean New York time (-05:00).
"""

import copy
from datetime import datetime, timedelta, timezone

from google_tools.backend import get_backend

NEW_YORK = timezone(timedelta(hours=-5))

# Fields everyone agreed on that may be missing; they get these defaults.
DEFAULTS = {"attendees": [], "description": "", "location": "", "organizer": "", "responses": {}}


def _shape(event: dict) -> dict:
    """Return a copy of the event in the agreed flat shape, filling any missing fields."""
    shaped = copy.deepcopy(event)
    for key, default in DEFAULTS.items():
        if shaped.get(key) is None:
            shaped[key] = copy.deepcopy(default)
    return shaped


def _read_time(value: str, name: str) -> datetime:
    """Turn an ISO time into a datetime. Gives the agent a clear error if it sends
    something like "Friday" instead of a time."""
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f'{name} must be an ISO time like "2026-11-20T09:00", got {value!r}') from None
    return t if t.tzinfo else t.replace(tzinfo=NEW_YORK)


def calendar_get(event_id: str) -> dict | None:
    """Return the event as it is right now, or None if it doesn't exist or was cancelled."""
    event = get_backend().get_event(event_id)
    return _shape(event) if event else None


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
    sorted by start time. Used to check for conflicts and find free slots.
    Cancelled events and all-day events are left out."""
    if _read_time(end, "end") <= _read_time(start, "start"):
        raise ValueError(f"end ({end}) must be after start ({start})")
    events = get_backend().list_events(start, end)
    return [_shape(e) for e in events if "T" in str(e.get("start", ""))]  # "T" = has a time, not all-day


def calendar_create(event: dict) -> dict:
    """Create a new event (e.g. a makeup meeting). Google gives it a new id.
    Returns {"before": None, "after": <created event dict, including its id>}"""
    return {"before": None, "after": None}  # PLACEHOLDER