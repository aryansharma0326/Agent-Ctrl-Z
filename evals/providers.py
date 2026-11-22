"""Free-tier baselines: Google Gemini and the Hugging Face Inference Router.

Each loop gets the same system prompt, task, calendar tools (evals/sandbox.py)
and turn cap as the Claude loop, so all baselines are scored identically. Only
the standard library is used (urllib), so no extra dependencies are needed.

  gemini       REST generateContent with function calling. Key: GEMINI_API_KEY.
  huggingface  OpenAI-compatible router (/v1/chat/completions) with native tool
               calling. Key: HF_TOKEN.

Both retry HTTP 429 / 5xx with backoff (honouring Retry-After or Gemini's
retryDelay hint), because free tiers have tight per-minute limits.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from evals.sandbox import TOOLS, SandboxCalendar, dispatch

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
HF_URL = "https://router.huggingface.co/v1/chat/completions"
DEFAULT_MODELS = {"gemini": "gemini-3.8-flash",
                  "huggingface": "meta-llama/Llama-3.3-70B-Instruct"}
KEY_VARS = {"gemini": "GEMINI_API_KEY", "huggingface": "HF_TOKEN"}
# Thinking budgets (tokens) per --effort level for gemini-2.5-* models only; -1 = model
# decides. Newer Gemini models (the gemini-3.8-flash default) use their own default.
GEMINI_THINKING = {"off": 0, "low": 1024, "medium": 4096, "high": -1, "xhigh": -1, "max": -1}
MAX_RETRIES = 6
TIMEOUT_S = 120

Post = Callable[[str, dict, dict], dict]


class ProviderError(RuntimeError):
    pass


def api_key(provider: str) -> str:
    var = KEY_VARS[provider]
    key = os.environ.get(var, "").strip()
    if not key:
        raise ProviderError(f"{var} is not set. Add a line '{var}=...' to the .env file "
                            f"at the repo root.")
    return key


# ------------------------------------------------------------------ HTTP

def _retry_after(err: urllib.error.HTTPError, body: str, attempt: int) -> float:
    hdr = err.headers.get("Retry-After") if err.headers else None
    if hdr:
        try:
            return min(float(hdr), 90.0)
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)   # Gemini's hint
    if m:
        return min(float(m.group(1)) + 1, 90.0)
    return min(2.0 * 2 ** attempt, 60.0)


def post_json(url: str, headers: dict, body: dict, log: Callable[[str], None] = print) -> dict:
    """POST JSON with retries on 429 and 5xx. Raises ProviderError otherwise."""
    data = json.dumps(body).encode()
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            text = err.read().decode(errors="replace")
            if (err.code == 429 or err.code >= 500) and attempt < MAX_RETRIES:
                wait = _retry_after(err, text, attempt)
                log(f"    HTTP {err.code}, retrying in {wait:.0f}s "
                    f"({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise ProviderError(f"HTTP {err.code}: {text[:500]}") from None
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < MAX_RETRIES:
                time.sleep(min(2.0 * 2 ** attempt, 30.0))
                continue
            raise ProviderError(f"connection error: {err}") from None
    raise ProviderError("unreachable")


# ------------------------------------------------------------------ shared tool step

def _run_tool(cal: SandboxCalendar, name: str, args: Any, calls: list[dict]) -> tuple[str, bool]:
    """Execute one tool call on the sandbox. Returns (result_json, is_error)."""
    entry: dict[str, Any] = {"tool": name, "input": args}
    try:
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
        out, err = dispatch(cal, name, dict(args)), False
    except Exception as exc:
        entry["error"] = str(exc)
        out, err = json.dumps({"error": str(exc)}), True
    calls.append(entry)
    return out, err


def _new_transcript() -> dict:
    return {"calls": [], "served_by": [], "final_text": "", "stop": None, "api_calls": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


# ------------------------------------------------------------------ Gemini

def gemini_tools() -> list[dict]:
    # parametersJsonSchema takes plain JSON Schema, which our free-form `event` /
    # `changes` objects need (the OpenAPI-subset `parameters` field rejects them).
    return [{"functionDeclarations": [
        {"name": t["name"], "description": t["description"],
         "parametersJsonSchema": t["input_schema"]} for t in TOOLS]}]


def run_gemini(system: str, task: str, cal: SandboxCalendar, model: str, effort: str,
               max_turns: int, post: Optional[Post] = None) -> dict:
    post = post or post_json
    key = api_key("gemini")
    tr = _new_transcript()
    contents: list[dict] = [{"role": "user", "parts": [{"text": task}]}]
    gen_cfg: dict[str, Any] = {"maxOutputTokens": 8192}
    if model.startswith("gemini-2.5"):
        gen_cfg["thinkingConfig"] = {"thinkingBudget": GEMINI_THINKING[effort]}

    for _ in range(max_turns):
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": gemini_tools(), "generationConfig": gen_cfg}
        try:
            resp = post(GEMINI_URL.format(model=model), {"x-goog-api-key": key}, body)
        except ProviderError as exc:
            tr["stop"] = f"api_error: {exc}"
            break
        tr["api_calls"] += 1
        tr["served_by"].append(resp.get("modelVersion", model))
        um = resp.get("usageMetadata", {})
        tr["usage"]["input_tokens"] += um.get("promptTokenCount", 0)
        tr["usage"]["output_tokens"] += um.get("candidatesTokenCount", 0) + \
            um.get("thoughtsTokenCount", 0)
        tr["usage"]["cache_read_input_tokens"] += um.get("cachedContentTokenCount", 0)

        cands = resp.get("candidates") or []
        if not cands:
            tr["stop"] = "blocked: " + json.dumps(resp.get("promptFeedback", {}))[:200]
            break
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts") or []
        fcalls = [p["functionCall"] for p in parts if "functionCall" in p]
        tr["stop"] = cand.get("finishReason")
        if not fcalls:
            tr["final_text"] = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            break
        # Send the model turn back verbatim: it carries thought signatures.
        contents.append({"role": "model", "parts": parts})
        replies = []
        for fc in fcalls:
            out, err = _run_tool(cal, fc.get("name", ""), fc.get("args", {}), tr["calls"])
            payload = json.loads(out)
            fr: dict[str, Any] = {"name": fc.get("name", ""),
                                  "response": payload if err else {"result": payload}}
            if "id" in fc:
                fr["id"] = fc["id"]
            replies.append({"functionResponse": fr})
        contents.append({"role": "user", "parts": replies})
        tr["stop"] = "tool_use"
    else:
        tr["stop"] = f"max_turns ({max_turns})"
    return tr


# ------------------------------------------------------------------ Hugging Face

def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in TOOLS]


def run_huggingface(system: str, task: str, cal: SandboxCalendar, model: str, effort: str,
                    max_turns: int, post: Optional[Post] = None) -> dict:
    post = post or post_json
    key = api_key("huggingface")
    tr = _new_transcript()
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": task}]

    for _ in range(max_turns):
        body = {"model": model, "messages": messages, "tools": openai_tools(),
                "tool_choice": "auto", "max_tokens": 4096}
        try:
            resp = post(HF_URL, {"Authorization": f"Bearer {key}"}, body)
        except ProviderError as exc:
            tr["stop"] = f"api_error: {exc}"
            break
        tr["api_calls"] += 1
        tr["served_by"].append(resp.get("model", model))
        u = resp.get("usage") or {}
        tr["usage"]["input_tokens"] += u.get("prompt_tokens", 0) or 0
        tr["usage"]["output_tokens"] += u.get("completion_tokens", 0) or 0

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        tr["stop"] = choice.get("finish_reason")
        if not tool_calls:
            tr["final_text"] = msg.get("content") or ""
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                args = None
                tr["calls"].append({"tool": fn.get("name", ""), "input": raw,
                                    "error": f"invalid JSON arguments: {exc}"})
                out = json.dumps({"error": f"invalid JSON arguments: {exc}"})
            if args is not None:
                out, _ = _run_tool(cal, fn.get("name", ""), args, tr["calls"])
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
        tr["stop"] = "tool_use"
    else:
        tr["stop"] = f"max_turns ({max_turns})"
    return tr


RUNNERS = {"gemini": run_gemini, "huggingface": run_huggingface}
