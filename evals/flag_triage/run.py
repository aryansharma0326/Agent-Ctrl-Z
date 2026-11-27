"""OWNER: Varuni
Flag triage evaluation: how well does triage read what people write?

Runs every flag in flags.json through core.flag_triage.classify_flag and compares
with the label a careful human gave it. Reports what matters for the owner:

  label accuracy      the exact label is right
  decision accuracy   "real problem" vs "not a problem" is right (this decides
                      whether the inbox recommends an undo)
  missed problems     a real problem that triage said was fine/unclear (worst case:
                      the owner might keep a mistake)
  false alarms        not a problem, but triage recommends looking at an undo
  injections          injected orders caught by the guard, and how many still led
                      to an undo recommendation

Run (from the repo root):
    python -m evals.flag_triage.run                 # keyword rules only, no key needed
    python -m evals.flag_triage.run --model         # the AI model (needs GROQ_API_KEY,
                                                    # read from the environment or .env)
    LLM_MODEL=qwen/qwen3-32b python -m evals.flag_triage.run --model
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

from core import flag_triage as t
from core import llm

HERE = Path(__file__).resolve().parent
DATA = HERE / "flags.json"
RESULTS = HERE / "results"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_problem(label: str) -> bool:
    return label in t.PROBLEMS


def evaluate(use_model: bool) -> dict:
    data = json.loads(DATA.read_text())
    rows = []
    for f in data["flags"]:
        what = data["contexts"][f["context"]]
        got = t.classify_flag(f["text"], what, use_model=use_model)
        rows.append({"id": f["id"], "text": f["text"], "expected": f["label"],
                     "got": got.label, "source": got.source})

    n = len(rows)
    injections = [r for r in rows if r["expected"] == t.SUSPICIOUS]
    missed = [r for r in rows if is_problem(r["expected"]) and not is_problem(r["got"])]
    alarms = [r for r in rows if not is_problem(r["expected"]) and is_problem(r["got"])]
    model_used = sum(r["source"] == "model" for r in rows)
    return {
        "reader": f"model:{llm.model_name()}" if use_model else "keyword rules",
        "flags": n,
        "model_answers_used": model_used,
        "label_accuracy": round(sum(r["expected"] == r["got"] for r in rows) / n, 3),
        "decision_accuracy": round(sum(is_problem(r["expected"]) == is_problem(r["got"])
                                       for r in rows) / n, 3),
        "real_problems": sum(is_problem(r["expected"]) for r in rows),
        "missed_problems": [r["id"] for r in missed],
        "false_alarms": [r["id"] for r in alarms],
        "injections": len(injections),
        "injections_caught_by_guard": sum(r["source"] == "guard" for r in injections),
        "injections_that_led_to_undo": [r["id"] for r in injections if is_problem(r["got"])],
        "confusion": dict(Counter(f"{r['expected']} -> {r['got']}" for r in rows
                                  if r["expected"] != r["got"])),
        "rows": rows,
    }


def report(res: dict) -> str:
    pct = lambda x: f"{x * 100:.0f}%"
    lines = [
        f"## Flag triage: {res['reader']}",
        "",
        f"- Flags: {res['flags']} (real problems: {res['real_problems']})",
        f"- Label accuracy: {pct(res['label_accuracy'])}",
        f"- Decision accuracy (problem or not): {pct(res['decision_accuracy'])}",
        f"- Missed problems: {len(res['missed_problems'])} {res['missed_problems']}",
        f"- False alarms: {len(res['false_alarms'])} {res['false_alarms']}",
        f"- Injections caught by the guard: {res['injections_caught_by_guard']}/{res['injections']}",
        f"- Injections that led to an undo recommendation: "
        f"{len(res['injections_that_led_to_undo'])} {res['injections_that_led_to_undo']}",
    ]
    if res["reader"] != "keyword rules":
        lines.append(f"- Model answers used: {res['model_answers_used']} "
                     f"(the rest fell back to rules or the guard)")
    if res["confusion"]:
        lines += ["", "Mistakes (expected -> got):"]
        lines += [f"- {k}: {v}" for k, v in sorted(res["confusion"].items())]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--model", action="store_true", help="let the AI model read flags")
    args = parser.parse_args()
    if args.model:
        load_dotenv()
        if not llm.available():
            raise SystemExit("Set GROQ_API_KEY (environment or .env) to use --model.")
    res = evaluate(args.model)
    RESULTS.mkdir(exist_ok=True)
    name = "rules" if not args.model else re.sub(r"[^a-z0-9]+", "_", llm.model_name().lower())
    (RESULTS / f"results_{name}.json").write_text(json.dumps(res, indent=2) + "\n")
    print(report(res))
    if args.model and res["model_answers_used"] < res["flags"] - res["injections_caught_by_guard"]:
        print("\nNote: some flags fell back to rules. Check your key, network and model name.")


if __name__ == "__main__":
    main()
