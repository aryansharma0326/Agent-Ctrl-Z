"""OWNER: Aryan
Plain-Python helpers behind pages/2_owner_dashboard.py, kept here (no Streamlit
import) so they can be unit tested.

Security: Flag.reason_text comes from outside the team (anyone on the invite can
flag). It is shown to the owner as inert text only: cleaned with
`sanitize_untrusted` and rendered with st.text / escaped markdown. It is never
passed to group repair, the router or any LLM.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from repair.blast_radius import BlastRadiusResult, score_calendar_action
from repair.timeutil import attendee_emails, human_range, parse_time
from shared import db
from shared.models import ActionRecord, Flag, UndoResult

MAX_REASON_CHARS = 1000

# Bidi overrides / isolates and zero-width characters can disguise text.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁩﻿]")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$=:])")


def sanitize_untrusted(text: object, max_chars: int = MAX_REASON_CHARS) -> str:
    """Make untrusted text safe to display verbatim: drop control and invisible
    characters (keeping newlines/tabs) and cap the length."""
    s = "" if text is None else str(text)
    s = _INVISIBLE.sub("", s)
    s = "".join(ch for ch in s
                if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    s = s.strip()
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + " … [truncated]"
    return s


def escape_markdown(text: object) -> str:
    """Backslash-escape everything Streamlit markdown would interpret
    (formatting, links, raw HTML, $LaTeX$)."""
    return _MD_SPECIAL.sub(r"\\\1", sanitize_untrusted(text, max_chars=10_000))


# ------------------------------------------------------------------ summaries

def describe_event(event: Optional[dict]) -> str:
    if not event:
        return "(nothing — event removed from the calendar)"
    if "subject" in event and "start" not in event:
        return describe_email(event)
    try:
        when = human_range(parse_time(event["start"]), parse_time(event["end"]))
    except Exception:
        when = "(unknown time)"
    n = len(attendee_emails(event))
    return f"{event.get('summary') or '(untitled)'} · {when} · {n} attendee{'s' * (n != 1)}"


def describe_email(msg: dict) -> str:
    to = msg.get("to") or []
    return f"Email “{msg.get('subject', '')}” to {len(to)} recipient{'s' * (len(to) != 1)}"


TOOL_LABELS = {
    "calendar.cancel_event": "Cancelled an event",
    "calendar.update_event": "Changed an event",
    "calendar.create_event": "Created an event",
    "email.send": "Sent an email",
}


def describe_change(action: ActionRecord) -> tuple[str, str, str]:
    """(verb, before, after) for the dashboard."""
    verb = TOOL_LABELS.get(action.tool, action.tool)
    if action.tool == "email.send":
        return verb, "(not sent)", describe_email(action.after or {})
    before = describe_event(action.before) if action.before else "(did not exist)"
    return verb, before, describe_event(action.after)


# ------------------------------------------------------------------ blast radius

def calendar_action_type(action: ActionRecord) -> Optional[str]:
    """cancel / move / edit / create for a calendar action; None for anything else.
    An update that changes the start or end time is a move, any other update an edit."""
    if action.tool == "calendar.cancel_event":
        return "cancel"
    if action.tool == "calendar.create_event":
        return "create"
    if action.tool == "calendar.update_event":
        before, after = action.before or {}, action.after or {}
        for key in ("start", "end"):
            try:
                changed = parse_time(before[key]) != parse_time(after[key])
            except Exception:
                changed = before.get(key) != after.get(key)
            if changed:
                return "move"
        return "edit"
    return None


def action_blast_radius(action: ActionRecord) -> Optional[BlastRadiusResult]:
    """Blast radius of the agent's original calendar action, as of when it was done.
    Scores the event before the change (the event itself for a create). None for
    non-calendar actions or when there is no event to score. Read-only."""
    kind = calendar_action_type(action)
    event = action.after if kind == "create" else action.before
    if kind is None or not event:
        return None
    return score_calendar_action(event, kind, now=action.timestamp)


# ------------------------------------------------------------------ data

@dataclass
class DashboardItem:
    action: ActionRecord
    flags: list[Flag] = field(default_factory=list)
    results: list[UndoResult] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return bool(self.flags) and self.action.status == "flagged"

    @property
    def flaggers(self) -> list[str]:
        out: list[str] = []
        for f in self.flags:
            if f.flagged_by.lower() not in out:
                out.append(f.flagged_by.lower())
        return out


def load_items(conn) -> list[DashboardItem]:
    flags_by_action: dict[str, list[Flag]] = {}
    for f in db.list_flags(conn):
        flags_by_action.setdefault(f.action_id, []).append(f)
    return [DashboardItem(action=a, flags=flags_by_action.get(a.action_id, []),
                          results=db.list_undo_results(conn, a.action_id))
            for a in db.list_actions(conn)]


def metrics(items: list[DashboardItem]) -> dict[str, int]:
    return {
        "Total actions": len(items),
        "Pending flags": sum(len(i.flags) for i in items if i.pending),
        "Undone": sum(1 for i in items if i.action.status == "undone"),
        "Failed": sum(1 for i in items if i.action.status == "undo_failed"),
    }


def dismiss_flags(conn, action_id: str) -> None:
    """Owner reviewed the flags and decided the agent's action was right
    (Scenario 5 style): mark the action back to 'done' without undoing anything.
    The flags stay in the table as a record; they are no longer pending."""
    db.set_action_status(conn, action_id, "done")


def preview_state(item: DashboardItem) -> dict:
    """current_state for a dry-run repair. Only trusted, structured data goes in:
    who flagged (an email address), never what they wrote."""
    return {"flagged_by": item.flaggers}
