"""Load every demo story into the fake calendar AND undo.db.

Run:  python -m fake_data.seed

After this, undo.db has one action per story (most of them flagged), and the
fake calendar shows the world right AFTER the agent's mistakes. Handy for
building the dashboard, receipts and undo without running the agent.
Running it again starts fresh.
"""
from __future__ import annotations

import copy

from fake_data import fake_google as g
from fake_data.sample_data import SCENARIOS
from shared import db
from shared.models import ActionRecord, Flag


def seed(conn) -> list[ActionRecord]:
    conn.executescript("DELETE FROM undo_results; DELETE FROM flags; DELETE FROM actions;")
    g.reset([e for s in SCENARIOS for e in s["events"]])

    records = []
    for s in SCENARIOS:
        do = s["agent_does"]
        if do["tool"] == "calendar.cancel_event":
            before, after = g.get_event(do["target_id"]), None
            g.delete_event(do["target_id"])
            target = do["target_id"]
        elif do["tool"] == "calendar.update_event":
            before = g.get_event(do["target_id"])
            after = g.update_event(do["target_id"], copy.deepcopy(do["changes"]))
            target = do["target_id"]
        elif do["tool"] == "email.send":
            before = None
            after = g.send_email(do["to"], do["subject"], do["body"])
            target = after["id"]
        else:
            raise ValueError(f"Unknown tool in story {s['key']}: {do['tool']}")

        rec = ActionRecord(tool=do["tool"], target_id=target, before=before, after=after,
                           affected_people=s["affected"], instruction=s["instruction"])
        db.save_action(conn, rec)

        for e in s["later_changes"]:
            g.add_event_as_is(e)

        if s["flag"]:
            db.save_flag(conn, Flag(action_id=rec.action_id, flagged_by=s["flag"]["by"],
                                    reason_text=s["flag"]["reason"]))
            db.set_action_status(conn, rec.action_id, "flagged")
            rec.status = "flagged"
        records.append(rec)
    return records


if __name__ == "__main__":
    recs = seed(db.connect())
    print(f"Loaded {len(recs)} stories into undo.db and the fake calendar:")
    for r, s in zip(recs, SCENARIOS):
        print(f"  {r.action_id}  [{r.status:7}]  {s['title']}")
