"""OWNER: Varuni. Tests for core/llm.py (no real network calls)."""
import io
import json

from core import llm


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply_with(content):
    body = {"choices": [{"message": {"content": content}}]}
    return lambda request, timeout: FakeResponse(json.dumps(body).encode())


def test_no_key_means_no_call(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    assert llm.available() is False
    assert llm.ask_json("system", "user") is None
    assert called == []


def test_returns_the_json_object(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    seen = {}

    def fake(request, timeout):
        seen["body"] = json.loads(request.data)
        seen["auth"] = request.get_header("Authorization")
        return reply_with('{"label": "conflict"}')(request, timeout)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    assert llm.ask_json("system", "user") == {"label": "conflict"}
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_bad_replies_and_errors_return_none(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    for content in ("not json", '["a list"]', '"a string"'):
        monkeypatch.setattr(llm.urllib.request, "urlopen", reply_with(content))
        assert llm.ask_json("s", "u") is None

    def boom(request, timeout):
        raise TimeoutError("slow")
    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    assert llm.ask_json("s", "u") is None


def test_model_can_be_changed(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert llm.model_name() == llm.DEFAULT_MODEL
    monkeypatch.setenv("LLM_MODEL", "qwen/qwen3-32b")
    assert llm.model_name() == "qwen/qwen3-32b"
