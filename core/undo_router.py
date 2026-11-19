"""OWNER: Varuni
CALLED BY: Aryan's owner dashboard (the Undo button)

PLACEHOLDER: returns the agreed UndoResult shape, but doesn't route or undo yet.
TODO(Varuni): build the real router.
"""
from shared.models import UndoResult


def route_undo(conn, action_id: str) -> UndoResult:
    """Undo one action and return what happened."""
    return UndoResult(action_id=action_id, fix_type="exact_restore", success=False,
                      details="PLACEHOLDER: route_undo not built yet", notify=[])
