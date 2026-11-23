"""OWNER: Visista
Live check of the whole google_tools stack on the REAL owner account (step 8 + step 9).
Run from the repo root:

    python -m google_tools.check_real_google
    python -m google_tools.check_real_google --attendee teammate@gmail.com   # step 9

It only touches things it creates itself: one test event (tomorrow, titled
"Agent-Ctrl-Z live check") and one email from the owner account to ITSELF.
The test event is cancelled again at the end.

With --attendee it is a real-life test of the other side (step 9):
  1. the meeting is created the NORMAL way, so the teammate gets a real invitation
     email from the owner and clicks Yes (that's how meetings exist before an agent acts)
  2. then everything the agent and our undo do is silent (no Google emails), and the
     script pauses so the teammate can look at their own Google Calendar each time:
     does the event move, disappear on cancel, come back on restore, and does their
     Yes survive?
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta

os.environ["USE_REAL_GOOGLE"] = "1"   # before anything else: this check is for real Google

from google_tools import calendar_tools as ct          # noqa: E402
from google_tools import email_tools                   # noqa: E402
from google_tools import google_api                    # noqa: E402
from google_tools.undo_fixes import restore_calendar, undo_email   # noqa: E402
from shared.models import ActionRecord                 # noqa: E402

TITLE = "Agent-Ctrl-Z live check (safe to delete)"


def ok(text: str) -> None:
    print(f"[ok] {text}")


def problem(text: str) -> None:
    print(f"[!!] {text}")


def pause(attendee: str | None, what_to_check: str) -> None:
    if attendee:
        input(f"\n   >> Ask {attendee}: {what_to_check}\n"
              "      Press Enter when they've checked...")


def invite_normally(fields: dict) -> dict:
    """Create the meeting the way a person would: Google emails the guests an invitation.
    (Only this setup step sends a Google email; everything after it is silent.)"""
    body = google_api._to_google(fields)
    g = google_api._calendar().events().insert(calendarId="primary", body=body,
                                               sendUpdates="all").execute()
    return google_api._flat(g)


def wait_for_yes(attendee: str, event_id: str) -> dict:
    """Give the teammate a few tries to click Yes, so the reply test means something."""
    for _ in range(3):
        responses = ct.calendar_get(event_id)["responses"]
        if responses.get(attendee) == "accepted":
            ok(f"{attendee} clicked Yes")
            return responses
        input(f"\n   >> Google still shows {attendee}'s reply as '{responses.get(attendee)}'.\n"
              "      Ask them to click Yes in the invitation email (or on the event in their\n"
              "      calendar), then press Enter to check again...")
    problem(f"{attendee}'s Yes never reached Google, so the reply test below won't mean much")
    return ct.calendar_get(event_id)["responses"]


def action(tool: str, target: str, result: dict, affected: list[str]) -> ActionRecord:
    return ActionRecord(tool=tool, target_id=target, before=result["before"],
                        after=result["after"], affected_people=affected, instruction="live check")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check google_tools on the real Google account.")
    parser.add_argument("--attendee", help="a teammate's real email, for the step 9 attendee check")
    args = parser.parse_args()

    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    start, end = tomorrow.strftime("%Y-%m-%dT%H:%M"), (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    guests = [args.attendee] if args.attendee else []

    # 1. create
    fields = {"summary": TITLE, "start": start, "end": end, "attendees": guests,
              "description": "Created by ['organizer']", "location": "https://zoom.us/j/123"}
    if args.attendee:
        made = invite_normally(fields)
        ok(f"meeting created the normal way and an invitation emailed to {args.attendee} "
           f"(id {made['id']}, {made['start']})")
        pause(args.attendee, f"check your INBOX for 'Invitation: {TITLE}' from {made['organizer']}.\n"
                             "      Click Yes in it, then open Google Calendar: is it there tomorrow 3-4 PM?")
    else:
        made = ct.calendar_create(fields)["after"]
        ok(f"calendar_create: '{made['summary']}' {made['start']} (id {made['id']}), owner {made['organizer']}")
    event_id, owner = made["id"], made["organizer"]

    # 2. read
    assert ct.calendar_get(event_id)["location"] == "https://zoom.us/j/123"
    day = (tomorrow.strftime("%Y-%m-%dT00:00"), (tomorrow + timedelta(days=1)).strftime("%Y-%m-%dT00:00"))
    assert event_id in [e["id"] for e in ct.calendar_list(*day)]
    ok("calendar_get and calendar_list find it, with all agreed fields")
    responses_before = (wait_for_yes(args.attendee, event_id) if args.attendee
                        else ct.calendar_get(event_id)["responses"])

    # 3. move it, then undo the move
    moved = ct.calendar_update(event_id, {"start": (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})
    assert moved["after"]["end"] > moved["before"]["end"]
    ok(f"calendar_update moved it to {moved['after']['start']} (same length)")
    pause(args.attendee, "open Google Calendar (no email this time): has the meeting moved to 4-5 PM?")
    r = restore_calendar(action("calendar.update_event", event_id, moved, guests))
    back = ct.calendar_get(event_id)
    same_time = ct._read_time(back["start"], "start") == ct._read_time(start, "start")
    (ok if r.success and same_time else problem)(f"restore_calendar (move): {r.details}")

    # 4. cancel, then restore with the same id
    cancelled = ct.calendar_cancel(event_id)
    (ok if ct.calendar_get(event_id) is None else problem)("calendar_cancel: event is gone")
    pause(args.attendee, "open Google Calendar (no email this time): has the meeting disappeared?")
    r = restore_calendar(action("calendar.cancel_event", event_id, cancelled, guests))
    same_id = r.success and r.new_target_id is None and ct.calendar_get(event_id) is not None
    (ok if same_id else problem)(f"restore_calendar (cancel): {r.details}")
    pause(args.attendee, "open Google Calendar (no email this time): is the meeting back at 3-4 PM,\n"
                         "      and does it still show your Yes?")
    if args.attendee:
        after = ct.calendar_get(event_id)["responses"]
        kept = after.get(args.attendee) == responses_before.get(args.attendee)
        (ok if kept else problem)(f"{args.attendee}'s reply: before {responses_before.get(args.attendee)}, "
                                  f"after restore {after.get(args.attendee)}")

    # 5. email to the owner only, then a correction in the same thread
    sent = email_tools.email_send([owner], "Agent-Ctrl-Z live check", "Test email to myself.",
                                  hold_seconds=0)
    r = undo_email(action("email.send", sent["after"]["id"], sent, [owner]))
    (ok if r.success and r.fix_type == "email_correction" else problem)(f"undo_email: {r.details}")
    print(f"     Check {owner}'s inbox: 'Re: Agent-Ctrl-Z live check' should be in the same conversation.")

    # 6. clean up
    ct.calendar_cancel(event_id)
    ok("cleaned up: test event cancelled")
    print("\nDone. If every line says [ok], the real backend works.")


if __name__ == "__main__":
    main()