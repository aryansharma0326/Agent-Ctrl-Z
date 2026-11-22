"""OWNER: Varuni

Two-sided evaluation: do the people an agent's action affects catch mistakes
that the owner alone would miss?

For every case, the agent's actions go through the REAL code path:
  action log  -> who is affected      (core/action_log.py)
  receipts    -> what each person sees (core/receipts.py)
  flags       -> signed, cleaned, saved (core/receipts.submit_flag)
Then two setups are compared on the same actions:
  owner only   a mistake is caught only if the owner flags it (how assistants work today)
  two-sided    caught if the owner OR any affected person flags it (our system)

Run:
  python -m evals.two_sided.run                      # Groq, needs GROQ_API_KEY
  python -m evals.two_sided.run --provider gemini    # needs GEMINI_API_KEY
  python -m evals.two_sided.run --offline            # pipeline smoke test, no key, NOT results
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core import receipts
from core.action_log import log_action
from evals.two_sided.reviewers import LLMReviewer, OfflineReviewer, ReviewerError
from shared import db

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"


def load_cases(path: Path = CASES) -> dict:
    return json.loads(path.read_text())


def owner_log_line(action) -> str:
    """How an action appears in the owner's activity log."""
    line = receipts.describe(action)
    if action.tool == "email.send":
        line += f" to {', '.join((action.after or {}).get('to', []))}"
    elif action.tool == "calendar.create_event":
        others = [p for p in (action.after or {}).get("attendees", [])]
        line += f" with {', '.join(others)}"
    return line


def _receipt_for_reviewer(body: str) -> str:
    return re.sub(r"https?://\S+", "[Flag link]", body)


def run_case(case: dict, owner: str, reviewer, conn) -> list[dict]:
    """Run one case through the real pipeline and both reviewers. One row per action."""
    records = []
    for a in case["actions"]:
        target = (a["before"] or a["after"])["id"]
        records.append(log_action(conn, a["tool"], target,
                                  {"before": a["before"], "after": a["after"]}, case["owner_request"]))

    log_lines = [owner_log_line(r) for r in records]
    try:
        owner_view = reviewer.owner(case["owner_context"], case["owner_request"], log_lines)
        owner_error = None
    except ReviewerError as exc:
        owner_view, owner_error = {}, str(exc)

    rows = []
    for n, (a, rec) in enumerate(zip(case["actions"], records), 1):
        row = {"case": case["id"], "action": a["id"], "category": case["category"],
               "is_mistake": a["is_mistake"], "what_happened": log_lines[n - 1],
               "owner_flag": owner_view.get(n, (None, ""))[0],
               "owner_reason": owner_view.get(n, (None, ""))[1],
               "affected": rec.affected_people, "person_flags": {}, "errors": []}
        if owner_error:
            row["errors"].append(f"owner: {owner_error}")

        for r in receipts.make_receipts(rec, owner):
            person = r["to"]
            profile = case["people"].get(person)
            if not profile:
                row["errors"].append(f"no context for affected person {person}")
                continue
            try:
                flag, reason = reviewer.person(profile["context"], _receipt_for_reviewer(r["body"]))
            except ReviewerError as exc:
                row["errors"].append(f"{person}: {exc}")
                continue
            row["person_flags"][person] = {"flag": flag, "reason": reason}
            if flag:   # goes through the real, signed flag path
                token = receipts.flag_token(rec.action_id, person)
                receipts.submit_flag(conn, rec.action_id, person, reason, token)

        saved = db.get_action(conn, rec.action_id)
        row["flags_saved_in_app"] = sum(1 for f in db.list_flags(conn) if f.action_id == rec.action_id)
        row["status_in_app"] = saved.status
        row["anyone_flagged"] = bool(row["owner_flag"]) or any(
            p["flag"] for p in row["person_flags"].values())
        rows.append(row)
    return rows


def score(rows: list[dict]) -> dict:
    """Headline numbers. Actions with any reviewer error are left out, not counted as misses."""
    clean = [r for r in rows if not r["errors"]]
    mistakes = [r for r in clean if r["is_mistake"]]
    correct = [r for r in clean if not r["is_mistake"]]

    def pct(n, d):
        return round(100 * n / d, 1) if d else 0.0

    caught_owner = sum(1 for r in mistakes if r["owner_flag"])
    caught_two = sum(1 for r in mistakes if r["anyone_flagged"])
    fa_owner = sum(1 for r in correct if r["owner_flag"])
    fa_two = sum(1 for r in correct if r["anyone_flagged"])
    only_people = sum(1 for r in mistakes if r["anyone_flagged"] and not r["owner_flag"])

    by_cat = defaultdict(lambda: {"mistakes": 0, "owner_only": 0, "two_sided": 0})
    for r in mistakes:
        c = by_cat[r["category"]]
        c["mistakes"] += 1
        c["owner_only"] += bool(r["owner_flag"])
        c["two_sided"] += bool(r["anyone_flagged"])

    return {
        "actions_scored": len(clean), "actions_with_errors": len(rows) - len(clean),
        "mistakes": len(mistakes), "correct_actions": len(correct),
        "caught_owner_only": caught_owner, "caught_two_sided": caught_two,
        "catch_rate_owner_only_pct": pct(caught_owner, len(mistakes)),
        "catch_rate_two_sided_pct": pct(caught_two, len(mistakes)),
        "caught_only_by_affected_people": only_people,
        "false_alarms_owner_only": fa_owner, "false_alarms_two_sided": fa_two,
        "false_alarm_rate_owner_only_pct": pct(fa_owner, len(correct)),
        "false_alarm_rate_two_sided_pct": pct(fa_two, len(correct)),
        "by_category": dict(by_cat),
    }


def evaluate(reviewer, cases_doc: dict | None = None) -> dict:
    cases_doc = cases_doc or load_cases()
    owner = cases_doc["owner"]
    old_owner = os.environ.get("OWNER_EMAIL")
    os.environ["OWNER_EMAIL"] = owner
    rows = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases_doc["cases"]:
                conn = db.connect(Path(tmp) / f"{case['id']}.db")   # fresh app state per case
                rows += run_case(case, owner, reviewer, conn)
                conn.close()
    finally:
        if old_owner is None:
            os.environ.pop("OWNER_EMAIL", None)
        else:
            os.environ["OWNER_EMAIL"] = old_owner
    return {"reviewer": reviewer.label, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": score(rows), "rows": rows}


def print_report(result: dict) -> None:
    s = result["summary"]
    smoke = result["reviewer"] == OfflineReviewer.label
    print(f"\nTwo-sided evaluation  |  reviewer: {result['reviewer']}")
    if smoke:
        print("!! OFFLINE SMOKE TEST: checks the pipeline only, these are NOT real results !!")
    print(f"Scored {s['actions_scored']} actions ({s['mistakes']} mistakes, {s['correct_actions']} correct)"
          f"; {s['actions_with_errors']} left out because of reviewer errors\n")
    print(f"{'':28}{'Owner only':>14}{'Two-sided':>14}")
    print(f"{'Mistakes caught':28}{s['caught_owner_only']:>8} ({s['catch_rate_owner_only_pct']:>4}%)"
          f"{s['caught_two_sided']:>8} ({s['catch_rate_two_sided_pct']:>4}%)")
    print(f"{'False alarms on correct':28}{s['false_alarms_owner_only']:>8} ({s['false_alarm_rate_owner_only_pct']:>4}%)"
          f"{s['false_alarms_two_sided']:>8} ({s['false_alarm_rate_two_sided_pct']:>4}%)")
    print(f"\nMistakes caught ONLY by affected people: {s['caught_only_by_affected_people']}")
    print("\nBy mistake type:")
    for cat, c in sorted(s["by_category"].items()):
        print(f"  {cat:20} owner only {c['owner_only']}/{c['mistakes']}   two-sided {c['two_sided']}/{c['mistakes']}")
    for r in result["rows"]:
        if r["errors"]:
            print(f"  ! {r['action']}: {'; '.join(r['errors'])[:160]}")


def save_chart(result: dict, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = result["summary"]
    groups = ["Mistakes caught", "False alarms\n(correct actions flagged)"]
    owner = [s["catch_rate_owner_only_pct"], s["false_alarm_rate_owner_only_pct"]]
    two = [s["catch_rate_two_sided_pct"], s["false_alarm_rate_two_sided_pct"]]
    counts_o = [f"{s['caught_owner_only']}/{s['mistakes']}", f"{s['false_alarms_owner_only']}/{s['correct_actions']}"]
    counts_t = [f"{s['caught_two_sided']}/{s['mistakes']}", f"{s['false_alarms_two_sided']}/{s['correct_actions']}"]

    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=150)
    x = range(len(groups))
    w = 0.36
    b1 = ax.bar([i - w / 2 for i in x], owner, w, label="Owner only (today's assistants)", color="#9AA5B1")
    b2 = ax.bar([i + w / 2 for i in x], two, w, label="Two-sided (receipts + flags)", color="#5B4FCF")
    for bars, labels in ((b1, counts_o), (b2, counts_t)):
        for bar, label in zip(bars, labels):
            ax.annotate(f"{bar.get_height():.0f}%\n({label})", (bar.get_x() + bar.get_width() / 2,
                        bar.get_height()), ha="center", va="bottom", fontsize=9,
                        xytext=(0, 3), textcoords="offset points")
    ax.set_xticks(list(x), groups)
    ax.set_ylim(0, 115)
    ax.set_ylabel("% of actions")
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title("Who catches an AI agent's mistakes?", loc="left", fontsize=13,
                 fontweight="bold", pad=24)
    ax.text(0, 1.025, f"Simulated reviewers ({result['reviewer']}), {s['mistakes']} mistakes and "
            f"{s['correct_actions']} correct actions", transform=ax.transAxes, fontsize=8.5, color="#555")
    ax.legend(frameon=False, loc="upper right", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", default="groq", choices=["groq", "gemini"])
    p.add_argument("--model", default=None, help="model name (default depends on provider)")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between API calls (free tiers)")
    p.add_argument("--offline", action="store_true", help="pipeline smoke test, no API key, NOT results")
    p.add_argument("--out", default=str(HERE / "results"), help="folder for results + chart")
    args = p.parse_args(argv)

    reviewer = OfflineReviewer() if args.offline else LLMReviewer(args.provider, args.model, args.delay)
    result = evaluate(reviewer)
    print_report(result)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^a-zA-Z0-9]+", "_", result["reviewer"]).strip("_")
    (out / f"results_{tag}.json").write_text(json.dumps(result, indent=2))
    chart = save_chart(result, out / f"chart_{tag}.png")
    print(f"\nSaved {out / f'results_{tag}.json'} and {chart}")
    return result


if __name__ == "__main__":
    main()
