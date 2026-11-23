"""OWNER: Varuni
CALLED BY: Aryan's owner dashboard (the Undo button)

Undo router: when the owner clicks Undo, decide HOW to undo that action, run
the right fixer, save the result, and update the action's status.

  email.send             -> undo_email          (Visista: stop it or send a correction)
  calendar.create_event  -> cancel the event the agent created
  calendar.cancel_event  \\  no conflict  -> restore_calendar (Visista: exact restore)
  calendar.update_event  /   conflict     -> group_repair     (Aryan: least-disruptive fix)

"Conflict" means a plain restore could hurt someone: an attendee booked the old
slot, the event is already back, or a person edited it after the agent did.
If the exact restore fails, group repair is tried as a fallback. After a
successful undo, everyone in result.notify gets an "it's fixed" note.

plan_undo() is the same decision as a dry run (nothing changes), so the owner or
the agent can see exactly what an undo will do before it happens.

The router never raises: it always returns an UndoResult. Flag text is never
read here (it's untrusted); routing uses only the action log and the calendar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from core.action_log import owner_email
from core.receipts import describe, send_fixed_notices
from google_tools import calendar_tools
from google_tools.undo_fixes import restore_calendar, undo_email
from repair.group_repair import group_repair
from shared import db
from shared.models import ActionRecord, UndoResult

DEFAULT_TZ = timezone(timedelta(hours=-5))   # times without a timezone are read as -05:00
COMPARED_FIELDS = ("summary", "start", "end", "attendees", "description", "location")


# ---------- small helpers ----------

def _time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=DEFAULT_TZ)


def _emails(event: dict) -> list[str]:
    out = []
    for a in event.get("attendees") or []:
        email = a.get("email", "") if isinstance(a, dict) else a
        if email:
            out.append(email.strip().lower())
    return out


def _fail(action_id: str, details: str, fix_type: str = "exact_restore") -> UndoResult:
    return UndoResult(action_id=action_id, fix_type=fix_type, success=False,
                      details=details, notify=[])


# ---------- conflict check ----------

def find_conflict(action: ActionRecord) -> str | None:
    """Why a plain restore would be unsafe, or None if it's safe."""
    before = action.before or {}
    current = calendar_tools.calendar_get(action.target_id)

    if action.tool == "calendar.cancel_event" and current:
        return "the event is already back on the calendar"

    if action.tool == "calendar.update_event" and current and action.after:
        if current.get("updated") and action.after.get("updated"):
            edited = current["updated"] != action.after["updated"]
        else:
            edited = any(current.get(f) != action.after.get(f) for f in COMPARED_FIELDS
                         if f in action.after)
        if edited:
            return "someone edited the event after the agent did"

    if not before.get("start") or not before.get("end"):
        return None
    start, end = _time(before["start"]), _time(before["end"])
    people = set(_emails(before))
    for event in calendar_tools.calendar_list(before["start"], before["end"]) or []:
        if event.get("id") == action.target_id or event.get("status") == "cancelled":
            continue
        try:
            overlaps = _time(event["start"]) < end and start < _time(event["end"])
        except (KeyError, TypeError, ValueError):
            continue
        clash = people & set(_emails(event))
        if overlaps and clash:
            return f"{', '.join(sorted(clash))} booked '{event.get('summary', 'something')}' in that slot"
    return None


# ---------- fixers per action type ----------

def _undo_create(action: ActionRecord) -> UndoResult:
    result = calendar_tools.calendar_cancel(action.target_id)
    if not result or not result.get("before"):
        return _fail(action.action_id, "Could not find the event the agent created, "
                                       "so nothing was removed.")
    title = result["before"].get("summary", action.target_id)
    return UndoResult(action_id=action.action_id, fix_type="exact_restore", success=True,
                      details=f"Removed '{title}', which the agent had created.",
                      notify=action.affected_people)


def _undo_calendar_change(action: ActionRecord, plan: "UndoPlan",
                          flagged_by: list[str]) -> UndoResult:
    state = {"flagged_by": flagged_by, "owner": owner_email()}
    if plan.strategy == "group_repair":
        result = group_repair(action, state)
        result.details = f"Plain restore skipped because {plan.conflict}. {result.details}"
        return result

    result = restore_calendar(action)
    if result.success:
        return result
    fallback = group_repair(action, state)
    fallback.details = (f"Exact restore didn't work ({result.details}), so group repair "
                        f"was used instead. {fallback.details}")
    return fallback


# ---------- the plan (dry run: decides, changes nothing) ----------

@dataclass
class UndoPlan:
    """What an undo WOULD do. Building one never changes the calendar or the database."""
    action_id: str
    can_undo: bool
    strategy: str              # exact_restore | group_repair | email_correction |
                               # remove_created_event | none
    explanation: str           # plain-English plan, safe to show the owner
    what_happened: str = ""    # e.g. "cancelled 'CS 598 demo prep' (Fri Nov 20, ...)"
    will_notify: list[str] = field(default_factory=list)
    conflict: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def plan_undo(conn, action_id: str) -> UndoPlan:
    """Decide how an action would be undone, without doing it. Never raises."""
    action = db.get_action(conn, action_id)
    if action is None:
        return UndoPlan(action_id, False, "none", f"No action with id {action_id}.")
    what = describe(action)
    if action.status == "undone":
        return UndoPlan(action_id, False, "none",
                        "This action was already undone, so there is nothing to do.", what)

    people = action.affected_people
    if action.tool == "email.send":
        return UndoPlan(action_id, True, "email_correction",
                        f"Stop the email if it is still being held; otherwise send a "
                        f"correction to the {len(people)} people who got it.", what, people)
    if action.tool == "calendar.create_event":
        return UndoPlan(action_id, True, "remove_created_event",
                        "Remove the event the agent created.", what, people)
    if action.tool not in ("calendar.cancel_event", "calendar.update_event"):
        return UndoPlan(action_id, False, "none", f"Don't know how to undo '{action.tool}'.", what)

    try:
        conflict = find_conflict(action)
    except Exception as exc:
        conflict = None
        note = f" (Couldn't fully check the calendar: {exc}.)"
    else:
        note = ""
    if conflict:
        return UndoPlan(action_id, True, "group_repair",
                        f"A plain restore would be unsafe because {conflict}, so group repair "
                        f"will find the least disruptive fix instead.", what, people, conflict)
    return UndoPlan(action_id, True, "exact_restore",
                    "Nothing has changed since, so it will be put back exactly as it was "
                    "(with group repair as a backup if that fails)." + note, what, people)


# ---------- the router ----------

def route_undo(conn, action_id: str) -> UndoResult:
    """Undo one action and return what happened. Never raises."""
    plan = plan_undo(conn, action_id)
    action = db.get_action(conn, action_id)
    if action is None or action.status == "undone":
        return _fail(action_id, plan.explanation)

    flagged_by = [f.flagged_by for f in db.list_flags(conn) if f.action_id == action_id]
    try:
        if not plan.can_undo:
            result = _fail(action_id, plan.explanation)
        elif plan.strategy == "email_correction":
            result = undo_email(action)
        elif plan.strategy == "remove_created_event":
            result = _undo_create(action)
        else:
            result = _undo_calendar_change(action, plan, flagged_by)
    except Exception as exc:   # a fixer crashed: report it, never crash the dashboard
        result = _fail(action_id, f"Undo failed before finishing: {exc}")

    result.action_id = action_id
    db.save_undo_result(conn, result)
    db.set_action_status(conn, action_id, "undone" if result.success else "undo_failed")
    if result.success:
        send_fixed_notices(action, result)   # "Sorry, it's fixed!" to whoever's plans changed
    return result
