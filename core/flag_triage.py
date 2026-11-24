"""OWNER: Varuni
CALLED BY: the Flag inbox page and the MCP server's flag_inbox tool

Flag triage: turn what affected people wrote into a decision the owner can make
in one click.

Without this, the owner gets a pile of free-text notes ("I can't make it, it's
Thanksgiving", "why was I removed?", "all good, thanks") and has to work out on
their own what they mean and what to do. Triage:
  1. labels each flag (conflict, wrong time, wrong people, ..., fine, unclear)
  2. groups the flags on each action and counts who reported a real problem
  3. recommends: undo it (with the exact dry-run plan from the undo router),
     keep it, or look yourself, and ranks actions by how urgent they are

Safety. Flag text comes from people outside the owner's control, so it is treated
as untrusted data everywhere:
  - text that looks like orders for the AI ("ignore previous instructions...")
    is never shown to a model at all; it is marked "suspicious"
  - the model may only pick a label from a fixed list; anything else is thrown
    away and simple rules are used instead
  - the model never decides what to do. The recommendation and the undo plan
    come from fixed code (the undo router), and the owner still approves
  - the agent (Claude) only ever sees labels and counts, never anyone's words
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.receipts import clean_flag_text, looks_suspicious

# What a flag can mean. Everything except "fine" and "unclear" is a real problem.
LABELS: dict[str, str] = {
    "conflict":      "it clashes with something else they have",
    "wrong_time":    "the time doesn't work for them",
    "wrong_people":  "the wrong people were added or removed",
    "wrong_details": "something in it is wrong (title, place, link, email text)",
    "bring_back":    "they still need it and want it put back",
    "fine":          "they say it's fine",
    "unclear":       "it's not clear what they mean",
}
SUSPICIOUS = "suspicious"   # set by the guard, never by a model
PROBLEMS = ("conflict", "wrong_time", "wrong_people", "wrong_details", "bring_back")
NOT_PROBLEMS = ("fine", "unclear", SUSPICIOUS)


@dataclass
class FlagLabel:
    label: str      # one of LABELS, or "suspicious"
    summary: str    # short, neutral, safe to show the owner
    source: str     # "guard", "rules" or "model"

    @property
    def is_problem(self) -> bool:
        return self.label in PROBLEMS


# ---------- rules (no AI needed) ----------
# Checked in this order; the first match wins. "fine" is checked last, so
# "thanks, but this clashes with my class" counts as a conflict.

def _any(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


RULES: list[tuple[str, re.Pattern]] = [
    ("wrong_people", _any(
        r"\b(removed|dropped|kicked|took) me\b", r"\badded me\b", r"\bwhy (am|was) i\b",
        r"\bnot (invited|on (the|this) (list|invite|team))\b", r"\bleft (me |us )?out\b",
        r"\bforgot (to (invite|add|include)\b|(me|us)\b)", r"\bwrong (person|people|recipient)",
        r"\bshouldn'?t (be|have been) (on|in|invited|included)\b",
        r"\bmeant for (someone|somebody|another)\b", r"\bnot (meant|for) me\b",
        r"\b(i'?m|i am) not (part of|in|on)\b")),
    ("conflict", _any(
        r"\bclash", r"\bconflict", r"\bdouble[- ]?book", r"\boverlap",
        r"\balready (have|has|booked|got|scheduled)\b", r"\bsame time as\b",
        r"\b(another|other) (meeting|class|lecture|lab|exam|appointment|commitment)\b",
        r"\b(appointment|dentist|doctor|exam|midterm|flight|interview|shift)\b",
        r"\b(thanksgiving|christmas|diwali|eid|holiday|vacation|out of office|ooo)\b")),
    ("wrong_time", _any(
        r"\b(can'?t|cannot|won'?t be able to|unable to) (make|do|attend|come|join)\b",
        r"\btoo (early|late)\b", r"\bwrong (time|day|date|week)\b", r"\btime ?zone\b",
        r"\b(doesn'?t|does not|won'?t) work for me\b", r"\b(earlier|later) (time|slot)\b",
        r"\bmove it (back|to)\b", r"\breschedul")),
    ("wrong_details", _any(
        r"\bwrong (title|name|room|place|location|link|address|attachment|info|subject|file)",
        r"\btypo\b", r"\bmisspel", r"\bincorrect\b", r"\bmeant to say\b",
        r"\bbroken link\b", r"\bwrong zoom\b", r"\b(link|room) (is|was) wrong\b")),
    ("bring_back", _any(
        r"\b(put|bring|add) (it|this|that) back\b", r"\brestore", r"\bundo\b",
        r"\buncancel", r"\bstill (need|want|happening|on)\b", r"\bwe need (it|this|that)\b",
        r"\bshouldn'?t (have been|be) (cancel|delet|remov|mov)",
        r"\bwhy (was|is|did) (it|this|that|our)?.{0,20}(cancel|delet|remov|disappear)",
        r"\bby mistake\b", r"\b(was|got) cancell?ed\b", r"\bdon'?t cancel\b")),
    ("fine", _any(
        r"\b(it'?s|that'?s|this is|all) (fine|good|ok(ay)?)\b", r"\bno (problem|worries|issue)",
        r"\bworks (for me|fine|great)\b", r"\blooks (good|right|fine)\b",
        r"^\s*(ok(ay)?|thanks?( you)?|thx|cool|great|perfect|sounds good)\W*$",
        r"\bthanks? for (the|letting)\b", r"\bnot a problem\b", r"\bfine by me\b")),
]


def rules_label(text: str) -> str:
    """Best guess from keywords alone. Fast, free and predictable."""
    for label, pattern in RULES:
        if pattern.search(text):
            return label
    return "unclear"


def classify_flag(text: object, what_happened: str = "") -> FlagLabel:
    """Label one flag using rules only. (A model is added on top of this later.)"""
    text = clean_flag_text(text)
    if looks_suspicious(text):
        return FlagLabel(SUSPICIOUS, "Looks like an attempt to give the AI orders, so no AI "
                                     "read it. Read it yourself if needed.", "guard")
    if not text:
        return FlagLabel("unclear", "They flagged it without writing a reason.", "rules")
    label = rules_label(text)
    return FlagLabel(label, f"Says {LABELS[label]}.", "rules")
