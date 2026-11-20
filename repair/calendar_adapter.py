"""OWNER: Aryan
Calendar adapter used by group repair.

Group repair needs three calendar operations: read events in a window, read one
event, and create/update an event. Visista's `google_tools.calendar_tools` will
provide these against the real Google Calendar API; until that lands, its
functions are placeholders (they return None / [] and change nothing).

This module hides that difference behind one small interface so the repair
algorithm never has to care which backend is live:

  * GoogleToolsCalendar -> calls google_tools.calendar_tools (real API)
  * FakeGoogleCalendar  -> calls fake_data.fake_google (JSON-file fake)

`get_calendar()` picks one:
  * env CTRLZ_CALENDAR_BACKEND="google" or "fake" forces a backend;
  * otherwise ("auto") we use google_tools once it is no longer a placeholder,
    and fall back to the fake calendar while it still is.
"""
from __future__ import annotations

import inspect
import os
from datetime import datetime
from typing import Optional, Protocol

from repair.timeutil import overlaps, parse_time


class CalendarBackend(Protocol):
    name: str

    def get(self, event_id: str) -> Optional[dict]: ...
    def list(self, start: str, end: str) -> list[dict]: ...
    def create(self, event: dict) -> dict: ...
    def update(self, event_id: str, changes: dict) -> dict: ...


class FakeGoogleCalendar:
    """Backend over fake_data/fake_google.py (shared JSON file)."""
    name = "fake_google"

    def get(self, event_id: str) -> Optional[dict]:
        from fake_data import fake_google as g
        return g.get_event(event_id)

    def list(self, start: str, end: str) -> list[dict]:
        from fake_data import fake_google as g
        lo, hi = parse_time(start), parse_time(end)
        return [e for e in g.list_events()
                if overlaps(parse_time(e["start"]), parse_time(e["end"]), lo, hi)]

    def create(self, event: dict) -> dict:
        from fake_data import fake_google as g
        return g.insert_event(event)

    def update(self, event_id: str, changes: dict) -> dict:
        from fake_data import fake_google as g
        return g.update_event(event_id, changes)


class GoogleToolsCalendar:
    """Backend over google_tools/calendar_tools.py (Visista's real API code)."""
    name = "google_tools"

    def get(self, event_id: str) -> Optional[dict]:
        from google_tools import calendar_tools as ct
        return ct.calendar_get(event_id)

    def list(self, start: str, end: str) -> list[dict]:
        from google_tools import calendar_tools as ct
        return ct.calendar_list(start, end)

    def create(self, event: dict) -> dict:
        from google_tools import calendar_tools as ct
        created = ct.calendar_create(event).get("after")
        if not created:
            raise RuntimeError("calendar_create returned no event")
        return created

    def update(self, event_id: str, changes: dict) -> dict:
        from google_tools import calendar_tools as ct
        updated = ct.calendar_update(event_id, changes).get("after")
        if not updated:
            raise RuntimeError("calendar_update returned no event")
        return updated


def google_tools_is_placeholder() -> bool:
    """True while Visista's calendar_tools is still the starter-kit stub."""
    try:
        from google_tools import calendar_tools as ct
        src = inspect.getsource(ct.calendar_create) + inspect.getsource(ct.calendar_list)
    except Exception:
        return True
    return "PLACEHOLDER" in src


def get_calendar() -> CalendarBackend:
    choice = os.environ.get("CTRLZ_CALENDAR_BACKEND", "auto").lower()
    if choice == "google":
        return GoogleToolsCalendar()
    if choice == "fake":
        return FakeGoogleCalendar()
    return FakeGoogleCalendar() if google_tools_is_placeholder() else GoogleToolsCalendar()


def window(start: datetime, end: datetime) -> tuple[str, str]:
    """ISO strings in the format calendar_list expects."""
    return start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")
