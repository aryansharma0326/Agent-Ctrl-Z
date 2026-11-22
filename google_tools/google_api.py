#This is the real backend connection with real Google Account connection if the toggle is enabled
#Do not enable USE_REAL_GOOGLE if all tests do not pass

#It logs in through google_auth.py, ensure your connection with the assigned gmail is correct
#Step 8 (built): the same functions as fake_data/fake_google.py, but on the real
#Google Calendar and Gmail of the logged-in (owner) account. See backend.py for the rules.

#How it matches the fake calendar:
# - Google's event format is turned into the agreed flat shape (id, summary, start, end,
#   attendees, description, location, organizer, responses), with the full Google event in "raw"
# - cancelled events come back as None from get_event and are left out of list_events
# - restore_event switches a cancelled event back to "confirmed": same id (tested Nov 21)
# - times without a timezone are New York time, and every time Google sends back
#   is turned into New York time (-05:00), even if Google answers in UTC

#Who gets told: Google sends NO emails for our changes (sendUpdates="none").
#Our own receipts and "it's fixed" notes do the telling, so nobody gets two emails.

#Safety: on real Gmail we refuse to email the sample-data addresses (@test.com,
#@example.com). Those are real domains owned by strangers.


#Importing packages
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from googleapiclient.errors import HttpError

CALENDAR = "primary"
TIMEZONE = "America/New_York"
NEW_YORK = timezone(timedelta(hours=-5))
NOTIFY = "none"
SAMPLE_DOMAINS = ("test.com", "example.com")


#The two Google connections (tests swap these for pretend ones)
def _calendar():
    from google_tools.google_auth import calendar_service
    return calendar_service()


def _gmail():
    from google_tools.google_auth import gmail_service
    return gmail_service()


def _gone(err: HttpError) -> bool:
    """404 / 410 mean the event doesn't exist (or was deleted)."""
    return getattr(err, "status_code", None) in (404, 410) or getattr(err.resp, "status", None) in (404, 410)


#Turning Google's event format into the agreed flat shape
def _meeting_link(g: dict) -> str:
    for entry in (g.get("conferenceData") or {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return g.get("hangoutLink", "")


def _new_york(value: str | None) -> str | None:
    """Google can answer in UTC ("...20:00:00Z") if the account's calendar is set to UTC.
    Everyone's code expects New York time, so turn it into "...15:00:00-05:00"."""
    if not value:
        return value
    return _parse(value).astimezone(NEW_YORK).isoformat()


def _flat(g: dict) -> dict:
    """One Google event in the agreed flat shape, with the full Google event in "raw"."""
    organizer = (g.get("organizer") or {}).get("email", "")
    people = [a for a in (g.get("attendees") or []) if a.get("email") and not a.get("resource")]
    attendees = [a["email"] for a in people]
    if organizer and organizer not in attendees:
        attendees.insert(0, organizer)        # attendees always include the owner
    start, end = g.get("start") or {}, g.get("end") or {}
    return {
        "id": g["id"],
        "summary": g.get("summary", "(no title)"),
        "start": _new_york(start.get("dateTime")) or start.get("date", ""),   # date only = all-day
        "end": _new_york(end.get("dateTime")) or end.get("date", ""),
        "attendees": attendees,
        "description": g.get("description", ""),
        "location": g.get("location") or _meeting_link(g),       # Zoom/Meet link if no location
        "organizer": organizer,
        "responses": {a["email"]: a.get("responseStatus", "needsAction") for a in people},
        "raw": g,
    }


#Turning our flat fields into Google's format
def _parse(value: str) -> datetime:
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=NEW_YORK)


def _google_time(value: str) -> dict:
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo:
        return {"dateTime": t.isoformat()}
    return {"dateTime": t.isoformat(), "timeZone": TIMEZONE}


def _to_google(fields: dict, current: dict | None = None) -> dict:
    body = {k: fields[k] for k in ("summary", "description", "location") if k in fields}
    for key in ("start", "end"):
        if key in fields:
            body[key] = _google_time(fields[key])
    if "attendees" in fields:
        # keep each existing guest's details (like their RSVP); only new guests are added fresh
        existing = {a.get("email"): a for a in (current or {}).get("attendees", [])}
        body["attendees"] = [existing.get(email, {"email": email}) for email in fields["attendees"]]
    return body


#Seven functions for the calendar and email (same names as the fake calendar)

#Getting event - None if it doesn't exist or was cancelled
def get_event(event_id: str) -> dict | None:
    try:
        g = _calendar().events().get(calendarId=CALENDAR, eventId=event_id).execute()
    except HttpError as err:
        if _gone(err):
            return None
        raise
    return None if g.get("status") == "cancelled" else _flat(g)


#Listing events - live events overlapping the window, sorted by start
def list_events(start: str | None = None, end: str | None = None) -> list[dict]:
    query = {"calendarId": CALENDAR, "singleEvents": True, "orderBy": "startTime",
             "showDeleted": False, "maxResults": 250}
    if start:
        query["timeMin"] = _parse(start).isoformat()
    if end:
        query["timeMax"] = _parse(end).isoformat()
    events, page = [], None
    while True:
        resp = _calendar().events().list(pageToken=page, **query).execute()
        events += [_flat(g) for g in resp.get("items", []) if g.get("status") != "cancelled"]
        page = resp.get("nextPageToken")
        if not page:
            return events


#Inserting event - always a new id
def insert_event(event: dict) -> dict:
    body = _to_google(event)
    body.setdefault("summary", "(no title)")
    g = _calendar().events().insert(calendarId=CALENDAR, body=body, sendUpdates=NOTIFY).execute()
    return _flat(g)


#Updating event - only the fields given change
def update_event(event_id: str, changes: dict) -> dict:
    current = _calendar().events().get(calendarId=CALENDAR, eventId=event_id).execute()
    if current.get("status") == "cancelled":
        raise KeyError(f"No live event {event_id!r}")
    g = _calendar().events().patch(calendarId=CALENDAR, eventId=event_id,
                                   body=_to_google(changes, current), sendUpdates=NOTIFY).execute()
    return _flat(g)


#Deleting event - cancels it; Google keeps it, so it can be restored with the same id
def delete_event(event_id: str) -> None:
    try:
        _calendar().events().delete(calendarId=CALENDAR, eventId=event_id,
                                    sendUpdates=NOTIFY).execute()
    except HttpError as err:
        if not _gone(err):
            raise


#Restoring event - brings a cancelled event back with the same id (None if it can't)
def restore_event(event_id: str) -> dict | None:
    try:
        g = _calendar().events().get(calendarId=CALENDAR, eventId=event_id).execute()
        if g.get("status") != "cancelled":
            return _flat(g)                    # already live
        g = _calendar().events().patch(calendarId=CALENDAR, eventId=event_id,
                                       body={"status": "confirmed"}, sendUpdates=NOTIFY).execute()
    except HttpError:
        return None                            # restore_calendar then makes an exact copy
    return _flat(g)


#Sending email - right away; reply_to = an earlier message, to answer in the same thread
def send_email(to: list[str], subject: str, body: str, reply_to: dict | None = None) -> dict:
    blocked = [e for e in to if e.lower().rsplit("@", 1)[-1] in SAMPLE_DOMAINS]
    if blocked:
        raise ValueError(f"Refusing to email sample-data addresses on real Gmail: {', '.join(blocked)}. "
                         "Use real addresses (e.g. your teammates') for the real demo.")
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="agent-ctrl-z.local")
    payload = {}
    if reply_to:
        if reply_to.get("message_id_header"):
            msg["In-Reply-To"] = msg["References"] = reply_to["message_id_header"]
        if reply_to.get("thread_id"):
            payload["threadId"] = reply_to["thread_id"]
    msg.set_content(body)
    payload["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = _gmail().users().messages().send(userId="me", body=payload).execute()
    return {"id": sent["id"], "thread_id": sent.get("threadId", sent["id"]), "to": list(to),
            "subject": subject, "body": body, "message_id_header": msg["Message-ID"]}