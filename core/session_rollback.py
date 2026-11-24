"""OWNER: Varuni
CALLED BY: the MCP server (preview_rollback / rollback_session)

Session rollback: "undo everything the agent did in the last hour."

Undoing one action at a time breaks down when the agent did several things. If it
moved an event twice and then cancelled it, undoing the actions one by one would
restore an in-between version, and the router would then see "someone edited it"
and switch to group repair for no reason. So rollback works on NET changes:

  1. take every agent action in the time window that isn't already undone
  2. group them by the event (or email) they touched, oldest first
  3. collapse each group into one net change: before the FIRST action -> after
     the LAST one. Created then cancelled = nothing left to undo.
  4. plan each net change with the undo router (dry run, nothing changes)
  5. skip anything a person changed after the agent, so a rollback never
     overwrites a human's edit; say why it was skipped
  6. run the rest newest-first, record a result for every original action, and
     send each affected person ONE note listing everything that was fixed
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

from core.receipts import describe, send_rollback_notices
from core.undo_router import (ALREADY_BACK, EDITED_AFTER, UndoPlan, calendar_tools,
                               edited_after_agent, plan_for, run_undo)
from shared import db
from shared.models import ActionRecord, UndoResult

ROLLBACKABLE = ("done", "flagged", "undo_failed")
CREATE, CANCEL, UPDATE = "calendar.create_event", "calendar.cancel_event", "calendar.update_event"
NOTHING = "nothing_to_undo"


def _when(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def actions_between(conn, since: str, until: str | None = None) -> list[ActionRecord]:
    """Agent actions in [since, until] that can still be rolled back, oldest first."""
    start = _when(since)
    end = _when(until) if until else datetime.now(timezone.utc)
    return [a for a in db.list_actions(conn)
            if a.status in ROLLBACKABLE and start <= _when(a.timestamp) <= end]


# ---------- collapse each event's actions into one net change ----------

@dataclass
class NetChange:
    """Everything the agent did to one event or email, as a single change."""
    target_id: str
    actions: list[ActionRecord]          # oldest first
    record: ActionRecord | None          # combined record to undo; None = nothing to undo

    @property
    def action_ids(self) -> list[str]:
        return [a.action_id for a in self.actions]

    @property
    def last_time(self) -> str:
        return self.actions[-1].timestamp


def _union(lists: list[list[str]]) -> list[str]:
    seen: list[str] = []
    for people in lists:
        seen += [p for p in people if p not in seen]
    return seen


def net_change(actions: list[ActionRecord]) -> NetChange:
    """Collapse one target's actions (oldest first) into before-first -> after-last."""
    first, last = actions[0], actions[-1]
    if first.tool == "email.send" or len(actions) == 1:
        return NetChange(first.target_id, actions, first if len(actions) == 1 else last)

    if first.tool == CREATE:
        tool = None if last.tool == CANCEL else CREATE      # made then removed: nothing left
    else:
        tool = CANCEL if last.tool == CANCEL else UPDATE
    if tool is None:
        return NetChange(first.target_id, actions, None)

    instructions = list(dict.fromkeys(a.instruction for a in actions))
    record = ActionRecord(
        tool=tool, target_id=first.target_id,
        before=None if tool == CREATE else first.before,
        after=None if tool == CANCEL else last.after,
        affected_people=_union([a.affected_people for a in actions]),
        instruction="; ".join(instructions), status="done",
        action_id=last.action_id, timestamp=last.timestamp)
    return NetChange(first.target_id, actions, record)


def net_changes(actions: list[ActionRecord]) -> list[NetChange]:
    """One NetChange per event/email, newest last change first (the order to undo in)."""
    groups: dict[str, list[ActionRecord]] = {}
    for a in sorted(actions, key=lambda a: _when(a.timestamp)):
        key = f"email:{a.action_id}" if a.tool == "email.send" else a.target_id
        groups.setdefault(key, []).append(a)
    changes = [net_change(group) for group in groups.values()]
    return sorted(changes, key=lambda c: _when(c.last_time), reverse=True)


# ---------- the plan (dry run: decides, changes nothing) ----------

@dataclass
class RollbackStep:
    """What rollback will do about one event or email."""
    target_id: str
    action_ids: list[str]
    what_happened: str              # every agent action on it, in order
    strategy: str                   # the router's strategy, or "nothing_to_undo"
    explanation: str                # plain English, safe to show the owner
    will_notify: list[str]
    will_run: bool
    skip_reason: str | None = None
    record: ActionRecord | None = field(default=None, repr=False)
    plan: UndoPlan | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in ("record", "plan")}


@dataclass
class RollbackPlan:
    since: str
    until: str
    steps: list[RollbackStep]       # newest first: the order they will run in

    @property
    def to_run(self) -> list[RollbackStep]:
        return [s for s in self.steps if s.will_run]

    @property
    def skipped(self) -> list[RollbackStep]:
        return [s for s in self.steps if not s.will_run]

    @property
    def people(self) -> list[str]:
        return _union([s.will_notify for s in self.to_run])

    def fingerprint(self) -> str:
        """Changes whenever what the rollback would do changes (used for approval codes)."""
        return f"{self.since}|{self.until}|" + "|".join(
            f"{s.target_id}:{','.join(s.action_ids)}:{s.strategy}:{s.will_run}"
            for s in self.steps)

    def summary(self) -> str:
        actions = sum(len(s.action_ids) for s in self.to_run)
        text = (f"Will undo {len(self.to_run)} change{'s' if len(self.to_run) != 1 else ''} "
                f"({actions} agent action{'s' if actions != 1 else ''})")
        if self.skipped:
            text += f", skip {len(self.skipped)}"
        return text + f" and tell {len(self.people)} people it's fixed."

    def to_dict(self) -> dict:
        return {"since": self.since, "until": self.until, "summary": self.summary(),
                "steps": [s.to_dict() for s in self.steps]}


def _skip_reason(change: NetChange, plan: UndoPlan) -> str | None:
    """Why this change must be left alone, or None if it's safe to undo."""
    if not plan.can_undo:
        return plan.explanation
    if plan.conflict == ALREADY_BACK:
        return "Someone already put it back, so there is nothing to undo."
    if plan.conflict == EDITED_AFTER:
        return ("A person edited it after the agent, so rolling back would overwrite "
                "their change. Undo it by hand if you still want to.")
    record = change.record
    if record.tool == CREATE:
        current = calendar_tools.calendar_get(record.target_id)
        if edited_after_agent(current, record.after):
            return ("A person edited the event the agent created, so removing it could "
                    "throw away their change. Remove it by hand if you still want to.")
    return None


def plan_rollback(conn, since: str, until: str | None = None) -> RollbackPlan:
    """Decide what rolling back [since, until] would do, without doing it. Never raises."""
    until = until or datetime.now(timezone.utc).isoformat(timespec="seconds")
    steps = []
    for change in net_changes(actions_between(conn, since, until)):
        what = " then ".join(describe(a) for a in change.actions)
        if change.record is None:
            steps.append(RollbackStep(
                change.target_id, change.action_ids, what, NOTHING,
                "The agent created this and later removed it itself, so there's nothing "
                "to put back.", [], True))
            continue
        try:
            plan = plan_for(change.record)
            reason = _skip_reason(change, plan)
        except Exception as exc:
            plan, reason = None, f"Couldn't check it safely ({exc}), so it was left alone."
        steps.append(RollbackStep(
            change.target_id, change.action_ids, what,
            plan.strategy if plan else "none",
            reason or plan.explanation,
            [] if reason else list(plan.will_notify),
            reason is None, reason, change.record, plan))
    return RollbackPlan(since, until, steps)


# ---------- run it ----------

@dataclass
class RollbackReport:
    undone: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    told_its_fixed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Undid {len(self.undone)} change{'s' if len(self.undone) != 1 else ''}"
        if self.failed:
            text += f", {len(self.failed)} failed"
        if self.skipped:
            text += f", skipped {len(self.skipped)}"
        return text + f". Told {len(self.told_its_fixed)} people it's fixed."

    def to_dict(self) -> dict:
        return {"summary": self.summary(), **asdict(self)}


def run_rollback(conn, plan: RollbackPlan) -> RollbackReport:
    """Carry out a rollback plan, newest change first. Every original agent action gets
    its own saved result and status. One failure never stops the rest. Never raises."""
    report = RollbackReport()
    flags = db.list_flags(conn)
    fixes: list[tuple[ActionRecord, UndoResult]] = []
    for step in plan.steps:
        if not step.will_run:
            report.skipped.append({**step.to_dict(), "details": step.skip_reason})
            continue
        if step.strategy == NOTHING:
            result = UndoResult(step.action_ids[-1], "exact_restore", True, step.explanation, [])
        else:
            flagged_by = [f.flagged_by for f in flags if f.action_id in step.action_ids]
            result = run_undo(step.record, step.plan, flagged_by)
        for action_id in step.action_ids:
            try:
                db.save_undo_result(conn, replace(result, action_id=action_id))
                db.set_action_status(conn, action_id, "undone" if result.success else "undo_failed")
            except Exception as exc:          # keep going; say what couldn't be saved
                result = replace(result, details=f"{result.details} (couldn't save: {exc})")
        entry = {**step.to_dict(), "fix_used": result.fix_type, "details": result.details,
                 "notify": result.notify}
        (report.undone if result.success else report.failed).append(entry)
        if result.success and step.record is not None:
            fixes.append((step.record, result))
    try:
        report.told_its_fixed = [n["to"] for n in send_rollback_notices(fixes)]
    except Exception:
        report.told_its_fixed = []
    return report
