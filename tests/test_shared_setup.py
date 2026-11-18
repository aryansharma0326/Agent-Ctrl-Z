"""Checks that the shared starting point connects. Run:  pytest

These only check names and formats, not anyone's real logic, so they should
keep passing while placeholders are swapped for real code. If one breaks,
someone changed a shared name or format: tell the group.
"""
from core.undo_router import route_undo
from fake_data import fake_google as g
from fake_data.sample_data import SCENARIOS
from fake_data.seed import seed
from google_tools.calendar_tools import calendar_cancel, calendar_get, calendar_update
from google_tools.email_tools import email_send
from google_tools.undo_fixes import restore_calendar, undo_email
from repair.group_repair import group_repair
from shared import db
from shared.models import ActionRecord, UndoResult


def test_seed_loads_every_story(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "STATE_PATH", tmp_path / "fake_google.json")
    conn = db.connect(tmp_path / "test.db")
    seed(conn)
    assert len(db.list_actions(conn)) == len(SCENARIOS)
    assert len(db.list_flags(conn)) == sum(1 for s in SCENARIOS if s["flag"])


def test_google_tools_return_before_and_after():
    assert calendar_get("evt_demo") is None or isinstance(calendar_get("evt_demo"), dict)
    for result in (calendar_cancel("evt_demo"),
                   calendar_update("evt_demo", {"start": "2026-11-20T16:00"}),
                   email_send(["aryan@test.com"], "Hi", "Test")):
        assert set(result) == {"before", "after"}


def test_every_fixer_returns_an_undo_result(tmp_path):
    action = ActionRecord(tool="calendar.cancel_event", target_id="evt_demo", before=None,
                          after=None, affected_people=[], instruction="Clear my Friday")
    conn = db.connect(tmp_path / "test.db")
    db.save_action(conn, action)
    for result in (restore_calendar(action), undo_email(action),
                   group_repair(action, {}), route_undo(conn, action.action_id)):
        assert isinstance(result, UndoResult)
        assert result.action_id == action.action_id
