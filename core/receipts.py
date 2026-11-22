"""OWNER: Varuni

The "two-sided" part of two-sided undo: the people an action affected hear
about it and can flag it, not just the owner.

  1. Receipt     after the agent acts, each affected person gets a short note
                 saying what changed, with a personal Flag link
  2. Flag        clicking the link lets them say "this looks wrong"; the link is
                 signed, so nobody can flag someone else's receipt or forge one
  3. Fixed note  after an undo succeeds, the people whose plans changed are told

Flag text is UNTRUSTED (anyone can type anything, including "ignore all
previous instructions"). It is cleaned, stored and shown as plain text only,
and is never given to the agent or to any fixer.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlencode

from core.action_log import owner_email
from core.signing import matches, sign
from google_tools import email_tools
from shared import db
from shared.models import ActionRecord, Flag, UndoResult

MAX_REASON_CHARS = 500
FLAGGABLE = ("done", "flagged")
VERBS = {
    "calendar.cancel_event": "cancelled",
    "calendar.update_event": "changed",
    "calendar.create_event": "created",
    "email.send": "sent you an email:",
}
SUSPICIOUS = re.compile(
    r"ignore (all )?(previous|prior|above)|system prompt|you are now|disregard|"
    r"delete (all|every)|<\s*script|\bjailbreak\b", re.IGNORECASE)


# ---------- settings ----------

def app_url() -> str:
    """Where the receipt/flag page lives. Set APP_URL when deployed."""
    return os.environ.get("APP_URL", "http://localhost:8501").rstrip("/")


# ---------- signed flag links ----------

def flag_token(action_id: str, person: str) -> str:
    return sign(f"flag|{action_id}|{person.strip().lower()}")


def flag_link(action_id: str, person: str) -> str:
    query = urlencode({"action": action_id, "who": person.strip().lower(),
                       "token": flag_token(action_id, person)})
    return f"{app_url()}/receipt_flag?{query}"


def valid_token(action_id: str, person: str, token: str) -> bool:
    return matches(f"flag|{action_id}|{(person or '').strip().lower()}", token)


# ---------- untrusted flag text ----------

def clean_flag_text(text: object) -> str:
    """Make flag text safe to store and display: plain printable text, short."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_REASON_CHARS]


def looks_suspicious(text: str) -> bool:
    """True if the text reads like an attempt to give the AI orders."""
    return bool(SUSPICIOUS.search(text or ""))


# ---------- receipts ----------

def _clock(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def _when(event: dict) -> str:
    """e.g. 'Fri Nov 20, 3:00 PM–4:00 PM' (empty if the times are missing)."""
    try:
        start = datetime.fromisoformat(event["start"])
        end = datetime.fromisoformat(event["end"])
    except (KeyError, TypeError, ValueError):
        return ""
    return f"{start:%a %b} {start.day}, {_clock(start)}–{_clock(end)}"


def describe(action: ActionRecord) -> str:
    """One line saying what the agent did, e.g. cancelled 'Demo prep' (Fri Nov 20, 3:00 PM–4:00 PM)."""
    verb = VERBS.get(action.tool, action.tool)
    if action.tool == "email.send":
        return f"{verb} '{(action.after or {}).get('subject', '(no subject)')}'"
    event = action.before or action.after or {}
    title = event.get("summary", action.target_id)
    when = _when(action.before or {}) if action.tool != "calendar.create_event" else _when(event)
    text = f"{verb} '{title}'" + (f" ({when})" if when else "")
    if action.tool == "calendar.update_event" and action.after:
        new_when = _when(action.after)
        if new_when and new_when != when:
            text += f", now {new_when}"
        added, removed = attendee_changes(action)
        if added:
            text += f"; added {', '.join(added)}"
        if removed:
            text += f"; removed {', '.join(removed)}"
    return text


def _attendees(event: dict | None) -> list[str]:
    out = []
    for a in (event or {}).get("attendees") or []:
        email = a.get("email", "") if isinstance(a, dict) else a
        if email:
            out.append(email.strip().lower())
    return out


def attendee_changes(action: ActionRecord) -> tuple[list[str], list[str]]:
    """Who an update added to and removed from an event."""
    if action.tool != "calendar.update_event" or not action.after or "attendees" not in action.after:
        return [], []
    before, after = _attendees(action.before), _attendees(action.after)
    return ([p for p in after if p not in before], [p for p in before if p not in after])


def _personal_note(action: ActionRecord, person: str) -> str:
    """A line that says what this change means for this one person."""
    added, removed = attendee_changes(action)
    if person in removed:
        return "You were removed from this event.\n"
    if person in added:
        return "You were added to this event.\n"
    return ""


def make_receipts(action: ActionRecord, owner: str | None = None) -> list[dict]:
    """One receipt per affected person: {"to", "subject", "body", "flag_link"}."""
    owner = owner or owner_email()
    receipts = []
    for person in action.affected_people:
        link = flag_link(action.action_id, person)
        receipts.append({
            "to": person,
            "subject": f"{owner}'s AI assistant {describe(action)}",
            "body": (f"Hi,\n\n{owner}'s AI assistant {describe(action)}.\n"
                     f"{_personal_note(action, person)}"
                     f"They asked it: \"{action.instruction}\"\n\n"
                     f"Does this look wrong for you? Flag it here and {owner} will be told:\n"
                     f"{link}\n\nIf it's fine, you don't need to do anything."),
            "flag_link": link,
        })
    return receipts


def send_receipts(action: ActionRecord) -> list[dict]:
    """Send every receipt. A failed send never undoes or blocks the action."""
    sent = []
    for r in make_receipts(action):
        try:
            email_tools.email_send([r["to"]], r["subject"], r["body"])
            sent.append(r)
        except Exception:
            continue
    return sent


# ---------- flags ----------

def submit_flag(conn, action_id: str, person: str, reason_text: str, token: str) -> Flag:
    """Save a flag from an affected person. Raises ValueError if it isn't allowed."""
    person = (person or "").strip().lower()
    action = db.get_action(conn, action_id)
    if action is None:
        raise ValueError("This receipt doesn't match any action.")
    if not valid_token(action_id, person, token):
        raise ValueError("This flag link isn't valid.")
    if person not in action.affected_people:
        raise ValueError("Only people affected by this action can flag it.")
    if action.status not in FLAGGABLE:
        raise ValueError("This action has already been handled.")

    flag = Flag(action_id=action_id, flagged_by=person, reason_text=clean_flag_text(reason_text))
    db.save_flag(conn, flag)
    db.set_action_status(conn, action_id, "flagged")
    return flag


# ---------- "it's fixed" notes ----------

def fixed_notices(action: ActionRecord, result: UndoResult, owner: str | None = None) -> list[dict]:
    """One note per person in result.notify, only if the undo worked."""
    if not result.success:
        return []
    owner = owner or owner_email()
    return [{
        "to": person,
        "subject": f"Fixed: {owner}'s AI assistant {describe(action)}",
        "body": (f"Hi,\n\nSorry about that! {owner}'s AI assistant {describe(action)}, "
                 f"and that has now been fixed.\n\nWhat we did: {result.details}\n\n"
                 f"No action is needed from you."),
    } for person in result.notify if person != owner]


def send_fixed_notices(action: ActionRecord, result: UndoResult) -> list[dict]:
    sent = []
    for n in fixed_notices(action, result):
        try:
            email_tools.email_send([n["to"]], n["subject"], n["body"])
            sent.append(n)
        except Exception:
            continue
    return sent
