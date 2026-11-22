"""OWNER: Varuni. Tests for the two-sided evaluation (evals/two_sided/).

No API calls here: the AI reviewer's HTTP is faked, and the offline reviewer
checks the pipeline. These prove the evaluation is FAIR and WIRED correctly,
not what the real numbers are.
"""
import json

import pytest

from evals.two_sided import reviewers as rv
from evals.two_sided import run

DOC = run.load_cases()


def test_cases_are_well_formed():
    ids = [a["id"] for c in DOC["cases"] for a in c["actions"]]
    assert len(ids) == len(set(ids)) == 18
    assert sum(a["is_mistake"] for c in DOC["cases"] for a in c["actions"]) == 10
    for c in DOC["cases"]:
        for a in c["actions"]:
            assert a["tool"] in ("calendar.cancel_event", "calendar.update_event",
                                 "calendar.create_event", "email.send")
            assert (a["before"] or a["after"])["id"]


def test_every_affected_person_has_private_context():
    result = run.evaluate(rv.OfflineReviewer())
    assert not [r for r in result["rows"] if r["errors"]]


class Recorder:
    """Captures every prompt a reviewer would be sent."""
    label = "recorder"

    def __init__(self):
        self.prompts = []

    def owner(self, ctx, request, lines):
        self.prompts.append(rv.owner_prompt(ctx, request, lines))
        return {n: (False, "") for n in range(1, len(lines) + 1)}

    def person(self, ctx, body):
        self.prompts.append(rv.person_prompt(ctx, body))
        return False, ""


def test_reviewers_never_see_the_answer_key():
    rec = Recorder()
    run.evaluate(rec)
    assert rec.prompts
    notes = [a["ground_truth_note"] for c in DOC["cases"] for a in c["actions"]
             if a.get("ground_truth_note")]
    assert len(notes) == 10
    for prompt in rec.prompts:
        assert "is_mistake" not in prompt and "ground_truth" not in prompt
        assert not any(note in prompt for note in notes), prompt[:200]


def test_owner_never_sees_other_peoples_private_context():
    rec = Recorder()
    run.evaluate(rec)
    owner_prompts = [p for p in rec.prompts if "activity log" in p]
    private = [p["context"] for c in DOC["cases"] for p in c["people"].values()]
    for prompt in owner_prompts:
        assert not any(ctx in prompt for ctx in private)


def test_flags_go_through_the_real_app_path():
    result = run.evaluate(rv.OfflineReviewer())
    row = next(r for r in result["rows"] if r["action"] == "c03a1")
    assert row["flags_saved_in_app"] == 4 and row["status_in_app"] == "flagged"
    calm = next(r for r in result["rows"] if r["action"] == "c14a1")
    assert calm["flags_saved_in_app"] == 0 and calm["status_in_app"] == "done"


def test_scoring_counts_catches_false_alarms_and_leaves_out_errors():
    rows = [
        {"is_mistake": True, "owner_flag": True, "anyone_flagged": True, "category": "a", "errors": []},
        {"is_mistake": True, "owner_flag": False, "anyone_flagged": True, "category": "a", "errors": []},
        {"is_mistake": True, "owner_flag": False, "anyone_flagged": False, "category": "b", "errors": []},
        {"is_mistake": False, "owner_flag": False, "anyone_flagged": True, "category": "correct", "errors": []},
        {"is_mistake": True, "owner_flag": None, "anyone_flagged": False, "category": "b", "errors": ["timeout"]},
    ]
    s = run.score(rows)
    assert (s["caught_owner_only"], s["caught_two_sided"], s["mistakes"]) == (1, 2, 3)
    assert s["caught_only_by_affected_people"] == 1
    assert (s["false_alarms_owner_only"], s["false_alarms_two_sided"]) == (0, 1)
    assert s["actions_with_errors"] == 1 and s["catch_rate_two_sided_pct"] == 66.7


# ---------- the AI reviewer, with HTTP faked ----------

class FakeHTTP:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def __call__(self, req, timeout=None):
        self.sent.append(json.loads(req.data))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return json.dumps(reply).encode()
        return Resp()


def groq_reply(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(rv.time, "sleep", lambda s: None)
    return rv.LLMReviewer("groq", delay=0)


def test_llm_person_and_owner_answers_are_parsed(llm, monkeypatch):
    http = FakeHTTP([groq_reply('{"flag": true, "reason": "I teach then"}'),
                     groq_reply('Sure! ```{"actions": [{"number": 1, "flag": false, "reason": "ok"}]}```')])
    monkeypatch.setattr(rv.urllib.request, "urlopen", http)
    assert llm.person("ctx", "receipt") == (True, "I teach then")
    assert llm.owner("ctx", "req", ["cancelled x"]) == {1: (False, "ok")}
    assert http.sent[0]["temperature"] == 0 and http.sent[0]["model"] == "openai/gpt-oss-120b"


def test_llm_retries_when_rate_limited(llm, monkeypatch):
    limited = rv.urllib.error.HTTPError("u", 429, "slow down", {}, None)
    limited.read = lambda: b"rate limited"
    http = FakeHTTP([limited, groq_reply('{"flag": false}')])
    monkeypatch.setattr(rv.urllib.request, "urlopen", http)
    assert llm.person("ctx", "receipt") == (False, "")
    assert len(http.sent) == 2


def test_llm_gives_up_cleanly_on_bad_answers(llm, monkeypatch):
    monkeypatch.setattr(rv.urllib.request, "urlopen", FakeHTTP([groq_reply("no json here")] * 4))
    with pytest.raises(rv.ReviewerError):
        llm.person("ctx", "receipt")


def test_owner_answer_missing_an_action_is_an_error(llm, monkeypatch):
    monkeypatch.setattr(rv.urllib.request, "urlopen", FakeHTTP(
        [groq_reply('{"actions": [{"number": 1, "flag": false}]}')]))
    with pytest.raises(rv.ReviewerError, match="skipped"):
        llm.owner("ctx", "req", ["a", "b"])


def test_missing_key_gives_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(rv, "load_dotenv", lambda *a: None)
    with pytest.raises(rv.ReviewerError, match="GROQ_API_KEY"):
        rv.LLMReviewer("groq")


def test_full_run_writes_results_and_chart(tmp_path):
    pytest.importorskip("matplotlib")
    run.main(["--offline", "--out", str(tmp_path)])
    assert (tmp_path / "results_offline_smoke_test.json").exists()
    assert (tmp_path / "chart_offline_smoke_test.png").stat().st_size > 10_000
