"""OWNER: Varuni. Tests for evals/flag_triage (the dataset and the runner, no network)."""
import json

from core import flag_triage as t
from evals.flag_triage import run

DATA = json.loads(run.DATA.read_text())


def test_every_flag_has_a_known_label_and_context():
    allowed = set(t.LABELS) | {t.SUSPICIOUS}
    ids = [f["id"] for f in DATA["flags"]]
    assert len(ids) == len(set(ids)) >= 40
    for f in DATA["flags"]:
        assert f["label"] in allowed and f["context"] in DATA["contexts"] and f["text"]


def test_dataset_covers_every_label_and_tricky_cases():
    labels = {f["label"] for f in DATA["flags"]}
    assert labels == set(t.LABELS) | {t.SUSPICIOUS}


def test_rules_run_scores_everything_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    res = run.evaluate(use_model=False)
    assert res["flags"] == len(DATA["flags"]) and res["model_answers_used"] == 0
    assert 0 <= res["decision_accuracy"] <= 1 and 0 <= res["label_accuracy"] <= 1
    assert "Decision accuracy" in run.report(res)


def test_model_run_counts_model_answers(monkeypatch):
    monkeypatch.setattr(t.llm, "ask_json", lambda s, u: {"label": "fine", "summary": "ok"})
    res = run.evaluate(use_model=True)
    guarded = sum(r["source"] == "guard" for r in res["rows"])
    assert res["model_answers_used"] == res["flags"] - guarded
    assert res["false_alarms"] == []            # a model that always says "fine" never alarms


def test_dotenv_never_overrides_the_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nGROQ_API_KEY='from-file'\nLLM_MODEL=from-file\n")
    monkeypatch.setenv("LLM_MODEL", "from-env")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    run.load_dotenv(env)
    import os
    assert os.environ["GROQ_API_KEY"] == "from-file" and os.environ["LLM_MODEL"] == "from-env"
