"""OWNER: Aryan
Blast radius for the agent's calendar changes: the calendar half of the escalation
policy. It complements Visista's email scorer (google_tools/blast_radius.py).

A calendar mistake spreads to other people's days: a cancelled meeting with outside
guests, a recurring series moved an hour before it starts. Each calendar mutation
(cancel, move, edit, create) gets a score from simple, explainable rules:

  factor      points                                              why it matters
  external    +2 per attendee outside your domain and contacts     outsiders can't be asked
                                                                     to ignore it
  attendees   +1 for 2-5, +2 for 6-20, +3 for more than 20          more calendars touched
  imminence   +2 if it starts within 24 hours, +3 within 2 hours    no time to notice and undo
              (+3 while it is in progress)
  recurring   +2 if it is a recurring series or one occurrence      one change can hit many
              of one                                                 occurrences
  accepted    +2 if anyone other than you already accepted          people already planned
                                                                     around it

The total maps to the same three thresholds Visista's email holding pen uses
(google_tools.blast_radius.tier_for: CTRLZ_BLAST_LOW_MAX=2, CTRLZ_BLAST_HIGH_MIN=6):

  tier          score     email tier   what happens
  act_now       0-2       low          execute immediately (a calendar change can be
                                       undone, unlike a sent email, so no hold)
  hold_briefly  3-5       medium       held 60 seconds so it can be stopped
  ask_owner     6+        high         not done until the owner confirms it

A solo event (nobody on it but the owner) is always act_now: no one else is affected.

The score is deterministic: the same event, action and `now` always give the same
answer. Nothing here calls an API, writes anything or reads free text written by
other people (Flag.reason_text is never an input).

Event shapes: both the fake calendar's ("start": "2026-11-20T15:00", "attendees":
[emails], "responses": {email: status}) and the Google API's ("start": {"dateTime":
...}, "attendees": [{"email", "responseStatus"}]) are accepted. Times without a
timezone are New York time, as in fake_data/sample_data.py.

Which event to pass: the event as it was before the change for cancel / move / edit,
and the new event for create.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

ACTION_TYPES = ("cancel", "move", "edit", "create")
FACTORS = ("external", "attendees", "imminence", "recurring", "accepted")
ACT_NOW, HOLD_BRIEFLY, ASK_OWNER = "act_now", "hold_briefly", "ask_owner"
TIERS = (ACT_NOW, HOLD_BRIEFLY, ASK_OWNER)
EMAIL_TIER = {"low": ACT_NOW, "medium": HOLD_BRIEFLY, "high": ASK_OWNER}
HOLD_SECONDS = {ACT_NOW: 0, HOLD_BRIEFLY: 60, ASK_OWNER: None}   # None = wait for owner
LOCAL_TZ = ZoneInfo("America/New_York")      # zone for times written without one
SOON = timedelta(hours=24)
VERY_SOON = timedelta(hours=2)
# Sharing one of these with the owner doesn't make someone a colleague.
PUBLIC_DOMAINS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                            "live.com", "yahoo.com", "icloud.com", "me.com", "aol.com",
                            "proton.me", "protonmail.com"})


# ------------------------------------------------------------------ the result
@dataclass(frozen=True)
class BlastRadiusResult:
    """How far a calendar change would spread if it is a mistake, and what to do about it."""
    score: int
    tier: str                          # act_now | hold_briefly | ask_owner
    hold_seconds: Optional[int]        # 0, 60, or None = only after the owner confirms
    reasons: list[str] = field(default_factory=list)   # every factor that added points

    @property
    def needs_approval(self) -> bool:
        return self.tier == ASK_OWNER

    def explain(self) -> str:
        """One line, e.g. "blast radius 7 (ask_owner): 3 attendees (+1), ..."."""
        return (f"blast radius {self.score} ({self.tier}): "
                f"{', '.join(self.reasons) or 'nothing risky found'}")

    def to_dict(self) -> dict:
        return {"score": self.score, "tier": self.tier, "hold_seconds": self.hold_seconds,
                "needs_approval": self.needs_approval, "reasons": list(self.reasons)}


def tier_for(score: int, low: Optional[int] = None, high: Optional[int] = None) -> str:
    """Same cut-offs as the email holding pen, so the two policies can't drift apart."""
    from google_tools.blast_radius import tier_for as email_tier_for   # read-only reuse
    return EMAIL_TIER[email_tier_for(score, low, high)]


# ------------------------------------------------------------------ inputs
def _owner_and_contacts(owner: Optional[str],
                        known: Optional[set[str]]) -> tuple[str, set[str]]:
    """Defaults come from the same settings the email scorer uses
    (CTRLZ_OWNER_EMAIL, CTRLZ_KNOWN_CONTACTS)."""
    if owner is None or known is None:
        from google_tools import security          # read-only use of Visista's settings
        owner = security.owner_email() if owner is None else owner
        known = security.known_contacts() if known is None else known
    owner = owner.strip().lower()
    return owner, {k.strip().lower() for k in known} | {owner}


def _domain(email: str) -> str:
    return email.rpartition("@")[2]


def _attendees(event: dict) -> list[str]:
    """Unique attendee emails, lower-cased, skipping rooms and other resources."""
    out: list[str] = []
    for a in event.get("attendees") or []:
        if isinstance(a, dict):
            if a.get("resource"):
                continue
            a = a.get("email", "")
        email = str(a).strip().lower()
        if email and email not in out:
            out.append(email)
    return out


def _responses(event: dict) -> dict[str, str]:
    """{email: responseStatus} from either event shape."""
    out = {str(k).strip().lower(): str(v) for k, v in (event.get("responses") or {}).items()}
    for a in event.get("attendees") or []:
        if isinstance(a, dict) and a.get("email") and a.get("responseStatus"):
            out[a["email"].strip().lower()] = str(a["responseStatus"])
    return out


def _as_aware(value: Any) -> Optional[datetime]:
    """An event time or `now` as a timezone-aware datetime (None if missing/unreadable)."""
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("dateTime"):
            value = value["dateTime"]
        elif value.get("date"):
            try:
                day = datetime.fromisoformat(str(value["date"])).date()
            except ValueError:
                return None
            return datetime.combine(day, time(0), tzinfo=LOCAL_TZ)
        else:
            return None
    if not isinstance(value, datetime):
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _until(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return _plural(minutes, "minute")
    hours = minutes // 60
    return _plural(hours, "hour")


# ------------------------------------------------------------------ the factors
def calendar_factors(event: dict, action_type: str, now: Any = None, *,
                     owner: Optional[str] = None,
                     known: Optional[set[str]] = None) -> list[tuple[str, int, str]]:
    """Each factor as (name, points, reason). Points can be 0."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"action_type must be one of {', '.join(ACTION_TYPES)}, "
                         f"not {action_type!r}")
    owner, known = _owner_and_contacts(owner, known)
    now = _as_aware(now) or datetime.now(timezone.utc)

    people = _attendees(event) or [owner]          # no guest list = just the owner
    home = _domain(owner)
    external = [p for p in people if p not in known
                and (home in PUBLIC_DOMAINS or _domain(p) != home)]
    n = len(people)

    start, end = _as_aware(event.get("start")), _as_aware(event.get("end"))
    if start is None:
        imminence = (0, "start time unknown")
    elif end is not None and end <= now:
        imminence = (0, "already over")
    elif start <= now:
        imminence = (3, "in progress now")
    elif start - now <= VERY_SOON:
        imminence = (3, f"starts in {_until(start - now)} (within 2 hours)")
    elif start - now <= SOON:
        imminence = (2, f"starts in {_until(start - now)} (within 24 hours)")
    else:
        imminence = (0, f"starts in {_plural((start - now).days, 'day')}")

    recurring = bool(event.get("recurrence") or event.get("recurringEventId"))
    accepted = sorted(p for p, status in _responses(event).items()
                      if status == "accepted" and p != owner)

    return [
        ("external", 2 * len(external),
         f"{_plural(len(external), 'attendee')} outside your domain and contacts"),
        ("attendees", 0 if n <= 1 else 1 if n <= 5 else 2 if n <= 20 else 3,
         _plural(n, "attendee")),
        ("imminence", *imminence),
        ("recurring", 2 if recurring else 0,
         ("occurrence of a recurring series" if event.get("recurringEventId")
          else "recurring series") if recurring else "one-off event"),
        ("accepted", 2 if accepted else 0,
         f"{len(accepted)} {'person' if len(accepted) == 1 else 'people'} already accepted"
         if accepted
         else "nobody has accepted yet"),
    ]


def score_calendar_action(event: dict, action_type: str, now: Any = None, *,
                          owner: Optional[str] = None,
                          known: Optional[set[str]] = None) -> BlastRadiusResult:
    """Blast radius of one calendar mutation: score, tier, hold time and reasons.

    event        the event before the change (cancel / move / edit) or the new event
                 (create), in either event shape
    action_type  "cancel" | "move" | "edit" | "create"
    now          when the change happens (datetime or ISO string; default: now, UTC).
                 Naive values are New York time.
    owner/known  the owner's email and known contacts; default to CTRLZ_OWNER_EMAIL
                 and CTRLZ_KNOWN_CONTACTS, like the email scorer
    """
    parts = calendar_factors(event, action_type, now, owner=owner, known=known)
    score = sum(points for _, points, _ in parts)
    reasons = [f"{reason} (+{points})" for _, points, reason in parts if points]
    owner_email, _ = _owner_and_contacts(owner, known)
    if set(_attendees(event)) <= {owner_email}:
        tier = ACT_NOW
        reasons.append("only you are on this event, so nobody else is affected")
    else:
        tier = tier_for(score)
    return BlastRadiusResult(score=score, tier=tier, hold_seconds=HOLD_SECONDS[tier],
                             reasons=reasons)
