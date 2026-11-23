"""Tests for the shared fake calendar (fake_data/fake_google.py).
Checks it behaves like real Google where undo depends on it."""
import json

import pytest

from fake_data import fake_google as g
from fake_data.sample_data import FRIDAY_EVENTS, OWNER, ARYAN, event


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "STATE_PATH", tmp_path / "fake_google.json")
    g.reset(FRIDAY_EVENTS)


# ---------- cancelling and restoring ----------
def test_cancelled_event_is_hidden_but_restorable_with_same_id():
    before = g.get_event("evt_demo")
    g.delete_event("evt_demo")
    assert g.get_event("evt_demo") is None
    assert "evt_demo" not in g.EVENTS
    assert "evt_demo" not in [e["id"] for e in g.list_events()]

    back = g.restore_event("evt_demo")
    assert back == before                       # same id, same everything
    assert g.get_event("evt_demo") == before


def test_restore_unknown_event_returns_none():
    assert g.restore_event("evt_nope") is None


def test_restore_live_event_just_returns_it():
    assert g.restore_event("evt_gym")["id"] == "evt_gym"


def test_cancelled_events_survive_reloading_the_file():
    g.delete_event("evt_demo")
    g._load()
    assert g.restore_event("evt_demo")["id"] == "evt_demo"


def test_old_state_files_without_cancelled_events_still_load(tmp_path):
    g.STATE_PATH.write_text(json.dumps({"events": {}, "sent_emails": []}))
    assert g.list_events() == [] and g.restore_event("evt_demo") is None


# ---------- listing ----------
def test_list_with_no_window_returns_everything_sorted():
    assert [e["id"] for e in g.list_events()] == ["evt_gym", "evt_1on1", "evt_demo"]


def test_list_with_window_returns_only_overlapping_events():
    ids = [e["id"] for e in g.list_events("2026-11-20T10:00", "2026-11-20T15:30")]
    assert ids == ["evt_1on1", "evt_demo"]


def test_list_window_mixes_times_with_and_without_timezone():
    # 16:00 UTC is 11:00 in New York, so only the 1:1 (11:00-11:30) overlaps
    ids = [e["id"] for e in g.list_events("2026-11-20T16:00:00Z", "2026-11-20T11:15:00-05:00")]
    assert ids == ["evt_1on1"]


# ---------- the agreed event shape ----------
def test_every_event_gets_the_agreed_fields():
    e = g.get_event("evt_gym")
    assert set(e) == {"id", "summary", "start", "end", "attendees",
                      "description", "location", "organizer", "responses"}
    assert (e["description"], e["location"], e["organizer"], e["responses"]) == ("", "", "", {})


def test_insert_accepts_the_flat_shape_and_gives_a_new_id():
    makeup = {"id": "evt_demo", "summary": "Demo prep (moved)", "start": "2026-11-23T09:00",
              "end": "2026-11-23T10:00", "attendees": [OWNER, ARYAN],
              "description": "Moved after a mix-up", "location": "Zoom"}
    made = g.insert_event(makeup)
    assert made["id"] != "evt_demo"
    assert made["location"] == "Zoom" and made["organizer"] == "" and made["responses"] == {}
    assert g.get_event("evt_demo")["summary"] == "CS 598 demo prep"   # original untouched


def test_sample_event_helper_matches_what_the_fake_returns():
    e = event("evt_x", "X", "2026-11-22T10:00", "2026-11-22T11:00", [OWNER])
    assert g.add_event_as_is(e) == e == g.get_event("evt_x")


def test_update_on_cancelled_event_raises():
    g.delete_event("evt_demo")
    with pytest.raises(KeyError):
        g.update_event("evt_demo", {"summary": "nope"})