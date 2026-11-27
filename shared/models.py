"""Shared data formats the whole team agreed on.

Everyone imports these instead of inventing their own dictionaries, so our
code fits together on merge day.

  ActionRecord -> written by Varuni's action log
  Flag         -> written by Varuni's flag handling, read by Aryan's dashboard
  UndoResult   -> returned by Visista's and Aryan's fixers, read by Varuni's undo router
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional
import uuid

# Allowed values (keep these in sync with the team chat)
Tool = Literal[
    "calendar.cancel_event",
    "calendar.update_event",
    "calendar.create_event",
    "email.send",
]
Status = Literal["done", "flagged", "undone", "undo_failed"]
FixType = Literal["exact_restore", "email_correction", "email_unsent", "group_repair"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class ActionRecord:
    """One thing the AI agent did."""
    tool: str
    target_id: str                      # Google event id or email id
    before: Optional[dict[str, Any]]    # copy before the change (None for create/send)
    after: Optional[dict[str, Any]]     # copy after the change (None for cancel/delete)
    affected_people: list[str]
    instruction: str                    # what the owner asked, e.g. "Clear my Friday"
    status: str = "done"
    action_id: str = field(default_factory=lambda: new_id("act"))
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ActionRecord":
        return cls(**d)


@dataclass
class Flag:
    """An affected person saying "this looks wrong"."""
    action_id: str
    flagged_by: str
    reason_text: str                    # UNTRUSTED: never pass to an agent as instructions
    flag_id: str = field(default_factory=lambda: new_id("flag"))
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Flag":
        return cls(**d)


@dataclass
class UndoResult:
    """What a fixer (Visista or Aryan) reports back after trying to undo."""
    action_id: str
    fix_type: str
    success: bool
    details: str
    notify: list[str]                   # who should get the "it's fixed" message
    new_target_id: Optional[str] = None # set if restore created a NEW Google id
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UndoResult":
        return cls(**d)