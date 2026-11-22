"""OWNER: Visista
Live check of the whole google_tools stack on the REAL owner account (step 8 + step 9).
Run from the repo root:

    python -m google_tools.check_real_google
    python -m google_tools.check_real_google --attendee teammate@gmail.com   # step 9

It only touches things it creates itself: one test event (tomorrow, titled
"Agent-Ctrl-Z live check") and one email from the owner account to ITSELF.
The test event is cancelled again at the end.

With --attendee it invites that person and pauses at each step, so they can look
at their own Google Calendar: does the event disappear on cancel, come back on
restore, and does their Yes/No reply survive? (Google sends them no emails.)
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta

os.environ["USE_REAL_GOOGLE"] = "1"   # before anything else: this check is for real Google

from google_tools import calendar_tools as ct          # noqa: E402
from google_tools import email_tools                   # noqa: E402
from google_tools.undo_fixes import restore_calendar, undo_email   # noqa: E402
from shared.models import ActionRecord                 # noqa: E402

TITLE = "Agent-Ctrl-Z live check (safe to delete)"


def ok(text: str) -> None:
    print(f"[ok] {text}")


def problem(text: str) -> None:
    print(f"[!!] {text}")


def pause(attendee: str | None, what_to_check: str) -> None:
    if attendee:
        input(f"\n   >> Ask {attendee} to open their Google Calendar: {what_to_check}\n"
              "      Press Enter to continue...")


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
    made = ct.calendar_create({"summary": TITLE, "start": start, "end": end, "attendees": guests,
                               "description": "Created by check_real_google.py",
                               "location": "https://zoom.us/j/123"})["after"]
    event_id, owner = made["id"], made["organizer"]
    ok(f"calendar_create: '{made['summary']}' {made['start']} (id {event_id}), owner {owner}")
    pause(args.attendee, "is 'Agent-Ctrl-Z live check' there tomorrow at 3 PM? Please click Yes on it.")

    # 2. read
    assert ct.calendar_get(event_id)["location"] == "https://zoom.us/j/123"
    day = (tomorrow.strftime("%Y-%m-%dT00:00"), (tomorrow + timedelta(days=1)).strftime("%Y-%m-%dT00:00"))
    assert event_id in [e["id"] for e in ct.calendar_list(*day)]
    ok("calendar_get and calendar_list find it, with all agreed fields")
    responses_before = ct.calendar_get(event_id)["responses"]
    if args.attendee:
        print(f"     replies so far: {responses_before}")

    # 3. move it, then undo the move
    moved = ct.calendar_update(event_id, {"start": (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})
    assert moved["after"]["end"] > moved["before"]["end"]
    ok(f"calendar_update moved it to {moved['after']['start']} (same length)")
    r = restore_calendar(action("calendar.update_event", event_id, moved, guests))
    back = ct.calendar_get(event_id)
    same_time = ct._read_time(back["start"], "start") == ct._read_time(start, "start")
    (ok if r.success and same_time else problem)(f"restore_calendar (move): {r.details}")

    # 4. cancel, then restore with the same id
    cancelled = ct.calendar_cancel(event_id)
    (ok if ct.calendar_get(event_id) is None else problem)("calendar_cancel: event is gone")
    pause(args.attendee, "has the event disappeared from your calendar?")
    r = restore_calendar(action("calendar.cancel_event", event_id, cancelled, guests))
    same_id = r.success and r.new_target_id is None and ct.calendar_get(event_id) is not None
    (ok if same_id else problem)(f"restore_calendar (cancel): {r.details}")
    pause(args.attendee, "is the event back? Is your 'Yes' still there?")
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