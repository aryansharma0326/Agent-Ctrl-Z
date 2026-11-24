"""OWNER: Visista. Tests for the Adaptive Holding Pen evaluation (google_tools/eval_blast_radius.py)
and the blind labeling tool (google_tools/label_emails.py)."""
import json

import pytest

from google_tools import eval_blast_radius as ev
from google_tools import label_emails


def _row(id_, mistake, tier, hold, people=1, labels=None):
    return {"id": id_, "mistake": mistake, "tier": tier, "hold_seconds": hold, "people": people,
            "labels": labels or {}}


def test_every_scenario_scores_without_touching_any_database(tmp_path, monkeypatch):
    monkeypatch.setattr("google_tools.security.db_path", lambda: tmp_path / "must_not_exist.db")
    rows = ev.score_all(ev.load())
    assert len(rows) == 24 and {r["tier"] for r in rows} == {"low", "medium", "high"}
    assert not (tmp_path / "must_not_exist.db").exists()


def test_whole_class_mistake_needs_approval():
    m1 = next(r for r in ev.score_all(ev.load()) if r["id"] == "M1")
    assert m1["tier"] == "high" and m1["people"] == 32


def test_fleiss_kappa_and_majority():
    assert ev.fleiss_kappa([["low", "low", "low"], ["high", "high", "high"]]) == 1.0
    assert ev.fleiss_kappa([["low", "high"], ["high", "low"]]) < 0
    assert ev.majority(["low", "high"]) == "high"            # a tie takes the cautious label
    assert ev.majority(["low", "low", "high"]) == "low"


def test_label_report_finds_dangerous_under_escalation():
    rows = [_row("A", True, "low", 10, labels={"x": "ask", "y": "ask", "z": "hold"}),
            _row("B", False, "high", None, labels={"x": "now", "y": "now", "z": "now"}),
            _row("C", False, "low", 10, labels={"x": "now", "y": "now", "z": "now"})]
    report = ev.label_report(rows)
    assert report["under"] == ["A"] and report["over"] == ["B"] and round(report["accuracy"], 2) == 0.33


def test_simulation_rules():
    rows = [_row("M", True, "low", 10, people=30), _row("H", False, "low", 10)]
    assert ev.simulate(rows, "fixed 60 s", 30, 0.9, 120)["mistakes_reached"] == 0
    assert ev.simulate(rows, "fixed 60 s", 90, 0.9, 120)["people_reached"] == 30
    asked = ev.simulate(rows, "always ask", 90, 0.9, 120)
    assert (asked["mistakes_reached"], asked["approvals"], asked["avg_wait_harmless_s"]) == (0.1, 2, 120)


def test_the_report_runs(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "LABELS_DIR", tmp_path / "labels")
    monkeypatch.setattr("sys.argv", ["eval"])
    ev.main()
    out = capsys.readouterr().out
    assert "## 3. Policy comparison" in out and "| adaptive |" in out and "No labels yet" in out


# ---------- blind labeling ----------
@pytest.fixture
def labels_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "LABELS_DIR", tmp_path / "labels")
    return tmp_path / "labels"


def _answer(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))


def test_labeling_never_shows_mistakes_scores_or_descriptions(labels_dir, monkeypatch, capsys):
    _answer(monkeypatch, ["n"] * 24)
    monkeypatch.setattr("sys.argv", ["label", "--name", "varuni"])
    label_emails.main()
    out = capsys.readouterr().out
    for hint in ("mistake", "blast radius", "score", "meant for the team", "prompt injection",
                 "wrong date", "stuck in a loop"):
        assert hint not in out.lower()
    assert "(in your contacts)" in out and "Done: all 24 labeled" in out
    saved = json.loads((labels_dir / "varuni.json").read_text())
    assert len(saved) == 24 and set(saved.values()) == {"now"}


def test_answers_are_saved_one_by_one_and_you_can_carry_on_later(labels_dir, monkeypatch, capsys):
    _answer(monkeypatch, ["x", "n", "h", "q"])          # x is not a valid answer: asked again
    monkeypatch.setattr("sys.argv", ["label", "--name", "aryan"])
    label_emails.main()
    assert "Please type n, h or a." in capsys.readouterr().out
    assert sorted(json.loads((labels_dir / "aryan.json").read_text()).values()) == ["hold", "now"]
    _answer(monkeypatch, ["a"] * 22)
    label_emails.main()
    saved = json.loads((labels_dir / "aryan.json").read_text())
    assert len(saved) == 24 and list(saved.values()).count("ask") == 22


def test_each_person_gets_their_own_shuffled_order():
    data = ev.load()
    order = lambda name: [s["id"] for s in __import__("random").Random(name).sample(data["scenarios"], 24)]
    assert order("varuni") == order("varuni") and order("varuni") != order("aryan")


def test_the_loop_scenario_shows_the_context_the_owner_would_see():
    data = ev.load()
    m9 = next(s for s in data["scenarios"] if s["id"] == "M9")
    assert "already sent 3 emails in the last 30 minutes" in label_emails.describe(m9, data)


def test_the_evaluation_reads_everyones_label_files(labels_dir):
    labels_dir.mkdir()
    ids = [s["id"] for s in ev.load()["scenarios"]]
    for name in ("visista", "varuni", "aryan"):
        (labels_dir / f"{name}.json").write_text(json.dumps({i: "ask" if i.startswith("M") else "now" for i in ids}))
    report = ev.label_report(ev.score_all(ev.load()))
    assert report["labeled"] == 24 and report["kappa"] == 1.0
    assert "M2" in report["under"]          # a low-scoring mistake everyone wanted to be asked about