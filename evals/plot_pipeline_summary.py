"""OWNER: Aryan
One presentation chart for the whole pipeline, as two independent stages side by side:

  Stage 1  Mistake detection   Varuni's two-sided evaluation (evals/two_sided/):
                                does the owner alone catch the agent's mistakes, or
                                do affected people with receipts and flags catch more?
  Stage 2  Safe repair         our group-repair benchmark (evals/run_benchmark.py,
                                20-turn cap): once a mistake is caught, does the fix
                                avoid double-booking, injection and the rest?

The two stages use different scenario sets, so their rates are NEVER multiplied into
one end-to-end number (Varuni's review): they are shown next to each other.

  python -m evals.plot_pipeline_summary            # writes evals/chart_pipeline_side_by_side.png

Every number on the chart is read from the saved results files; nothing is typed in.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
TWO_SIDED_DIR = HERE / "two_sided"
REPAIR_FILES = [HERE / "results_groq_gptoss120b_20turns.json",
                HERE / "results_groq_qwen_20turns.json"]
REPAIR_MAX_TURNS = 20
OUT = HERE / "chart_pipeline_side_by_side.png"


class SummaryDataError(ValueError):
    """The results files can't support an honest chart (mock run, mismatched sets...)."""


# ------------------------------------------------------------------ Stage 1: detection
@dataclass(frozen=True)
class DetectionStage:
    cases: int                     # situations in cases.json
    actions: int                   # agent actions scored across those cases
    mistakes: int
    correct_actions: int
    caught_owner_only: int
    caught_two_sided: int
    false_alarms_owner_only: int
    false_alarms_two_sided: int
    reviewers: tuple[str, ...]     # reviewer models that produced these same totals

    @property
    def catch_rate_owner_only(self) -> float:
        return self.caught_owner_only / self.mistakes

    @property
    def catch_rate_two_sided(self) -> float:
        return self.caught_two_sided / self.mistakes


_DETECTION_TOTALS = ("actions_scored", "mistakes", "correct_actions", "caught_owner_only",
                     "caught_two_sided", "false_alarms_owner_only", "false_alarms_two_sided")


def load_detection(folder: Path = TWO_SIDED_DIR) -> DetectionStage:
    """Varuni's two-sided results. Every reviewer model's run must agree on the totals,
    so one pair of bars stands for all of them; offline smoke-test files are ignored."""
    cases = json.loads((folder / "cases.json").read_text())["cases"]
    files = sorted(p for p in (folder / "results").glob("results_*.json")
                   if "offline" not in p.name)
    if not files:
        raise SummaryDataError(f"no two-sided results in {folder / 'results'}")
    runs = [json.loads(p.read_text()) for p in files]
    for p, run in zip(files, runs):
        if run["summary"].get("actions_with_errors"):
            raise SummaryDataError(f"{p.name} has actions that errored; re-run it first")
    totals = [tuple(run["summary"][k] for k in _DETECTION_TOTALS) for run in runs]
    if len(set(totals)) != 1:
        raise SummaryDataError("the reviewer models disagree on the two-sided totals: "
                               + "; ".join(f"{r['reviewer']} {dict(zip(_DETECTION_TOTALS, t))}"
                                           for r, t in zip(runs, totals)))
    s = runs[0]["summary"]
    return DetectionStage(
        cases=len(cases), actions=s["actions_scored"], mistakes=s["mistakes"],
        correct_actions=s["correct_actions"], caught_owner_only=s["caught_owner_only"],
        caught_two_sided=s["caught_two_sided"],
        false_alarms_owner_only=s["false_alarms_owner_only"],
        false_alarms_two_sided=s["false_alarms_two_sided"],
        reviewers=tuple(r["reviewer"].split(":", 1)[-1] for r in runs))


# ------------------------------------------------------------------ Stage 2: repair
@dataclass(frozen=True)
class RepairSystem:
    name: str
    failed: int                    # scenarios with any rubric check failed
    n: int
    double_booked: int             # scenarios that created a new double-booking
    double_booking_n: int
    injection_failed: int          # injection scenarios where it obeyed the flag text
    injection_n: int

    @property
    def clean(self) -> int:
        return self.n - self.failed

    @property
    def failure_rate(self) -> float:
        return self.failed / self.n

    @property
    def clean_rate(self) -> float:
        return self.clean / self.n


@dataclass(frozen=True)
class RepairStage:
    scenarios: int
    max_turns: int
    baselines: tuple[RepairSystem, ...]
    ours: RepairSystem


def _system(name: str, runs: list[dict], scored: set[str]) -> RepairSystem:
    runs = [r for r in runs if r["scenario"] in scored]

    def dim(key: str) -> tuple[int, int]:
        checked = [r["dimensions"][key]["pass"] for r in runs
                   if r["dimensions"][key]["pass"] is not None]
        return sum(not p for p in checked), len(checked)

    dbl, dbl_n = dim("no_double_booking")
    inj, inj_n = dim("injection_resisted")
    return RepairSystem(name=name, failed=sum(not r["passed"] for r in runs), n=len(runs),
                        double_booked=dbl, double_booking_n=dbl_n,
                        injection_failed=inj, injection_n=inj_n)


def short_model(model: str) -> str:
    """'openai/gpt-oss-120b' -> 'gpt-oss-120b'"""
    return model.rsplit("/", 1)[-1]


def load_repair(files: Optional[list[Path]] = None,
                max_turns: int = REPAIR_MAX_TURNS) -> RepairStage:
    """Our real (non-mock) 20-turn benchmark runs. All files must cover the same full
    scenario set with no errored scenarios, and Agent-Ctrl-Z must score the same in each."""
    files = files or REPAIR_FILES
    baselines, ours, scenario_sets = [], [], []
    for path in files:
        d = json.loads(Path(path).read_text())
        b, s = d["baseline"], d["summary"]
        if b.get("mock"):
            raise SummaryDataError(f"{Path(path).name} is a mock run, not model results")
        if b.get("max_turns") != max_turns:
            raise SummaryDataError(f"{Path(path).name} used {b.get('max_turns')} turns, "
                                   f"expected {max_turns}")
        if s.get("excluded_errored"):
            raise SummaryDataError(f"{Path(path).name} has errored scenarios "
                                   f"{s['excluded_errored']}; re-run it first")
        scored = set(s["scored_scenarios"])
        scenario_sets.append(scored)
        baselines.append(_system(short_model(b["model"]), d["runs"]["baseline"], scored))
        ours.append(_system("Agent-Ctrl-Z", d["runs"]["ours"], scored))
    if any(sc != scenario_sets[0] for sc in scenario_sets):
        raise SummaryDataError("the repair results cover different scenario sets")
    if len(set(ours)) != 1:
        raise SummaryDataError("Agent-Ctrl-Z scored differently across the results files")
    return RepairStage(scenarios=len(scenario_sets[0]), max_turns=max_turns,
                       baselines=tuple(baselines), ours=ours[0])


def load_all() -> tuple[DetectionStage, RepairStage]:
    return load_detection(), load_repair()


if __name__ == "__main__":
    detection, repair = load_all()
    print(detection)
    print(repair)
