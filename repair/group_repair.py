"""OWNER: Aryan
CALLED BY: Varuni's undo router, when a plain restore would clash with
plans people made after the mistake (e.g. someone booked the freed slot)

PLACEHOLDER: returns the agreed UndoResult shape, but doesn't repair anything yet.
TODO(Aryan): build the least-disruptive group fix.
"""
from shared.models import ActionRecord, UndoResult


def group_repair(action: ActionRecord, current_state: dict) -> UndoResult:
    """current_state: what the calendar looks like now (from calendar_get / fake data)."""
    return UndoResult(action_id=action.action_id, fix_type="group_repair", success=False,
                      details="PLACEHOLDER: group_repair not built yet", notify=[])
