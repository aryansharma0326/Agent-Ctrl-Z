"""OWNER: Visista
CALLED BY: Varuni's MCP server (the agent's emails) and her receipts / "it's fixed" notes

Step 6 (built): email_send HOLDS the agent's emails for a short time (60 seconds by
default) before they really go out. Once an email is in someone's inbox it can't be
pulled back, so the hold is the only true undo: undo_email stops a held email, and
sends a correction if it already went out.

  email_send(to, subject, body)                  -> held, goes out after the hold time
  email_send(to, subject, body, hold_seconds=0)  -> sent right away (receipts, notices)

Held emails wait in a small SQLite file, outbox.db (git ignores *.db):
  * next to the fake calendar file while using the fake calendar, so tests stay separate
  * in the repo folder when using real Google
  * or wherever the CTRLZ_OUTBOX setting points
Change the hold time with the CTRLZ_EMAIL_HOLD_SECONDS setting.

Something must send held emails when their time is up, by calling
release_held_emails() regularly:
  * run   python -m google_tools.email_tools   in its own terminal, or
  * call  start_background_sender()   once inside a long-running server
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google_tools.backend import backend_name, get_backend, using_real_google

DEFAULT_HOLD_SECONDS = 60
REPO = Path(__file__).resolve().parent.parent


# ---------- settings ----------
def hold_seconds_default() -> int:
    return int(os.environ.get("CTRLZ_EMAIL_HOLD_SECONDS", DEFAULT_HOLD_SECONDS))


def outbox_path() -> Path:
    if os.environ.get("CTRLZ_OUTBOX"):
        return Path(os.environ["CTRLZ_OUTBOX"])
    if using_real_google():
        return REPO / "outbox.db"
    from fake_data import fake_google
    return Path(fake_google.STATE_PATH).parent / "outbox.db"


# ---------- the outbox (held emails) ----------
@contextmanager
def _outbox():
    conn = sqlite3.connect(outbox_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS held_emails (
                            id TEXT PRIMARY KEY, to_json TEXT, subject TEXT, body TEXT,
                            send_at TEXT, status TEXT, message_json TEXT)""")
        yield conn
        conn.commit()
    finally:
        conn.close()   # always close: Windows keeps open files locked


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    """UTC time as text. Same format everywhere, so times can be compared as text."""
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _recipients(to) -> list[str]:
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, list) or not to:
        raise ValueError("to must be a list with at least one email address")
    for email in to:
        if not isinstance(email, str) or "@" not in email:
            raise ValueError(f"not an email address: {email!r}")
    return list(to)


# ---------- sending ----------
def email_send(to: list[str], subject: str, body: str, hold_seconds: int | None = None) -> dict:
    """Send an email, held for a short time first so it can still be stopped.
    Returns {"before": None, "after": <email>}, where <email> has id, status
    ("held" or "sent"), to, subject, body, and send_at for held emails."""
    to = _recipients(to)
    hold = hold_seconds_default() if hold_seconds is None else int(hold_seconds)
    if hold <= 0:
        message = get_backend().send_email(to, subject, body)
        return {"before": None, "after": {**message, "status": "sent"}}

    email_id = "held_" + uuid.uuid4().hex[:8]
    send_at = _stamp(_now() + timedelta(seconds=hold))
    with _outbox() as db:
        db.execute("INSERT INTO held_emails VALUES (?, ?, ?, ?, ?, 'held', NULL)",
                   (email_id, json.dumps(to), subject, body, send_at))
    return {"before": None, "after": {"id": email_id, "status": "held", "to": to,
                                      "subject": subject, "body": body, "send_at": send_at}}


def release_held_emails(now: datetime | None = None) -> list[dict]:
    """Send every held email whose time is up. Returns what was sent.
    Safe to call from several places at once: each email is sent only once."""
    now = now or _now()
    with _outbox() as db:
        due = db.execute("SELECT * FROM held_emails WHERE status = 'held' AND send_at <= ?",
                         (_stamp(now),)).fetchall()
    released = []
    for row in due:
        with _outbox() as db:   # claim it first, so nobody else sends it too
            claimed = db.execute("UPDATE held_emails SET status = 'sending' "
                                 "WHERE id = ? AND status = 'held'", (row["id"],)).rowcount
        if not claimed:
            continue
        try:
            message = get_backend().send_email(json.loads(row["to_json"]), row["subject"],
                                               row["body"])
        except Exception:
            with _outbox() as db:   # try again next time
                db.execute("UPDATE held_emails SET status = 'held' WHERE id = ?", (row["id"],))
            continue
        with _outbox() as db:
            db.execute("UPDATE held_emails SET status = 'sent', message_json = ? WHERE id = ?",
                       (json.dumps(message), row["id"]))
        released.append({"id": row["id"], "message": message})
    return released


# ---------- looking up and stopping held emails (used by undo_email) ----------
def email_status(email_id: str) -> str | None:
    """"held", "sending", "sent" or "stopped"; None if it was never held
    (it was sent right away)."""
    with _outbox() as db:
        row = db.execute("SELECT status FROM held_emails WHERE id = ?", (email_id,)).fetchone()
    return row["status"] if row else None


def stop_held_email(email_id: str) -> bool:
    """Stop a held email so it never goes out. True if it was stopped in time."""
    with _outbox() as db:
        return db.execute("UPDATE held_emails SET status = 'stopped' "
                          "WHERE id = ? AND status = 'held'", (email_id,)).rowcount == 1


def sent_message(email_id: str) -> dict | None:
    """The real sent message (with its id and thread_id) for a held email that went out."""
    with _outbox() as db:
        row = db.execute("SELECT message_json FROM held_emails WHERE id = ?",
                         (email_id,)).fetchone()
    return json.loads(row["message_json"]) if row and row["message_json"] else None


# ---------- keeping the sender running ----------
def run_forever(every_seconds: float = 5, quiet: bool = False) -> None:
    """Send held emails when their time is up, forever (Ctrl+C to stop)."""
    while True:
        for sent in release_held_emails():
            if not quiet:
                m, n = sent["message"], len(sent["message"].get("to", []))
                print(f"sent {sent['id']}: '{m.get('subject')}' to {n} {'person' if n == 1 else 'people'}")
        time.sleep(every_seconds)


def start_background_sender(every_seconds: float = 5) -> threading.Thread:
    """Start run_forever in the background of a long-running program (e.g. the MCP server)."""
    t = threading.Thread(target=run_forever, args=(every_seconds, True), daemon=True,
                         name="held-email-sender")
    t.start()
    return t


if __name__ == "__main__":
    print(f"Sending held emails ({backend_name()}, outbox: {outbox_path()}). Ctrl+C to stop.")
    try:
        run_forever()
    except KeyboardInterrupt:
        pass