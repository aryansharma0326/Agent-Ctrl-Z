"""OWNER: Visista
The owner's approval desk for high-blast-radius emails. Run from the repo root,
with the same settings as the server (e.g. USE_REAL_GOOGLE=1), so it sees the same outbox:

    python -m google_tools.approvals                  # list emails waiting for you
    python -m google_tools.approvals approve held_ab12cd34
    python -m google_tools.approvals deny held_ab12cd34

Approving only works from here (the owner's own computer), never from an email reply:
a reply could come from anyone, or be a prompt injection.
"""
from __future__ import annotations

import argparse
from datetime import datetime

from google_tools import email_tools
from google_tools.backend import backend_name
from google_tools.security import NEW_YORK, _clock


def _local(stamp: str) -> str:
    t = datetime.fromisoformat(stamp).astimezone(NEW_YORK)
    return f"{t:%a %b} {t.day}, {_clock(t)}"


def show_pending() -> None:
    pending = email_tools.pending_approvals()
    print(f"Emails waiting for your approval ({backend_name()}): {len(pending)}\n")
    for e in pending:
        blast = e["blast_radius"] or {}
        print(f"  {e['id']}   expires {_local(e['expires_at'])}")
        print(f"    To:      {', '.join(e['to'][:8])}{' ...' if len(e['to']) > 8 else ''} ({len(e['to'])})")
        print(f"    Subject: {e['subject']}")
        print(f"    Message: {e['body'].strip().replace(chr(10), ' ')[:160]}")
        print(f"    Why:     blast radius {blast.get('score')} ({blast.get('tier')}): "
              f"{', '.join(blast.get('reasons', []))}\n")
    if pending:
        print("Approve:  python -m google_tools.approvals approve <id>")
        print("Deny:     python -m google_tools.approvals deny <id>")


def main() -> None:
    parser = argparse.ArgumentParser(description="Approve or deny the agent's high-risk emails.")
    parser.add_argument("decision", nargs="?", choices=["approve", "deny"])
    parser.add_argument("email_id", nargs="?")
    args = parser.parse_args()
    if not args.decision:
        show_pending()
        return
    if not args.email_id:
        parser.error("give the email id, e.g. approve held_ab12cd34")
    if args.decision == "approve":
        try:
            sent = email_tools.approve_email(args.email_id)
        except ValueError as problem:
            raise SystemExit(f"[!!] {problem}")
        print(f"[ok] Approved and sent '{sent.get('subject')}' to {len(sent.get('to', []))} people.")
    else:
        if email_tools.deny_email(args.email_id):
            print(f"[ok] Denied: {args.email_id} will never be sent.")
        else:
            raise SystemExit(f"[!!] {args.email_id} isn't waiting for approval "
                             f"(it is {email_tools.email_status(args.email_id) or 'unknown'}).")


if __name__ == "__main__":
    main()