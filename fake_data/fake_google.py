"""A pretend Google Calendar + Gmail, saved to a small JSON file.

Use this until the 25th so nobody needs real Google accounts to work.
Visista swaps it for the real Google API later; the function names stay the same.

It's saved to a file (not just memory) so the seed script, the MCP server and
the Streamlit app all see the same fake calendar.

Behaves like real Google where it matters for undo (tested on a real account):
  * delete_event doesn't erase the event. It moves to CANCELLED, so get_event and
    list_events no longer show it, and restore_event(id) brings it back with the SAME id.
  * insert_event always gives a NEW id.
Times without a timezone (e.g. "2026-11-20T15:00") mean New York time (-05:00).
Every event has: id, summary, start, end, attendees, description, location,
organizer, responses (missing ones are filled with "" / {}).
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get(
    "FAKE_GOOGLE_STATE", Path(__file__).resolve().parent.parent / "fake_google_state.json"))

NEW_YORK = timezone(timedelta(hours=-5))
DEFAULTS = {"description": "", "location": "", "organizer": "", "responses": {}}

EVENTS: dict[str, dict] = {}      # live events
CANCELLED: dict[str, dict] = {}   # cancelled events, kept so they can be restored
SENT_EMAILS: list[dict] = []


def _with_defaults(event: dict) -> dict:
    e = copy.deepcopy(event)
    for key, value in DEFAULTS.items():
        if e.get(key) is None:
            e[key] = copy.deepcopy(value)
    return e


def _time(value: str) -> datetime:
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=NEW_YORK)


def _load() -> None:
    EVENTS.clear()
    CANCELLED.clear()
    SENT_EMAILS.clear()
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())
        EVENTS.update({k: _with_defaults(v) for k, v in data["events"].items()})
        CANCELLED.update(data.get("cancelled_events", {}))   # older files don't have it
        SENT_EMAILS.extend(data["sent_emails"])


def _save() -> None:
    STATE_PATH.write_text(json.dumps(
        {"events": EVENTS, "cancelled_events": CANCELLED, "sent_emails": SENT_EMAILS}, indent=2))


def reset(events: list[dict] | None = None) -> None:
    """Wipe the fake calendar + outbox and start with these events."""
    EVENTS.clear()
    CANCELLED.clear()
    SENT_EMAILS.clear()
    for e in events or []:
        EVENTS[e["id"]] = _with_defaults(e)
    _save()


def list_events(start: str | None = None, end: str | None = None) -> list[dict]:
    """Live (not cancelled) events, sorted by start time.
    With start and/or end: only events that overlap that time window."""
    _load()
    events = list(EVENTS.values())
    if start:
        events = [e for e in events if _time(e["end"]) > _time(start)]
    if end:
        events = [e for e in events if _time(e["start"]) < _time(end)]
    return sorted(copy.deepcopy(events), key=lambda e: _time(e["start"]))


def get_event(event_id: str) -> dict | None:
    """The live event, or None if it doesn't exist or is cancelled."""
    _load()
    e = EVENTS.get(event_id)
    return copy.deepcopy(e) if e else None


def delete_event(event_id: str) -> None:
    """Cancel the event. Like real Google it isn't erased: restore_event can bring it back."""
    _load()
    e = EVENTS.pop(event_id, None)
    if e is not None:
        CANCELLED[event_id] = e
    _save()


def restore_event(event_id: str) -> dict | None:
    """Un-cancel an event, keeping its id. Returns it, or None if there's nothing to restore."""
    _load()
    if event_id in EVENTS:                       # already live
        return copy.deepcopy(EVENTS[event_id])
    e = CANCELLED.pop(event_id, None)
    if e is None:
        return None
    EVENTS[event_id] = e
    _save()
    return copy.deepcopy(e)


def update_event(event_id: str, changes: dict) -> dict:
    _load()
    if event_id not in EVENTS:
        raise KeyError(f"No live event {event_id!r}")
    EVENTS[event_id].update(copy.deepcopy(changes))
    _save()
    return copy.deepcopy(EVENTS[event_id])


def insert_event(event: dict) -> dict:
    """Like real Google: a new or re-created event gets a NEW id."""
    _load()
    e = _with_defaults(event)
    e["id"] = "evt_" + uuid.uuid4().hex[:6]
    EVENTS[e["id"]] = e
    _save()
    return copy.deepcopy(e)


def add_event_as_is(event: dict) -> dict:
    """Add an event keeping its id (only for loading sample data)."""
    _load()
    e = _with_defaults(event)
    EVENTS[e["id"]] = e
    _save()
    return copy.deepcopy(e)


def send_email(to: list[str], subject: str, body: str) -> dict:
    _load()
    msg = {"id": "msg_" + uuid.uuid4().hex[:6], "to": to, "subject": subject, "body": body}
    SENT_EMAILS.append(msg)
    _save()
    return copy.deepcopy(msg)