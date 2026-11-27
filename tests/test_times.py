"""OWNER: Varuni. Tests for core/times.py and the places that read times."""
from datetime import timedelta, timezone

import pytest

from core import undo_router as r
from core.receipts import _when
from core.times import parse_iso
from shared import db
from shared.models import ActionRecord


@pytest.mark.parametrize("text", ["2026-03-04T15:00:00Z", "2026-03-04T15:00:00z",
                                  " 2026-03-04T15:00:00Z "])
def test_trailing_z_means_utc_on_every_python(text):
    t = parse_iso(text)
    assert t.utcoffset() == timedelta(0) and (t.hour, t.minute) == (15, 0)


def test_other_times_read_exactly_like_fromisoformat():
    assert parse_iso("2026-03-04T10:00:00-05:00").utcoffset() == timedelta(hours=-5)
    assert parse_iso("2026-03-04T10:00").tzinfo is None
    with pytest.raises(ValueError):
        parse_iso("Friday")


def test_router_sees_a_clash_written_in_utc(tmp_path, monkeypatch):
    """Aryan's dentist at 15:00Z is 10:00 New York, the same slot as the cancelled
    review, so a plain restore would double-book him: group repair must be used."""
    review = {"id": "evt_review", "summary": "Design review", "start": "2026-03-04T10:00:00-05:00",
              "end": "2026-03-04T11:00:00-05:00", "attendees": ["aryan@test.com"]}
    dentist = {"id": "evt_dent", "summary": "Dentist", "start": "2026-03-04T15:00:00Z",
               "end": "2026-03-04T16:00:00Z", "attendees": ["aryan@test.com"]}
    monkeypatch.setattr(r.calendar_tools, "calendar_get", lambda i: None)
    monkeypatch.setattr(r.calendar_tools, "calendar_list", lambda a, b: [dentist])
    conn = db.connect(tmp_path / "t.db")
    a = ActionRecord(tool="calendar.cancel_event", target_id="evt_review", before=review,
                     after=None, affected_people=["aryan@test.com"], instruction="x")
    db.save_action(conn, a)
    plan = r.plan_undo(conn, a.action_id)
    assert plan.strategy == "group_repair" and "Dentist" in plan.conflict


def test_receipts_show_utc_times():
    assert _when({"start": "2026-03-04T15:00:00Z", "end": "2026-03-04T16:00:00Z"}) \
        == "Wed Mar 4, 3:00 PM–4:00 PM UTC"
