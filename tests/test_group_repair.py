"""Tests for minimal-disruption group repair and the owner dashboard.  Run:  pytest

Every test runs against a throwaway fake calendar + undo.db in tmp_path.
"""
from __future__ import annotations



import pytest

from fake_data import fake_google as g
from fake_data.sample_data import ARYAN, OWNER, SCENARIOS, TEAM, VISISTA, event
from fake_data.seed import seed
from repair import calendar_adapter
from repair.dashboard_helpers import (dismiss_flags, escape_markdown,
                                      load_items, metrics, preview_state, sanitize_untrusted)
from repair.group_repair import (CASE_A, CASE_B, CASE_C, CASE_D, apply_plan, group_repair,
                                 plan_group_repair)
from repair.timeutil import overlaps, parse_time
from shared import db
from shared.models import ActionRecord, Flag, UndoResult

INJECTION = SCENARIOS[5]["flag"]["reason"]


@pytest.fixture(autouse=True)
def fake_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "STATE_PATH", tmp_path / "fake_google.json")
    monkeypatch.setenv("CTRLZ_CALENDAR_BACKEND", "fake")
    g.reset([])


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "undo.db")


@pytest.fixture
def seeded(conn):
    """{scenario key: ActionRecord} after loading all six demo stories."""
    return {s["key"]: r for s, r in zip(SCENARIOS, seed(conn))}


def events_named(summary):
    return [e for e in g.list_events() if e["summary"] == summary]


def cancel(ev, affected, instruction="Clear my day"):
    """Put `ev` on the fake calendar, then have the 'agent' cancel it."""
    g.add_event_as_is(ev)
    before = g.get_event(ev["id"])
    g.delete_event(ev["id"])
    return ActionRecord(tool="calendar.cancel_event", target_id=ev["id"], before=before,
                        after=None, affected_people=affected, instruction=instruction)


def move(ev, start, end, affected, instruction="Push my meeting"):
    g.add_event_as_is(ev)
    before = g.get_event(ev["id"])
    after = g.update_event(ev["id"], {"start": start, "end": end})
    return ActionRecord(tool="calendar.update_event", target_id=ev["id"], before=before,
                        after=after, affected_people=affected, instruction=instruction)


def assert_no_double_booking(person):
    mine = [e for e in g.list_events() if person in e["attendees"]]
    for i, a in enumerate(mine):
        for b in mine[i + 1:]:
            assert not overlaps(parse_time(a["start"]), parse_time(a["end"]),
                                parse_time(b["start"]), parse_time(b["end"])), (a, b)


# =================================================================== demo stories 1-6

def test_s1_clear_friday_exact_restore(seeded):
    r = group_repair(seeded["clear_friday"], {})
    assert isinstance(r, UndoResult) and r.fix_type == "group_repair"
    assert r.success and r.details.startswith("Exact restore")
    restored = g.get_event(r.new_target_id)
    original = seeded["clear_friday"].before
    assert restored["start"] == original["start"] and restored["end"] == original["end"]
    assert restored["summary"] == original["summary"]
    assert restored["attendees"] == original["attendees"]
    assert r.notify == [ARYAN, VISISTA]          # the owner isn't spammed


def test_s2_wrong_meeting_moved_is_moved_back(seeded):
    act = seeded["wrong_meeting_moved"]
    r = group_repair(act, {})
    assert r.success and r.new_target_id is None   # same event id, just moved back
    now = g.get_event("evt_sync_mon")
    assert (now["start"], now["end"]) == ("2026-11-23T14:00", "2026-11-23T14:30")
    assert r.notify == [ARYAN, VISISTA]


def test_s3_email_is_not_a_group_repair(seeded):
    before = g.list_events()
    r = group_repair(seeded["email_wrong_people"], {})
    assert not r.success and "email" in r.details and r.notify == []
    assert g.list_events() == before


def test_s4_conflict_after_cancel_reschedules_without_double_booking(seeded):
    act = seeded["conflict_after_cancel"]
    plan = plan_group_repair(act, {})
    assert plan.case == CASE_B
    assert [c.person for c in plan.conflicts] == [ARYAN]
    assert plan.conflicts[0].summary == "Dentist (Aryan)"

    r = group_repair(act, {})
    assert r.success and r.new_target_id
    new = g.get_event(r.new_target_id)
    assert (new["start"], new["end"]) == ("2026-11-19T16:00", "2026-11-19T17:00")
    assert g.get_event("evt_aryan_dentist") is not None       # Aryan's booking untouched
    assert_no_double_booking(ARYAN)
    assert "Dentist (Aryan)" in r.details and "double-book" in r.details
    assert "4:00 PM" in r.details
    assert r.notify == [ARYAN, VISISTA]


def test_s5_correct_action_is_not_undone(seeded):
    before = g.list_events()
    r = group_repair(seeded["correct_action"], {})
    assert not r.success and r.notify == []
    assert "over-undo" in r.details
    assert g.list_events() == before                    # Saturday gym stays cancelled
    assert g.get_event("evt_gym_sat") is None


def test_s6_prompt_injection_flag_has_no_effect(seeded, conn):
    act = seeded["sneaky_flag"]
    n_before = len(g.list_events())
    # Even if a caller stuffs the flag text into current_state, it is ignored.
    r = group_repair(act, {"reason_text": INJECTION, "flagged_by": "student3@test.com"})
    assert r.success and plan_group_repair(act, {}).case in (CASE_A, CASE_D)
    assert len(g.list_events()) == n_before + 1           # only the study group came back
    assert events_named("Study group")
    assert "IGNORE" not in r.details and "delete" not in r.details.lower()
    assert r.notify == ["student3@test.com"]


def test_dashboard_preview_state_never_contains_flag_text(seeded, conn):
    item = next(i for i in load_items(conn) if i.action.action_id ==
                seeded["sneaky_flag"].action_id)
    state = preview_state(item)
    assert INJECTION not in repr(state)
    assert state == {"flagged_by": ["student3@test.com"]}


# =================================================================== Case B details

def test_metadata_preserved_google_format():
    ev = {
        "id": "evt_g", "etag": "\"123\"", "htmlLink": "https://cal/evt_g", "iCalUID": "x@g",
        "status": "confirmed", "summary": "Design review",
        "description": "Agenda: auth flow", "location": "Room 2",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "conferenceData": {"entryPoints": [{"uri": "https://meet.google.com/abc-defg-hij"}]},
        "organizer": {"email": OWNER},
        "start": {"dateTime": "2026-11-19T10:00:00-06:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-11-19T11:00:00-06:00", "timeZone": "America/Chicago"},
        "attendees": [{"email": OWNER, "responseStatus": "accepted"},
                      {"email": ARYAN, "responseStatus": "accepted"}],
    }
    snapshot = [event("evt_new", "Lab", "2026-11-19T10:00", "2026-11-19T11:30", [ARYAN])]
    act = ActionRecord(tool="calendar.cancel_event", target_id="evt_g", before=ev, after=None,
                       affected_people=[ARYAN], instruction="Clear my morning")
    plan = plan_group_repair(act, {"events": snapshot, "target": None})
    assert plan.case == CASE_B and plan.operation == "create"
    p = plan.payload
    for k in ("summary", "description", "location", "hangoutLink", "conferenceData",
              "organizer", "attendees"):
        assert p[k] == ev[k], k
    for k in ("id", "etag", "htmlLink", "iCalUID", "status"):
        assert k not in p
    assert p["start"] == {"dateTime": "2026-11-19T11:30:00", "timeZone": "America/Chicago"}
    assert p["end"]["dateTime"] == "2026-11-19T12:30:00"
    assert plan.notify == [ARYAN]                   # organizer (owner) not notified


def test_reschedule_skips_to_next_weekday_when_day_is_full():
    # Friday 4-6pm cancelled; Aryan booked it and is busy until close of business.
    act = cancel(event("evt_fri", "Retro", "2026-11-20T16:00", "2026-11-20T18:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is(event("evt_busy", "Deadline crunch", "2026-11-20T15:00",
                            "2026-11-20T18:00", [ARYAN]))
    r = group_repair(act, {})
    new = g.get_event(r.new_target_id)
    assert r.success and new["start"] == "2026-11-23T09:00"      # Monday, not Saturday
    assert_no_double_booking(ARYAN)


def test_reschedule_respects_working_hours_and_every_attendee():
    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is(event("evt_a", "Aryan thing", "2026-11-19T15:00", "2026-11-19T16:30",
                            [ARYAN]))
    g.add_event_as_is(event("evt_v", "Visista thing", "2026-11-19T16:30", "2026-11-19T17:30",
                            [VISISTA]))
    r = group_repair(act, {})
    new = g.get_event(r.new_target_id)
    # 4:30 blocked for Visista, 5:30-6:30 runs past 6 PM -> next day 9 AM
    assert new["start"] == "2026-11-20T09:00"
    for p in TEAM:
        assert_no_double_booking(p)


def test_optional_and_declined_events_do_not_block():
    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is({"id": "evt_declined", "summary": "Optional talk",
                       "start": "2026-11-19T15:00", "end": "2026-11-19T16:00",
                       "attendees": [{"email": ARYAN, "responseStatus": "declined"}]})
    g.add_event_as_is({"id": "evt_free", "summary": "Focus (free)", "transparency": "transparent",
                       "start": "2026-11-19T15:00", "end": "2026-11-19T16:00",
                       "attendees": [VISISTA]})
    plan = plan_group_repair(act, {})
    assert plan.case == CASE_A and not plan.conflicts


def test_no_free_slot_changes_nothing():
    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is(event("evt_leave", "Vacation", "2026-11-19T00:00", "2026-12-31T00:00",
                            [ARYAN]))
    snapshot = g.list_events()
    r = group_repair(act, {})
    assert not r.success and r.notify == [] and r.new_target_id is None
    assert "Nothing was changed" in r.details
    assert g.list_events() == snapshot


def test_original_slot_in_the_past_is_not_reused():
    act = cancel(event("evt_x", "Planning", "2026-11-19T10:00", "2026-11-19T11:00", TEAM),
                 [ARYAN, VISISTA])
    plan = plan_group_repair(act, {"now": "2026-11-19T13:05"})
    assert plan.case == CASE_B and plan.payload["start"] == "2026-11-19T13:05"
    plan = plan_group_repair(act, {"now": "2026-11-19T13:00"})
    assert plan.payload["start"] == "2026-11-19T13:00"


# =================================================================== Case C

BIG_TEAM = TEAM + ["s1@test.com", "s2@test.com"]


def _moved_group_meeting():
    """Group sync moved from Mon 2pm to Tue 2pm. Since then, s1 booked Mon 2pm,
    and Visista has class on Tue 2pm (so she flags it)."""
    act = move(event("evt_grp", "Project sync", "2026-11-23T14:00", "2026-11-23T15:00",
                     BIG_TEAM), "2026-11-24T14:00", "2026-11-24T15:00",
               [p for p in BIG_TEAM if p != OWNER])
    g.add_event_as_is(event("evt_s1", "s1 office hours", "2026-11-23T14:00", "2026-11-23T15:00",
                            ["s1@test.com"]))
    g.add_event_as_is(event("evt_class", "Visista class", "2026-11-24T14:00",
                            "2026-11-24T15:15", [VISISTA]))
    return act


def test_case_c_majority_accepted_keeps_meeting_and_books_makeup():
    act = _moved_group_meeting()
    state = {"flagged_by": VISISTA,
             "responses": {ARYAN: "accepted", "s1@test.com": "accepted",
                           "s2@test.com": "accepted"}}
    r = group_repair(act, state)
    assert r.success and r.new_target_id
    group = g.get_event("evt_grp")
    assert group["start"] == "2026-11-24T14:00"        # group meeting left intact
    makeup = g.get_event(r.new_target_id)
    assert makeup["attendees"] == [OWNER, VISISTA]
    assert makeup["summary"] == "Makeup sync: Project sync"
    assert makeup["start"] == "2026-11-23T14:00" and makeup["end"] == "2026-11-23T14:30"
    assert r.notify == [VISISTA]                       # nobody else is pinged
    assert "3 of 4" in r.details and "makeup" in r.details.lower()
    # The repair adds no new clash: the makeup avoids its attendees' other events,
    # and nobody but Visista (whose clash the agent caused) is double-booked.
    ms, me = parse_time(makeup["start"]), parse_time(makeup["end"])
    for e in g.list_events():
        if e["id"] != makeup["id"] and set(e["attendees"]) & set(makeup["attendees"]):
            assert not overlaps(ms, me, parse_time(e["start"]), parse_time(e["end"])), e
    for p in BIG_TEAM:
        if p != VISISTA:
            assert_no_double_booking(p)


def test_case_c_reads_google_style_response_status():
    act = _moved_group_meeting()
    cur = g.get_event("evt_grp")
    cur["attendees"] = [{"email": p, "responseStatus": "accepted"} for p in BIG_TEAM
                        if p != VISISTA] + [{"email": VISISTA, "responseStatus": "declined"}]
    plan = plan_group_repair(act, {"target": cur})
    assert plan.case == CASE_C and plan.notify == [VISISTA]


def test_case_c_moves_back_when_zero_conflicts():
    act = move(event("evt_grp", "Project sync", "2026-11-23T14:00", "2026-11-23T15:00",
                     BIG_TEAM), "2026-11-24T14:00", "2026-11-24T15:00", BIG_TEAM[1:])
    r = group_repair(act, {"flagged_by": VISISTA,
                           "responses": {p: "accepted" for p in BIG_TEAM[1:]}})
    assert r.success and "zero conflicts" in r.details
    assert g.get_event("evt_grp")["start"] == "2026-11-23T14:00"


def test_case_c_everyone_can_make_new_time_is_a_no_op():
    act = _moved_group_meeting()
    g.delete_event("evt_class")
    plan = plan_group_repair(act, {"responses": {p: "accepted" for p in BIG_TEAM[1:]}})
    assert plan.case == CASE_D and plan.success and not plan.writes


def test_update_without_majority_and_blocked_slot_reschedules_in_place():
    act = _moved_group_meeting()
    r = group_repair(act, {"flagged_by": VISISTA})       # nobody has accepted yet
    assert r.success and r.new_target_id is None
    grp = g.get_event("evt_grp")
    assert grp["start"] == "2026-11-23T15:00"             # earliest slot everyone is free
    assert sorted(r.notify) == sorted(BIG_TEAM[1:])


# =================================================================== Case D / drift

def test_cancel_already_restored_is_a_no_op():
    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is(act.before)
    r = group_repair(act, {})
    assert r.success and r.notify == [] and "Already repaired" in r.details
    assert len(events_named("Planning")) == 1


def test_cancel_already_recreated_under_new_id_is_a_no_op():
    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    g.add_event_as_is(event("evt_manual", "Planning", "2026-11-19T17:00", "2026-11-19T18:00",
                            TEAM))
    r = group_repair(act, {})
    assert r.success and "evt_manual" in r.details
    assert len(events_named("Planning")) == 1


def test_update_moved_again_by_a_person_is_not_overwritten():
    act = move(event("evt_m", "Sync", "2026-11-23T10:00", "2026-11-23T11:00", TEAM),
               "2026-11-30T10:00", "2026-11-30T11:00", [ARYAN, VISISTA])
    g.update_event("evt_m", {"start": "2026-11-25T10:00", "end": "2026-11-25T11:00"})
    r = group_repair(act, {})
    assert not r.success and "moved again" in r.details
    assert g.get_event("evt_m")["start"] == "2026-11-25T10:00"


def test_update_already_back_and_deleted_target():
    act = move(event("evt_m", "Sync", "2026-11-23T10:00", "2026-11-23T11:00", TEAM),
               "2026-11-30T10:00", "2026-11-30T11:00", [ARYAN, VISISTA])
    g.update_event("evt_m", {"start": "2026-11-23T10:00", "end": "2026-11-23T11:00"})
    assert group_repair(act, {}).success
    g.delete_event("evt_m")
    r = group_repair(act, {})
    assert not r.success and "no longer exists" in r.details and not events_named("Sync")


def test_missing_before_snapshot_returns_clean_result():
    act = ActionRecord(tool="calendar.cancel_event", target_id="evt_demo", before=None,
                       after=None, affected_people=[], instruction="Clear my Friday")
    r = group_repair(act, {})
    assert isinstance(r, UndoResult) and r.action_id == act.action_id and not r.success


# =================================================================== plumbing

def test_plan_is_a_dry_run(seeded):
    snapshot = g.list_events()
    for act in seeded.values():
        plan_group_repair(act, {"flagged_by": ARYAN})
    assert g.list_events() == snapshot


def test_snapshot_state_needs_no_calendar_reads():
    class NoReads:
        name = "no_reads"
        def get(self, *_): raise AssertionError("should use snapshot")
        def list(self, *_): raise AssertionError("should use snapshot")

    before = event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM)
    act = ActionRecord(tool="calendar.cancel_event", target_id="evt_x", before=before,
                       after=None, affected_people=[ARYAN, VISISTA], instruction="Clear")
    plan = plan_group_repair(act, {"events": [event("evt_d", "Dentist", "2026-11-19T15:00",
                                                    "2026-11-19T16:00", [ARYAN])]},
                             calendar=NoReads())
    assert plan.case == CASE_B and plan.payload["start"] == "2026-11-19T16:00"


def test_failed_write_reports_failure_and_notifies_nobody():
    class Broken:
        name = "broken"
        def create(self, _): raise RuntimeError("API down")

    act = cancel(event("evt_x", "Planning", "2026-11-19T15:00", "2026-11-19T16:00", TEAM),
                 [ARYAN, VISISTA])
    r = apply_plan(plan_group_repair(act, {}), calendar=Broken())
    assert not r.success and r.notify == [] and "API down" in r.details


def test_adapter_falls_back_to_fake_while_google_tools_is_placeholder(monkeypatch):
    monkeypatch.setenv("CTRLZ_CALENDAR_BACKEND", "auto")
    # Pin the placeholder check so the test doesn't depend on whether
    # google_tools/calendar_tools.py is the stub or the real Calendar API.
    monkeypatch.setattr(calendar_adapter, "google_tools_is_placeholder", lambda: True)
    assert calendar_adapter.get_calendar().name == "fake_google"
    monkeypatch.setattr(calendar_adapter, "google_tools_is_placeholder", lambda: False)
    assert calendar_adapter.get_calendar().name == "google_tools"
    monkeypatch.setenv("CTRLZ_CALENDAR_BACKEND", "fake")
    assert calendar_adapter.get_calendar().name == "fake_google"


# =================================================================== dashboard helpers

def test_sanitize_untrusted_strips_hidden_characters_and_truncates():
    sneaky = "ok‮ evil​\x00\x1b[31m\nline2"
    out = sanitize_untrusted(sneaky)
    assert out == "ok evil[31m\nline2"
    assert sanitize_untrusted("x" * 5000).endswith("[truncated]")
    assert sanitize_untrusted(None) == ""


def test_escape_markdown_neutralises_links_html_and_latex():
    out = escape_markdown("[click](http://evil) <img src=x> $x$ **bold**")
    assert out == r"\[click\]\(http\://evil\) \<img src\=x\> \$x\$ \*\*bold\*\*"


def test_dashboard_metrics_and_dismiss(seeded, conn):
    m = metrics(load_items(conn))
    assert m == {"Total actions": 6, "Pending flags": 5, "Undone": 0, "Failed": 0}
    gym = seeded["correct_action"].action_id
    db.save_flag(conn, Flag(action_id=gym, flagged_by=OWNER, reason_text="Oops?"))
    db.set_action_status(conn, gym, "flagged")
    assert metrics(load_items(conn))["Pending flags"] == 6
    dismiss_flags(conn, gym)
    assert db.get_action(conn, gym).status == "done"
    assert metrics(load_items(conn))["Pending flags"] == 5
    assert g.get_event("evt_gym_sat") is None          # dismissing never undoes


def test_owner_dashboard_page_renders_injection_as_inert_text(tmp_path, monkeypatch, conn):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(db, "DEFAULT_DB", tmp_path / "undo.db")
    seed(conn)
    at = AppTest.from_file("../pages/2_owner_dashboard.py", default_timeout=30).run()
    assert not at.exception
    assert [m.value for m in at.metric] == ["6", "5", "0", "0"]
    # The injection text only ever appears inside a code (verbatim) block.
    assert any(INJECTION in c.value for c in at.code)
    assert not any("IGNORE ALL PREVIOUS" in m.value for m in at.markdown)

    dismiss = next(b for b in at.button if b.key.startswith("dismiss_"))
    dismiss.click().run()
    assert not at.exception
    assert [m.value for m in at.metric][1] == "4"
