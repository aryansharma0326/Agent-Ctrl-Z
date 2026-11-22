"""OWNER: Varuni

MCP server: the only doorway Claude uses to act on Google Calendar and Gmail.

Every tool that changes something goes through the action log first, so each
action is recorded with a "before" copy and can be undone later. Read-only
tools (list/get) are not logged.

Run it:      python -m core.mcp_server
Connect it:  add this command as an MCP server in Claude Desktop / Claude Code.

The Google work itself is done by google_tools (Visista). Until those are built
they are placeholders, so write tools report "not found" instead of acting.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

from core.action_log import log_action
from google_tools import calendar_tools, email_tools
from shared import db

mcp = FastMCP("two-sided-undo")

_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """One shared database connection for the server."""
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


def _done(rec, message: str) -> dict[str, Any]:
    return {"ok": True, "action_id": rec.action_id, "message": message,
            "affected_people": rec.affected_people}


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message}


# ---------- read-only tools (not logged) ----------

@mcp.tool()
def list_events(start: str, end: str) -> list[dict]:
    """List calendar events between two times (ISO format with timezone,
    e.g. 2026-11-20T00:00:00-05:00). Always look before changing anything."""
    return calendar_tools.calendar_list(start, end)


@mcp.tool()
def get_event(event_id: str) -> dict | None:
    """Get one calendar event by id. Returns null if it doesn't exist or is cancelled."""
    return calendar_tools.calendar_get(event_id)


# ---------- tools that change things (always logged) ----------

@mcp.tool()
def cancel_event(event_id: str, instruction: str) -> dict:
    """Cancel a calendar event.
    instruction: the owner's original request, word for word (e.g. "Clear my Friday")."""
    result = calendar_tools.calendar_cancel(event_id)
    if not result.get("before"):
        return _fail(f"Event {event_id} not found, nothing was cancelled.")
    rec = log_action(get_conn(), "calendar.cancel_event", event_id, result, instruction)
    return _done(rec, f"Cancelled '{result['before'].get('summary', event_id)}'.")


@mcp.tool()
def update_event(event_id: str, changes: dict, instruction: str) -> dict:
    """Change a calendar event, e.g. {"start": ..., "end": ...} to move it.
    instruction: the owner's original request, word for word."""
    if not changes:
        return _fail("No changes given.")
    result = calendar_tools.calendar_update(event_id, changes)
    if not result.get("before"):
        return _fail(f"Event {event_id} not found, nothing was changed.")
    rec = log_action(get_conn(), "calendar.update_event", event_id, result, instruction)
    return _done(rec, f"Updated '{result['before'].get('summary', event_id)}'.")


@mcp.tool()
def create_event(event: dict, instruction: str) -> dict:
    """Create a calendar event: {"summary", "start", "end", "attendees", ...}.
    instruction: the owner's original request, word for word."""
    result = calendar_tools.calendar_create(event)
    created = result.get("after")
    if not created or not created.get("id"):
        return _fail("The event could not be created.")
    rec = log_action(get_conn(), "calendar.create_event", created["id"], result, instruction)
    return _done(rec, f"Created '{created.get('summary', created['id'])}'.")


@mcp.tool()
def send_email(to: list[str], subject: str, body: str, instruction: str) -> dict:
    """Send an email.
    instruction: the owner's original request, word for word."""
    if not to:
        return _fail("No recipients given.")
    result = email_tools.email_send(to, subject, body)
    sent = result.get("after")
    if not sent or not sent.get("id"):
        return _fail("The email could not be sent.")
    rec = log_action(get_conn(), "email.send", sent["id"], result, instruction)
    return _done(rec, f"Email '{subject}' sent to {len(to)} people.")


if __name__ == "__main__":
    mcp.run()
