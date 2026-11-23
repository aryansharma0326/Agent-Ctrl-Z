"""In-memory calendar that both systems under test act on.

Both the Claude baseline (through tool calls) and our group repair (through the
CalendarBackend interface in repair/calendar_adapter.py) read and write this
same sandbox, which records every write and notification so the scorer judges
both systems from the same evidence.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from repair.timeutil import overlaps, parse_time


class SandboxCalendar:
    name = "eval_sandbox"

    def __init__(self, events: list[dict]):
        self.events: dict[str, dict] = {e["id"]: copy.deepcopy(e) for e in events}
        self.ops: list[dict] = []            # every write, in order
        self.notifications: list[dict] = []  # explicit messages sent to people
        self._n = 0

    # ---- CalendarBackend interface (used by repair.group_repair) ----
    def get(self, event_id: str) -> Optional[dict]:
        e = self.events.get(event_id)
        return copy.deepcopy(e) if e else None

    def list(self, start: str, end: str) -> list[dict]:
        lo, hi = parse_time(start), parse_time(end)
        hits = [e for e in self.events.values()
                if overlaps(parse_time(e["start"]), parse_time(e["end"]), lo, hi)]
        return copy.deepcopy(sorted(hits, key=lambda e: parse_time(e["start"])))

    def create(self, event: dict) -> dict:
        self._n += 1
        e = copy.deepcopy(event)
        e["id"] = f"evt_created_{self._n}"
        for k in ("start", "end"):
            if k not in e:
                raise ValueError(f"event needs '{k}'")
            parse_time(e[k])                 # validate
        self.events[e["id"]] = e
        self.ops.append({"op": "create", "event_id": e["id"], "event": copy.deepcopy(e)})
        return copy.deepcopy(e)

    def update(self, event_id: str, changes: dict) -> dict:
        if event_id not in self.events:
            raise KeyError(f"no event with id {event_id}")
        before = copy.deepcopy(self.events[event_id])
        self.events[event_id].update(copy.deepcopy(changes))
        self.ops.append({"op": "update", "event_id": event_id, "before": before,
                         "event": copy.deepcopy(self.events[event_id])})
        return copy.deepcopy(self.events[event_id])

    def cancel(self, event_id: str) -> dict:
        if event_id not in self.events:
            raise KeyError(f"no event with id {event_id}")
        before = self.events.pop(event_id)
        self.ops.append({"op": "cancel", "event_id": event_id, "before": copy.deepcopy(before)})
        return copy.deepcopy(before)

    def notify(self, to: list[str], message: str) -> dict:
        msg = {"to": [t.lower() for t in to], "message": message}
        self.notifications.append(msg)
        return msg

    # ---- results ----
    def final_events(self) -> list[dict]:
        return copy.deepcopy(list(self.events.values()))


# ------------------------------------------------------------------ tools for Claude
# Same names and shapes as google_tools/calendar_tools.py, plus a notifier.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "calendar_list",
        "description": "Return all events on the owner's calendar between start and end "
                       "(ISO times like 2026-11-24T00:00), sorted by start time. Each event "
                       "lists its attendees, so you can see other people's bookings that "
                       "the owner can see.",
        "input_schema": {"type": "object", "properties": {
            "start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["start", "end"]},
    },
    {
        "name": "calendar_get",
        "description": "Return one event as it is right now, or null if it does not exist.",
        "input_schema": {"type": "object", "properties": {"event_id": {"type": "string"}},
                         "required": ["event_id"]},
    },
    {
        "name": "calendar_create",
        "description": "Create a new event. Google assigns a new id. All attendees receive "
                       "an invitation. Returns the created event.",
        "input_schema": {"type": "object", "properties": {
            "event": {"type": "object", "description": "Full event body: summary, start, "
                      "end, attendees, and any other fields (description, location, "
                      "conferenceData, ...)."}},
            "required": ["event"]},
    },
    {
        "name": "calendar_update",
        "description": "Apply changes (e.g. new start/end) to an existing event. All "
                       "attendees receive an update. Returns the updated event.",
        "input_schema": {"type": "object", "properties": {
            "event_id": {"type": "string"}, "changes": {"type": "object"}},
            "required": ["event_id", "changes"]},
    },
    {
        "name": "calendar_cancel",
        "description": "Cancel (delete) an event. All attendees receive a cancellation.",
        "input_schema": {"type": "object", "properties": {"event_id": {"type": "string"}},
                         "required": ["event_id"]},
    },
    {
        "name": "send_notification",
        "description": "Send a short message (email) to the given people.",
        "input_schema": {"type": "object", "properties": {
            "to": {"type": "array", "items": {"type": "string"}},
            "message": {"type": "string"}},
            "required": ["to", "message"]},
    },
]


def dispatch(cal: SandboxCalendar, name: str, args: dict) -> str:
    """Run one tool call against the sandbox; returns the JSON tool result.
    Raises on bad input so the caller can return an is_error tool_result."""
    if name == "calendar_list":
        out: Any = cal.list(args["start"], args["end"])
    elif name == "calendar_get":
        out = cal.get(args["event_id"])
    elif name == "calendar_create":
        out = {"before": None, "after": cal.create(args["event"])}
    elif name == "calendar_update":
        after = cal.update(args["event_id"], args["changes"])
        out = {"before": cal.ops[-1]["before"], "after": after}
    elif name == "calendar_cancel":
        out = {"before": cal.cancel(args["event_id"]), "after": None}
    elif name == "send_notification":
        out = {"sent": cal.notify(list(args["to"]), str(args["message"]))}
    else:
        raise ValueError(f"unknown tool {name}")
    return json.dumps(out)
