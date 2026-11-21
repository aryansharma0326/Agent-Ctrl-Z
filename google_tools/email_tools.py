"""OWNER: Visista
CALLED BY: Varuni's MCP server

from google_tools.backend import get_backend

PLACEHOLDER: right name, inputs and output shape, but nothing is sent.
TODO(Visista): build this for real (Gmail API + holding emails briefly before sending).
"""


def email_send(to: list[str], subject: str, body: str) -> dict:
    """Send (or hold) an email.
    Returns {"before": None, "after": {"id": ..., "to": [...], "subject": ..., "body": ...}}"""
    return {"before": None,
            "after": {"id": "msg_placeholder", "to": to, "subject": subject, "body": body}}
