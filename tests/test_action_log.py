"""OWNER: Varuni. Tests for core/action_log.py"""
import pytest

from core.action_log import affected_people, log_action
from shared import db

OWNER = "varuni@test.com"
DEMO = {"id": "evt_demo", "summary": "CS 598 demo prep", "start": "2026-11-20T15:00:00-05:00",
        "end": "2026-11-20T16:00:00-05:00",
        "attendees": [OWNER, "aryan@test.com", "visista@test.com"]}


def test_owner_is_not_affected():
    assert affected_people({"before": DEMO, "after": None}, OWNER) == ["aryan@test.com",
                                                                        "visista@test.com"]


def test_someone_removed_from_a_meeting_is_still_affected():
    after = {**DEMO, "attendees": [OWNER, "aryan@test.com"]}
    assert "visista@test.com" in affected_people({"before": DEMO, "after": after}, OWNER)


def test_email_recipients_are_affected_without_duplicates():
    sent = {"id": "msg_1", "to": ["Aryan@test.com", "aryan@test.com", OWNER]}
    assert affected_people({"before": None, "after": sent}, OWNER) == ["aryan@test.com"]


def test_organizer_counts_as_owner():
    event = {**DEMO, "organizer": "aryan@test.com"}
    assert "aryan@test.com" not in affected_people({"before": event, "after": None}, OWNER)


def test_log_action_saves_the_before_copy(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    rec = log_action(conn, "calendar.cancel_event", "evt_demo",
                     {"before": DEMO, "after": None}, "Clear my Friday")
    saved = db.get_action(conn, rec.action_id)
    assert saved.before == DEMO and saved.status == "done"
    assert saved.affected_people == ["aryan@test.com", "visista@test.com"]


def test_unknown_tool_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown tool"):
        log_action(db.connect(tmp_path / "t.db"), "calendar.explode", "x",
                   {"before": None, "after": None}, "oops")


def test_result_must_have_before_and_after(tmp_path):
    with pytest.raises(ValueError, match="before"):
        log_action(db.connect(tmp_path / "t.db"), "email.send", "x", {"after": {}}, "oops")
