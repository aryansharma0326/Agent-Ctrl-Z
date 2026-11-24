"""OWNER: Visista
Blind labeling of the Adaptive Holding Pen scenarios. Each teammate runs it on their own:

    python -m google_tools.label_emails --name varuni

It shows the 24 emails one at a time, in a shuffled order, with only what the owner would
see (recipients, subject, message). It never shows the scores, whether the email is a
mistake, or anyone else's answers, so the labels measure your own judgment.
Answers are saved after every email to google_tools/labels/<name>.json, so you can stop
(q) and carry on later. Send that file to Visista when you're done.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from google_tools import eval_blast_radius as ev

CHOICES = {"n": "now", "now": "now", "h": "hold", "hold": "hold", "a": "ask", "ask": "ask"}
QUESTION = ("Your AI assistant is about to send this email from your account, and you're not\n"
            "sure it got everything right. How much checking do you want before it goes out?\n"
            "  n = now   send it right away\n"
            "  h = hold  hold it briefly, so I can still stop it\n"
            "  a = ask   don't send it without my OK")


def labels_path(name: str) -> Path:
    return ev.LABELS_DIR / f"{name}.json"


def load_labels(name: str) -> dict:
    path = labels_path(name)
    return json.loads(path.read_text()) if path.exists() else {}


def save_labels(name: str, labels: dict) -> None:
    ev.LABELS_DIR.mkdir(parents=True, exist_ok=True)
    labels_path(name).write_text(json.dumps(labels, indent=2, sort_keys=True))


def describe(s: dict, data: dict) -> str:
    """Only what the owner would see. No 'what', no 'mistake', no score."""
    owner = data["owner"].lower()
    known = {k.lower() for k in data["known_contacts"]}
    people = list(dict.fromkeys(s["to"]))
    shown = people[:8]
    lines = [f"To ({len(people)}):"]
    for p in shown:
        tag = " (you)" if p.lower() == owner else " (in your contacts)" if p.lower() in known else ""
        lines.append(f"    {p}{tag}")
    if len(people) > len(shown):
        others = people[len(shown):]
        n_known = sum(1 for p in others if p.lower() in known)
        lines.append(f"    ... and {len(others)} more ({n_known} in your contacts)")
    lines += [f"Subject: {s['subject']}", f"Message: {s['body']}"]
    if s.get("recent_agent_emails"):
        lines.append(f"(Your assistant already sent {s['recent_agent_emails']} emails "
                     "in the last 30 minutes.)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Label the holding-pen scenarios, blind.")
    parser.add_argument("--name", required=True, help="your first name, e.g. varuni")
    parser.add_argument("--redo", action="store_true", help="answer every email again")
    args = parser.parse_args()
    name = args.name.strip().lower()

    data = ev.load()
    labels = {} if args.redo else load_labels(name)
    order = random.Random(name).sample(data["scenarios"], len(data["scenarios"]))
    todo = [s for s in order if s["id"] not in labels]
    print(f"Hi {name}. {len(labels)} of {len(order)} done, {len(todo)} to go. "
          "Answer on your own, without discussing them.\n")
    print(QUESTION)
    for s in todo:
        done = len(labels) + 1
        print(f"\n---------- email {done} of {len(order)} ----------\n{describe(s, data)}\n")
        while True:
            answer = input("now / hold / ask? [n/h/a], or q to stop for now: ").strip().lower()
            if answer in ("q", "quit"):
                print(f"Saved {len(labels)} answers. Run the same command to carry on.")
                return
            if answer in CHOICES:
                labels[s["id"]] = CHOICES[answer]
                save_labels(name, labels)
                break
            print("Please type n, h or a.")
    print(f"\nDone: all {len(order)} labeled. Your answers are in {labels_path(name)}. "
          "Send that file to Visista.")


if __name__ == "__main__":
    main()