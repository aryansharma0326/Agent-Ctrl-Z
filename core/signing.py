"""OWNER: Varuni

Signing for links and approvals, so they can't be forged or edited.
Used by flag links (receipts) and undo approval codes (MCP server).
Set SIGNING_SECRET (or the older FLAG_SECRET) in real use.
"""
from __future__ import annotations

import hashlib
import hmac
import os


def _secret() -> bytes:
    return (os.environ.get("SIGNING_SECRET") or os.environ.get("FLAG_SECRET")
            or "dev-only-secret-change-me").encode()


def sign(message: str) -> str:
    return hmac.new(_secret(), message.encode(), hashlib.sha256).hexdigest()[:32]


def matches(message: str, signature: str) -> bool:
    return hmac.compare_digest(sign(message), signature or "")
