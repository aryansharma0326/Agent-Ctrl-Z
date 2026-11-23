"""OWNER: Visista
Live check of the whole google_tools stack on the REAL owner account (step 8 + step 9).
Run from the repo root:

    python -m google_tools.check_real_google                      # owner only, no one else involved
    python -m google_tools.check_real_google --attendee a@gmail.com --attendee b@gmail.com
    python -m google_tools.check_real_google --attendee a@gmail.com --events 5

It only touches things it creates itself: a few test meetings tomorrow, titled
"Agent-Ctrl-Z test 1/3" and so on, and one test email. Everything is cleaned up at the end.

With --attendee it is a real-life test of the other side (step 9). The teammates:
  1. get a normal invitation email for each meeting, and click Yes on each
  2. see what happens on their side while the "agent" moves one meeting and then
     clears the whole day, and while our undo puts everything back:
     the meetings move / disappear / come back on their calendar, Google emails them
     each time ("Updated invitation", "Canceled event", ...), and their Yes should survive
  3. get a test email from the owner, then the correction ("Please ignore my earlier
     email...") in the same conversation
The script pauses at each step so they can look.
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

START_HOURS = [9, 11, 15, 16, 17, 13, 14, 10]          # tomorrow's meeting times, in order
results: list[tuple[bool, str]] = []


def ok(text: str) -> None:
    results.append((True, text))
    print(f"[ok] {text}")


def problem(text: str) -> None:
    results.append((False, text))
    print(f"[!!] {text}")


def check(passed: bool, text: str) -> None:
    (ok if passed else problem)(text)


def pause(people: list[str], what_to_check: str) -> None:
    if people:
        input(f"\n   >> Ask {', '.join(people)}:\n      {what_to_check}\n"
              "      Press Enter when they've checked...")


def action(tool: str, target: str, result: dict, affected: list[str], instruction: str) -> ActionRecord:
    return ActionRecord(tool=tool, target_id=target, before=result["before"],
                        after=result["after"], affected_people=affected, instruction=instruction)


def owner_email() -> str:
    """The logged-in account (its primary calendar is named after it)."""
    return google_api._calendar().events().list(calendarId="primary", maxResults=1).execute()["summary"]


def invite_normally(fields: dict) -> dict:
    """Create a meeting the way a person would: Google emails each guest an invitation."""
    g = google_api._calendar().events().insert(calendarId="primary", body=google_api._to_google(fields),
                                               sendUpdates="all").execute()
    return google_api._flat(g)


def delete_quietly(event_id: str) -> None:
    """Clean-up only: delete without emailing anyone."""
    try:
        google_api._calendar().events().delete(calendarId="primary", eventId=event_id,
                                               sendUpdates="none").execute()
    except Exception:
        pass


def wait_for_yes(people: list[str], event_ids: list[str]) -> dict[str, dict]:
    """Give the teammates a few tries to click Yes on every meeting."""
    for _ in range(3):
        replies = {eid: ct.calendar_get(eid)["responses"] for eid in event_ids}
        missing = [(p, eid) for eid in event_ids for p in people if replies[eid].get(p) != "accepted"]
        if not missing:
            ok(f"everyone clicked Yes on all {len(event_ids)} meetings")
            return replies
        input(f"\n   >> Google doesn't have a Yes yet from: "
              f"{', '.join(sorted({p for p, _ in missing}))} ({len(missing)} replies missing).\n"
              "      Ask them to click Yes in each invitation email, then press Enter to check again...")
    problem("not every Yes reached Google, so the reply checks below won't mean much")
    return {eid: ct.calendar_get(eid)["responses"] for eid in event_ids}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check google_tools on the real Google account.")
    parser.add_argument("--attendee", action="append", default=[],
                        help="a teammate's real email (repeat for several teammates)")
    parser.add_argument("--events", type=int, default=3, help="how many test meetings (1-8, default 3)")
    args = parser.parse_args()
    people, n = args.attendee, max(1, min(args.events, len(START_HOURS)))

    owner = owner_email()
    day = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    window = (day.strftime("%Y-%m-%dT%H:%M"), (day + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
    print(f"Owner account: {owner}. Test day: {day:%a %b %d}. Meetings: {n}. "
          f"Teammates: {', '.join(people) or 'none'}. Google emails guests: {google_api._notify()}\n")

    # 1. create the meetings the normal way (invitations go out)
    meetings = []
    for i, hour in enumerate(START_HOURS[:n], start=1):
        start = day.replace(hour=hour)
        fields = {"summary": f"Agent-Ctrl-Z test {i}/{n}",
                  "start": start.strftime("%Y-%m-%dT%H:%M"),
                  "end": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                  "attendees": [owner] + people,
                  "description": (f"Test meeting from {owner} for our Agent-Ctrl-Z project "
                                  "(two-sided undo). It will be deleted after the test."),
                  "location": "https://github.com/aryansharma0326/Agent-Ctrl-Z"}
        meetings.append(invite_normally(fields) if people else ct.calendar_create(fields)["after"])
    ids = [m["id"] for m in meetings]
    check(set(ids) <= {e["id"] for e in ct.calendar_list(*window)},
          f"created {n} meetings tomorrow: " + ", ".join(f"{m['summary']} {m['start'][11:16]}" for m in meetings))
    pause(people, f"check your INBOX: {n} emails 'Invitation: Agent-Ctrl-Z test ...' from {owner}.\n"
                  "      Click Yes in each. Then open Google Calendar: are all of them there tomorrow?")
    replies_before = wait_for_yes(people, ids) if people else {}

    # 2. the "agent" moves meeting 1 by an hour, then our undo moves it back
    first = meetings[0]
    later = (datetime.fromisoformat(first["start"]) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    moved = ct.calendar_update(first["id"], {"start": later})
    check(moved["after"]["start"][11:16] == later[11:16], f"agent moved '{first['summary']}' to {later[11:16]}")
    pause(people, f"Google Calendar: has '{first['summary']}' moved one hour later? "
                  "Did an email 'Updated invitation' arrive?")
    r = restore_calendar(action("calendar.update_event", first["id"], moved, people, "Move my first meeting"))
    check(r.success and ct.calendar_get(first["id"])["start"][11:16] == first["start"][11:16],
          f"undo of the move: {r.details}")
    pause(people, f"has '{first['summary']}' moved back to its original time? Another email?")

    # 3. the "agent" clears the whole day
    cancelled = {eid: ct.calendar_cancel(eid) for eid in ids}
    check(all(ct.calendar_get(eid) is None for eid in ids) and not
          ({e["id"] for e in ct.calendar_list(*window)} & set(ids)),
          f"agent cleared the day: all {n} meetings cancelled")
    pause(people, f"Google Calendar: have all {n} test meetings disappeared? "
                  f"Did {n} emails 'Canceled event: ...' arrive?")

    # 4. our undo puts every meeting back with its own id
    for m in meetings:
        r = restore_calendar(action("calendar.cancel_event", m["id"], cancelled[m["id"]], people,
                                    "Clear my day"))
        check(r.success and r.new_target_id is None and ct.calendar_get(m["id"]) is not None,
              f"restored '{m['summary']}' with the same id")
    pause(people, f"Google Calendar: are all {n} meetings back at their times, still showing your Yes?\n"
                  "      Which emails arrived this time (if any)?")
    for eid in ids:
        now = ct.calendar_get(eid)["responses"]
        for p in people:
            check(now.get(p) == replies_before[eid].get(p),
                  f"{p}'s reply on {ct.calendar_get(eid)['summary']}: before "
                  f"{replies_before[eid].get(p)}, after restore {now.get(p)}")

    # 5. an email, then the correction (to the teammates, or to the owner if there are none)
    to = people or [owner]
    sent = email_tools.email_send(to, "Agent-Ctrl-Z test: team update",
                                  f"This is a test email from {owner}'s AI assistant for our project.",
                                  hold_seconds=0)
    pause(people, "check your INBOX: did 'Agent-Ctrl-Z test: team update' arrive?")
    r = undo_email(action("email.send", sent["after"]["id"], sent, to, "Email the team"))
    check(r.success and r.fix_type == "email_correction", f"undo_email: {r.details}")
    pause(people, "open that same email conversation: is there now a second email\n"
                  "      'Please ignore my earlier email ... sent it to you by mistake' inside it?")
    if not people:
        print(f"     Check {owner}'s inbox: the test email and the correction should be one conversation.")

    # 6. clean up quietly (no more emails)
    for eid in ids:
        delete_quietly(eid)
    ok(f"cleaned up: {n} test meetings deleted (no emails sent for this)")

    bad = [text for passed, text in results if not passed]
    print("\nDone." + (" Everything passed." if not bad else f" {len(bad)} problem(s):"))
    for text in bad:
        print(f"   - {text}")


if __name__ == "__main__":
    main()