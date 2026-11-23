"""OWNER: Varuni

Action log: records every action the AI agent takes, together with a copy of
how things looked BEFORE, so any action can be undone later.

Every tool in the MCP server calls log_action() right after it acts.
"""
from __future__ import annotations

import os
from typing import get_args

from shared import db
from shared.models import ActionRecord, Tool

DEFAULT_OWNER = "varuni@test.com"


def owner_email() -> str:
    """The person the agent works for. Set OWNER_EMAIL to change it."""
    return os.environ.get("OWNER_EMAIL", DEFAULT_OWNER).strip().lower()


def affected_people(result: dict, owner: str | None = None) -> list[str]:
    """Everyone an action touched, except the owner.

    Looks at the event BEFORE and AFTER the change, so someone who was removed
    from a meeting still counts as affected. For emails, uses the recipients.
    If an event has an "organizer", that is treated as the owner.
    """
    people: list[str] = []
    for copy in (result.get("before"), result.get("after")):
        if not copy:
            continue
        this_owner = (copy.get("organizer") or owner or owner_email()).strip().lower()
        for email in list(copy.get("attendees", [])) + list(copy.get("to", [])):
            email = email.strip().lower()
            if email and email != this_owner and email not in people:
                people.append(email)
    return people


def log_action(conn, tool: str, target_id: str, result: dict, instruction: str,
               affected: list[str] | None = None) -> ActionRecord:
    """Save one agent action to the database and return its record.

    result: what the Google tool returned, {"before": ..., "after": ...}
    affected: who to notify; worked out from result if not given
    """
    if tool not in get_args(Tool):
        raise ValueError(f"Unknown tool {tool!r}; expected one of {get_args(Tool)}")
    if "before" not in result or "after" not in result:
        raise ValueError("result must have 'before' and 'after' keys")

    record = ActionRecord(
        tool=tool,
        target_id=target_id,
        before=result["before"],
        after=result["after"],
        affected_people=affected if affected is not None else affected_people(result),
        instruction=instruction,
    )
    db.save_action(conn, record)
    return record
