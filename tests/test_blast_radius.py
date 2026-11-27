"""OWNER: Visista. Tests for the Adaptive Holding Pen (google_tools/blast_radius.py and the
approval flow in email_tools.py). Fake calendar, temp folders, no network."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from fake_data import fake_google
from fake_data.sample_data import ARYAN, CLASS_LIST, OWNER, VISISTA
from google_tools import approvals, blast_radius, email_tools
from google_tools.undo_fixes import undo_email
from shared.models import ActionRecord

STRANGERS = ["prof@university.edu", "ta@university.edu"]


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("USE_REAL_GOOGLE", raising=False)
    for name in ("CTRLZ_KNOWN_CONTACTS", "CTRLZ_OWNER_EMAIL", "CTRLZ_BLAST_LOW_MAX",
                 "CTRLZ_BLAST_HIGH_MIN", "CTRLZ_HOLD_LOW_SECONDS", "CTRLZ_EMAIL_HOLD_SECONDS",
                 "CTRLZ_APPROVAL_MINUTES", "CTRLZ_MAX_AGENT_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTRLZ_KNOWN_CONTACTS", f"{ARYAN},{VISISTA}")
    monkeypatch.setattr(fake_google, "STATE_PATH", tmp_path / "fake_google.json")
    fake_google.reset([])


def _later(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


def _sent():
    fake_google._load()
    return fake_google.SENT_EMAILS


def _agent_emails_sent():
    return [m for m in _sent() if not m["subject"].startswith("Agent-Ctrl-Z")]


def _action(after):
    return ActionRecord(tool="email.send", target_id=after["id"], before=None, after=after,
                        affected_people=list(after["to"]), instruction="test")


# ---------- the score ----------
def test_note_to_self_and_known_teammates_are_low():
    for to in ([OWNER], [ARYAN], [ARYAN, VISISTA, OWNER]):
        r = blast_radius.score_email(to, "Running late", "5 minutes late")
        assert r["tier"] == "low" and r["hold_seconds"] == 10


def test_two_strangers_are_medium():
    r = blast_radius.score_email(STRANGERS, "Question", "When is the deadline?")
    assert (r["score"], r["tier"], r["hold_seconds"]) == (5, "medium", 60)


def test_whole_class_is_high_and_needs_approval_with_reasons():
    r = blast_radius.score_email(CLASS_LIST, "Meeting cancelled", "No meeting today")
    assert (r["score"], r["tier"], r["needs_approval"], r["hold_seconds"]) == (9, "high", True, None)
    assert "30 recipients not in your contacts (+6)" in r["reasons"]
    assert "32 recipients (+3)" in r["reasons"]       # 33 addresses minus the owner
    assert blast_radius.explain(r).startswith("blast radius 9 (high): 30 recipients not in")


def test_links_and_sensitive_words_add_points():
    r = blast_radius.score_email([ARYAN], "Login", "My password is hunter2, see https://github.com/x")
    assert r["points"]["links"] == 1 and r["points"]["sensitive"] == 2
    assert "mentions password (+2)" in r["reasons"]


def test_owner_and_duplicate_addresses_are_not_counted():
    r = blast_radius.score_email([OWNER, "prof@university.edu", "PROF@university.edu"], "s", "b")
    assert r["points"]["unknown"] == 2 and r["points"]["recipients"] == 0


def test_a_burst_of_agent_emails_raises_the_score():
    for i in range(3):
        email_tools.email_send([ARYAN], f"Update {i}", "b")
    r = blast_radius.score_email([ARYAN], "Update 3", "b")
    assert r["points"]["burst"] == 2 and "3 other agent emails in the last 30 minutes (+2)" in r["reasons"]


def test_thresholds_and_factors_can_be_changed_for_the_evaluation(monkeypatch):
    assert blast_radius.score_email(CLASS_LIST, "s", "b", drop=("unknown",))["tier"] == "medium"
    monkeypatch.setenv("CTRLZ_BLAST_HIGH_MIN", "10")
    assert blast_radius.score_email(CLASS_LIST, "s", "b")["tier"] == "medium"


# ---------- the holding pen ----------
def test_low_email_is_held_10_seconds_and_its_score_is_recorded():
    after = email_tools.email_send([ARYAN], "Running late", "5 min")["after"]
    assert after["status"] == "held" and after["blast_radius"]["tier"] == "low"
    wait = datetime.fromisoformat(after["send_at"]) - datetime.now(timezone.utc)
    assert timedelta(seconds=5) < wait <= timedelta(seconds=10)
    assert blast_radius.recent_scores()[0]["email_id"] == after["id"]
    assert email_tools.release_held_emails(now=_later(seconds=11))[0]["id"] == after["id"]


def test_medium_email_is_held_60_seconds():
    after = email_tools.email_send(STRANGERS, "Question", "When is it due?")["after"]
    assert after["blast_radius"]["tier"] == "medium"
    assert email_tools.release_held_emails(now=_later(seconds=30)) == []
    assert len(email_tools.release_held_emails(now=_later(seconds=61))) == 1


def test_high_email_waits_for_the_owner_and_the_owner_is_asked():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "No meeting today")["after"]
    assert after["status"] == "awaiting_approval" and "send_at" not in after
    assert email_tools.release_held_emails(now=_later(minutes=29)) == []
    assert _agent_emails_sent() == []
    [ask] = [m for m in _sent() if m["subject"].startswith("Agent-Ctrl-Z: approve this email?")]
    assert ask["to"] == [OWNER]
    assert f"python -m google_tools.approvals approve {after['id']}" in ask["body"]
    assert "blast radius 9 (high)" in ask["body"]


def test_owner_approves_and_it_goes_out_once():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    sent = email_tools.approve_email(after["id"])
    assert sent["to"] == CLASS_LIST and email_tools.email_status(after["id"]) == "sent"
    assert len(_agent_emails_sent()) == 1
    with pytest.raises(ValueError, match="isn't waiting for approval"):
        email_tools.approve_email(after["id"])


def test_owner_denies_and_it_never_goes_out():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    assert email_tools.deny_email(after["id"])
    email_tools.release_held_emails(now=_later(hours=2))
    assert _agent_emails_sent() == [] and email_tools.email_status(after["id"]) == "stopped"


def test_unanswered_approval_expires_unsent():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    email_tools.release_held_emails(now=_later(minutes=31))
    assert email_tools.email_status(after["id"]) == "expired" and _agent_emails_sent() == []
    assert email_tools.pending_approvals() == []


def test_approving_after_the_deadline_is_refused():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    with pytest.raises(ValueError, match="Too late"):
        email_tools.approve_email(after["id"], now=_later(minutes=31))
    assert _agent_emails_sent() == []


def test_pending_list_shows_what_the_owner_needs():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "No meeting")["after"]
    [p] = email_tools.pending_approvals()
    assert (p["id"], p["subject"], len(p["to"])) == (after["id"], "Meeting cancelled", 33)
    assert p["blast_radius"]["tier"] == "high"


def test_receipts_and_notices_are_never_scored_or_held():
    after = email_tools.email_send(CLASS_LIST, "Receipt", "b", hold_seconds=0)["after"]
    assert after["status"] == "sent" and "blast_radius" not in after
    assert blast_radius.recent_scores() == []


def test_a_broken_score_falls_back_to_the_normal_hold(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("oops")
    monkeypatch.setattr(blast_radius, "score_email", broken)
    after = email_tools.email_send(CLASS_LIST, "s", "b")["after"]
    assert after["status"] == "held" and "couldn't score it" in after["blast_radius"]["reasons"][0]


def test_an_older_outbox_file_is_upgraded(tmp_path):
    conn = sqlite3.connect(tmp_path / "outbox.db")
    conn.execute("CREATE TABLE held_emails (id TEXT PRIMARY KEY, to_json TEXT, subject TEXT, "
                 "body TEXT, send_at TEXT, status TEXT, message_json TEXT)")
    conn.commit()
    conn.close()
    assert email_tools.email_send(CLASS_LIST, "s", "b")["after"]["status"] == "awaiting_approval"


# ---------- undo ----------
def test_undo_while_waiting_for_approval_stops_it():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    r = undo_email(_action(after))
    assert r.success and r.fix_type == "email_unsent" and "waiting for your approval" in r.details
    assert email_tools.pending_approvals() == [] and _agent_emails_sent() == []


def test_undo_after_it_expired_says_it_never_went_out():
    after = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    email_tools.release_held_emails(now=_later(minutes=31))
    r = undo_email(_action(after))
    assert not r.success and "nobody approved it in time" in r.details


# ---------- the owner's approval desk ----------
def test_approvals_command_lists_approves_and_denies(monkeypatch, capsys):
    first = email_tools.email_send(CLASS_LIST, "Meeting cancelled", "b")["after"]
    second = email_tools.email_send(CLASS_LIST, "Party!", "b")["after"]
    monkeypatch.setattr("sys.argv", ["approvals"])
    approvals.main()
    listing = capsys.readouterr().out
    assert "Emails waiting for your approval (fake calendar): 2" in listing
    assert first["id"] in listing and "blast radius 9 (high)" in listing
    monkeypatch.setattr("sys.argv", ["approvals", "approve", first["id"]])
    approvals.main()
    assert "[ok] Approved and sent 'Meeting cancelled' to 33 people." in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["approvals", "deny", second["id"]])
    approvals.main()
    assert "will never be sent" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["approvals", "deny", second["id"]])
    with pytest.raises(SystemExit, match="isn't waiting for approval"):
        approvals.main()