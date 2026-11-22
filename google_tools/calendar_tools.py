"""OWNER: Visista
CALLED BY: Varuni's MCP server (to act) and Aryan's group repair (to read the
calendar, check for conflicts and create makeup events)

Step 3 (built): calendar_get and calendar_list read the calendar.
Step 4 (built): calendar_cancel, calendar_update and calendar_create change it. Each one
reads the event first and returns {"before": ..., "after": ...}; Varuni's action log
stores those copies (nothing is saved here).
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

# Fields that can be set when creating or changing an event.
EDITABLE = ("summary", "start", "end", "attendees", "description", "location")

# Fields that are quietly ignored: Google decides them. Aryan's repairs copy whole
# events, so these can show up in what he sends, and that must not cause an error.
READ_ONLY = {"id", "organizer", "responses", "raw", "status", "etag", "htmlLink", "iCalUID",
             "created", "updated", "sequence", "kind", "recurringEventId", "originalStartTime"}


# ---------- helpers ----------
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


def _check_order(start: str, end: str) -> None:
    if _read_time(end, "end") <= _read_time(start, "start"):
        raise ValueError(f"end ({end}) must be after start ({start})")


def _same_style(example: str, t: datetime) -> str:
    """Write t like the example time: with a timezone only if the example had one."""
    if datetime.fromisoformat(str(example).replace("Z", "+00:00")).tzinfo:
        return t.isoformat()
    t = t.astimezone(NEW_YORK).replace(tzinfo=None)
    return t.isoformat(timespec="minutes" if t.second == 0 else "seconds")


def _emails(attendees) -> list[str]:
    """Attendees as a plain list of emails (also accepts Google-style {"email": ...})."""
    if not isinstance(attendees, list):
        raise ValueError("attendees must be a list of email addresses")
    out = []
    for a in attendees:
        email = a.get("email") if isinstance(a, dict) else a
        if not isinstance(email, str) or "@" not in email:
            raise ValueError(f"not an email address: {a!r}")
        out.append(email)
    return out


def _editable(fields: dict) -> dict:
    """Keep only the fields that may be set, checked and cleaned.
    Read-only fields are dropped; anything unknown is an error the agent can read."""
    if not isinstance(fields, dict):
        raise ValueError("expected a dict of event fields")
    unknown = sorted(set(fields) - set(EDITABLE) - READ_ONLY)
    if unknown:
        raise ValueError(f"can't set {', '.join(unknown)}. You can set: {', '.join(EDITABLE)}")
    clean = {k: copy.deepcopy(fields[k]) for k in EDITABLE if k in fields}
    for key in ("start", "end"):
        if key in clean:
            _read_time(clean[key], key)
    for key in ("description", "location"):
        if key in clean and clean[key] is None:
            clean[key] = ""
    if "attendees" in clean:
        clean["attendees"] = _emails(clean["attendees"])
    return clean


# ---------- reading (step 3) ----------
def calendar_get(event_id: str) -> dict | None:
    """Return the event as it is right now, or None if it doesn't exist or was cancelled."""
    event = get_backend().get_event(event_id)
    return _shape(event) if event else None


def calendar_list(start: str, end: str) -> list[dict]:
    """Return all events between start and end (ISO times, e.g. "2026-11-19T00:00"),
    sorted by start time. Used to check for conflicts and find free slots.
    Cancelled events and all-day events are left out."""
    _check_order(start, end)
    events = get_backend().list_events(start, end)
    return [_shape(e) for e in events if "T" in str(e.get("start", ""))]  # "T" = has a time, not all-day


# ---------- changing (step 4) ----------
def calendar_cancel(event_id: str) -> dict:
    """Save a copy of the event, then cancel it.
    Returns {"before": <event dict>, "after": None}.
    If there's no such event (or it's already cancelled), nothing happens and
    "before" is None, which Varuni's server reports as "not found"."""
    before = calendar_get(event_id)
    if before is None:
        return {"before": None, "after": None}
    get_backend().delete_event(event_id)
    return {"before": before, "after": None}


def calendar_update(event_id: str, changes: dict) -> dict:
    """Save a copy of the event, then apply changes (e.g. new start/end).
    Returns {"before": <event dict>, "after": <updated event dict>}.
    Changing only "start" keeps the meeting's length (the end moves with it).
    If there's no such event, nothing happens and "before" is None."""
    before = calendar_get(event_id)
    if before is None:
        return {"before": None, "after": None}
    clean = _editable(changes)
    if not clean:
        raise ValueError(f"nothing to change. You can set: {', '.join(EDITABLE)}")
    if "start" in clean and "end" not in clean:
        length = _read_time(before["end"], "end") - _read_time(before["start"], "start")
        clean["end"] = _same_style(clean["start"], _read_time(clean["start"], "start") + length)
    _check_order(clean.get("start", before["start"]), clean.get("end", before["end"]))
    after = get_backend().update_event(event_id, clean)
    return {"before": before, "after": _shape(after)}


def calendar_create(event: dict) -> dict:
    """Create a new event (e.g. a makeup meeting). Google gives it a new id.
    Accepts the agreed flat shape: summary, start, end, attendees, description, location.
    Fields like id / organizer / responses are ignored, so a copy of another event works.
    Returns {"before": None, "after": <created event dict, including its id>}"""
    clean = _editable(event)
    if "start" not in clean or "end" not in clean:
        raise ValueError("a new event needs both a start and an end time")
    _check_order(clean["start"], clean["end"])
    clean.setdefault("summary", "(no title)")
    clean.setdefault("attendees", [])
    created = get_backend().insert_event(clean)
    return {"before": None, "after": _shape(created)}