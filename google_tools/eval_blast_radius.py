"""OWNER: Visista
Evaluation of the Adaptive Holding Pen on the labeled email scenarios.
Nothing is sent: it only scores emails and simulates what each policy would do.

    python -m google_tools.eval_blast_radius
    python -m google_tools.eval_blast_radius --out results     # also save CSV files

What it reports:
  1. Every scenario's score, tier and reasons
  2. Labels (once the team has filled them in): how much you agree (Fleiss' kappa), and how
     often the score's tier matches the majority label. "Under-escalation" (you wanted to be
     asked, the score said otherwise) is the dangerous error.
  3. Policy comparison: no hold / Gmail's 5 s / fixed 60 s (the old system) / always ask /
     adaptive. For each: mistakes that reach people, people reached, approvals the owner
     must give, and waiting added to harmless emails.
  4. Ablation: the adaptive policy with one factor removed at a time
  5. Threshold sweep: other low/high cut-offs

Assumptions of the simulation (stated in the output, change them with the options):
  * The owner notices a mistake T seconds after the agent reports it. A held email is caught
    if T is shorter than its hold. Results are shown for several T.
  * When the owner is asked to approve, they catch a mistake with probability --catch
    (default 0.9: people sometimes approve without reading).
  * Approving takes the owner --approval-seconds (default 120) for a harmless email.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from google_tools import blast_radius

SCENARIOS = Path(__file__).resolve().parent / "blast_radius_scenarios.json"
NOTICE_TIMES = (5, 15, 30, 60, 120, 300)
LABEL_TO_TIER = {"now": "low", "act now": "low", "low": "low",
                 "hold": "medium", "hold briefly": "medium", "medium": "medium",
                 "ask": "high", "ask me first": "high", "high": "high"}


# ---------- loading + scoring ----------
def load(path: Path = SCENARIOS) -> dict:
    return json.loads(path.read_text())


def score_all(data: dict, **options) -> list[dict]:
    rows = []
    for s in data["scenarios"]:
        r = blast_radius.score_email(s["to"], s["subject"], s["body"], owner=data["owner"],
                                     known=set(data["known_contacts"]),
                                     recent_emails=s.get("recent_agent_emails", 0), **options)
        people = len({e.lower() for e in s["to"]} - {data["owner"].lower()})
        rows.append({**s, **r, "people": people})
    return rows


# ---------- labels ----------
def labels_of(s: dict) -> list[str]:
    return [LABEL_TO_TIER[v.strip().lower()] for v in s.get("labels", {}).values()
            if v and v.strip().lower() in LABEL_TO_TIER]


def fleiss_kappa(items: list[list[str]]) -> float | None:
    """Agreement between raters beyond chance (1 = perfect, 0 = chance). Items need equal raters."""
    items = [i for i in items if i]
    if not items or len({len(i) for i in items}) != 1 or len(items[0]) < 2:
        return None
    n, N = len(items[0]), len(items)
    counts = [Counter(i) for i in items]
    p_i = [(sum(c[t] ** 2 for t in blast_radius.TIERS) - n) / (n * (n - 1)) for c in counts]
    p_j = [sum(c[t] for c in counts) / (N * n) for t in blast_radius.TIERS]
    p_e = sum(p ** 2 for p in p_j)
    return 1.0 if p_e == 1 else (sum(p_i) / N - p_e) / (1 - p_e)


def majority(labels: list[str]) -> str | None:
    if not labels:
        return None
    (top, count), *rest = Counter(labels).most_common()
    if rest and rest[0][1] == count:   # tie: take the more cautious label
        return max((t for t, c in Counter(labels).items() if c == count), key=blast_radius.TIERS.index)
    return top


def label_report(rows: list[dict]) -> dict:
    labeled = [(r, majority(labels_of(r))) for r in rows if labels_of(r)]
    if not labeled:
        return {}
    rank = blast_radius.TIERS.index
    return {
        "labeled": len(labeled),
        "kappa": fleiss_kappa([labels_of(r) for r, _ in labeled]),
        "accuracy": sum(r["tier"] == m for r, m in labeled) / len(labeled),
        "under": [r["id"] for r, m in labeled if rank(r["tier"]) < rank(m)],
        "over": [r["id"] for r, m in labeled if rank(r["tier"]) > rank(m)],
        "confusion": Counter((m, r["tier"]) for r, m in labeled),
    }


# ---------- policy simulation ----------
def policy_hold(policy: str, row: dict) -> float | None:
    """Seconds held, or None if the owner must approve."""
    return {"no hold": 0, "Gmail 5 s": 5, "fixed 60 s": 60, "always ask": None,
            "adaptive": row["hold_seconds"]}[policy]


POLICIES = ("no hold", "Gmail 5 s", "fixed 60 s", "always ask", "adaptive")


def simulate(rows: list[dict], policy: str, notice: float, catch: float,
             approval_seconds: float) -> dict:
    reached = people = approvals = delay = 0.0
    harmless = [r for r in rows if not r["mistake"]]
    for r in rows:
        hold = policy_hold(policy, r)
        if hold is None:
            approvals += 1
        if r["mistake"]:
            escapes = (1 - catch) if hold is None else (0.0 if notice < hold else 1.0)
            reached += escapes
            people += escapes * r["people"]
        else:
            delay += approval_seconds if hold is None else hold
    return {"policy": policy, "notice_s": notice, "mistakes_reached": round(reached, 2),
            "people_reached": round(people, 1), "approvals": int(approvals),
            "avg_wait_harmless_s": round(delay / max(len(harmless), 1), 1)}


# ---------- printing ----------
def table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the Adaptive Holding Pen.")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--catch", type=float, default=0.9,
                        help="chance the owner catches a mistake when asked to approve")
    parser.add_argument("--approval-seconds", type=float, default=120,
                        help="time the owner takes to approve a harmless email")
    parser.add_argument("--out", type=Path, help="folder to save CSV results in")
    args = parser.parse_args()

    data = load(args.scenarios)
    rows = score_all(data)
    mistakes = [r for r in rows if r["mistake"]]
    print(f"# Adaptive Holding Pen evaluation\n\n{len(rows)} scenarios ({len(mistakes)} mistakes). "
          f"Tiers: low <= {blast_radius.low_max()}, high >= {blast_radius.high_min()}.\n")

    print("## 1. Scores\n")
    print(table(["id", "mistake", "people", "score", "tier", "reasons"],
                [[r["id"], "yes" if r["mistake"] else "", r["people"], r["score"], r["tier"],
                  "; ".join(r["reasons"]) or "-"] for r in rows]))
    tiers = Counter(r["tier"] for r in rows)
    print(f"\nTiers: {tiers['low']} low, {tiers['medium']} medium, {tiers['high']} high. "
          f"Mistakes in each tier: " + ", ".join(
              f"{t} {sum(1 for r in mistakes if r['tier'] == t)}" for t in blast_radius.TIERS) + "\n")

    print("## 2. Team labels\n")
    report = label_report(rows)
    if not report:
        print("No labels yet. Each teammate fills in 'now' / 'hold' / 'ask' for every scenario in "
              f"{args.scenarios.name}, without looking at the scores, then run this again.\n")
    else:
        kappa = report["kappa"]
        print(f"Labeled scenarios: {report['labeled']}. Agreement (Fleiss' kappa): "
              f"{'n/a' if kappa is None else f'{kappa:.2f}'}. "
              f"Score tier = majority label: {report['accuracy']:.0%}.")
        print(f"Under-escalated (dangerous): {', '.join(report['under']) or 'none'}. "
              f"Over-escalated (extra friction): {', '.join(report['over']) or 'none'}.\n")
        print(table(["majority label \\ score tier"] + list(blast_radius.TIERS),
                    [[m] + [report["confusion"][(m, t)] for t in blast_radius.TIERS]
                     for m in blast_radius.TIERS]))
        print()

    print("## 3. Policy comparison\n")
    print(f"Assumptions: the owner notices a mistake T seconds after the agent reports it; "
          f"when asked to approve, they catch a mistake {args.catch:.0%} of the time; approving a "
          f"harmless email takes them {args.approval_seconds:.0f} s.\n")
    sims = [simulate(rows, p, t, args.catch, args.approval_seconds) for t in NOTICE_TIMES for p in POLICIES]
    print(table(["policy"] + [f"mistakes reached (T={t}s)" for t in NOTICE_TIMES]
                + ["approvals asked", "avg wait, harmless (s)"],
                [[p] + [next(s["mistakes_reached"] for s in sims if s["policy"] == p and s["notice_s"] == t)
                        for t in NOTICE_TIMES]
                 + [next(s["approvals"] for s in sims if s["policy"] == p),
                    next(s["avg_wait_harmless_s"] for s in sims if s["policy"] == p)]
                 for p in POLICIES]))
    print("\nPeople who received a mistake (a mistake to 30 people counts 30):\n")
    print(table(["policy"] + [f"T={t}s" for t in NOTICE_TIMES],
                [[p] + [next(s["people_reached"] for s in sims if s["policy"] == p and s["notice_s"] == t)
                        for t in NOTICE_TIMES] for p in POLICIES]))
    print()

    print("## 4. Ablation: adaptive policy with one factor removed (T=30 s)\n")
    ablation = []
    for drop in (None,) + blast_radius.FACTORS:
        variant = score_all(data, drop=(drop,) if drop else ())
        sim = simulate(variant, "adaptive", 30, args.catch, args.approval_seconds)
        rep = label_report(variant)
        ablation.append([f"without {drop}" if drop else "all factors", sim["mistakes_reached"],
                         sim["people_reached"], sim["approvals"],
                         f"{rep['accuracy']:.0%}" if rep else "-"])
    print(table(["variant", "mistakes reached", "people reached", "approvals", "matches labels"], ablation))

    print("\n## 5. Threshold sweep (T=30 s)\n")
    sweep = []
    for low in (1, 2, 3):
        for high in (4, 5, 6, 7, 8, 9):
            if high <= low + 1:
                continue
            variant = score_all(data, low=low, high=high)
            sim = simulate(variant, "adaptive", 30, args.catch, args.approval_seconds)
            rep = label_report(variant)
            sweep.append([low, high, sim["mistakes_reached"], sim["approvals"],
                          sim["avg_wait_harmless_s"], f"{rep['accuracy']:.0%}" if rep else "-"])
    print(table(["low <=", "high >=", "mistakes reached", "approvals", "avg wait (s)",
                 "matches labels"], sweep))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        with open(args.out / "scores.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "mistake", "people", "score", "tier", "reasons", "majority_label"])
            for r in rows:
                w.writerow([r["id"], r["mistake"], r["people"], r["score"], r["tier"],
                            "; ".join(r["reasons"]), majority(labels_of(r)) or ""])
        with open(args.out / "policies.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(sims[0]))
            w.writeheader()
            w.writerows(sims)
        print(f"\nSaved scores.csv and policies.csv in {args.out}")


if __name__ == "__main__":
    main()