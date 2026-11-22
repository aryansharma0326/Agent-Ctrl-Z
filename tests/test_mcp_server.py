"""OWNER: Varuni. Tests for core/mcp_server.py

Visista's google_tools are still placeholders, so these tests swap in a
pretend version backed by the shared fake calendar (fake_data/fake_google).
"""
import pytest

from core import mcp_server
from fake_data import fake_google as g
from fake_data.sample_data import FRIDAY_EVENTS
from shared import db


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "STATE_PATH", tmp_path / "fake_google.json")
    g.reset(FRIDAY_EVENTS)
    conn = db.connect(tmp_path / "test.db")
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)

    ct = mcp_server.calendar_tools
    monkeypatch.setattr(ct, "calendar_get", g.get_event)
    monkeypatch.setattr(ct, "calendar_list", lambda start, end: g.list_events())

    def cancel(event_id):
        before = g.get_event(event_id)
        if before:
            g.delete_event(event_id)
        return {"before": before, "after": None}

    def update(event_id, changes):
        before = g.get_event(event_id)
        return {"before": before, "after": g.update_event(event_id, changes) if before else None}

    monkeypatch.setattr(ct, "calendar_cancel", cancel)
    monkeypatch.setattr(ct, "calendar_update", update)
    monkeypatch.setattr(ct, "calendar_create", lambda e: {"before": None, "after": g.insert_event(e)})
    monkeypatch.setattr(mcp_server.email_tools, "email_send",
                        lambda to, s, b: {"before": None, "after": g.send_email(to, s, b)})
    return conn


def test_all_tools_are_registered():
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert names == {"list_events", "get_event", "cancel_event", "update_event",
                     "create_event", "send_email"}


def test_cancel_is_logged_with_before_copy(server):
    out = mcp_server.cancel_event("evt_demo", "Clear my Friday")
    assert out["ok"] and "evt_demo" not in g.EVENTS
    rec = db.get_action(server, out["action_id"])
    assert rec.tool == "calendar.cancel_event" and rec.before["summary"] == "CS 598 demo prep"
    assert rec.instruction == "Clear my Friday"
    assert rec.affected_people == ["aryan@test.com", "visista@test.com"]


def test_update_is_logged(server):
    out = mcp_server.update_event("evt_1on1", {"start": "2026-11-27T11:00"}, "Push my 1:1")
    rec = db.get_action(server, out["action_id"])
    assert rec.before["start"] == "2026-11-20T11:00" and rec.after["start"] == "2026-11-27T11:00"


def test_create_and_email_are_logged(server):
    made = mcp_server.create_event({"summary": "Retro", "start": "2026-11-21T10:00",
                                    "end": "2026-11-21T11:00", "attendees": ["aryan@test.com"]},
                                   "Book a retro")
    sent = mcp_server.send_email(["aryan@test.com"], "Hi", "Test", "Email Aryan")
    assert made["ok"] and sent["ok"]
    assert [a.tool for a in db.list_actions(server)] == ["calendar.create_event", "email.send"]


def test_missing_event_is_not_logged(server):
    out = mcp_server.cancel_event("evt_nope", "Clear my Friday")
    assert not out["ok"] and db.list_actions(server) == []


def test_read_tools_are_not_logged(server):
    assert len(mcp_server.list_events("2026-11-20T00:00", "2026-11-21T00:00")) == 3
    assert mcp_server.get_event("evt_gym")["summary"] == "Gym"
    assert db.list_actions(server) == []


def test_empty_inputs_are_refused(server):
    assert not mcp_server.update_event("evt_gym", {}, "x")["ok"]
    assert not mcp_server.send_email([], "s", "b", "x")["ok"]
    assert db.list_actions(server) == []


def test_placeholders_do_nothing_and_log_nothing(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "test.db")
    monkeypatch.setattr(mcp_server, "get_conn", lambda: conn)
    assert not mcp_server.cancel_event("evt_demo", "Clear my Friday")["ok"]
    assert db.list_actions(conn) == []
