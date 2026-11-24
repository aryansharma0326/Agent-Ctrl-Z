"""OWNER: Varuni

A tiny client for a hosted language model (Groq's OpenAI-compatible API),
standard library only, so the app needs no extra packages.

It is used where a model's judgement helps but must never be trusted blindly,
like reading what people wrote in a flag. So the rules are:
  - ask_json() never raises. No key, a network error, a timeout or a reply that
    isn't a JSON object all return None.
  - Callers always check what comes back and have a non-AI fallback, so the app
    works the same (just less smart) without a key.

Settings (environment variables):
  GROQ_API_KEY   no key -> ask_json() returns None and callers fall back
  LLM_MODEL      default openai/gpt-oss-120b
  LLM_TIMEOUT    seconds to wait for a reply, default 20
"""
from __future__ import annotations

import json
import os
import urllib.request

URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


def _key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


def available() -> bool:
    """True if a model can be asked (a key is set)."""
    return bool(_key())


def model_name() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("LLM_TIMEOUT", "20")))
    except ValueError:
        return 20.0


def ask_json(system: str, user: str) -> dict | None:
    """Ask the model for exactly one JSON object, deterministically (temperature 0).
    Returns the object, or None if there's no key or anything goes wrong."""
    key = _key()
    if not key:
        return None
    payload = {
        "model": model_name(),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    request = urllib.request.Request(
        URL, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "two-sided-undo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            reply = json.load(response)
        answer = json.loads(reply["choices"][0]["message"]["content"])
    except Exception:
        return None
    return answer if isinstance(answer, dict) else None
