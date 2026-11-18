"""A pretend Google Calendar + Gmail, saved to a small JSON file.

Use this until the 25th so nobody needs real Google accounts to work.
Visista swaps it for the real Google API later; the function names stay the same.

It's saved to a file (not just memory) so the seed script, the MCP server and
the Streamlit app all see the same fake calendar.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

STATE_PATH = Path(os.environ.get(
    "FAKE_GOOGLE_STATE", Path(__file__).resolve().parent.parent / "fake_google_state.json"))

EVENTS: dict[str, dict] = {}
SENT_EMAILS: list[dict] = []


def _load() -> None:
    EVENTS.clear()
    SENT_EMAILS.clear()
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())
        EVENTS.update(data["events"])
        SENT_EMAILS.extend(data["sent_emails"])


def _save() -> None:
    STATE_PATH.write_text(json.dumps({"events": EVENTS, "sent_emails": SENT_EMAILS}, indent=2))


def reset(events: list[dict] | None = None) -> None:
    """Wipe the fake calendar + outbox and start with these events."""
    EVENTS.clear()
    SENT_EMAILS.clear()
    for e in events or []:
        EVENTS[e["id"]] = copy.deepcopy(e)
    _save()


def list_events() -> list[dict]:
    _load()
    return sorted(copy.deepcopy(list(EVENTS.values())), key=lambda e: e["start"])


def get_event(event_id: str) -> dict | None:
    _load()
    e = EVENTS.get(event_id)
    return copy.deepcopy(e) if e else None


def delete_event(event_id: str) -> None:
    _load()
    EVENTS.pop(event_id, None)
    _save()


def update_event(event_id: str, changes: dict) -> dict:
    _load()
    EVENTS[event_id].update(changes)
    _save()
    return copy.deepcopy(EVENTS[event_id])


def insert_event(event: dict) -> dict:
    """Like real Google: a re-created event gets a NEW id."""
    _load()
    e = copy.deepcopy(event)
    e["id"] = "evt_" + uuid.uuid4().hex[:6]
    EVENTS[e["id"]] = e
    _save()
    return copy.deepcopy(e)


def add_event_as_is(event: dict) -> dict:
    """Add an event keeping its id (only for loading sample data)."""
    _load()
    EVENTS[event["id"]] = copy.deepcopy(event)
    _save()
    return copy.deepcopy(event)


def send_email(to: list[str], subject: str, body: str) -> dict:
    _load()
    msg = {"id": "msg_" + uuid.uuid4().hex[:6], "to": to, "subject": subject, "body": body}
    SENT_EMAILS.append(msg)
    _save()
    return copy.deepcopy(msg)
