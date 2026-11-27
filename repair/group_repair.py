"""OWNER: Aryan
CALLED BY: Varuni's undo router, when a plain restore would clash with
plans people made after the mistake (e.g. someone booked the freed slot)

Minimal-disruption group repair
===============================
A blind revert can hurt the very people it is meant to help: if Aryan booked
the dentist into the slot the agent freed up, putting the meeting back
double-books him. So instead of reverting, we look at the calendar *now* and
pick the least disruptive fix:

  Case A  exact restore     nobody's calendar changed -> put it back as it was
  Case B  reschedule        original slot is now blocked for someone -> move /
                            re-create it at the earliest slot everyone is free
                            (working hours, 9 AM-6 PM, weekdays)
  Case C  keep + makeup     a moved group meeting that most people already
                            accepted -> leave it alone (moving it would cause
                            secondary collisions) and book a separate makeup
                            sync for the person who can't make it; or move it
                            back only if that creates zero conflicts
  Case D  no-op             nothing to repair, already repaired, a person has
                            edited the event since, or not a calendar action

Only people whose schedules the repair actually touches go into `notify`.

Flag.reason_text is never read here: it is untrusted text from outside and
the repair is decided purely from calendar data.

Public API
----------
  group_repair(action, current_state) -> UndoResult        plan + apply (writes)
  plan_group_repair(action, current_state) -> RepairPlan   dry run (no writes)
  apply_plan(plan) -> UndoResult                           apply a dry-run plan

`current_state` (all keys optional; `{}` means "read the live calendar"):
  "events":     snapshot list of events on the calendar now (else read live)
  "target":     the target event as it is now, or None if it no longer exists
                (a bare event dict with the target's "id" is accepted too)
  "flagged_by": email or list of emails of whoever flagged the action
  "responses":  {email: "accepted" | "declined" | "tentative" | "needsAction"}
  "owner":      the calendar owner's email (otherwise inferred)
  "now":        ISO time; never propose a slot before this
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from repair.calendar_adapter import CalendarBackend, get_calendar, window
from repair.timeutil import (attendee_email, attendee_emails, human_range, organizer_email,
                             overlaps, parse_time, format_like, response_status)
from shared.models import ActionRecord, UndoResult

FIX_TYPE = "group_repair"
WORK_START = time(9, 0)
WORK_END = time(18, 0)
SLOT_STEP = timedelta(minutes=15)
SEARCH_DAYS = 14
MAKEUP_MAX = timedelta(minutes=30)

# Fields Google generates per event; a re-created event must not carry them over.
VOLATILE_KEYS = {"id", "etag", "htmlLink", "iCalUID", "created", "updated", "sequence",
                 "status", "kind", "recurringEventId", "originalStartTime"}
CALENDAR_TOOLS = {"calendar.cancel_event", "calendar.update_event"}
NEW_YORK = timezone(timedelta(hours=-5))   # the app's zone for times written without one

CASE_A = "A_exact_restore"
CASE_B = "B_reschedule"
CASE_C = "C_keep_and_makeup"
CASE_D = "D_no_op"


@dataclass
class Conflict:
    person: str
    event_id: str
    summary: str
    when: str           # human-readable time range of the clashing event
    ends_at: str        # ISO end time (used by the slot search)


@dataclass
class RepairPlan:
    """What group repair would do. Built without touching the calendar."""
    action_id: str
    case: str
    success: bool                       # True if applying it leaves things fixed
    details: str
    notify: list[str] = field(default_factory=list)
    operation: Optional[str] = None     # "create" | "update" | None
    target_id: Optional[str] = None     # event to update (operation="update")
    payload: Optional[dict] = None      # event to create, or changes to apply
    conflicts: list[Conflict] = field(default_factory=list)
    original_slot: Optional[str] = None
    proposed_slot: Optional[str] = None

    @property
    def writes(self) -> bool:
        return self.operation is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_undo_result(self, new_target_id: Optional[str] = None) -> UndoResult:
        return UndoResult(action_id=self.action_id, fix_type=FIX_TYPE, success=self.success,
                          details=self.details, notify=list(self.notify),
                          new_target_id=new_target_id)


# --------------------------------------------------------------------------- API

def current_time() -> datetime:
    """The current New York wall-clock time (one place, so tests can freeze it)."""
    return datetime.now(NEW_YORK).replace(tzinfo=None, microsecond=0)


def group_repair(action: ActionRecord, current_state: dict) -> UndoResult:
    """current_state: what the calendar looks like now (from calendar_get / fake data).
    See the module docstring for the keys it understands; {} reads the live calendar."""
    try:
        cal = get_calendar()
        plan = plan_group_repair(action, current_state, calendar=cal)
        return apply_plan(plan, calendar=cal)
    except Exception as exc:   # the router must always get an UndoResult back
        return UndoResult(action_id=action.action_id, fix_type=FIX_TYPE, success=False,
                          details=f"Group repair failed before changing anything: {exc}",
                          notify=[])


def plan_group_repair(action: ActionRecord, current_state: Optional[dict] = None,
                      calendar: Optional[CalendarBackend] = None) -> RepairPlan:
    """Dry run: decide the repair and explain it, without writing anything."""
    if action.tool not in CALENDAR_TOOLS:
        return _noop(action, False,
                     f"Not repaired: group repair only handles calendar cancels and moves, "
                     f"and this action was '{action.tool}'. Route it to the matching fixer "
                     f"(e.g. email_correction). Nothing was changed.")
    before = action.before
    if not before or not before.get("start") or not before.get("end"):
        return _noop(action, False,
                     "Not repaired: the action log has no 'before' copy of the event, so "
                     "there is nothing reliable to restore. Nothing was changed.")

    ctx = _Context(action, current_state or {}, calendar or get_calendar())

    attendees = attendee_emails(before)
    third_parties = [p for p in attendees if p != ctx.owner]
    if not action.affected_people and (len(attendees) <= 1 or not third_parties):
        return _noop(action, False,
                     f"No group repair needed: '{_title(before)}' only involved the owner and "
                     f"nobody else was affected, so there are no other schedules to protect. "
                     f"Leaving the calendar as is (avoids over-undo); use exact restore if the "
                     f"owner wants it back.")

    if action.tool == "calendar.cancel_event":
        return _plan_cancel(action, ctx)
    return _plan_update(action, ctx)


def apply_plan(plan: RepairPlan, calendar: Optional[CalendarBackend] = None) -> UndoResult:
    """Carry out a plan from plan_group_repair and report it as an UndoResult."""
    if not plan.writes:
        return plan.to_undo_result()
    cal = calendar or get_calendar()
    try:
        if plan.operation == "create":
            created = cal.create(copy.deepcopy(plan.payload))
            return plan.to_undo_result(new_target_id=created.get("id"))
        if plan.operation == "update":
            cal.update(plan.target_id, copy.deepcopy(plan.payload))
            return plan.to_undo_result()
        raise ValueError(f"unknown operation {plan.operation!r}")
    except Exception as exc:
        return UndoResult(action_id=plan.action_id, fix_type=FIX_TYPE, success=False,
                          details=f"Repair was planned but the calendar write failed, so "
                                  f"nothing changed ({exc}). Planned: {plan.details}",
                          notify=[])


# ------------------------------------------------------------------ calendar view

class _Context:
    """Everything the planner needs to know about the calendar right now."""

    def __init__(self, action: ActionRecord, state: dict, cal: CalendarBackend):
        self.action = action
        self.state = state
        self.cal = cal
        self._snapshot: Optional[list[dict]] = state.get("events")
        self.target = self._current_target()
        self.owner = self._infer_owner()
        flagged = state.get("flagged_by") or []
        self.flaggers = [f.lower() for f in ([flagged] if isinstance(flagged, str) else flagged)]
        self.responses = {k.lower(): v for k, v in (state.get("responses") or {}).items()}
        # Never plan a slot in the past: without an explicit "now", use the current time
        # (as a New York wall-clock time, like every other time here).
        self.now = parse_time(state["now"]) if state.get("now") else current_time()

    def _current_target(self) -> Optional[dict]:
        tid = self.action.target_id
        if "target" in self.state:
            return self.state["target"]
        if self.state.get("id") == tid and self.state.get("start"):
            return self.state
        if self._snapshot is not None:
            return next((copy.deepcopy(e) for e in self._snapshot if e.get("id") == tid), None)
        return self.cal.get(tid)

    def _infer_owner(self) -> Optional[str]:
        if self.state.get("owner"):
            return str(self.state["owner"]).lower()
        event = self.action.before or {}
        org = organizer_email(event)
        if org:
            return org
        affected = {p.lower() for p in self.action.affected_people}
        if affected:
            others = [p for p in attendee_emails(event) if p not in affected]
            if len(others) == 1:
                return others[0]
        return None

    def events(self, lo: datetime, hi: datetime) -> list[dict]:
        if self._snapshot is not None:
            return [copy.deepcopy(e) for e in self._snapshot
                    if overlaps(parse_time(e["start"]), parse_time(e["end"]), lo, hi)]
        return self.cal.list(*window(lo, hi))

    def response(self, person: str) -> Optional[str]:
        return self.responses.get(person) or _rsvp(self.target, person)


def _rsvp(event: Optional[dict], person: str) -> Optional[str]:
    """A person's RSVP from either event shape: the app's "responses" dict (fake calendar
    and calendar_tools' flat shape) or Google's attendee dicts with "responseStatus"."""
    responses = (event or {}).get("responses")
    if isinstance(responses, dict):
        for email, status in responses.items():
            if str(email).strip().lower() == person:
                return status
    return response_status(event, person)


def _recurrence_rule(event: Optional[dict]) -> Optional[list]:
    """A whole series' recurrence rule (not one occurrence's). The flat shape keeps it in raw."""
    event = event or {}
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return event.get("recurrence") or raw.get("recurrence") or None


def _is_series(event: Optional[dict]) -> bool:
    return bool(_recurrence_rule(event))


def _is_busy(event: dict, person: str, exclude_ids: set[str]) -> bool:
    if event.get("id") in exclude_ids:
        return False
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return False
    if person not in attendee_emails(event):
        return False
    return _rsvp(event, person) != "declined"


def _conflicts(people: list[str], start: datetime, end: datetime, events: list[dict],
               exclude_ids: set[str]) -> list[Conflict]:
    found = []
    for e in events:
        es, ee = parse_time(e["start"]), parse_time(e["end"])
        if not overlaps(start, end, es, ee):
            continue
        for p in people:
            if _is_busy(e, p, exclude_ids):
                found.append(Conflict(person=p, event_id=e.get("id", ""),
                                      summary=_title(e), when=human_range(es, ee),
                                      ends_at=ee.isoformat(timespec="minutes")))
    return found


def _ceil_to_step(dt: datetime) -> datetime:
    midnight = datetime.combine(dt.date(), time(0))
    step = SLOT_STEP.total_seconds()
    steps = -(-(dt - midnight).total_seconds() // step)   # ceiling division
    return midnight + timedelta(seconds=steps * step)


def find_free_slot(people: list[str], duration: timedelta, earliest: datetime,
                   events: list[dict], exclude_ids: set[str] = frozenset(),
                   days: int = SEARCH_DAYS) -> Optional[tuple[datetime, datetime]]:
    """Earliest start >= `earliest` where everyone in `people` is free for `duration`,
    inside working hours on a weekday. Returns (start, end) or None."""
    if duration <= timedelta(0):
        return None
    for d in range(days + 1):
        day = (earliest + timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        t = datetime.combine(day, WORK_START)
        if d == 0 and earliest > t:
            t = earliest
        day_end = datetime.combine(day, WORK_END)
        while t + duration <= day_end:
            clash = _conflicts(people, t, t + duration, events, set(exclude_ids))
            if not clash:
                return t, t + duration
            # jump past everything that blocked this attempt
            t = max(t + SLOT_STEP, _ceil_to_step(max(datetime.fromisoformat(c.ends_at)
                                                     for c in clash)))
    return None


# ------------------------------------------------------------------ the planner

def _plan_cancel(action: ActionRecord, ctx: _Context) -> RepairPlan:
    before = action.before
    title = _title(before)
    o_start, o_end = parse_time(before["start"]), parse_time(before["end"])
    orig = human_range(o_start, o_end)
    duration = o_end - o_start
    everyone = attendee_emails(before)
    required = attendee_emails(before, include_optional=False)
    # Someone who had declined the meeting isn't affected by it coming back.
    notify = [p for p in everyone if p != ctx.owner and _rsvp(before, p) != "declined"]

    # A cancelled recurring series can only come back as a series. If this calendar
    # can't create one, re-creating it would silently make one single meeting: refuse.
    if _is_series(before) and not getattr(ctx.cal, "supports_recurrence", False):
        return _noop(action, False,
                     f"Not repaired: '{title}' is a recurring series, and this calendar can "
                     f"only re-create single meetings, which would silently turn the series "
                     f"into one meeting. Restore the series from Google Calendar's trash "
                     f"instead. Nothing was changed.", original_slot=orig)

    # Drift: the event is already back (someone restored it by hand or a fixer ran).
    t = ctx.target
    if t and t.get("status") != "cancelled":
        return _noop(action, True,
                     f"Already repaired: '{title}' is back on the calendar (id {t.get('id')}). "
                     f"Not re-creating it, so nobody gets a duplicate invite.",
                     original_slot=orig)

    earliest = max(o_start, ctx.now) if ctx.now else o_start
    lo = datetime.combine(min(o_start, earliest).date(), time(0))
    events = ctx.events(lo, lo + timedelta(days=SEARCH_DAYS + 2))

    # Drift: someone already re-created the same meeting that day under a new id.
    for e in events:
        if (e.get("id") != action.target_id and e.get("status") != "cancelled"
                and _title(e) == title and set(attendee_emails(e)) == set(everyone)
                and parse_time(e["start"]).date() == o_start.date()):
            when = human_range(parse_time(e["start"]), parse_time(e["end"]))
            return _noop(action, True,
                         f"Already repaired: '{title}' was re-created at {when} "
                         f"(id {e.get('id')}). Restoring it again would create a duplicate.",
                         original_slot=orig)

    conflicts = _conflicts(required, o_start, o_end, events, {action.target_id})
    in_past = ctx.now is not None and o_start < ctx.now

    if not conflicts and not in_past:
        return RepairPlan(
            action_id=action.action_id, case=CASE_A, success=True,
            details=(f"Exact restore: nobody booked anything into {orig} after the cancel, so "
                     f"the original slot is still free for {_all(len(required))} attendees. "
                     f"Putting '{title}' back exactly as it was is the least disruptive fix."),
            notify=notify, operation="create", payload=_clone(before, o_start, o_end),
            original_slot=orig, proposed_slot=orig)

    slot = find_free_slot(required, duration, earliest, events, {action.target_id})
    why = (_blocked_text(conflicts) if conflicts
           else f"the original slot ({orig}) is already in the past")
    if slot is None:
        return RepairPlan(
            action_id=action.action_id, case=CASE_B, success=False,
            details=(f"Not repaired: a blind revert would double-book people because {why}, "
                     f"and there is no time in the next {SEARCH_DAYS} days (weekdays, 9 AM–6 PM) "
                     f"when {_all(len(required))} attendees are free. Nothing was changed; "
                     f"please reschedule '{title}' by hand."),
            conflicts=conflicts, original_slot=orig)
    new = human_range(*slot)
    return RepairPlan(
        action_id=action.action_id, case=CASE_B, success=True,
        details=(f"Rescheduled instead of a blind revert: {why}, so restoring '{title}' at "
                 f"{orig} would double-book them. Re-created it at {new}, the earliest time "
                 f"{_all(len(required))} attendees are free within working hours (9 AM–6 PM). "
                 f"Title, description, meeting link and attendee list are unchanged."),
        notify=notify, operation="create", payload=_clone(before, *slot),
        conflicts=conflicts, original_slot=orig, proposed_slot=new)


def _plan_update(action: ActionRecord, ctx: _Context) -> RepairPlan:
    before, after = action.before, action.after or {}
    title = _title(before)
    o_start, o_end = parse_time(before["start"]), parse_time(before["end"])
    orig = human_range(o_start, o_end)
    duration = o_end - o_start

    cur = ctx.target
    if not cur or cur.get("status") == "cancelled":
        return _noop(action, False,
                     f"Not repaired: '{title}' no longer exists (it was deleted after the agent "
                     f"moved it). Someone may have removed it on purpose, so it was not "
                     f"re-created automatically.", original_slot=orig)

    c_start, c_end = parse_time(cur["start"]), parse_time(cur["end"])
    now_txt = human_range(c_start, c_end)
    if (c_start, c_end) == (o_start, o_end):
        return _noop(action, True,
                     f"Already repaired: '{title}' is back at its original time ({orig}).",
                     original_slot=orig)
    if after.get("start") and after.get("end"):
        a_start, a_end = parse_time(after["start"]), parse_time(after["end"])
        if (c_start, c_end) != (a_start, a_end):
            return _noop(action, False,
                         f"Not repaired: '{title}' was moved again by a person after the agent's "
                         f"change (it is now at {now_txt}). Undoing would overwrite their edit, "
                         f"so it was left alone for the owner to review.", original_slot=orig)

    everyone = _union(attendee_emails(before), attendee_emails(cur))
    required = _union(attendee_emails(before, False), attendee_emails(cur, False))
    others = [p for p in everyone if p != ctx.owner]
    earliest = max(o_start, ctx.now) if ctx.now else o_start
    lo = datetime.combine(min(o_start, c_start, earliest).date(), time(0))
    hi = datetime.combine(max(o_start, c_start).date(), time(0)) + timedelta(days=SEARCH_DAYS + 2)
    events = ctx.events(lo, hi)

    conflicts = _conflicts(required, o_start, o_end, events, {action.target_id})
    accepted = [p for p in others if ctx.response(p) == "accepted"]
    majority = bool(others) and 2 * len(accepted) > len(others)
    in_past = ctx.now is not None and o_start < ctx.now
    revert = _revert_changes(before, after)

    if not conflicts and not in_past:
        if majority:
            details = (f"Moved back: {len(accepted)} of {len(others)} attendees had already "
                       f"accepted the new time ({now_txt}), but the original slot ({orig}) is "
                       f"still free for everyone, so moving '{title}' back creates zero "
                       f"conflicts.")
        else:
            details = (f"Exact restore: nobody has reacted to the agent's move and the original "
                       f"slot ({orig}) is still free for {_all(len(required))} attendees, so "
                       f"'{title}' was moved back from {now_txt}.")
        return RepairPlan(action_id=action.action_id, case=CASE_A, success=True,
                          details=details, notify=others, operation="update",
                          target_id=action.target_id, payload=revert,
                          original_slot=orig, proposed_slot=orig)

    why = (_blocked_text(conflicts) if conflicts
           else f"the original slot ({orig}) is already in the past")

    if majority:
        # Case C: keep the group meeting; give whoever can't make it a makeup sync.
        cant_make = [p for p in others
                     if p in ctx.flaggers or ctx.response(p) == "declined"
                     or _conflicts([p], c_start, c_end, events, {action.target_id})]
        if not cant_make:
            return _noop(action, True,
                         f"Kept as is: {len(accepted)} of {len(others)} attendees already "
                         f"accepted '{title}' at {now_txt}, moving it back would clash because "
                         f"{why}, and everyone can attend the current time.",
                         conflicts=conflicts, original_slot=orig, proposed_slot=now_txt)
        sync_people = ([ctx.owner] if ctx.owner else []) + cant_make
        sync_len = min(duration, MAKEUP_MAX)
        slot = find_free_slot(sync_people, sync_len, earliest, events)
        who = _names(cant_make)
        if slot is None:
            return RepairPlan(
                action_id=action.action_id, case=CASE_C, success=False,
                details=(f"Kept the group meeting at {now_txt} ({len(accepted)} of "
                         f"{len(others)} already accepted; moving it back would clash because "
                         f"{why}). Could not find a makeup slot for {who} in the next "
                         f"{SEARCH_DAYS} days, so nothing was changed."),
                conflicts=conflicts, original_slot=orig, proposed_slot=now_txt)
        new = human_range(*slot)
        return RepairPlan(
            action_id=action.action_id, case=CASE_C, success=True,
            details=(f"Kept the group meeting and added a makeup sync instead of reverting: "
                     f"{len(accepted)} of {len(others)} attendees already accepted '{title}' at "
                     f"{now_txt}, and moving it back would cause new collisions because {why}. "
                     f"Only {who} can't make the new time, so a separate "
                     f"{int(sync_len.total_seconds() // 60)}-minute makeup sync was booked at "
                     f"{new}. Nobody else's calendar changes."),
            notify=cant_make, operation="create",
            payload=_makeup_event(cur, before, sync_people, slot, cant_make, now_txt),
            conflicts=conflicts, original_slot=orig, proposed_slot=new)

    # Case B: few/no reactions yet, original slot blocked -> earliest slot for everyone.
    slot = find_free_slot(required, duration, earliest, events, {action.target_id})
    if slot is None:
        return RepairPlan(
            action_id=action.action_id, case=CASE_B, success=False,
            details=(f"Not repaired: moving '{title}' back would double-book people because "
                     f"{why}, and there is no time in the next {SEARCH_DAYS} days when all "
                     f"{len(required)} attendees are free. It stays at {now_txt}."),
            conflicts=conflicts, original_slot=orig)
    if slot == (c_start, c_end):
        return _noop(action, True,
                     f"Kept as is: moving '{title}' back would double-book people because {why}, "
                     f"and its current time ({now_txt}) is already the earliest slot when "
                     f"everyone is free.", conflicts=conflicts, original_slot=orig,
                     proposed_slot=now_txt)
    new = human_range(*slot)
    changes = dict(revert)
    changes["start"] = format_like(before["start"], slot[0])
    changes["end"] = format_like(before["end"], slot[1])
    return RepairPlan(
        action_id=action.action_id, case=CASE_B, success=True,
        details=(f"Rescheduled instead of a blind revert: {why}, so moving '{title}' back to "
                 f"{orig} would double-book them. Moved it from {now_txt} to {new}, the earliest "
                 f"time {_all(len(required))} attendees are free within working hours "
                 f"(9 AM–6 PM)."),
        notify=others, operation="update", target_id=action.target_id, payload=changes,
        conflicts=conflicts, original_slot=orig, proposed_slot=new)


# ------------------------------------------------------------------ helpers

def _noop(action: ActionRecord, success: bool, details: str, **kw) -> RepairPlan:
    return RepairPlan(action_id=action.action_id, case=CASE_D, success=success,
                      details=details, notify=[], **kw)


def _title(event: Optional[dict]) -> str:
    return str((event or {}).get("summary") or "(untitled event)")


def _union(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        out.extend(p for p in lst if p not in out)
    return out


def _all(n: int) -> str:
    return "both" if n == 2 else f"all {n}"


def _names(people: list[str]) -> str:
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + f" and {people[-1]}"


def _blocked_text(conflicts: list[Conflict]) -> str:
    parts = [f"{c.person} now has '{c.summary}' ({c.when})" for c in conflicts]
    return "the original slot is taken: " + "; ".join(parts)


def _clone(event: dict, start: datetime, end: datetime) -> dict:
    """Copy every piece of the original event (title, description, meeting link,
    attendees, ...) except Google's per-event ids, at the given time."""
    out = {k: copy.deepcopy(v) for k, v in event.items() if k not in VOLATILE_KEYS}
    if _recurrence_rule(event):              # a series comes back as a series
        out["recurrence"] = copy.deepcopy(_recurrence_rule(event))
    out["start"] = format_like(event["start"], start)
    out["end"] = format_like(event["end"], end)
    return out


def _revert_changes(before: dict, after: dict) -> dict:
    """Changes that put every field the agent touched back to its 'before' value."""
    changes = {k: copy.deepcopy(v) for k, v in before.items()
               if k not in VOLATILE_KEYS and after.get(k) != v}
    changes["start"] = copy.deepcopy(before["start"])
    changes["end"] = copy.deepcopy(before["end"])
    return changes


def _makeup_event(current: dict, before: dict, people: list[str],
                  slot: tuple[datetime, datetime], cant_make: list[str], group_time: str) -> dict:
    ev = _clone(current, *slot)
    ev.pop("recurrence", None)                  # a makeup sync is a single meeting
    ev.pop("raw", None)
    ev["summary"] = f"Makeup sync: {_title(before)}"
    by_email = {attendee_email(a): a for a in (current.get("attendees") or [])}
    uses_dicts = any(isinstance(a, dict) for a in by_email.values())
    ev["attendees"] = [copy.deepcopy(by_email[p]) if p in by_email
                       else ({"email": p} if uses_dicts else p) for p in people]
    note = (f"Makeup sync for {_names(cant_make)}, who can't attend the rescheduled group "
            f"meeting ({group_time}). Booked by Agent-Ctrl-Z group repair.")
    desc = (before.get("description") or "").strip()
    ev["description"] = f"{desc}\n\n{note}" if desc else note
    return ev
