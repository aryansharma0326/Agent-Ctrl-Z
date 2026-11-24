"""OWNER: Visista
Blast-radius score for the agent's emails: the Adaptive Holding Pen (added Nov 24).

Once an email is in someone's inbox it can't be pulled back, so how long we hold it
should depend on how much damage it would do if it's a mistake. Before an agent email
is held, it gets a score from simple, explainable rules:

  factor           points                                            why it matters
  unknown          +2 per recipient not in your contacts (max +6)     strangers can't be asked to ignore it
  recipients       +1 for 2-5, +2 for 6-20, +3 for more than 20       more people reached
  links            +1 if the email contains a link                    links can be phishing or leak data
  sensitive        +2 if it mentions passwords, bank details,         private data can't be "unsent"
                      salary, grades, medical or confidential info
  burst            +2 if the agent already sent 3+ emails in the      an agent stuck in a loop, or one
                      last 30 minutes                                  big send split into small ones

  score 0-2  low     held 10 seconds  (never 0: an email can't be undone)
  score 3-5  medium  held 60 seconds  (CTRLZ_EMAIL_HOLD_SECONDS)
  score 6+   high    NOT sent until the owner approves it; expires unsent after 30 minutes

Only the agent's emails are scored (email_send without hold_seconds). Receipts, "it's
fixed" notes and alerts pass hold_seconds=0 and are never scored or held.

Settings (all optional): CTRLZ_BLAST_LOW_MAX (2), CTRLZ_BLAST_HIGH_MIN (6),
CTRLZ_HOLD_LOW_SECONDS (10), CTRLZ_EMAIL_HOLD_SECONDS (60), CTRLZ_APPROVAL_MINUTES (30),
CTRLZ_MAX_AGENT_EMAILS (3), plus CTRLZ_KNOWN_CONTACTS from security.py.

Every score is recorded in security.db (table email_scores) for the evaluation.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from google_tools import security

FACTORS = ("unknown", "recipients", "links", "sensitive", "burst")
TIERS = ("low", "medium", "high")
SENSITIVE_WORDS = ("password", "passcode", "ssn", "social security", "bank account",
                   "routing number", "credit card", "salary", "grades", "medical",
                   "diagnosis", "confidential")


# ---------- settings ----------
def low_max() -> int:
    return security._number("CTRLZ_BLAST_LOW_MAX", 2)


def high_min() -> int:
    return security._number("CTRLZ_BLAST_HIGH_MIN", 6)


def hold_for(tier: str) -> int:
    if tier == "low":
        return security._number("CTRLZ_HOLD_LOW_SECONDS", 10)
    return security._number("CTRLZ_EMAIL_HOLD_SECONDS", 60)


def approval_window() -> timedelta:
    return timedelta(minutes=security._number("CTRLZ_APPROVAL_MINUTES", 30))


def max_agent_emails() -> int:
    return security._number("CTRLZ_MAX_AGENT_EMAILS", 3)


# ---------- the score ----------
def tier_for(score: int, low: int | None = None, high: int | None = None) -> str:
    low = low_max() if low is None else low
    high = high_min() if high is None else high
    return "low" if score <= low else "high" if score >= high else "medium"


def _recent_agent_emails(now: datetime) -> int:
    since = security._stamp(now - security.window())
    with _scores_db() as db:
        return db.execute("SELECT COUNT(*) FROM email_scores WHERE at >= ?", (since,)).fetchone()[0]


def factors(to: list[str], subject: str, body: str, *, recent_emails: int = 0,
            owner: str | None = None, known: set[str] | None = None) -> list[tuple[str, int, str]]:
    """Each factor as (name, points, reason). Points can be 0."""
    owner = (owner or security.owner_email()).lower()
    known = {k.lower() for k in (known if known is not None else security.known_contacts())} | {owner}
    people = [p for p in dict.fromkeys(e.strip().lower() for e in to) if p != owner]
    unknown = [p for p in people if p not in known]
    text = f"{subject}\n{body}"
    links = security.find_links(text)
    sensitive = [w for w in SENSITIVE_WORDS if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE)]
    n = len(people)
    return [
        ("unknown", min(2 * len(unknown), 6),
         f"{len(unknown)} recipient{'s' if len(unknown) != 1 else ''} not in your contacts"),
        ("recipients", 0 if n <= 1 else 1 if n <= 5 else 2 if n <= 20 else 3,
         f"{n} recipient{'s' if n != 1 else ''}"),
        ("links", 1 if links else 0, f"{len(links)} link{'s' if len(links) != 1 else ''}"),
        ("sensitive", 2 if sensitive else 0,
         f"mentions {', '.join(sensitive)}" if sensitive else "no sensitive words"),
        ("burst", 2 if recent_emails >= max_agent_emails() else 0,
         f"{recent_emails} other agent email{'s' if recent_emails != 1 else ''} in the last "
         f"{int(security.window().total_seconds() // 60)} minutes"),
    ]


def score_email(to: list[str], subject: str, body: str, *, now: datetime | None = None,
                drop: tuple[str, ...] = (), low: int | None = None, high: int | None = None,
                recent_emails: int | None = None, owner: str | None = None,
                known: set[str] | None = None) -> dict:
    """The blast radius of one agent email: score, tier, hold time and the reasons.
    drop / low / high / recent_emails / owner / known are for the evaluation."""
    now = now or security._now()
    if recent_emails is None:
        recent_emails = _recent_agent_emails(now)
    parts = [f for f in factors(to, subject, body, recent_emails=recent_emails, owner=owner,
                                known=known) if f[0] not in drop]
    score = sum(points for _, points, _ in parts)
    tier = tier_for(score, low, high)
    return {"score": score, "tier": tier,
            "hold_seconds": None if tier == "high" else hold_for(tier),
            "needs_approval": tier == "high",
            "reasons": [f"{reason} (+{points})" for _, points, reason in parts if points],
            "points": {name: points for name, points, _ in parts}}


# ---------- record every score (for the evaluation and the dashboard) ----------
def _scores_db():
    return security._db()   # security.db, which has the email_scores table


def record(email_id: str, to: list[str], subject: str, result: dict) -> None:
    try:
        with _scores_db() as db:
            db.execute("INSERT INTO email_scores (at, email_id, score, tier, recipients, subject, "
                       "reasons_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (security._stamp(security._now()), email_id, result["score"], result["tier"],
                        len(to), subject, json.dumps(result["reasons"])))
    except Exception as exc:   # recording must never block an email
        print(f"[blast radius] couldn't record the score: {exc}")


def recent_scores(limit: int = 50) -> list[dict]:
    with _scores_db() as db:
        rows = db.execute("SELECT * FROM email_scores ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "reasons": json.loads(r["reasons_json"])} for r in rows]


def explain(result: dict) -> str:
    """One line for messages, e.g. "blast radius 9 (high): 30 recipients not in your contacts (+6), ..."."""
    reasons = ", ".join(result["reasons"]) or "nothing risky found"
    return f"blast radius {result['score']} ({result['tier']}): {reasons}"