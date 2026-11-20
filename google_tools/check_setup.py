#Importing packages
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from googleapiclient.errors import HttpError

from google_tools.google_auth import calendar_service, gmail_service

TEST_TITLE = "Agent-Ctrl-Z setup check (safe to delete)"

#Reading the calender
def check_calendar_read(cal) -> str:
    now = datetime.now(timezone.utc).isoformat()
    resp = cal.events().list(calendarId="primary", timeMin=now, maxResults=5,
                             singleEvents=True, orderBy="startTime").execute()
    who = resp.get("summary", "?")
    items = resp.get("items", [])
    print(f"[ok] Logged in. Calendar: {who}")
    print(f"[ok] Calendar read works ({len(items)} upcoming events shown)")
    for e in items:
        start = e["start"].get("dateTime", e["start"].get("date"))
        print(f"       {start}  {e.get('summary', '(no title)')}")
    return who


#Checkign the events
def _make_test_event(cal) -> dict:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    body = {"summary": TEST_TITLE,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()}}
    return cal.events().insert(calendarId="primary", body=body, sendUpdates="none").execute()


def _delete_quietly(cal, event_id: str) -> None:
    try:
        cal.events().delete(calendarId="primary", eventId=event_id, sendUpdates="none").execute()
    except HttpError:
        pass  # already deleted


def check_calendar_write(cal) -> None:
    e = _make_test_event(cal)
    cal.events().delete(calendarId="primary", eventId=e["id"], sendUpdates="none").execute()
    print("[ok] Calendar write works (created and deleted a test event)")


def check_gmail_draft(gmail, me: str) -> None:
    msg = EmailMessage()
    if "@" in me:
        msg["To"] = me
    msg["Subject"] = TEST_TITLE
    msg.set_content("Created by check_setup.py and deleted right away. Never sent.")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    gmail.users().drafts().delete(userId="me", id=draft["id"]).execute()
    print("[ok] Gmail drafts work (created and deleted a draft; nothing was sent)")


def restore_test(cal) -> None:
    """Can a deleted event be brought back with the SAME id? Decides how restore_calendar works."""
    print("\nRestore test:")
    e = _make_test_event(cal)
    cal.events().delete(calendarId="primary", eventId=e["id"], sendUpdates="none").execute()
    try:
        gone = cal.events().get(calendarId="primary", eventId=e["id"]).execute()
        print(f"       after delete, get() still finds it with status = {gone.get('status')!r}")
    except HttpError as err:
        print(f"       after delete, get() fails ({err.status_code})")
    try:
        back = cal.events().patch(calendarId="primary", eventId=e["id"],
                                  body={"status": "confirmed"}, sendUpdates="none").execute()
        kept = back["id"] == e["id"] and back.get("status") == "confirmed"
        print(f"[{'ok' if kept else '??'}] patch status -> 'confirmed': "
              f"id {'kept' if back['id'] == e['id'] else 'CHANGED'}, status = {back.get('status')!r}")
        if kept:
            print("       => restore_calendar can bring events back with the SAME id")
    except HttpError as err:
        print(f"[no] patch failed ({err.status_code}): restore will need insert + new_target_id")
    finally:
        _delete_quietly(cal, e["id"])


def _hint(err: HttpError) -> str:
    text = str(err).lower()
    if "has not been used" in text or "is disabled" in text:
        return "That API isn't enabled. Enable the Calendar API and Gmail API in the SAME project."
    if "insufficient" in text or "scope" in text:
        return "Missing permission. Delete token.json and run this again to log in fresh."
    return "See the error above."


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the Google Calendar + Gmail setup.")
    parser.add_argument("--restore-test", action="store_true",
                        help="also test whether a deleted event can be restored with the same id")
    args = parser.parse_args()
    try:
        cal, gmail = calendar_service(), gmail_service()
        me = check_calendar_read(cal)
        check_calendar_write(cal)
        check_gmail_draft(gmail, me)
        if args.restore_test:
            restore_test(cal)
    except FileNotFoundError as err:
        raise SystemExit(f"[fail] {err}")
    except HttpError as err:
        raise SystemExit(f"[fail] Google said {err.status_code}: {err.reason}\n       {_hint(err)}")
    print("\nAll good: the Google setup works.")


if __name__ == "__main__":
    main()