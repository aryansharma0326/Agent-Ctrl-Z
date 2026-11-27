"""OWNER: Visista
Security checks on the agent's meetings and emails (added Nov 23).

  1. Unknown guests     a meeting invites someone who isn't a known contact
                        -> the invite still goes out, the owner gets a security email
  2. Too many meetings  more than 5 new meetings within 30 minutes (for any date)
                        -> the owner gets a security email listing every one of them
                           (title, time, guests, description) to review
  3. Tampering          more than 6 changes to the same meeting within 30 minutes
                        -> the owner gets a security email reporting that meeting
  4. Collisions         the agent books or moves a meeting on top of another one, or
                        with less than 15 minutes between them
                        -> refused, with the next free time suggested
                        (only when the caller asks: calendar_create(..., check_schedule=True);
                        undo and group repair are not checked)
  5. Links              every link in a meeting or email must be a real web link:
                        only http/https; on real Google, links that only work on this
                        computer (localhost) are refused when other people will get them,
                        and each link is opened once to check it really answers
                        -> refused, with the reason

Checks 1-3 only ever send alerts, they never block anything. Checks 4-5 refuse with a
ValueError whose message the agent can read and act on.

Settings (all optional):
  CTRLZ_KNOWN_CONTACTS          comma-separated emails the owner knows, e.g. "a@gmail.com,b@gmail.com"
  CTRLZ_OWNER_EMAIL             who gets the alerts (default: the logged-in account)
  CTRLZ_MAX_NEW_MEETINGS        default 5    (check 2: alert when MORE than this)
  CTRLZ_MAX_CHANGES             default 6    (check 3: alert when MORE than this)
  CTRLZ_WINDOW_MINUTES          default 30   (time window for checks 2 and 3)
  CTRLZ_MEETING_GAP_MINUTES     default 15   (check 4)
  CTRLZ_CHECK_LINKS_ONLINE      1 / 0        (check 5: open each link; default on with real Google)
  CTRLZ_ALLOW_LOCAL_LINKS       1            (check 5: allow localhost links, for rehearsals on one laptop)

Activity and alerts are kept in security.db, in the same folder as outbox.db.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from google_tools import email_tools
from google_tools.backend import using_real_google

NEW_YORK = timezone(timedelta(hours=-5))
CHANGE_KINDS = ("update", "cancel", "restore")


# ---------- settings ----------
def _number(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def max_new_meetings() -> int:
    return _number("CTRLZ_MAX_NEW_MEETINGS", 5)


def max_changes() -> int:
    return _number("CTRLZ_MAX_CHANGES", 6)


def window() -> timedelta:
    return timedelta(minutes=_number("CTRLZ_WINDOW_MINUTES", 30))


def meeting_gap() -> timedelta:
    return timedelta(minutes=_number("CTRLZ_MEETING_GAP_MINUTES", 15))


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes")


def owner_email() -> str:
    if os.environ.get("CTRLZ_OWNER_EMAIL"):
        return os.environ["CTRLZ_OWNER_EMAIL"].strip()
    if using_real_google():
        from google_tools import google_api
        return google_api.owner_email()
    from fake_data.sample_data import OWNER
    return OWNER


def known_contacts() -> set[str]:
    listed = os.environ.get("CTRLZ_KNOWN_CONTACTS", "")
    return {e.strip().lower() for e in listed.split(",") if e.strip()} | {owner_email().lower()}


# ---------- where activity and alerts are kept ----------
def db_path() -> Path:
    return email_tools.outbox_path().parent / "security.db"


@contextmanager
def _db():
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS activity (
                            id INTEGER PRIMARY KEY, at TEXT, kind TEXT, event_id TEXT, event_json TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
                            id INTEGER PRIMARY KEY, at TEXT, kind TEXT, key TEXT,
                            subject TEXT, body TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS email_scores (
                            id INTEGER PRIMARY KEY, at TEXT, email_id TEXT, score INTEGER,
                            tier TEXT, recipients INTEGER, subject TEXT, reasons_json TEXT)""")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=NEW_YORK)


def _clock(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _when(start, end) -> str:
    try:
        s = (start if isinstance(start, datetime) else _parse(start)).astimezone(NEW_YORK)
        e = (end if isinstance(end, datetime) else _parse(end)).astimezone(NEW_YORK)
    except (TypeError, ValueError):
        return "unknown time"
    return f"{s:%a %b} {s.day}, {_clock(s)}-{_clock(e)}"


def _describe(event: dict) -> str:
    notes = (event.get("description") or "").strip().replace("\n", " ")
    return (f"- '{event.get('summary', '(no title)')}', {_when(event.get('start'), event.get('end'))}\n"
            f"    guests: {', '.join(event.get('attendees') or []) or 'none'}\n"
            f"    description: {notes[:200] or '(none)'}")


# ---------- alerts to the owner (checks 1-3) ----------
def _alert(kind: str, key: str, subject: str, body: str) -> str | None:
    """Record an alert and email the owner. The same alert is sent at most once per window."""
    with _db() as db:
        if db.execute("SELECT 1 FROM alerts WHERE kind = ? AND key = ? AND at >= ?",
                      (kind, key, _stamp(_now() - window()))).fetchone():
            return None
        db.execute("INSERT INTO alerts (at, kind, key, subject, body) VALUES (?, ?, ?, ?, ?)",
                   (_stamp(_now()), kind, key, subject, body))
    try:
        email_tools.email_send([owner_email()], subject, body, hold_seconds=0)
    except Exception as exc:   # the alert is still recorded in security.db
        print(f"[security] couldn't email the alert: {exc}")
    return subject


def recent_alerts(limit: int = 20) -> list[dict]:
    """Newest alerts first, e.g. for the owner dashboard."""
    with _db() as db:
        rows = db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _unknown_guests(event: dict, guests: list[str]) -> list[str]:
    known = known_contacts()
    unknown = [g for g in guests if g.lower() not in known]
    if not unknown:
        return []
    hint = ("" if os.environ.get("CTRLZ_KNOWN_CONTACTS") else
            "\n(No known-contacts list is set yet, so every guest counts as unknown. "
            "Set CTRLZ_KNOWN_CONTACTS to the people you work with.)")
    alert = _alert("unknown_guests", f"{event.get('id')}:{','.join(sorted(unknown))}",
                   "Agent-Ctrl-Z security alert: unknown guests invited",
                   "A meeting on your calendar invites people who aren't on your known-contacts list:\n"
                   + "".join(f"  - {g}\n" for g in unknown)
                   + "\nThe invitation has gone out. If you didn't expect this, open the dashboard "
                     "and undo it.\n\n" + _describe(event) + hint)
    return [alert] if alert else []


def _too_many_new_meetings() -> list[str]:
    since = _stamp(_now() - window())
    with _db() as db:
        rows = db.execute("SELECT event_json FROM activity WHERE kind = 'create' AND at >= ? "
                          "ORDER BY id", (since,)).fetchall()
    if len(rows) <= max_new_meetings():
        return []
    events = [json.loads(r["event_json"]) for r in rows]
    minutes = int(window().total_seconds() // 60)
    alert = _alert("many_new_meetings", "",
                   f"Agent-Ctrl-Z security alert: {len(events)} meetings created in {minutes} minutes",
                   f"{len(events)} new meetings were created on your calendar in the last {minutes} "
                   f"minutes (the limit is {max_new_meetings()}). Please review them. If you didn't "
                   "schedule these, the agent may be out of control: undo them from the dashboard.\n\n"
                   + "\n".join(_describe(e) for e in events))
    return [alert] if alert else []


def _too_many_changes(event: dict) -> list[str]:
    since = _stamp(_now() - window())
    with _db() as db:
        rows = db.execute(f"SELECT at, kind FROM activity WHERE event_id = ? AND at >= ? "
                          f"AND kind IN ({','.join('?' * len(CHANGE_KINDS))}) ORDER BY id",
                          (event["id"], since, *CHANGE_KINDS)).fetchall()
    if len(rows) <= max_changes():
        return []
    minutes = int(window().total_seconds() // 60)
    history = "".join(f"  - {_clock(_parse(r['at']).astimezone(NEW_YORK))}: {r['kind']}\n" for r in rows)
    alert = _alert("many_changes", event["id"],
                   f"Agent-Ctrl-Z security alert: '{event.get('summary')}' changed {len(rows)} times "
                   f"in {minutes} minutes",
                   f"The meeting below was changed {len(rows)} times in the last {minutes} minutes "
                   f"(the limit is {max_changes()}). This can be spam, or someone tampering with it. "
                   "Please review it.\n\n" + _describe(event) + "\n\nChanges:\n" + history)
    return [alert] if alert else []


def after_change(kind: str, event: dict | None, new_guests: list[str] | None = None) -> list[str]:
    """Call after every change: kind is "create", "update", "cancel" or "restore".
    Records it and runs checks 1-3. Never raises: a broken check must not block the calendar."""
    if not event:
        return []
    try:
        with _db() as db:
            db.execute("INSERT INTO activity (at, kind, event_id, event_json) VALUES (?, ?, ?, ?)",
                       (_stamp(_now()), kind, event.get("id"),
                        json.dumps({k: v for k, v in event.items() if k != "raw"})))
        alerts = _too_many_new_meetings() if kind == "create" else _too_many_changes(event)
        if kind in ("create", "update"):
            guests = event.get("attendees", []) if new_guests is None else new_guests
            alerts += _unknown_guests(event, guests)
        return alerts
    except Exception as exc:
        print(f"[security] check skipped: {exc}")
        return []


# ---------- check 4: no collisions, keep a gap ----------
def _busy(start: datetime, end: datetime, ignore_id: str | None) -> list[tuple[datetime, datetime, dict]]:
    from google_tools.calendar_tools import calendar_list   # here, to avoid a circular import
    events = calendar_list(start.isoformat(), end.isoformat())
    return sorted(((_parse(e["start"]), _parse(e["end"]), e) for e in events if e["id"] != ignore_id),
                  key=lambda b: b[0])


def _next_free(start: datetime, length: timedelta, ignore_id: str | None,
               days: int = 7) -> datetime | None:
    """Earliest start, from the time asked for, in 15-minute steps between 8 AM and 8 PM,
    that keeps the gap on both sides. Meetings are sorted by start time and swept once."""
    gap, step = meeting_gap(), timedelta(minutes=15)
    busy = _busy(start - gap - length, start + timedelta(days=days) + length + gap, ignore_id)
    t = start
    while t < start + timedelta(days=days):
        local = t.astimezone(NEW_YORK)
        fits_day = local.hour >= 8 and (local + length).hour * 60 + (local + length).minute <= 20 * 60 \
            and (local + length).date() == local.date()
        if fits_day and not any(s < t + length + gap and t - gap < e for s, e, _ in busy):
            return t
        t += step
    return None


def check_schedule(start: str, end: str, ignore_id: str | None = None) -> None:
    """Refuse a time that overlaps another meeting or leaves less than the gap (15 min)."""
    s, e, gap = _parse(start), _parse(end), meeting_gap()
    clashes = _busy(s - gap, e + gap, ignore_id)
    if not clashes:
        return
    other = clashes[0][2]
    minutes = int(gap.total_seconds() // 60)
    free = _next_free(s, e - s, ignore_id)
    from google_tools.calendar_tools import _same_style
    suggestion = (f" The next free time is {_when(free, free + (e - s))} "
                  f"(start {_same_style(start, free)}, end {_same_style(start, free + (e - s))})."
                  if free else " There's no free time in the next 7 days.")
    raise ValueError(f"Can't book {_when(s, e)}: it collides with '{other.get('summary')}' "
                     f"({_when(other['start'], other['end'])}) or leaves less than {minutes} minutes "
                     f"between them.{suggestion}")


# ---------- check 5: links ----------
URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s<>\"']+")
SCRIPT_LINK = re.compile(r"\b(javascript|vbscript|data|file):", re.IGNORECASE)


def find_links(text: str) -> list[str]:
    return [u.rstrip(".,;:!?)]") for u in URL.findall(text or "")]


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified


@lru_cache(maxsize=256)
def _answers(url: str) -> int | None:
    """Open the link once: the HTTP status it answers with, or None if it doesn't answer."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": "Agent-Ctrl-Z link check"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            if method == "HEAD" and err.code in (403, 405):
                continue                      # some sites refuse HEAD: try a normal GET
            return err.code
        except Exception:
            return None
    return None


def check_links(texts: list[str], recipients: list[str]) -> None:
    """Refuse links that aren't real, working web links for the people who'll get them."""
    joined = "\n".join(t for t in texts if t)
    bad_script = SCRIPT_LINK.search(joined)
    if bad_script:
        raise ValueError(f"Link not allowed: '{bad_script.group(0)}' links can run code. "
                         "Only http and https links are allowed.")
    links = find_links(joined)
    if not links:
        return
    owner = owner_email().lower()
    others = [r for r in recipients if r.lower() != owner]
    for url in links:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in ("http", "https"):
            raise ValueError(f"Link not allowed: {url} (only http and https links are allowed)")
        if not host or ("." not in host and host != "localhost"):
            raise ValueError(f"Not a valid link: {url}")
        if _is_local(host):
            if others and using_real_google() and not _flag("CTRLZ_ALLOW_LOCAL_LINKS", False):
                raise ValueError(f"The link {url} only works on this computer, so {others[0]} "
                                 "can't open it. Use a public address (for receipts: set APP_URL), "
                                 "or set CTRLZ_ALLOW_LOCAL_LINKS=1 when rehearsing on one laptop.")
            continue
        if _flag("CTRLZ_CHECK_LINKS_ONLINE", using_real_google()):
            status = _answers(url)
            if status is None or status >= 400:
                raise ValueError(f"The link {url} doesn't open "
                                 f"({'no answer' if status is None else f'error {status}'}).")