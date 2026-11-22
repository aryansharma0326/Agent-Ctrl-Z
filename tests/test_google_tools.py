"""OWNER: Visista. Tests for google_tools/. Run:  pytest tests/test_google_tools.py

These never touch real Google: every test forces the fake calendar unless it is
checking the switch itself (and the real backend is only placeholders for now).
"""
import pytest

from fake_data import fake_google
from fake_data.sample_data import FRIDAY_EVENTS, OWNER
from google_tools import backend, calendar_tools as ct, google_api

AGREED_FIELDS = {"id", "summary", "start", "end", "attendees", "description", "location",
                 "organizer", "responses"}
FRIDAY = ("2026-11-20T00:00", "2026-11-21T00:00")


@pytest.fixture(autouse=True)
def use_fake(monkeypatch):
    monkeypatch.delenv("USE_REAL_GOOGLE", raising=False)


# ---------- step 2: the backend switch ----------
def test_fake_calendar_is_the_default():
    assert backend.get_backend() is fake_google
    assert backend.backend_name() == "fake calendar"


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_flag_switches_to_real_google(monkeypatch, value):
    monkeypatch.setenv("USE_REAL_GOOGLE", value)
    assert backend.get_backend() is google_api
    assert backend.backend_name() == "real Google"


@pytest.mark.parametrize("value", ["0", "", "false"])
def test_other_flag_values_stay_on_fake(monkeypatch, value):
    monkeypatch.setenv("USE_REAL_GOOGLE", value)
    assert backend.get_backend() is fake_google


def test_both_backends_have_every_required_function():
    for module in (fake_google, google_api):
        for name in backend.REQUIRED:
            assert callable(getattr(module, name, None)), f"{module.__name__}.{name} missing"


def test_missing_function_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(backend, "FAKE", "json")  # a module with none of the functions
    with pytest.raises(RuntimeError, match="missing: get_event"):
        backend.get_backend()


def test_real_placeholders_fail_loudly(monkeypatch):
    monkeypatch.setenv("USE_REAL_GOOGLE", "1")
    with pytest.raises(NotImplementedError, match="step 8"):
        backend.get_backend().get_event("evt_demo")

# ---------- step 3: reading the calendar ----------
@pytest.fixture
def friday(tmp_path, monkeypatch):
    """A fresh fake calendar with the three Friday events, in a temp file."""
    monkeypatch.setattr(fake_google, "STATE_PATH", tmp_path / "fake_google.json")
    fake_google.reset(FRIDAY_EVENTS)


def test_get_returns_the_event_in_the_agreed_shape(friday):
    e = ct.calendar_get("evt_demo")
    assert e["summary"] == "CS 598 demo prep"
    assert e["start"] == "2026-11-20T15:00"
    assert AGREED_FIELDS <= set(e)


def test_get_missing_event_is_none(friday):
    assert ct.calendar_get("evt_nope") is None


def test_get_cancelled_event_is_none(friday):
    fake_google.delete_event("evt_demo")
    assert ct.calendar_get("evt_demo") is None


def test_get_returns_a_copy_not_the_stored_event(friday):
    ct.calendar_get("evt_demo")["summary"] = "changed by mistake"
    assert ct.calendar_get("evt_demo")["summary"] == "CS 598 demo prep"


def test_missing_fields_get_defaults():
    e = ct._shape({"id": "x", "summary": "X", "start": "2026-11-20T09:00",
                   "end": "2026-11-20T10:00", "location": None})
    assert (e["attendees"], e["description"], e["location"], e["organizer"], e["responses"]) \
        == ([], "", "", "", {})


def test_list_friday_returns_all_three_sorted(friday):
    assert [e["id"] for e in ct.calendar_list(*FRIDAY)] == ["evt_gym", "evt_1on1", "evt_demo"]


def test_list_only_returns_events_overlapping_the_window(friday):
    # 10:45-11:15 overlaps the 1:1 (11:00-11:30) only
    assert [e["id"] for e in ct.calendar_list("2026-11-20T10:45", "2026-11-20T11:15")] == ["evt_1on1"]


def test_list_leaves_out_cancelled_events(friday):
    fake_google.delete_event("evt_demo")
    assert [e["id"] for e in ct.calendar_list(*FRIDAY)] == ["evt_gym", "evt_1on1"]


def test_list_accepts_times_with_or_without_timezone(friday):
    # 16:00 UTC = 11:00 New York, so this is the same 11:00-11:15 window
    ids = [e["id"] for e in ct.calendar_list("2026-11-20T16:00:00Z", "2026-11-20T11:15:00-05:00")]
    assert ids == ["evt_1on1"]


def test_list_leaves_out_all_day_events(friday):
    fake_google.add_event_as_is({"id": "evt_holiday", "summary": "Day off", "start": "2026-11-20",
                                 "end": "2026-11-21", "attendees": [OWNER]})
    assert "evt_holiday" not in [e["id"] for e in ct.calendar_list(*FRIDAY)]


def test_list_events_are_in_the_agreed_shape(friday):
    assert all(AGREED_FIELDS <= set(e) for e in ct.calendar_list(*FRIDAY))


def test_list_gives_a_clear_error_for_bad_times(friday):
    with pytest.raises(ValueError, match="ISO time"):
        ct.calendar_list("Friday", "2026-11-21T00:00")


def test_list_end_must_be_after_start(friday):
    with pytest.raises(ValueError, match="must be after start"):
        ct.calendar_list("2026-11-21T00:00", "2026-11-20T00:00")


# ---------- step 4: changing the calendar ----------
def test_cancel_returns_the_before_copy_and_removes_the_event(friday):
    result = ct.calendar_cancel("evt_demo")
    assert set(result) == {"before", "after"}
    assert result["before"]["summary"] == "CS 598 demo prep"
    assert AGREED_FIELDS <= set(result["before"])
    assert result["after"] is None
    assert ct.calendar_get("evt_demo") is None
    assert "evt_demo" not in [e["id"] for e in ct.calendar_list(*FRIDAY)]


def test_cancelled_event_can_come_back_with_the_same_id(friday):
    before = ct.calendar_cancel("evt_demo")["before"]
    fake_google.restore_event("evt_demo")          # what step 5 will do
    assert ct.calendar_get("evt_demo") == before


def test_cancel_missing_or_already_cancelled_event_does_nothing(friday):
    assert ct.calendar_cancel("evt_nope") == {"before": None, "after": None}
    ct.calendar_cancel("evt_demo")
    assert ct.calendar_cancel("evt_demo") == {"before": None, "after": None}


def test_update_returns_before_and_after(friday):
    result = ct.calendar_update("evt_demo", {"start": "2026-11-20T16:00", "end": "2026-11-20T17:00"})
    assert (result["before"]["start"], result["before"]["end"]) == ("2026-11-20T15:00", "2026-11-20T16:00")
    assert (result["after"]["start"], result["after"]["end"]) == ("2026-11-20T16:00", "2026-11-20T17:00")
    assert result["after"]["id"] == "evt_demo"
    assert ct.calendar_get("evt_demo")["start"] == "2026-11-20T16:00"


def test_update_with_only_a_new_start_keeps_the_length(friday):
    after = ct.calendar_update("evt_demo", {"start": "2026-11-20T17:30"})["after"]
    assert (after["start"], after["end"]) == ("2026-11-20T17:30", "2026-11-20T18:30")


def test_update_keeps_the_timezone_style_of_the_new_start(friday):
    after = ct.calendar_update("evt_demo", {"start": "2026-11-20T17:00:00-05:00"})["after"]
    assert after["end"] == "2026-11-20T18:00:00-05:00"


def test_update_quietly_ignores_fields_google_decides(friday):
    # Aryan's repairs copy whole events, so these can arrive in changes
    after = ct.calendar_update("evt_demo", {"start": "2026-11-20T16:00", "end": "2026-11-20T17:00",
                                            "id": "evt_other", "responses": {}, "organizer": "x@test.com",
                                            "raw": {}, "status": "confirmed"})["after"]
    assert after["id"] == "evt_demo" and after["organizer"] == ""


def test_update_rejects_unknown_fields_with_a_clear_error(friday):
    with pytest.raises(ValueError, match="can't set title. You can set: summary"):
        ct.calendar_update("evt_demo", {"title": "New name"})


def test_update_with_nothing_to_change_is_an_error(friday):
    with pytest.raises(ValueError, match="nothing to change"):
        ct.calendar_update("evt_demo", {"responses": {}})


def test_update_end_before_start_is_refused_and_changes_nothing(friday):
    with pytest.raises(ValueError, match="must be after start"):
        ct.calendar_update("evt_demo", {"end": "2026-11-20T14:00"})
    assert ct.calendar_get("evt_demo")["end"] == "2026-11-20T16:00"


def test_update_missing_event_does_nothing(friday):
    assert ct.calendar_update("evt_nope", {"summary": "x"}) == {"before": None, "after": None}


def test_update_turns_google_style_attendees_into_emails(friday):
    after = ct.calendar_update("evt_1on1", {"attendees": [{"email": OWNER}, "aryan@test.com"]})["after"]
    assert after["attendees"] == [OWNER, "aryan@test.com"]


def test_before_copy_is_safe_to_change(friday):
    result = ct.calendar_update("evt_demo", {"summary": "Renamed"})
    result["before"]["summary"] = "changed by mistake"
    assert ct.calendar_get("evt_demo")["summary"] == "Renamed"


def test_create_takes_the_flat_shape_and_gets_a_new_id(friday):
    result = ct.calendar_create({"summary": "Makeup sync", "start": "2026-11-23T10:00",
                                 "end": "2026-11-23T10:30", "attendees": [OWNER, "aryan@test.com"],
                                 "description": "Notes", "location": "https://zoom.us/j/123"})
    assert result["before"] is None
    made = result["after"]
    assert made["id"].startswith("evt_") and AGREED_FIELDS <= set(made)
    assert (made["location"], made["description"]) == ("https://zoom.us/j/123", "Notes")
    assert ct.calendar_get(made["id"]) == made


def test_create_from_a_copy_of_another_event_leaves_the_original_alone(friday):
    copy_of_demo = ct.calendar_get("evt_demo")       # has id, organizer, responses...
    copy_of_demo.update(start="2026-11-23T15:00", end="2026-11-23T16:00")
    made = ct.calendar_create(copy_of_demo)["after"]
    assert made["id"] != "evt_demo"
    assert ct.calendar_get("evt_demo")["start"] == "2026-11-20T15:00"


def test_create_needs_start_and_end(friday):
    with pytest.raises(ValueError, match="needs both a start and an end time"):
        ct.calendar_create({"summary": "No end", "start": "2026-11-23T10:00"})


def test_create_fills_in_a_title_and_attendees_if_missing(friday):
    made = ct.calendar_create({"start": "2026-11-23T10:00", "end": "2026-11-23T10:30"})["after"]
    assert (made["summary"], made["attendees"]) == ("(no title)", [])


def test_finished_functions_never_say_placeholder():
    # Aryan's adapter switches to these functions only when this word is gone
    import inspect
    for fn in (ct.calendar_get, ct.calendar_list, ct.calendar_cancel, ct.calendar_update,
               ct.calendar_create):
        assert "PLACEHOLDER" not in inspect.getsource(fn)