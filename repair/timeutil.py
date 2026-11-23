"""OWNER: Aryan
Small helpers for event times and attendees.

Events come in two shapes and we accept both:
  * fake calendar:  "start": "2026-11-19T15:00", "attendees": ["a@x.com", ...]
  * Google API:     "start": {"dateTime": "2026-11-19T15:00:00-06:00", "timeZone": ...},
                    "attendees": [{"email": "a@x.com", "responseStatus": "accepted"}, ...]

Times are compared as wall-clock times in the event's own zone (tz info is
dropped after parsing), which is what "working hours" means to a person.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Optional


def parse_time(value: Any) -> datetime:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=None)


def format_like(original: Any, new: datetime) -> Any:
    """Write `new` back in the same shape the event used (string or Google dict)."""
    if isinstance(original, dict):
        out = copy.deepcopy(original)
        out.pop("date", None)
        out["dateTime"] = new.strftime("%Y-%m-%dT%H:%M:%S")
        return out
    return new.strftime("%Y-%m-%dT%H:%M")


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def human_time(dt: datetime) -> str:
    """'Thu Nov 19, 3:00 PM'"""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%a %b')} {dt.day}, {hour}:{dt.strftime('%M %p')}"


def human_range(start: datetime, end: datetime) -> str:
    end_hour = end.strftime("%I").lstrip("0") or "12"
    end_txt = f"{end_hour}:{end.strftime('%M %p')}"
    if end.date() != start.date():
        end_txt = human_time(end)
    return f"{human_time(start)}–{end_txt}"


def attendee_email(a: Any) -> str:
    return (a.get("email", "") if isinstance(a, dict) else str(a)).strip().lower()


def attendee_emails(event: Optional[dict], include_optional: bool = True) -> list[str]:
    if not event:
        return []
    out: list[str] = []
    for a in event.get("attendees") or []:
        if not include_optional and isinstance(a, dict) and a.get("optional"):
            continue
        email = attendee_email(a)
        if email and email not in out:
            out.append(email)
    return out


def response_status(event: Optional[dict], email: str) -> Optional[str]:
    for a in (event or {}).get("attendees") or []:
        if isinstance(a, dict) and attendee_email(a) == email:
            return a.get("responseStatus")
    return None


def organizer_email(event: Optional[dict]) -> Optional[str]:
    for key in ("organizer", "creator"):
        v = (event or {}).get(key)
        if isinstance(v, dict) and v.get("email"):
            return v["email"].lower()
        if isinstance(v, str) and v:
            return v.lower()
    return None
