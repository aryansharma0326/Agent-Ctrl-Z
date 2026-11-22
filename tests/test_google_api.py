"""OWNER: Visista. Tests for google_tools/google_api.py (the real-Google backend).

These never touch real Google. They swap in a small pretend Google Calendar + Gmail
that behaves like the real one where it matters (checked on the real account):
a deleted event stays retrievable with status "cancelled", patching it back to
"confirmed" keeps its id, deleting it twice gives 410, a missing id gives 404.
"""
import base64
import copy
import email
import uuid
from datetime import datetime

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from google_tools import calendar_tools as ct
from google_tools import email_tools, google_api
from google_tools.undo_fixes import restore_calendar, undo_email
from shared.models import ActionRecord

OWNER = "owner@gmail.com"


# ---------- a pretend Google ----------
def _error(status):
    return HttpError(Response({"status": status}), b"pretend Google error")


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def _when(t: dict) -> datetime:
    return google_api._parse(t.get("dateTime") or t["date"])


class PretendCalendar:
    PAGE = 2                                   # small pages, to test paging

    def __init__(self):
        self.events_by_id, self.send_updates = {}, []

    def events(self):
        return self

    def _find(self, eventId):
        if eventId not in self.events_by_id:
            raise _error(404)
        return self.events_by_id[eventId]

    def get(self, calendarId, eventId):
        return _Call(lambda: copy.deepcopy(self._find(eventId)))

    def list(self, calendarId, pageToken=None, timeMin=None, timeMax=None, showDeleted=False,
             **_):
        def run():
            items = [e for e in self.events_by_id.values()
                     if showDeleted or e["status"] != "cancelled"]
            if timeMin:
                items = [e for e in items if _when(e["end"]) > google_api._parse(timeMin)]
            if timeMax:
                items = [e for e in items if _when(e["start"]) < google_api._parse(timeMax)]
            items.sort(key=lambda e: _when(e["start"]))
            first = int(pageToken or 0)
            page = items[first:first + self.PAGE]
            more = first + self.PAGE < len(items)
            return {"items": copy.deepcopy(page),
                    **({"nextPageToken": str(first + self.PAGE)} if more else {})}
        return _Call(run)

    def insert(self, calendarId, body, sendUpdates=None):
        def run():
            self.send_updates.append(sendUpdates)
            e = copy.deepcopy(body)
            e.update(id=uuid.uuid4().hex[:10], status="confirmed",
                     organizer={"email": OWNER, "self": True})
            for a in e.get("attendees", []):
                a.setdefault("responseStatus", "accepted" if a["email"] == OWNER else "needsAction")
            self.events_by_id[e["id"]] = e
            return copy.deepcopy(e)
        return _Call(run)

    def patch(self, calendarId, eventId, body, sendUpdates=None):
        def run():
            self.send_updates.append(sendUpdates)
            e = self._find(eventId)
            e.update(copy.deepcopy(body))
            return copy.deepcopy(e)
        return _Call(run)

    def delete(self, calendarId, eventId, sendUpdates=None):
        def run():
            self.send_updates.append(sendUpdates)
            e = self._find(eventId)
            if e["status"] == "cancelled":
                raise _error(410)
            e["status"] = "cancelled"
        return _Call(run)


class PretendGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        def run():
            msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
            msg_id = "gm_" + uuid.uuid4().hex[:8]
            self.sent.append({"payload": body, "msg": msg})
            return {"id": msg_id, "threadId": body.get("threadId", msg_id)}
        return _Call(run)


@pytest.fixture
def google(monkeypatch, tmp_path):
    """Real backend switched on, but talking to the pretend Google."""
    cal, gmail = PretendCalendar(), PretendGmail()
    monkeypatch.setattr(google_api, "_calendar", lambda: cal)
    monkeypatch.setattr(google_api, "_gmail", lambda: gmail)
    monkeypatch.setenv("USE_REAL_GOOGLE", "1")
    monkeypatch.setenv("CTRLZ_OUTBOX", str(tmp_path / "outbox.db"))
    return cal, gmail


def _add(cal, summary, start, end, attendees=(), **extra):
    body = {"summary": summary, "start": {"dateTime": start, "timeZone": "America/New_York"},
            "end": {"dateTime": end, "timeZone": "America/New_York"},
            "attendees": [{"email": a} for a in attendees], **extra}
    return cal.insert(calendarId="primary", body=body).execute()


# ---------- Google's format -> the agreed flat shape ----------
def test_google_event_becomes_the_agreed_flat_shape():
    g = {"id": "abc", "summary": "Demo prep", "status": "confirmed",
         "start": {"dateTime": "2026-11-20T15:00:00-05:00"},
         "end": {"dateTime": "2026-11-20T16:00:00-05:00"},
         "organizer": {"email": OWNER},
         "attendees": [{"email": OWNER, "responseStatus": "accepted"},
                       {"email": "aryan@gmail.com", "responseStatus": "declined"},
                       {"email": "room@resource.calendar.google.com", "resource": True}],
         "description": "Notes",
         "conferenceData": {"entryPoints": [{"entryPointType": "video",
                                             "uri": "https://zoom.us/j/1"}]}}
    flat = google_api._flat(g)
    assert flat == {"id": "abc", "summary": "Demo prep", "start": "2026-11-20T15:00:00-05:00",
                    "end": "2026-11-20T16:00:00-05:00", "attendees": [OWNER, "aryan@gmail.com"],
                    "description": "Notes", "location": "https://zoom.us/j/1", "organizer": OWNER,
                    "responses": {OWNER: "accepted", "aryan@gmail.com": "declined"}, "raw": g}


def test_event_without_guests_still_lists_the_owner_and_all_day_keeps_dates():
    flat = google_api._flat({"id": "x", "start": {"date": "2026-11-20"},
                             "end": {"date": "2026-11-21"}, "organizer": {"email": OWNER}})
    assert flat["attendees"] == [OWNER] and flat["summary"] == "(no title)"
    assert (flat["start"], flat["end"]) == ("2026-11-20", "2026-11-21")


# ---------- calendar functions ----------
def test_get_returns_flat_event_none_if_cancelled_or_missing(google):
    cal, _ = google
    e = _add(cal, "Gym", "2026-11-20T08:00:00", "2026-11-20T09:00:00")
    assert google_api.get_event(e["id"])["summary"] == "Gym"
    google_api.delete_event(e["id"])
    assert google_api.get_event(e["id"]) is None
    assert google_api.get_event("nope") is None


def test_list_uses_the_window_skips_cancelled_and_reads_every_page(google):
    cal, _ = google
    ids = [_add(cal, f"E{i}", f"2026-11-20T{9 + i:02d}:00:00", f"2026-11-20T{9 + i:02d}:30:00")["id"]
           for i in range(5)]                                  # 9:00, 10:00 ... 13:00
    google_api.delete_event(ids[1])
    got = [e["summary"] for e in google_api.list_events("2026-11-20T09:15", "2026-11-20T13:15")]
    assert got == ["E0", "E2", "E3", "E4"]                     # 3 pages of 2, E1 cancelled


def test_insert_gives_new_id_new_york_time_and_sends_no_google_emails(google):
    cal, _ = google
    made = google_api.insert_event({"summary": "Makeup", "start": "2026-11-23T10:00",
                                    "end": "2026-11-23T10:30", "attendees": [OWNER]})
    raw = cal.events_by_id[made["id"]]
    assert raw["start"] == {"dateTime": "2026-11-23T10:00:00", "timeZone": "America/New_York"}
    assert cal.send_updates == ["none"]


def test_times_with_a_timezone_are_sent_as_they_are(google):
    cal, _ = google
    made = google_api.insert_event({"start": "2026-11-23T15:00:00Z", "end": "2026-11-23T16:00:00Z"})
    assert cal.events_by_id[made["id"]]["start"] == {"dateTime": "2026-11-23T15:00:00+00:00"}
    assert made["summary"] == "(no title)"


def test_update_changes_only_what_is_given_and_keeps_rsvps(google):
    cal, _ = google
    e = _add(cal, "Sync", "2026-11-23T14:00:00", "2026-11-23T14:30:00", [OWNER, "aryan@gmail.com"])
    cal.events_by_id[e["id"]]["attendees"][1]["responseStatus"] = "accepted"
    cal.send_updates.clear()                          # only check what update_event sends
    after = google_api.update_event(e["id"], {"attendees": [OWNER, "aryan@gmail.com", "v@gmail.com"]})
    assert after["responses"] == {OWNER: "accepted", "aryan@gmail.com": "accepted",
                                  "v@gmail.com": "needsAction"}
    assert after["summary"] == "Sync" and set(cal.send_updates) == {"none"}


def test_update_of_cancelled_event_raises(google):
    cal, _ = google
    e = _add(cal, "Sync", "2026-11-23T14:00:00", "2026-11-23T14:30:00")
    google_api.delete_event(e["id"])
    with pytest.raises(KeyError):
        google_api.update_event(e["id"], {"summary": "x"})


def test_delete_then_restore_keeps_the_same_id(google):
    cal, _ = google
    e = _add(cal, "Demo", "2026-11-20T15:00:00", "2026-11-20T16:00:00")
    google_api.delete_event(e["id"])
    google_api.delete_event(e["id"])                  # second delete (410) is not an error
    back = google_api.restore_event(e["id"])
    assert back["id"] == e["id"] and cal.events_by_id[e["id"]]["status"] == "confirmed"
    assert google_api.restore_event(e["id"])["id"] == e["id"]   # already live: just returns it
    assert google_api.restore_event("nope") is None
    google_api.delete_event("nope")                   # missing (404) is not an error


# ---------- email ----------
def test_send_email_builds_a_proper_message(google):
    _, gmail = google
    sent = google_api.send_email(["aryan@gmail.com", "v@gmail.com"], "Meeting off", "No meeting")
    msg = gmail.sent[-1]["msg"]
    assert msg["To"] == "aryan@gmail.com, v@gmail.com" and msg["Subject"] == "Meeting off"
    assert msg.get_payload().strip() == "No meeting"
    assert sent["thread_id"] and sent["message_id_header"] == msg["Message-ID"]


def test_reply_goes_into_the_same_thread(google):
    _, gmail = google
    first = google_api.send_email(["aryan@gmail.com"], "Meeting off", "x")
    google_api.send_email(["aryan@gmail.com"], "Re: Meeting off", "ignore", reply_to=first)
    reply = gmail.sent[-1]
    assert reply["payload"]["threadId"] == first["thread_id"]
    assert reply["msg"]["In-Reply-To"] == first["message_id_header"]
    assert reply["msg"]["References"] == first["message_id_header"]


def test_sample_data_addresses_are_never_emailed_for_real(google):
    _, gmail = google
    with pytest.raises(ValueError, match="sample-data addresses"):
        google_api.send_email(["aryan@gmail.com", "student7@test.com"], "s", "b")
    assert gmail.sent == []


# ---------- your whole stack on the real backend ----------
def test_clear_my_friday_and_undo_on_the_real_backend(google):
    cal, _ = google
    demo = ct.calendar_create({"summary": "CS 598 demo prep", "start": "2026-11-20T15:00",
                               "end": "2026-11-20T16:00",
                               "attendees": [OWNER, "aryan@gmail.com"]})["after"]
    result = ct.calendar_cancel(demo["id"])
    assert ct.calendar_list("2026-11-20T00:00", "2026-11-21T00:00") == []
    action = ActionRecord(tool="calendar.cancel_event", target_id=demo["id"],
                          before=result["before"], after=None,
                          affected_people=["aryan@gmail.com"], instruction="Clear my Friday")
    r = restore_calendar(action)
    assert r.success and r.new_target_id is None
    assert ct.calendar_get(demo["id"])["summary"] == "CS 598 demo prep"
    assert set(cal.send_updates) == {"none"}


def test_email_correction_on_the_real_backend_threads_with_the_original(google):
    _, gmail = google
    result = email_tools.email_send(["aryan@gmail.com"], "Meeting off", "x", hold_seconds=0)
    action = ActionRecord(tool="email.send", target_id=result["after"]["id"], before=None,
                          after=result["after"], affected_people=["aryan@gmail.com"],
                          instruction="Tell the team")
    r = undo_email(action)
    assert r.success and r.fix_type == "email_correction"
    assert gmail.sent[-1]["payload"]["threadId"] == result["after"]["thread_id"]
    assert gmail.sent[-1]["msg"]["Subject"] == "Re: Meeting off"


# ---------- Google answering in UTC (seen on the real account, Nov 22) ----------
def test_utc_times_from_google_become_new_york_time():
    flat = google_api._flat({"id": "x", "organizer": {"email": OWNER},
                             "start": {"dateTime": "2026-11-23T20:00:00Z"},
                             "end": {"dateTime": "2026-11-23T21:00:00Z"}})
    assert (flat["start"], flat["end"]) == ("2026-11-23T15:00:00-05:00", "2026-11-23T16:00:00-05:00")


def test_dropping_the_timezone_still_gives_the_right_new_york_hour():
    # Aryan's parse_time drops the timezone; after our conversion that is still 3 PM
    flat = google_api._flat({"id": "x", "start": {"dateTime": "2026-11-23T20:00:00Z"},
                             "end": {"dateTime": "2026-11-23T21:00:00Z"}})
    assert datetime.fromisoformat(flat["start"]).replace(tzinfo=None).hour == 15


def test_whole_stack_sees_new_york_times_when_google_answers_in_utc(google, monkeypatch):
    cal, _ = google
    real_get = cal.get
    def get_in_utc(calendarId, eventId):            # pretend the account is set to UTC
        def run():
            e = real_get(calendarId, eventId).execute()
            for key in ("start", "end"):
                t = google_api._parse(e[key]["dateTime"]).astimezone(google_api.timezone.utc)
                e[key] = {"dateTime": t.strftime("%Y-%m-%dT%H:%M:%SZ")}
            return e
        return _Call(run)
    monkeypatch.setattr(cal, "get", get_in_utc)
    made = ct.calendar_create({"summary": "Demo", "start": "2026-11-23T15:00",
                               "end": "2026-11-23T16:00"})["after"]
    assert ct.calendar_get(made["id"])["start"] == "2026-11-23T15:00:00-05:00"