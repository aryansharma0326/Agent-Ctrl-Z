"""OWNER: Aryan. Tests for the calendar blast radius (repair/blast_radius.py) and its
owner-dashboard helpers. Pure functions: no network, no database, fixed clock.

(Visista's email blast-radius tests live in tests/test_blast_radius.py.)"""
from datetime import datetime, timezone

import pytest

from fake_data.sample_data import ARYAN, CLASS_LIST, OWNER, VISISTA, event
from google_tools import blast_radius as email_blast_radius
from repair.blast_radius import (ACT_NOW, ASK_OWNER, HOLD_BRIEFLY, BlastRadiusResult,
                                 calendar_factors, score_calendar_action, tier_for)
from repair.dashboard_helpers import action_blast_radius, calendar_action_type
from shared.models import ActionRecord

NOW = "2026-11-20T09:00"                  # Friday 9 AM, New York time
FAR = ("2026-11-27T15:00", "2026-11-27T16:00")   # a week out: no imminence points
STRANGERS = ["prof@university.edu", "ta@university.edu", "recruiter@company.com"]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name in ("USE_REAL_GOOGLE", "CTRLZ_BLAST_LOW_MAX", "CTRLZ_BLAST_HIGH_MIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTRLZ_OWNER_EMAIL", OWNER)
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", f"{ARYAN},{VISISTA}")


def ev(attendees, start=FAR[0], end=FAR[1], **extra):
    e = event("evt_x", "Meeting", start, end, attendees)
    e.update(extra)
    return e


def points(result_or_event, action="cancel", now=NOW):
    return {name: pts for name, pts, _ in calendar_factors(result_or_event, action, now)}


# =================================================================== solo events

def test_solo_event_far_away_scores_zero_and_acts_now():
    r = score_calendar_action(ev([OWNER]), "cancel", NOW)
    assert (r.score, r.tier, r.hold_seconds) == (0, ACT_NOW, 0)
    assert not r.needs_approval
    assert r.reasons == ["only you are on this event, so nobody else is affected"]


def test_event_with_no_guest_list_counts_as_solo():
    r = score_calendar_action({"summary": "Focus time", "start": FAR[0]}, "create", NOW)
    assert (r.score, r.tier) == (0, ACT_NOW)


def test_solo_event_acts_now_even_when_imminent_and_recurring():
    solo = ev([OWNER], "2026-11-20T09:30", "2026-11-20T10:00",
              recurrence=["RRULE:FREQ=WEEKLY"])
    r = score_calendar_action(solo, "move", NOW)
    assert r.score == 5                     # +3 imminent, +2 recurring
    assert (r.tier, r.hold_seconds) == (ACT_NOW, 0)


def test_owners_own_acceptance_does_not_count():
    solo = ev([{"email": OWNER, "responseStatus": "accepted"}])
    assert points(solo)["accepted"] == 0


# =================================================================== attendees

@pytest.mark.parametrize("n, expected", [(1, 0), (2, 1), (5, 1), (6, 2), (20, 2), (21, 3),
                                         (33, 3)])
def test_attendee_count_bands(n, expected):
    people = [OWNER] + [f"p{i}@test.com" for i in range(n - 1)]
    assert points(ev(people))["attendees"] == expected


def test_duplicate_attendees_and_rooms_are_not_counted():
    e = ev([OWNER, ARYAN, ARYAN.upper(),
            {"email": "room-4b@resource.calendar.google.com", "resource": True}])
    assert points(e)["attendees"] == 1      # owner + Aryan = 2 people


# =================================================================== external attendees

def test_multi_attendee_external_meeting_asks_owner():
    r = score_calendar_action(ev([OWNER, ARYAN] + STRANGERS), "cancel", NOW)
    assert r.score == 2 * 3 + 1             # 3 outsiders (+6), 5 attendees (+1)
    assert (r.tier, r.hold_seconds) == (ASK_OWNER, None)
    assert r.needs_approval
    assert "3 attendees outside your domain and contacts (+6)" in r.reasons
    assert "5 attendees (+1)" in r.reasons


def test_one_outsider_is_two_points():
    assert points(ev([OWNER, "guest@other.org"]))["external"] == 2


def test_same_domain_and_known_contacts_are_not_external(monkeypatch):
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", "friend@gmail.com")
    # test.com is the owner's domain; friend@gmail.com is a known contact
    e = ev([OWNER, "student1@test.com", "friend@gmail.com"])
    assert points(e)["external"] == 0


def test_sharing_a_public_email_domain_does_not_make_someone_internal():
    e = ev(["me@gmail.com", "stranger@gmail.com"])
    parts = {n: p for n, p, _ in calendar_factors(e, "edit", NOW, owner="me@gmail.com",
                                                   known=set())}
    assert parts["external"] == 2


def test_whole_class_is_held_briefly_until_outsiders_join():
    r = score_calendar_action(ev(CLASS_LIST), "cancel", NOW)
    assert r.score == 3 and r.tier == HOLD_BRIEFLY    # 33 people, all @test.com
    outsiders = ev(CLASS_LIST + STRANGERS)
    assert score_calendar_action(outsiders, "cancel", NOW).tier == ASK_OWNER


# =================================================================== imminence

@pytest.mark.parametrize("start, end, expected, reason", [
    ("2026-11-20T10:30", "2026-11-20T11:00", 3, "starts in 1 hour (within 2 hours)"),
    ("2026-11-20T11:00", "2026-11-20T12:00", 3, "starts in 2 hours (within 2 hours)"),
    ("2026-11-20T11:01", "2026-11-20T12:00", 2, "starts in 2 hours (within 24 hours)"),
    ("2026-11-21T09:00", "2026-11-21T10:00", 2, "starts in 24 hours (within 24 hours)"),
    ("2026-11-21T09:01", "2026-11-21T10:00", 0, "starts in 1 day"),
    ("2026-11-20T08:30", "2026-11-20T09:30", 3, "in progress now"),
    ("2026-11-19T15:00", "2026-11-19T16:00", 0, "already over"),
])
def test_imminence_bands(start, end, expected, reason):
    parts = {n: (p, why) for n, p, why in calendar_factors(ev([OWNER, ARYAN], start, end),
                                                             "cancel", NOW)}
    assert parts["imminence"] == (expected, reason)


def test_imminent_team_meeting_is_held_briefly():
    r = score_calendar_action(ev([OWNER, ARYAN, VISISTA], "2026-11-20T10:00",
                                 "2026-11-20T11:00"), "cancel", NOW)
    assert (r.score, r.tier, r.hold_seconds) == (4, HOLD_BRIEFLY, 60)
    assert "starts in 1 hour (within 2 hours) (+3)" in r.reasons


def test_imminence_uses_real_timezones():
    # 15:30 UTC is 10:30 in New York (EST), 1.5 hours after NOW
    google = ev([OWNER, ARYAN], {"dateTime": "2026-11-20T15:30:00Z"},
                {"dateTime": "2026-11-20T16:00:00Z"})
    assert points(google)["imminence"] == 3
    # the same instant passed as an aware `now`
    utc_now = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
    assert points(google, now=utc_now)["imminence"] == 3


def test_all_day_events_and_missing_times():
    tomorrow = ev([OWNER, ARYAN], {"date": "2026-11-21"}, {"date": "2026-11-22"})
    assert points(tomorrow)["imminence"] == 2           # midnight is 15 hours away
    assert points({"attendees": [OWNER, ARYAN]})["imminence"] == 0
    assert points(ev([OWNER, ARYAN], "not a time", "nope"))["imminence"] == 0


# =================================================================== recurring series

def test_recurring_series_adds_two():
    e = ev([OWNER, ARYAN], recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"])
    r = score_calendar_action(e, "move", NOW)
    assert "recurring series (+2)" in r.reasons
    assert r.score == 3 and r.tier == HOLD_BRIEFLY


def test_one_occurrence_of_a_series_also_counts():
    e = ev([OWNER, ARYAN], recurringEventId="series_123")
    r = score_calendar_action(e, "edit", NOW)
    assert "occurrence of a recurring series (+2)" in r.reasons


def test_one_off_event_adds_nothing():
    assert points(ev([OWNER, ARYAN], recurrence=[]))["recurring"] == 0


# =================================================================== accepted RSVPs

def test_accepted_rsvp_in_fake_calendar_shape():
    e = ev([OWNER, ARYAN, VISISTA], responses={ARYAN: "accepted", VISISTA: "declined"})
    r = score_calendar_action(e, "cancel", NOW)
    assert "1 person already accepted (+2)" in r.reasons
    assert r.score == 3 and r.tier == HOLD_BRIEFLY


def test_accepted_rsvp_in_google_shape():
    e = ev([{"email": OWNER, "responseStatus": "accepted"},
            {"email": ARYAN, "responseStatus": "accepted"},
            {"email": VISISTA, "responseStatus": "accepted"}])
    parts = {n: (p, why) for n, p, why in calendar_factors(e, "cancel", NOW)}
    assert parts["accepted"] == (2, "2 people already accepted")


@pytest.mark.parametrize("status", ["needsAction", "tentative", "declined"])
def test_other_responses_do_not_count_as_accepted(status):
    assert points(ev([OWNER, ARYAN], responses={ARYAN: status}))["accepted"] == 0


# =================================================================== tiers and the result

def test_every_factor_together():
    e = ev([{"email": OWNER, "responseStatus": "accepted"},
            {"email": ARYAN, "responseStatus": "accepted"}]
           + [{"email": s, "responseStatus": "needsAction"} for s in STRANGERS],
           "2026-11-20T10:00", "2026-11-20T11:00", recurringEventId="r1")
    r = score_calendar_action(e, "move", NOW)
    assert r.score == 6 + 1 + 3 + 2 + 2
    assert r.tier == ASK_OWNER and r.hold_seconds is None
    assert len(r.reasons) == 5 and all(x.endswith(")") for x in r.reasons)
    assert r.explain().startswith("blast radius 14 (ask_owner): 3 attendees outside")


@pytest.mark.parametrize("score, tier", [(0, ACT_NOW), (2, ACT_NOW), (3, HOLD_BRIEFLY),
                                         (5, HOLD_BRIEFLY), (6, ASK_OWNER), (40, ASK_OWNER)])
def test_tier_cut_offs_match_the_email_holding_pen(score, tier):
    assert tier_for(score) == tier
    email_tier = email_blast_radius.tier_for(score)
    assert {"low": ACT_NOW, "medium": HOLD_BRIEFLY, "high": ASK_OWNER}[email_tier] == tier


def test_tier_settings_are_shared_with_the_email_scorer(monkeypatch):
    monkeypatch.setenv("CTRLZ_BLAST_HIGH_MIN", "4")
    r = score_calendar_action(ev([OWNER, ARYAN], "2026-11-20T10:00", "2026-11-20T11:00"),
                              "cancel", NOW)
    assert (r.score, r.tier) == (4, ASK_OWNER)


def test_result_is_a_plain_dataclass():
    r = score_calendar_action(ev([OWNER, ARYAN]), "create", NOW)
    assert isinstance(r, BlastRadiusResult)
    assert r.to_dict() == {"score": 1, "tier": ACT_NOW, "hold_seconds": 0,
                           "needs_approval": False, "reasons": ["2 attendees (+1)"]}


def test_score_is_deterministic_and_does_not_change_the_event():
    e = ev([OWNER, ARYAN] + STRANGERS, "2026-11-20T10:00", "2026-11-20T11:00",
           responses={ARYAN: "accepted"})
    snapshot = repr(e)
    assert all(score_calendar_action(e, "cancel", NOW) == score_calendar_action(e, "cancel", NOW)
               for _ in range(5))
    assert repr(e) == snapshot


@pytest.mark.parametrize("action", ["cancel", "move", "edit", "create"])
def test_all_four_action_types_are_scored(action):
    assert score_calendar_action(ev([OWNER, ARYAN]), action, NOW).score == 1


def test_unknown_action_type_is_rejected():
    with pytest.raises(ValueError, match="action_type"):
        score_calendar_action(ev([OWNER]), "delete", NOW)


# =================================================================== dashboard helpers

def record(tool, before=None, after=None, timestamp="2026-11-20T14:00:00+00:00"):
    return ActionRecord(tool=tool, target_id="evt_x", before=before, after=after,
                        affected_people=[ARYAN], instruction="Clear my Friday",
                        timestamp=timestamp)


def test_action_type_from_the_agents_tool():
    meeting = ev([OWNER, ARYAN])
    moved = {**meeting, "start": "2026-11-28T15:00", "end": "2026-11-28T16:00"}
    renamed = {**meeting, "summary": "Renamed"}
    assert calendar_action_type(record("calendar.cancel_event", meeting)) == "cancel"
    assert calendar_action_type(record("calendar.create_event", None, meeting)) == "create"
    assert calendar_action_type(record("calendar.update_event", meeting, moved)) == "move"
    assert calendar_action_type(record("calendar.update_event", meeting, renamed)) == "edit"
    assert calendar_action_type(record("email.send", None, {"to": [ARYAN]})) is None


def test_dashboard_scores_the_original_action_as_of_when_it_happened():
    # agent cancelled a 10 AM team meeting at 9 AM New York time (14:00 UTC)
    meeting = ev([OWNER, ARYAN, VISISTA], "2026-11-20T10:00", "2026-11-20T11:00")
    r = action_blast_radius(record("calendar.cancel_event", meeting))
    assert (r.score, r.tier) == (4, HOLD_BRIEFLY)
    # the same cancel done a week earlier was low risk
    early = action_blast_radius(record("calendar.cancel_event", meeting,
                                       timestamp="2026-11-13T14:00:00+00:00"))
    assert (early.score, early.tier) == (1, ACT_NOW)


def test_dashboard_scores_a_create_by_the_new_event():
    new = ev([OWNER] + STRANGERS)
    assert action_blast_radius(record("calendar.create_event", None, new)).tier == ASK_OWNER


def test_dashboard_skips_emails_and_actions_without_an_event():
    assert action_blast_radius(record("email.send", None, {"to": CLASS_LIST})) is None
    assert action_blast_radius(record("calendar.cancel_event", None, None)) is None
