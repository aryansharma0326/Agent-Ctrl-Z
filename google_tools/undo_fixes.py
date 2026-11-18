"""OWNER: Visista
CALLED BY: Varuni's undo router

PLACEHOLDERS: return the agreed UndoResult shape, but don't undo anything yet.
TODO(Visista): build exact restore + email undo for real.
"""
from shared.models import ActionRecord, UndoResult


def restore_calendar(action: ActionRecord) -> UndoResult:
    """Put a cancelled/changed event back exactly as it was.
    If Google gives the restored event a new id, put it in new_target_id."""
    return UndoResult(action_id=action.action_id, fix_type="exact_restore", success=False,
                      details="PLACEHOLDER: restore_calendar not built yet", notify=[])


def undo_email(action: ActionRecord) -> UndoResult:
    """Stop the email if it's still being held, otherwise send a correction."""
    return UndoResult(action_id=action.action_id, fix_type="email_correction", success=False,
                      details="PLACEHOLDER: undo_email not built yet", notify=[])
