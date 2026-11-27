"""OWNER: Visista. Tests for google_tools/security.py (the 5 security checks).
All on the fake calendar, in temp folders: no real Google, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from fake_data import fake_google
from fake_data.sample_data import ARYAN, FRIDAY_EVENTS, OWNER
from google_tools import calendar_tools as ct
from google_tools import email_tools, security
from google_tools.undo_fixes import restore_calendar
from shared.models import ActionRecord


@pytest.fixture(autouse=True)
def friday(tmp_path, monkeypatch):
    monkeypatch.delenv("USE_REAL_GOOGLE", raising=False)
    for name in ("CTRLZ_KNOWN_CONTACTS", "CTRLZ_OWNER_EMAIL", "CTRLZ_MAX_NEW_MEETINGS",
                 "CTRLZ_MAX_CHANGES", "CTRLZ_CHECK_LINKS_ONLINE", "CTRLZ_ALLOW_LOCAL_LINKS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fake_google, "STATE_PATH", tmp_path / "fake_google.json")
    fake_google.reset(FRIDAY_EVENTS)


def _alerts_emailed():
    fake_google._load()
    return [m for m in fake_google.SENT_EMAILS if m["subject"].startswith("Agent-Ctrl-Z security alert")]


def _meeting(i, day=23, hour=None, attendees=(OWNER,), **extra):
    h = hour if hour is not None else 9 + i
    return {"summary": f"Meeting {i}", "start": f"2026-11-{day}T{h:02d}:00",
            "end": f"2026-11-{day}T{h:02d}:30", "attendees": list(attendees), **extra}


def test_security_data_stays_next_to_the_fake_calendar(tmp_path):
    assert security.db_path() == tmp_path / "security.db"


# ---------- 1. unknown guests: allowed, owner alerted ----------
def test_unknown_guest_alerts_the_owner_but_the_meeting_is_still_made(monkeypatch):
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", ARYAN)
    made = ct.calendar_create(_meeting(1, attendees=[OWNER, ARYAN, "stranger@evil.com"]))["after"]
    assert ct.calendar_get(made["id"]) is not None
    [alert] = _alerts_emailed()
    assert alert["to"] == [OWNER] and "unknown guests" in alert["subject"]
    assert "stranger@evil.com" in alert["body"] and f"  - {ARYAN}" not in alert["body"]
    assert security.recent_alerts()[0]["kind"] == "unknown_guests"


def test_known_guests_and_the_owner_raise_no_alert(monkeypatch):
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", f"{ARYAN}, someone@gmail.com")
    ct.calendar_create(_meeting(1, attendees=[OWNER, ARYAN]))
    assert _alerts_emailed() == []


def test_without_a_contacts_list_the_alert_explains_how_to_set_one():
    ct.calendar_create(_meeting(1, attendees=[OWNER, ARYAN]))
    assert "Set CTRLZ_KNOWN_CONTACTS" in _alerts_emailed()[0]["body"]


def test_adding_an_unknown_guest_later_alerts_about_that_guest_only(monkeypatch):
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", ARYAN)
    ct.calendar_update("evt_1on1", {"attendees": [OWNER, ARYAN, "new.person@evil.com"]})
    [alert] = _alerts_emailed()
    assert "new.person@evil.com" in alert["body"]


# ---------- 2. too many new meetings in 30 minutes ----------
def test_sixth_new_meeting_in_30_minutes_alerts_with_everything_to_review():
    for i in range(5):
        ct.calendar_create(_meeting(i, description=f"notes {i}"))
    assert _alerts_emailed() == []
    ct.calendar_create(_meeting(5, description="notes 5"))
    [alert] = _alerts_emailed()
    assert alert["subject"] == "Agent-Ctrl-Z security alert: 6 meetings created in 30 minutes"
    for i in range(6):
        assert f"'Meeting {i}'" in alert["body"] and f"notes {i}" in alert["body"]
    assert f"guests: {OWNER}" in alert["body"]


def test_the_same_burst_is_reported_only_once():
    for i in range(8):
        ct.calendar_create(_meeting(i))
    assert len(_alerts_emailed()) == 1


def test_meetings_created_more_than_30_minutes_apart_dont_add_up(monkeypatch):
    for i in range(5):
        ct.calendar_create(_meeting(i))
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    monkeypatch.setattr(security, "_now", lambda: later)
    ct.calendar_create(_meeting(5))
    assert _alerts_emailed() == []


def test_the_limit_is_a_setting(monkeypatch):
    monkeypatch.setenv("CTRLZ_MAX_NEW_MEETINGS", "10")
    for i in range(8):
        ct.calendar_create(_meeting(i))
    assert _alerts_emailed() == []


# ---------- 3. too many changes to one meeting in 30 minutes ----------
def test_seventh_change_to_one_meeting_is_reported():
    for minute in range(6):
        ct.calendar_update("evt_demo", {"description": f"edit {minute}"})
    assert _alerts_emailed() == []
    ct.calendar_update("evt_demo", {"description": "edit 6"})
    [alert] = _alerts_emailed()
    assert "'CS 598 demo prep' changed 7 times in 30 minutes" in alert["subject"]
    assert alert["body"].count(": update") == 7 and "tampering" in alert["body"]


def test_cancels_and_restores_count_as_changes():
    for i in range(4):
        ct.calendar_update("evt_demo", {"description": f"edit {i}"})
    result = ct.calendar_cancel("evt_demo")                        # 5
    restore_calendar(ActionRecord(tool="calendar.cancel_event", target_id="evt_demo",
                                  before=result["before"], after=None,
                                  affected_people=[ARYAN], instruction="x"))   # 6
    assert _alerts_emailed() == []
    ct.calendar_update("evt_demo", {"description": "one more"})   # 7
    assert len(_alerts_emailed()) == 1


def test_changes_to_different_meetings_dont_add_up():
    for i in range(4):
        ct.calendar_update("evt_demo", {"description": f"a{i}"})
        ct.calendar_update("evt_1on1", {"description": f"b{i}"})
    assert _alerts_emailed() == []


# ---------- alerts never get in the way ----------
def test_a_failing_alert_email_never_blocks_the_calendar(monkeypatch):
    def gmail_down(*args, **kwargs):
        raise RuntimeError("Gmail is down")
    monkeypatch.setattr(email_tools, "email_send", gmail_down)
    made = ct.calendar_create(_meeting(1, attendees=[OWNER, "stranger@evil.com"]))["after"]
    assert ct.calendar_get(made["id"]) is not None
    assert security.recent_alerts()[0]["kind"] == "unknown_guests"     # still recorded


def test_a_broken_security_log_never_blocks_the_calendar(monkeypatch):
    def broken():
        raise RuntimeError("disk full")
    monkeypatch.setattr(security, "_db", broken)
    assert ct.calendar_create(_meeting(1))["after"]["id"]


# ---------- 4. no collisions, 15-minute gap (agent's bookings only) ----------
def test_without_the_check_overlaps_are_allowed_so_group_repair_is_unaffected():
    made = ct.calendar_create({"summary": "Overlap", "start": "2026-11-20T15:30",
                               "end": "2026-11-20T16:00"})["after"]
    assert ct.calendar_get(made["id"]) is not None


def test_agent_booking_on_top_of_a_meeting_is_refused_with_the_next_free_time():
    before = len(ct.calendar_list("2026-11-20T00:00", "2026-11-21T00:00"))
    with pytest.raises(ValueError) as refused:
        ct.calendar_create({"summary": "Sync", "start": "2026-11-20T15:30",
                            "end": "2026-11-20T16:00"}, check_schedule=True)
    message = str(refused.value)
    assert "collides with 'CS 598 demo prep' (Fri Nov 20, 3:00 PM-4:00 PM)" in message
    assert "next free time is Fri Nov 20, 4:15 PM-4:45 PM" in message      # 4:00 + 15 min gap
    assert "(start 2026-11-20T16:15, end 2026-11-20T16:45)" in message
    assert len(ct.calendar_list("2026-11-20T00:00", "2026-11-21T00:00")) == before


def test_exactly_15_minutes_after_a_meeting_is_fine_10_is_not():
    ct.calendar_create({"summary": "OK", "start": "2026-11-20T16:15",
                        "end": "2026-11-20T16:45"}, check_schedule=True)
    with pytest.raises(ValueError, match="less than 15 minutes"):
        ct.calendar_create({"summary": "Too close", "start": "2026-11-20T13:00",
                            "end": "2026-11-20T14:50"}, check_schedule=True)


def test_agent_move_into_another_meeting_is_refused_but_moving_within_its_own_slot_is_fine():
    with pytest.raises(ValueError, match="collides with 'CS 598 demo prep'"):
        ct.calendar_update("evt_1on1", {"start": "2026-11-20T15:15"}, check_schedule=True)
    after = ct.calendar_update("evt_demo", {"start": "2026-11-20T15:10"}, check_schedule=True)["after"]
    assert after["start"] == "2026-11-20T15:10"


def test_the_gap_is_a_setting(monkeypatch):
    monkeypatch.setenv("CTRLZ_MEETING_GAP_MINUTES", "0")
    ct.calendar_create({"summary": "Right after", "start": "2026-11-20T16:00",
                        "end": "2026-11-20T16:30"}, check_schedule=True)


# ---------- 5. links ----------
@pytest.mark.parametrize("bad, reason", [
    ("javascript:alert(1)", "can run code"),
    ("ftp://files.example.org/x", "only http and https"),
    ("https://nodot/page", "Not a valid link"),
])
def test_bad_links_in_a_meeting_are_refused(bad, reason):
    with pytest.raises(ValueError, match=reason):
        ct.calendar_create({**_meeting(1), "location": bad})


def test_links_are_found_without_trailing_punctuation():
    assert security.find_links("See https://github.com/x/y. Or (https://a.org/b).") == \
        ["https://github.com/x/y", "https://a.org/b"]


def test_local_links_are_fine_on_the_fake_calendar():
    email_tools.email_send([ARYAN], "Receipt", "Flag it: http://localhost:8501/flag?a=1",
                           hold_seconds=0)


def _real_rules(monkeypatch):
    monkeypatch.setattr(security, "using_real_google", lambda: True)
    monkeypatch.setenv("CTRLZ_OWNER_EMAIL", OWNER)
    monkeypatch.setenv("CTRLZ_CHECK_LINKS_ONLINE", "0")


def test_on_real_google_links_that_only_work_on_this_computer_are_refused(monkeypatch):
    _real_rules(monkeypatch)
    with pytest.raises(ValueError, match=f"only works on this computer, so {ARYAN} can't open it"):
        email_tools.email_send([ARYAN], "Receipt", "Flag it: http://localhost:8501/flag?a=1",
                               hold_seconds=0)
    with pytest.raises(ValueError, match="only works on this computer"):
        security.check_links(["http://192.168.1.5/x"], [ARYAN])
    security.check_links(["http://localhost:8501/x"], [OWNER])        # only the owner: fine
    monkeypatch.setenv("CTRLZ_ALLOW_LOCAL_LINKS", "1")
    security.check_links(["http://localhost:8501/x"], [ARYAN])        # rehearsal mode


def test_links_that_dont_open_are_refused(monkeypatch):
    monkeypatch.setenv("CTRLZ_CHECK_LINKS_ONLINE", "1")
    answers = {"https://github.com/ok": 200, "https://example.org/gone": 404,
               "https://down.example.org/": None}
    monkeypatch.setattr(security, "_answers", lambda url: answers[url])
    security.check_links(["https://github.com/ok"], [ARYAN])
    with pytest.raises(ValueError, match=r"doesn't open \(error 404\)"):
        security.check_links(["https://example.org/gone"], [ARYAN])
    with pytest.raises(ValueError, match=r"doesn't open \(no answer\)"):
        security.check_links(["https://down.example.org/"], [ARYAN])


def test_an_email_with_a_bad_link_is_not_sent_or_held():
    with pytest.raises(ValueError):
        email_tools.email_send([ARYAN], "Hi", "click javascript:steal()")
    fake_google._load()
    assert fake_google.SENT_EMAILS == []