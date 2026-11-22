"""OWNER: Visista
CALLED BY: Varuni's undo router

Step 5 (built): restore_calendar puts a calendar event back exactly as it was.
Step 6 (still a placeholder): undo_email.

These only RETURN an UndoResult. Varuni's router saves it to undo.db, marks the action
undone, and sends the "it's fixed" messages to the people in result.notify.
The router has already checked it's safe to restore (nobody booked the slot since);
if a restore can't be done, success=False and the router falls back to group repair.
"""
from datetime import datetime

from google_tools.backend import get_backend
from google_tools.calendar_tools import (EDITABLE, NEW_YORK, _read_time, calendar_cancel,
                                         calendar_create, calendar_get)
from shared.models import ActionRecord, UndoResult


# ---------- helpers ----------
def _clock(t: datetime) -> str:
    """3:00 PM (written by hand: Windows doesn't support %-I)."""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _when(event: dict) -> str:
    """e.g. "Fri Nov 20, 3:00 PM-4:00 PM", in New York time."""
    try:
        s = _read_time(event["start"], "start").astimezone(NEW_YORK)
        e = _read_time(event["end"], "end").astimezone(NEW_YORK)
    except (KeyError, ValueError):
        return "its original time"
    return f"{s:%a %b} {s.day}, {_clock(s)}-{_clock(e)}"


def _title(event: dict | None, fallback: str) -> str:
    return (event or {}).get("summary") or fallback


def _result(action: ActionRecord, success: bool, details: str, notify=None,
            new_target_id=None) -> UndoResult:
    return UndoResult(action_id=action.action_id, fix_type="exact_restore", success=success,
                      details=details, notify=list(notify or []), new_target_id=new_target_id)


def _differences(current: dict, wanted: dict) -> dict:
    """The editable fields where current doesn't match wanted yet."""
    return {k: wanted[k] for k in EDITABLE if k in wanted and current.get(k) != wanted[k]}


# ---------- one function per kind of action ----------
def _restore_cancelled(action: ActionRecord) -> UndoResult:
    before, event_id = action.before, action.target_id
    title = _title(before, event_id)
    if calendar_get(event_id) is not None:
        return _result(action, False, f"'{title}' is already back on the calendar, "
                                      "so there was nothing to restore.")

    restored = get_backend().restore_event(event_id)
    if restored is not None:
        # Same id. Put back any field that doesn't match the "before" copy.
        fixes = _differences(calendar_get(event_id) or {}, before)
        if fixes:
            get_backend().update_event(event_id, fixes)
        return _result(action, True, f"Put '{title}' back on the calendar ({_when(before)}), "
                                     "exactly as it was, with the same event id.",
                       notify=action.affected_people)

    # Couldn't un-cancel it: make an exact copy instead. That copy gets a new id.
    created = calendar_create(before)["after"]
    return _result(action, True, f"Re-created '{title}' ({_when(before)}) exactly as it was. "
                                 f"It has a new event id: {created['id']}.",
                   notify=action.affected_people, new_target_id=created["id"])


def _restore_updated(action: ActionRecord) -> UndoResult:
    before, event_id = action.before, action.target_id
    title = _title(before, event_id)
    current = calendar_get(event_id)
    if current is None:
        return _result(action, False, f"'{title}' has been cancelled since the agent changed "
                                      "it, so its old details can't be put back.")
    fixes = _differences(current, before)
    if not fixes:
        return _result(action, True, f"'{title}' already matches how it was before the agent "
                                     "changed it, so nothing needed to change.")
    get_backend().update_event(event_id, fixes)
    changed = ", ".join(fixes)
    return _result(action, True, f"Put '{title}' back as it was ({_when(before)}). "
                                 f"Changed back: {changed}.",
                   notify=action.affected_people)


def _remove_created(action: ActionRecord) -> UndoResult:
    title = _title(action.after, action.target_id)
    if calendar_cancel(action.target_id)["before"] is None:
        return _result(action, False, f"Couldn't find '{title}', the event the agent created, "
                                      "so nothing was removed.")
    return _result(action, True, f"Removed '{title}', which the agent had created.",
                   notify=action.affected_people)


# ---------- the functions Varuni's router calls ----------
def restore_calendar(action: ActionRecord) -> UndoResult:
    """Put a cancelled/changed event back exactly as it was.
    If Google gives the restored event a new id, put it in new_target_id.
    Never raises: problems come back as success=False with a plain-English reason."""
    try:
        if action.tool == "calendar.create_event":
            return _remove_created(action)
        if action.tool not in ("calendar.cancel_event", "calendar.update_event"):
            return _result(action, False, f"restore_calendar only undoes calendar actions, "
                                          f"not {action.tool}.")
        if not action.before:
            return _result(action, False, "There's no saved 'before' copy of this event, "
                                          "so it can't be restored exactly.")
        if action.tool == "calendar.cancel_event":
            return _restore_cancelled(action)
        return _restore_updated(action)
    except Exception as exc:
        return _result(action, False, f"Restore failed: {exc}")


def undo_email(action: ActionRecord) -> UndoResult:
    """Stop the email if it's still being held, otherwise send a correction."""
    return UndoResult(action_id=action.action_id, fix_type="email_correction", success=False,
                      details="PLACEHOLDER: undo_email not built yet", notify=[])