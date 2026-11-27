"""OWNER: Visista
Shows the 5 security checks working, on a throwaway fake calendar (no Google, nothing sent).
Run from the repo root:

    python -m google_tools.check_security

Good for the demo/slides: each check prints what triggered it and what the owner gets.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# a fresh fake calendar in a temp folder, before anything is imported
_folder = Path(tempfile.mkdtemp(prefix="ctrlz-security-"))
os.environ["FAKE_GOOGLE_STATE"] = str(_folder / "fake_google.json")
os.environ.pop("USE_REAL_GOOGLE", None)
os.environ.setdefault("CTRLZ_KNOWN_CONTACTS", "aryan@test.com,visista@test.com")

from fake_data import fake_google                           # noqa: E402
from fake_data.sample_data import FRIDAY_EVENTS, OWNER       # noqa: E402
from google_tools import calendar_tools as ct                # noqa: E402
from google_tools import security                            # noqa: E402


def show_alert(title: str) -> None:
    fake_google._load()
    alerts = [m for m in fake_google.SENT_EMAILS if m["subject"].startswith("Agent-Ctrl-Z security alert")]
    if not alerts:
        print(f"[!!] {title}: no alert was sent")
        return
    a = alerts[-1]
    print(f"[ok] {title}\n     email to {', '.join(a['to'])}: \"{a['subject']}\"")
    for line in a["body"].splitlines()[:6]:
        print(f"       | {line}")


def show_refusal(title: str, attempt) -> None:
    try:
        attempt()
    except ValueError as refused:
        print(f"[ok] {title}\n     refused: {refused}")
    else:
        print(f"[!!] {title}: it was NOT refused")


def main() -> None:
    fake_google.reset(FRIDAY_EVENTS)
    print(f"Fake calendar in {_folder}. Owner: {OWNER}. "
          f"Known contacts: {os.environ['CTRLZ_KNOWN_CONTACTS']}\n")

    print("1. Unknown guest")
    ct.calendar_create({"summary": "Quick chat", "start": "2026-11-23T10:00", "end": "2026-11-23T10:30",
                        "attendees": [OWNER, "aryan@test.com", "stranger@evil.example.org"]})
    show_alert("meeting made, owner alerted about the unknown guest")

    print("\n2. Too many new meetings in 30 minutes")
    for i in range(6):
        ct.calendar_create({"summary": f"Agent meeting {i + 1}", "start": f"2026-11-24T{9 + i:02d}:00",
                            "end": f"2026-11-24T{9 + i:02d}:30", "attendees": [OWNER],
                            "description": f"Booked by the agent, batch item {i + 1}"})
    show_alert(f"more than {security.max_new_meetings()} new meetings: owner gets the list to review")

    print("\n3. Too many changes to one meeting in 30 minutes")
    for i in range(security.max_changes() + 1):
        ct.calendar_update("evt_demo", {"description": f"edit number {i + 1}"})
    show_alert(f"more than {security.max_changes()} changes to 'CS 598 demo prep': reported")

    print("\n4. Collisions and the 15-minute gap (agent bookings)")
    show_refusal("booking on top of 'CS 598 demo prep'",
                 lambda: ct.calendar_create({"summary": "Sync", "start": "2026-11-20T15:30",
                                             "end": "2026-11-20T16:00"}, check_schedule=True))
    show_refusal("booking with only 10 minutes before it",
                 lambda: ct.calendar_create({"summary": "Prep", "start": "2026-11-20T14:00",
                                             "end": "2026-11-20T14:50"}, check_schedule=True))

    print("\n5. Links")
    show_refusal("a script link in the meeting notes",
                 lambda: ct.calendar_create({"summary": "x", "start": "2026-11-25T10:00",
                                             "end": "2026-11-25T10:30",
                                             "description": "join: javascript:steal()"}))
    os.environ["CTRLZ_OWNER_EMAIL"] = OWNER
    security.using_real_google = lambda: True          # pretend real Google, just for this rule
    os.environ["CTRLZ_CHECK_LINKS_ONLINE"] = "0"
    show_refusal("on real Google: a localhost link emailed to a teammate",
                 lambda: security.check_links(["Flag it here: http://localhost:8501/flag"],
                                              ["aryan@test.com"]))
    print("\nOn real Google every link is also opened once to check it answers "
          "(CTRLZ_CHECK_LINKS_ONLINE).")


if __name__ == "__main__":
    main()