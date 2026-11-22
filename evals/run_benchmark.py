"""Run all 15 scenarios through a self-repair baseline and Agent-Ctrl-Z's group
repair, then score, compare, save and chart.

  python -m evals.run_benchmark --mock                       # offline dry run, no API
  python -m evals.run_benchmark                              # Gemini 3.8 Flash, free tier
  python -m evals.run_benchmark --provider groq              # gpt-oss-120b via Groq, free tier
  python -m evals.run_benchmark --provider huggingface       # Llama 3.3 70B via HF router
  python -m evals.run_benchmark --provider anthropic         # Claude Sonnet 5.5 (paid)
  python -m evals.run_benchmark --prompt guided              # baseline told the rubric goals
  python -m evals.run_benchmark --only S04 S11               # a subset

  # several baselines on one chart: save each run, then compare
  python -m evals.run_benchmark --out evals/results_gemini.json
  python -m evals.run_benchmark --provider groq --out evals/results_groq.json
  python -m evals.run_benchmark --compare evals/results_gemini.json evals/results_groq.json

Outputs: a comparison table in the terminal, evals/results.json (with the exact
provider and model) and evals/comparison_chart.png (failure rate by scenario
category and by failure mode, every baseline named in the legend).

Fairness: every system starts from the same sandbox calendar, sees the same action
record, and is scored by the same code from the same evidence (final calendar,
writes, notifications). Agent-Ctrl-Z receives who flagged the action but never the
flag text; the baseline model receives the flag text, as a self-repairing agent would.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evals.claude_baseline import (FREE_TIER_DELAY_S, USAGE_FIELDS, add_cost_flags, cost_line,
                                   load_scenarios, resolve_model, run_baseline)
from evals.providers import ProviderError
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
    print(f"\n{'':4}{'':30}| {('BASELINE ' + baseline_name)[:30]:<30}| AGENT-CTRL-Z")
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

# Agent-Ctrl-Z always keeps orange and is drawn first in each group; baselines take
# blue, aqua, yellow in that fixed order. This order passes the palette validator's
# colour-blind and normal-vision separation checks (a 4th baseline would not), and
# every bar carries a value label, which covers the low-contrast aqua/yellow.
BASELINE_COLORS = [BASELINE_COLOR, "#1baf7a", "#eda100"]
OURS_NAME = "Agent-Ctrl-Z (group repair)"
PROVIDER_LABELS = {"gemini": "Gemini", "groq": "Groq", "huggingface": "Hugging Face",
                   "anthropic": "Anthropic"}


def baseline_label(provider: Optional[str], model: Optional[str], prompt: str,
                   mock: bool) -> str:
    if mock:
        return "Mock blind revert (NOT a model)"
    return f"{PROVIDER_LABELS.get(provider or '', provider)} · {model} ({prompt} prompt)"


def make_chart(baselines: list[tuple[str, dict]], our_rates: dict, mock: bool,
               path: Path, n_scenarios: int) -> None:
    """Failure rates by category and by failure mode: one bar per baseline plus ours."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if len(baselines) > len(BASELINE_COLORS):
        raise ValueError(f"at most {len(BASELINE_COLORS)} baselines per chart")
    ink, ink2, grid, surface = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10.5,
                         "axes.edgecolor": grid, "axes.labelcolor": ink2,
                         "xtick.color": ink2, "ytick.color": ink})
    series = [(OURS_NAME, our_rates, OURS_COLOR)]
    series += [(name, rates, BASELINE_COLORS[i]) for i, (name, rates) in enumerate(baselines)]
    n = len(series)

    panels = [
        ("Failure rate by scenario category", list(CATEGORY_LABELS), CATEGORY_LABELS,
         "by_category"),
        ("Failure rate by failure mode", DIMENSIONS, DIMENSION_LABELS, "by_dimension"),
    ]
    legend_bottom = 0.85 + 0.215 * n + 0.05                # inches from the top
    header = legend_bottom + (0.35 if mock else 0) - 0.15  # tight_layout adds ~0.4in pad
    body = 4.0 + 0.55 * (n - 2)
    fig, axes = plt.subplots(1, 2, figsize=(14, header + body), facecolor=surface)
    h = 0.78 / n
    for ax, (title, keys, labels, field) in zip(axes, panels):
        ax.set_facecolor(surface)
        keys = [k for k in keys if any(k in r[field] for _, r, _ in series)]
        for j, (name, rates, color) in enumerate(series):
            off = (j - (n - 1) / 2) * h
            vals = [(rates[field].get(k) or {}).get("rate") or 0 for k in keys]
            ax.barh([i + off for i in range(len(keys))], vals, height=h * 0.94, color=color,
                    label=name, zorder=3)
            for i, (k, v) in enumerate(zip(keys, vals)):
                e = rates[field].get(k) or {}
                ax.text(v + 0.012, i + off, f"{v:.0%} ({e.get('failed', 0)}/{e.get('n', 0)})",
                        va="center", fontsize=8.5 if n > 2 else 9, color=ink2)
        ax.set_yticks(list(range(len(keys))))
        ax.set_yticklabels([labels[k] for k in keys])
        ax.invert_yaxis()
        ax.set_xlim(0, 1.24)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.xaxis.grid(True, color=grid, linewidth=1, zorder=0)
        ax.tick_params(length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.set_title(title, loc="left", fontsize=12.5, color=ink, pad=10, fontweight="bold")

    total_h = header + body
    y = lambda inches: 1 - inches / total_h            # noqa: E731  (inches from the top)
    fig.suptitle("Undoing an agent's calendar mistake: self-repair vs. Agent-Ctrl-Z",
                 x=0.01, y=y(0.2), ha="left", va="top", fontsize=14.5, color=ink,
                 fontweight="bold")
    fig.text(0.01, y(0.62), f"Share of {n_scenarios} scenarios failed (any rubric check), "
             "lower is better. Overall:", ha="left", va="top", fontsize=10, color=ink2)
    handles, _ = axes[0].get_legend_handles_labels()
    legend_labels = [f"{name}: {rates['overall']:.0%} failed" for name, rates, _ in series]
    fig.legend(handles, legend_labels, loc="upper left", bbox_to_anchor=(0.005, y(0.85)),
               ncol=1, frameon=False, fontsize=10.5, handlelength=1.2)
    if mock:
        fig.text(0.01, y(legend_bottom + 0.1), "MOCK RUN: the baseline is a scripted blind-revert "
                 "policy, NOT a model. Re-run without --mock for real results.",
                 ha="left", va="top", fontsize=10, color="#b3261e")
    fig.tight_layout(rect=(0, 0, 1, y(header)))
    fig.savefig(path, dpi=200, facecolor=surface)
    plt.close(fig)


# ------------------------------------------------------------------ compare saved runs

def compare(files: list[str], chart: Path) -> int:
    """Plot several saved results files side by side against Agent-Ctrl-Z."""
    loaded = [json.loads(Path(f).read_text()) for f in files]
    baselines, mock = [], False
    for f, d in zip(files, loaded):
        b = d["baseline"]
        mock = mock or b.get("mock", False)
        baselines.append((b.get("label") or b.get("name", f), d["summary"]["baseline"]))
    ours = loaded[0]["summary"]["ours"]
    n = len(loaded[0]["summary"].get("scored_scenarios") or loaded[0]["runs"]["ours"])
    for f, d in zip(files[1:], loaded[1:]):
        if d["summary"]["ours"]["overall"] != ours["overall"] or len(d["runs"]["ours"]) != n:
            print(f"warning: {f} has a different Agent-Ctrl-Z result or scenario set; "
                  f"using the one from {files[0]}")
    print(f"\n{'system':60} overall failure rate")
    for name, rates in [(OURS_NAME, ours)] + baselines:
        print(f"  {name[:58]:58} {rates['overall']:.0%}")
    make_chart(baselines, ours, mock, chart, n)
    print(f"\nSaved {chart}")
    return 0


# ------------------------------------------------------------------ main

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Self-repair baselines vs. Agent-Ctrl-Z benchmark")
    ap.add_argument("--mock", action="store_true",
                    help="offline: scripted blind-revert baseline instead of a model")
    add_cost_flags(ap)
    ap.add_argument("--prompt", default="naive", choices=["naive", "guided"])
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--chart", default=str(HERE / "comparison_chart.png"))
    ap.add_argument("--compare", nargs="+", metavar="RESULTS_JSON",
                    help="don't run anything: chart these saved results files side by side")
    args = ap.parse_args(argv)
    if args.compare:
        return compare(args.compare, Path(args.chart))

    model = None if args.mock else resolve_model(args.provider, args.model)
    delay = args.delay if args.delay is not None else (
        0.0 if args.mock or args.provider == "anthropic" else FREE_TIER_DELAY_S)
    scenarios = load_scenarios()
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]

    base_runs, our_runs = [], []
    for i, s in enumerate(scenarios):
        if i and delay:
            time.sleep(delay)                       # stay under free-tier RPM limits
        print(f"running {s['id']} ({s['category']}) ...", flush=True)
        try:
            base_runs.append(run_baseline(s, mock=args.mock, provider=args.provider,
                                          model=model, effort=args.effort,
                                          variant=args.prompt,
                                          fallbacks=not args.no_fallbacks,
                                          max_turns=args.max_turns))
        except ProviderError as exc:                # e.g. missing API key
            print(f"error: {exc}", file=sys.stderr)
            return 2
        r = base_runs[-1]
        if "usage" in r:
            print(f"  {cost_line(r)} · stop {str(r['stop_reason'])[:200]}", flush=True)
        if r["errored"] and r.get("api_calls", 0) == 0:
            # Nothing reached the model (bad key, unknown model, quota exhausted after
            # retries): stop rather than write results full of fake failures.
            print(f"\nAborting: {s['id']} failed before the model answered, so the setup "
                  "is likely wrong. No results were written.", file=sys.stderr)
            return 2
        our_runs.append(run_ours(s))

    errored = [r["scenario"] for r in base_runs if r["errored"]]
    if errored:
        print(f"\nExcluded from the rates (API failed mid-run): {', '.join(errored)}")
    scored = [s for s in scenarios if s["id"] not in errored]
    if not scored:
        print("Every scenario errored; no results were written.", file=sys.stderr)
        return 2

    label = baseline_label(args.provider, model, args.prompt, args.mock)
    keep = lambda runs: [r for r in runs if r["scenario"] not in errored]   # noqa: E731
    base_rates = failure_rates(keep(base_runs), scored)
    our_rates = failure_rates(keep(our_runs), scored)
    print_table(scored, {r["scenario"]: r for r in base_runs},
                {r["scenario"]: r for r in our_runs}, base_rates, our_rates,
                "mock" if args.mock else model)

    cost = None
    if not args.mock:
        tok = {f: sum(r.get("usage", {}).get(f, 0) for r in base_runs) for f in USAGE_FIELDS}
        costs = [r.get("est_cost_usd") for r in base_runs]
        usd = None if any(c is None for c in costs) else round(sum(costs), 4)
        cost = {"usage": tok, "est_cost_usd": usd,
                "note": base_runs[0].get("cost_note") if base_runs else None,
                "api_calls": sum(r.get("api_calls", 0) for r in base_runs)}
        print(f"\n{label} usage: {cost['api_calls']} API calls, "
              + ", ".join(f"{k} {v:,}" for k, v in tok.items())
              + "  ->  est. " + (f"${usd:.2f}" if usd is not None else "n/a")
              + (f" ({cost['note']})" if cost["note"] else ""))

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {"label": label, "name": label, "mock": args.mock,
                     "provider": None if args.mock else args.provider, "model": model,
                     "served_by": sorted({m for r in base_runs for m in r.get("served_by", [])}),
                     "effort": args.effort, "prompt_variant": args.prompt,
                     "max_turns": args.max_turns, "delay_s": delay,
                     "fallbacks": (args.provider == "anthropic" and not args.no_fallbacks
                                   and not args.mock),
                     "cost": cost},
        "summary": {"baseline": base_rates, "ours": our_rates,
                    "scored_scenarios": [s["id"] for s in scored],
                    "excluded_errored": errored},
        "runs": {"baseline": base_runs, "ours": our_runs},
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    make_chart([(label, base_rates)], our_rates, args.mock, Path(args.chart), len(scored))
    print(f"\nSaved {args.out}\nSaved {args.chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
