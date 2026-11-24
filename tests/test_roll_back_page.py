"""OWNER: Varuni. Smoke test for pages/5_roll_back.py using Streamlit's test runner."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import undo_router as r
from shared import db
from shared.models import ActionRecord, UndoResult

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "5_roll_back.py")
DEMO = {"id": "evt_demo", "summary": "CS 598 demo prep", "start": "2026-11-20T15:00:00-05:00",
        "end": "2026-11-20T16:00:00-05:00", "attendees": ["aryan@test.com"]}


@pytest.fixture
def one_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB", tmp_path / "t.db")
    monkeypatch.setattr(r.calendar_tools, "calendar_get", lambda i: None)
    monkeypatch.setattr(r.calendar_tools, "calendar_list", lambda a, b: [])
    monkeypatch.setattr(r, "restore_calendar", lambda a: UndoResult(
        a.action_id, "exact_restore", True, "Put it back.", []))
    ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    a = ActionRecord(tool="calendar.cancel_event", target_id="evt_demo", before=DEMO, after=None,
                     affected_people=["aryan@test.com"], instruction="Clear my Friday",
                     timestamp=ten_min_ago)
    db.save_action(db.connect(), a)
    return a


def test_preview_then_roll_back(one_cancel):
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
    assert at.subheader[0].value.startswith("Will undo 1 change")
    assert db.get_action(db.connect(), one_cancel.action_id).status == "done"   # preview only
    at.button[0].click().run()
    assert not at.exception
    assert db.get_action(db.connect(), one_cancel.action_id).status == "undone"
    assert "Undid 1 change" in at.success[-1].value


def test_a_shorter_window_leaves_older_actions_out(one_cancel):
    half_hour_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    one_cancel.timestamp = half_hour_ago.isoformat(timespec="seconds")
    db.save_action(db.connect(), one_cancel)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert at.subheader[0].value.startswith("Will undo 1 change")       # last hour: included
    at.radio[0].set_value("Last 15 minutes").run()
    assert "didn't do anything" in at.info[0].value                     # 30 minutes ago: not
