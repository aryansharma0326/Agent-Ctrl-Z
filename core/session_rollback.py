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

from dataclasses import dataclass
from datetime import datetime, timezone

from shared import db
from shared.models import ActionRecord

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
