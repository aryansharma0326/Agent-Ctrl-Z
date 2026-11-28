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


# ------------------------------------------------------------------ chart
# Same palette as the benchmark charts (validated there): our system is orange, every
# comparison is blue. Names are on the axis and every bar has a value label, so colour
# is never the only cue.
INK, INK2, MUTED, TRACK, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#ecebe7", "#fcfcfb"


@dataclass(frozen=True)
class Bar:
    label: str            # y-axis name (may be two lines)
    value: float          # 0..1, the good outcome: mistakes caught / scenarios clean
    headline: str         # first line next to the bar
    detail: str           # second, smaller line
    ours: bool


def _frac(a: int, b: int) -> str:
    return f"{a}/{b}"


def detection_bars(d: DetectionStage) -> list[Bar]:
    fa = f"{_frac(d.false_alarms_owner_only, d.correct_actions)} false alarms"
    fa2 = f"{_frac(d.false_alarms_two_sided, d.correct_actions)} false alarms"
    return [
        Bar("Owner alone\n(today's assistants)", d.catch_rate_owner_only,
            f"{d.catch_rate_owner_only:.0%} caught ({_frac(d.caught_owner_only, d.mistakes)})",
            f"{d.mistakes - d.caught_owner_only} mistakes missed · {fa}", ours=False),
        Bar("Two-sided\nreceipts + flags", d.catch_rate_two_sided,
            f"{d.catch_rate_two_sided:.0%} caught ({_frac(d.caught_two_sided, d.mistakes)})",
            f"{d.caught_two_sided - d.caught_owner_only} caught only by affected people · {fa2}",
            ours=True),
    ]


def repair_bars(r: RepairStage) -> list[Bar]:
    def bar(s: RepairSystem, label: str, ours: bool) -> Bar:
        return Bar(label, s.clean_rate,
                   f"{s.clean_rate:.0%} clean · {s.failure_rate:.0%} failed "
                   f"({_frac(s.failed, s.n)})",
                   f"{_frac(s.double_booked, s.double_booking_n)} double-booked · "
                   f"{_frac(s.injection_failed, s.injection_n)} injections obeyed", ours)
    return ([bar(b, f"{b.name}\nself-repair", False) for b in r.baselines]
            + [bar(r.ours, "Agent-Ctrl-Z\ngroup repair", True)])


def make_pipeline_chart(detection: DetectionStage, repair: RepairStage,
                        path: Path = OUT) -> Path:
    """Two independent stages side by side in one high-resolution figure."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    from evals.run_benchmark import BASELINE_COLOR, OURS_COLOR

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "xtick.color": INK2, "ytick.color": INK})
    panels = [
        ("Stage 1 · Mistake detection",
         f"{detection.cases} cases · {detection.actions} agent actions "
         f"({detection.mistakes} mistakes, {detection.correct_actions} correct)\n"
         "Share of the agent's mistakes that got caught (higher is better)",
         detection_bars(detection)),
        ("Stage 2 · Safe multi-party repair",
         f"{repair.scenarios} scenarios · {repair.max_turns}-turn cap · naive prompt\n"
         "Share repaired with no rubric failure (higher is better)",
         repair_bars(repair)),
    ]
    fig = plt.figure(figsize=(18, 7.6), facecolor=SURFACE)
    # Fixed layout in figure fractions: [left, bottom, width, height]. The space right
    # of each panel holds the value labels; both panels share one bar spacing.
    axes = [fig.add_axes((0.125, 0.17, 0.19, 0.47)),
            fig.add_axes((0.635, 0.17, 0.165, 0.47))]
    slots = max(len(bars) for _, _, bars in panels)
    for ax, (_, _, bars) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        ys = [i + (slots - len(bars)) / 2 for i in range(len(bars))]   # centred
        ax.barh(ys, [1.0] * len(bars), height=0.58, color=TRACK, zorder=1)
        ax.barh(ys, [b.value for b in bars], height=0.58, zorder=2,
                color=[OURS_COLOR if b.ours else BASELINE_COLOR for b in bars])
        for y, b in zip(ys, bars):
            ax.text(1.05, y - 0.13, b.headline, va="center", ha="left", fontsize=11.5,
                    color=INK, fontweight="bold" if b.ours else "normal", clip_on=False)
            ax.text(1.05, y + 0.2, b.detail, va="center", ha="left", fontsize=9.5,
                    color=INK2, clip_on=False)
        ax.set_yticks(ys)
        ax.set_yticklabels([b.label for b in bars], fontsize=11)
        for tick, b in zip(ax.get_yticklabels(), bars):
            tick.set_fontweight("bold" if b.ours else "normal")
        ax.set_xlim(0, 1)
        ax.set_ylim(slots - 0.45, -0.55)
        ax.set_xticks([0, 0.5, 1.0])
        ax.set_xticklabels(["0%", "50%", "100%"], fontsize=9.5)
        ax.tick_params(length=0, pad=10)
        for side in ax.spines.values():
            side.set_visible(False)

    # Each panel's title block starts where its y-axis names start, not at the bars.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for ax, (title, subtitle, _) in zip(axes, panels):
        left_px = min(t.get_window_extent(renderer).x0 for t in ax.get_yticklabels())
        x = fig.transFigure.inverted().transform((left_px, 0))[0]
        fig.text(x, 0.80, title, ha="left", va="top", fontsize=14, fontweight="bold",
                 color=INK)
        fig.text(x, 0.752, subtitle, ha="left", va="top", fontsize=10, color=INK2,
                 linespacing=1.45)

    fig.text(0.02, 0.955, "Agent-Ctrl-Z: catch the agent's mistakes, then repair them safely",
             ha="left", va="top", fontsize=18, fontweight="bold", color=INK)
    fig.text(0.02, 0.905, "Two independent evaluations on different scenario sets, shown side "
             "by side. Their rates are not multiplied into one end-to-end number.",
             ha="left", va="top", fontsize=11.5, color=INK2)
    fig.legend(handles=[Patch(color=OURS_COLOR, label="Our system"),
                        Patch(color=BASELINE_COLOR, label="Comparison")],
               loc="upper right", bbox_to_anchor=(0.985, 0.955), frameon=False, ncol=2,
               fontsize=10.5, handlelength=1.2)
    fig.text(0.02, 0.035,
             f"Stage 1: Varuni's two-sided evaluation; each person is simulated by an LLM at "
             f"temperature 0, and both reviewer models ({', '.join(map(short_model, detection.reviewers))}) "
             f"gave identical totals.\n"
             f"Stage 2: self-repair baselines run on Groq with the same calendar tools; "
             f"'clean' = every rubric check passed (no double-booking, details kept, right "
             f"people notified, injection resisted, nothing else undone).",
             ha="left", va="bottom", fontsize=9, color=MUTED, linespacing=1.5)
    path = Path(path)
    fig.savefig(path, dpi=220, facecolor=SURFACE)
    plt.close(fig)
    return path


def main() -> int:
    detection, repair = load_all()
    path = make_pipeline_chart(detection, repair)
    print(f"Stage 1 detection: owner alone {detection.catch_rate_owner_only:.0%}, "
          f"two-sided {detection.catch_rate_two_sided:.0%} "
          f"({detection.cases} cases, {detection.mistakes} mistakes)")
    for s in (*repair.baselines, repair.ours):
        print(f"Stage 2 repair: {s.name:14} {s.clean_rate:.0%} clean, "
              f"{s.failure_rate:.0%} failed ({s.failed}/{s.n})")
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
