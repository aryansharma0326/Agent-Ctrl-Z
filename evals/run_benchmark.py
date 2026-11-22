"""Run all 15 scenarios through the Claude baseline and our group repair, then
score, compare, save and chart.

  python -m evals.run_benchmark --mock              # offline dry run (no API calls)
  python -m evals.run_benchmark                     # real Claude baseline (costs money):
                                                    #   Sonnet 5.5, effort medium, <= 8 turns
  python -m evals.run_benchmark --model claude-haiku-5-5 --effort off   # cheapest
  python -m evals.run_benchmark --prompt guided     # baseline told the rubric goals
  python -m evals.run_benchmark --only S04 S11      # a subset

Outputs: a comparison table in the terminal, evals/results.json and
evals/comparison_chart.png (failure rate by scenario category and by failure mode).

Fairness: both systems start from the same sandbox calendar, see the same action
record, and are scored by the same code from the same evidence (final calendar,
writes, notifications). Our system receives who flagged the action but never the
flag text; Claude receives the flag text, as a self-repairing agent would.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evals.claude_baseline import (USAGE_FIELDS, add_cost_flags, cost_line, load_scenarios,
                                   run_baseline)
from evals.sandbox import SandboxCalendar
from evals.scoring import DIMENSION_LABELS, DIMENSIONS, score
from repair.group_repair import apply_plan, plan_group_repair
from shared.models import ActionRecord

HERE = Path(__file__).resolve().parent
CATEGORY_LABELS = {
    "post_reaction_conflict": "Post-reaction conflict",
    "majority_accepted_reschedule": "Majority-accepted reschedule",
    "metadata_heavy": "Metadata-heavy event",
    "recurring_confusion": "Recurring vs. instance",
    "prompt_injection": "Prompt injection",
    "false_alarm": "False-alarm flag",
}
BASELINE_COLOR, OURS_COLOR = "#2a78d6", "#eb6834"     # categorical slots 1 and 2


# ------------------------------------------------------------------ our system

def run_ours(sc: dict) -> dict:
    state = sc["current_calendar_state"]
    ar = sc["action_record"]
    cal = SandboxCalendar(state["events"])
    action = ActionRecord(tool=ar["tool"], target_id=ar["target_id"], before=ar["before"],
                          after=ar["after"], affected_people=ar["affected_people"],
                          instruction=ar["instruction"])
    # Who flagged is structured, trusted data; the flag's free text is never passed in.
    current = {"owner": state["owner"], "now": state["now"],
               "flagged_by": sc["flag"]["flagged_by"] if sc["flag"] else []}
    plan = plan_group_repair(action, current, calendar=cal)
    result = apply_plan(plan, calendar=cal)
    scored = score(sc, cal.final_events(), cal.ops, cal.notifications,
                   explicit_notify=result.notify)
    return {"scenario": sc["id"], "system": "ours", "mode": "group_repair",
            "case": plan.case, "details": result.details, "success": result.success,
            "writes": cal.ops, **scored}


# ------------------------------------------------------------------ aggregation

def failure_rates(runs: list[dict], scenarios: list[dict]) -> dict:
    cat = {s["id"]: s["category"] for s in scenarios}
    by_cat: dict[str, list[bool]] = defaultdict(list)
    by_dim: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        by_cat[cat[r["scenario"]]].append(not r["passed"])
        for d in DIMENSIONS:
            p = r["dimensions"][d]["pass"]
            if p is not None:
                by_dim[d].append(not p)
    rate = lambda xs: (sum(xs) / len(xs)) if xs else None   # noqa: E731
    return {
        "overall": rate([not r["passed"] for r in runs]),
        "by_category": {c: {"rate": rate(v), "failed": sum(v), "n": len(v)}
                        for c, v in by_cat.items()},
        "by_dimension": {d: {"rate": rate(v), "failed": sum(v), "n": len(v)}
                         for d, v in by_dim.items()},
    }


# ------------------------------------------------------------------ terminal table

def _mark(p: Optional[bool]) -> str:
    return "  -  " if p is None else (" pass" if p else " FAIL")


def print_table(scenarios: list[dict], base: dict, ours: dict, base_rates: dict,
                our_rates: dict, baseline_name: str) -> None:
    short = ["dbl", "meta", "notif", "inj", "undo"]
    head = "".join(f"{s:>6}" for s in short)
    print(f"\n{'':4}{'':30}| {'BASELINE (' + baseline_name + ')':<31}| OURS (group_repair)")
    print(f"{'id':4}{'category':30}|{head} |{head}")
    print("-" * 4 + "-" * 30 + "+" + "-" * 31 + "+" + "-" * 31)
    for s in scenarios:
        b, o = base[s["id"]], ours[s["id"]]
        bc = "".join(f"{_mark(b['dimensions'][d]['pass']):>6}" for d in DIMENSIONS)
        oc = "".join(f"{_mark(o['dimensions'][d]['pass']):>6}" for d in DIMENSIONS)
        print(f"{s['id']:4}{CATEGORY_LABELS[s['category']][:29]:30}|{bc} |{oc}")

    print("\nFailure rate by failure mode (scenarios where the check applies)")
    for d in DIMENSIONS:
        b, o = base_rates["by_dimension"].get(d), our_rates["by_dimension"].get(d)
        print(f"  {DIMENSION_LABELS[d]:28} baseline {_pct(b):>14}   ours {_pct(o):>14}")
    print("\nFailure rate by scenario category (any check failed)")
    for c in CATEGORY_LABELS:
        b, o = base_rates["by_category"].get(c), our_rates["by_category"].get(c)
        print(f"  {CATEGORY_LABELS[c]:28} baseline {_pct(b):>14}   ours {_pct(o):>14}")
    print(f"\n  {'OVERALL':28} baseline {base_rates['overall']:>13.0%}    "
          f"ours {our_rates['overall']:>13.0%}")


def _pct(x: Optional[dict]) -> str:
    return "n/a" if not x or x["rate"] is None else f"{x['rate']:.0%} ({x['failed']}/{x['n']})"


# ------------------------------------------------------------------ chart

def make_chart(base_rates: dict, our_rates: dict, baseline_name: str, mock: bool,
               path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, ink2, grid, surface = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10.5,
                         "axes.edgecolor": grid, "axes.labelcolor": ink2,
                         "xtick.color": ink2, "ytick.color": ink})

    panels = [
        ("Failure rate by scenario category", list(CATEGORY_LABELS), CATEGORY_LABELS,
         "by_category"),
        ("Failure rate by failure mode", DIMENSIONS, DIMENSION_LABELS, "by_dimension"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2), facecolor=surface)
    for ax, (title, keys, labels, field) in zip(axes, panels):
        ax.set_facecolor(surface)
        keys = [k for k in keys if k in base_rates[field] or k in our_rates[field]]
        y = range(len(keys))
        h = 0.36
        for off, rates, color, name in ((-h / 2 - 0.01, base_rates, BASELINE_COLOR, baseline_name),
                                        (h / 2 + 0.01, our_rates, OURS_COLOR, "Ours (group repair)")):
            vals = [(rates[field].get(k) or {}).get("rate") or 0 for k in keys]
            ax.barh([i + off for i in y], vals, height=h, color=color, label=name, zorder=3)
            for i, (k, v) in enumerate(zip(keys, vals)):
                entry = rates[field].get(k) or {}
                ax.text(v + 0.015, i + off, f"{v:.0%} ({entry.get('failed', 0)}/{entry.get('n', 0)})",
                        va="center", fontsize=9, color=ink2)
        ax.set_yticks(list(y))
        ax.set_yticklabels([labels[k] for k in keys])
        ax.invert_yaxis()
        ax.set_xlim(0, 1.22)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.xaxis.grid(True, color=grid, linewidth=1, zorder=0)
        ax.tick_params(length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.set_title(title, loc="left", fontsize=12.5, color=ink, pad=10, fontweight="bold")
    handles, labels_ = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels_, loc="upper right", bbox_to_anchor=(0.99, 0.985),
               ncol=2, frameon=False, fontsize=10.5)

    sub = (f"Overall: {base_rates['overall']:.0%} of scenarios failed by the baseline vs "
           f"{our_rates['overall']:.0%} by ours (15 scenarios, lower is better)")
    if mock:
        sub += ("\nMOCK RUN: the baseline here is a scripted blind-revert policy, NOT Claude. "
                "Re-run without --mock for real results.")
    fig.suptitle("Undoing an agent's calendar mistake:\nself-repair vs. minimal-disruption "
                 "group repair", x=0.01, y=0.985, ha="left", va="top", fontsize=14.5, color=ink, fontweight="bold")
    fig.text(0.01, 0.865, sub, ha="left", va="top", fontsize=10,
             color="#b3261e" if mock else ink2)
    fig.tight_layout(rect=(0, 0, 1, 0.86 if mock else 0.89))
    fig.savefig(path, dpi=200, facecolor=surface)
    plt.close(fig)


# ------------------------------------------------------------------ main

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Claude self-repair vs. group repair benchmark")
    ap.add_argument("--mock", action="store_true",
                    help="offline: scripted blind-revert baseline instead of the Claude API")
    add_cost_flags(ap)
    ap.add_argument("--prompt", default="naive", choices=["naive", "guided"])
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--chart", default=str(HERE / "comparison_chart.png"))
    args = ap.parse_args(argv)

    scenarios = load_scenarios()
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]

    base_runs, our_runs = [], []
    for s in scenarios:
        print(f"running {s['id']} ({s['category']}) ...", flush=True)
        base_runs.append(run_baseline(s, mock=args.mock, model=args.model,
                                      effort=args.effort, variant=args.prompt,
                                      fallbacks=not args.no_fallbacks,
                                      max_turns=args.max_turns))
        if "usage" in base_runs[-1]:
            print(f"  {cost_line(base_runs[-1])}", flush=True)
        our_runs.append(run_ours(s))

    baseline_name = "Mock blind revert" if args.mock else f"Claude ({args.model}, {args.prompt})"
    base_rates, our_rates = failure_rates(base_runs, scenarios), failure_rates(our_runs, scenarios)
    print_table(scenarios, {r["scenario"]: r for r in base_runs},
                {r["scenario"]: r for r in our_runs}, base_rates, our_rates,
                "mock" if args.mock else args.model)

    cost = None
    if not args.mock:
        tok = {f: sum(r.get("usage", {}).get(f, 0) for r in base_runs) for f in USAGE_FIELDS}
        usd = sum(r.get("est_cost_usd") or 0 for r in base_runs)
        cost = {"usage": tok, "est_cost_usd": round(usd, 4),
                "api_calls": sum(r.get("api_calls", 0) for r in base_runs)}
        print(f"\nClaude usage: {cost['api_calls']} API calls, "
              + ", ".join(f"{k} {v:,}" for k, v in tok.items())
              + f"  ->  est. ${usd:.2f}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {"name": baseline_name, "mock": args.mock, "model": None if args.mock
                     else args.model, "effort": args.effort, "prompt_variant": args.prompt,
                     "max_turns": args.max_turns,
                     "fallbacks": not args.no_fallbacks and not args.mock, "cost": cost},
        "summary": {"baseline": base_rates, "ours": our_rates},
        "runs": {"baseline": base_runs, "ours": our_runs},
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    make_chart(base_rates, our_rates, baseline_name, args.mock, Path(args.chart))
    print(f"\nSaved {args.out}\nSaved {args.chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
