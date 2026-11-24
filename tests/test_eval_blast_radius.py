"""OWNER: Visista. Tests for the Adaptive Holding Pen evaluation (google_tools/eval_blast_radius.py)."""
from google_tools import eval_blast_radius as ev


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


def test_the_report_runs(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["eval"])
    ev.main()
    out = capsys.readouterr().out
    assert "## 3. Policy comparison" in out and "| adaptive |" in out and "No labels yet" in out