"""OWNER: Varuni. Tests for core/session_rollback.py.

The calendar and the fixers are pretend versions (like test_undo_router.py), so these
tests check only rollback's own logic: which actions, how they combine, what is
skipped, the order, what gets saved and who is told.
"""
import pytest

from core import session_rollback as s
from shared import db
from shared.models import ActionRecord

T0 = "2026-11-20T14:00:00+00:00"
DEMO = {"id": "evt_demo", "summary": "CS 598 demo prep", "start": "2026-11-20T15:00:00-05:00",
        "end": "2026-11-20T16:00:00-05:00", "attendees": ["aryan@test.com", "visista@test.com"]}
MOVED = {**DEMO, "start": "2026-11-20T17:00:00-05:00", "end": "2026-11-20T18:00:00-05:00"}
MOVED_AGAIN = {**DEMO, "start": "2026-11-21T09:00:00-05:00", "end": "2026-11-21T10:00:00-05:00"}
NEW = {"id": "evt_new", "summary": "Planning sync", "start": "2026-11-23T14:00:00-05:00",
       "end": "2026-11-23T14:30:00-05:00", "attendees": ["sam@test.com"]}


def at(minute: int) -> str:
    return f"2026-11-20T14:{minute:02d}:00+00:00"


def act(conn, tool, target, before, after, minute, people=("aryan@test.com",),
        instruction="Sort out my Friday", status="done"):
    a = ActionRecord(tool=tool, target_id=target, before=before, after=after,
                     affected_people=list(people), instruction=instruction,
                     status=status, timestamp=at(minute))
    db.save_action(conn, a)
    return a


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


# ---------- which actions ----------

def test_only_actions_in_the_window_that_are_not_undone(conn):
    old = act(conn, s.CANCEL, "evt_a", DEMO, None, 1)
    inside = act(conn, s.CANCEL, "evt_b", DEMO, None, 20)
    act(conn, s.CANCEL, "evt_c", DEMO, None, 25, status="undone")
    later = act(conn, s.CANCEL, "evt_d", DEMO, None, 50)
    got = s.actions_between(conn, at(10), at(40))
    assert [a.action_id for a in got] == [inside.action_id]
    assert old not in got and later not in got


# ---------- net changes ----------

def test_move_twice_then_cancel_is_one_cancel_of_the_original(conn):
    a1 = act(conn, s.UPDATE, "evt_demo", DEMO, MOVED, 1, instruction="Move demo prep")
    a2 = act(conn, s.UPDATE, "evt_demo", MOVED, MOVED_AGAIN, 2, people=("visista@test.com",))
    a3 = act(conn, s.CANCEL, "evt_demo", MOVED_AGAIN, None, 3, instruction="Clear my Friday")
    [change] = s.net_changes([a1, a2, a3])
    rec = change.record
    assert rec.tool == s.CANCEL and rec.before == DEMO and rec.after is None
    assert rec.affected_people == ["aryan@test.com", "visista@test.com"]
    assert rec.instruction == "Move demo prep; Sort out my Friday; Clear my Friday"
    assert change.action_ids == [a1.action_id, a2.action_id, a3.action_id]


def test_moved_twice_is_one_update_from_the_original(conn):
    a1 = act(conn, s.UPDATE, "evt_demo", DEMO, MOVED, 1)
    a2 = act(conn, s.UPDATE, "evt_demo", MOVED, MOVED_AGAIN, 2)
    rec = s.net_changes([a1, a2])[0].record
    assert (rec.tool, rec.before, rec.after) == (s.UPDATE, DEMO, MOVED_AGAIN)


def test_created_then_changed_is_one_create(conn):
    a1 = act(conn, s.CREATE, "evt_new", None, NEW, 1)
    a2 = act(conn, s.UPDATE, "evt_new", NEW, {**NEW, "summary": "Planning"}, 2)
    rec = s.net_changes([a1, a2])[0].record
    assert rec.tool == s.CREATE and rec.before is None and rec.after["summary"] == "Planning"


def test_created_then_cancelled_leaves_nothing_to_undo(conn):
    a1 = act(conn, s.CREATE, "evt_new", None, NEW, 1)
    a2 = act(conn, s.CANCEL, "evt_new", NEW, None, 2)
    [change] = s.net_changes([a1, a2])
    assert change.record is None and change.action_ids == [a1.action_id, a2.action_id]


def test_each_email_stays_separate_and_newest_change_comes_first(conn):
    e1 = act(conn, "email.send", "msg_1", None, {"id": "msg_1"}, 1)
    c1 = act(conn, s.CANCEL, "evt_demo", DEMO, None, 2)
    e2 = act(conn, "email.send", "msg_2", None, {"id": "msg_2"}, 3)
    order = [c.action_ids for c in s.net_changes([e1, c1, e2])]
    assert order == [[e2.action_id], [c1.action_id], [e1.action_id]]


# ---------- planning (dry run) ----------

from core import undo_router as r
from shared.models import UndoResult


@pytest.fixture
def cal(conn, monkeypatch):
    """A pretend calendar and fixers. .calendar is what Google has now; .calls records fixers."""
    class Env:
        calendar, calls = {}, []
    e = Env()
    e.calendar, e.calls, e.conn = {}, [], conn

    def fixer(name):
        def run(action, *args):
            e.calls.append((name, action.target_id))
            return UndoResult(action.action_id, name, True, f"{name} on {action.target_id}",
                              list(action.affected_people))
        return run

    monkeypatch.setattr(r.calendar_tools, "calendar_get", lambda i: e.calendar.get(i))
    monkeypatch.setattr(r.calendar_tools, "calendar_list", lambda a, b: list(e.calendar.values()))
    monkeypatch.setattr(r.calendar_tools, "calendar_cancel",
                        lambda i: {"before": e.calendar.pop(i, None), "after": None})
    monkeypatch.setattr(r, "restore_calendar", fixer("exact_restore"))
    monkeypatch.setattr(r, "undo_email", fixer("email_correction"))
    monkeypatch.setattr(r, "group_repair", fixer("group_repair"))
    monkeypatch.setattr(r, "send_fixed_notices", lambda a, res: [])
    return e


def test_plan_changes_nothing_and_explains_each_step(cal):
    act(cal.conn, s.CANCEL, "evt_demo", DEMO, None, 5)
    act(cal.conn, "email.send", "msg_1", None, {"id": "msg_1", "to": ["sam@test.com"]}, 6,
        people=("sam@test.com",))
    plan = s.plan_rollback(cal.conn, at(0), at(30))
    assert [st.strategy for st in plan.steps] == ["email_correction", "exact_restore"]
    assert all(st.will_run for st in plan.steps) and cal.calls == []
    assert plan.summary() == "Will undo 2 changes (2 agent actions) and tell 2 people it's fixed."
    assert all(a.status == "done" for a in db.list_actions(cal.conn))
    assert "cancelled 'CS 598 demo prep'" in plan.steps[1].what_happened


def test_event_a_person_edited_after_the_agent_is_skipped(cal):
    act(cal.conn, s.UPDATE, "evt_demo", DEMO, MOVED, 5)
    cal.calendar["evt_demo"] = {**MOVED, "location": "CDS 701"}        # a person edited it
    [step] = s.plan_rollback(cal.conn, at(0), at(30)).steps
    assert not step.will_run and "would overwrite their change" in step.skip_reason
    assert step.will_notify == []


def test_cancelled_event_someone_already_put_back_is_skipped(cal):
    act(cal.conn, s.CANCEL, "evt_demo", DEMO, None, 5)
    cal.calendar["evt_demo"] = DEMO
    [step] = s.plan_rollback(cal.conn, at(0), at(30)).steps
    assert not step.will_run and "already put it back" in step.skip_reason


def test_created_event_a_person_edited_is_skipped(cal):
    act(cal.conn, s.CREATE, "evt_new", None, NEW, 5)
    cal.calendar["evt_new"] = {**NEW, "description": "agenda added by Sam"}
    [step] = s.plan_rollback(cal.conn, at(0), at(30)).steps
    assert not step.will_run and "edited the event the agent created" in step.skip_reason


def test_created_then_removed_needs_no_work(cal):
    act(cal.conn, s.CREATE, "evt_new", None, NEW, 5)
    act(cal.conn, s.CANCEL, "evt_new", NEW, None, 6)
    [step] = s.plan_rollback(cal.conn, at(0), at(30)).steps
    assert step.strategy == s.NOTHING and step.will_run and step.will_notify == []


def test_fingerprint_changes_when_the_situation_changes(cal):
    act(cal.conn, s.UPDATE, "evt_demo", DEMO, MOVED, 5)
    cal.calendar["evt_demo"] = MOVED
    before = s.plan_rollback(cal.conn, at(0), at(30)).fingerprint()
    cal.calendar["evt_demo"] = {**MOVED, "summary": "Renamed by Aryan"}
    assert s.plan_rollback(cal.conn, at(0), at(30)).fingerprint() != before


def test_to_dict_is_plain_data(cal):
    act(cal.conn, s.CANCEL, "evt_demo", DEMO, None, 5)
    out = s.plan_rollback(cal.conn, at(0), at(30)).to_dict()
    assert "record" not in out["steps"][0] and out["summary"].startswith("Will undo 1 change ")
