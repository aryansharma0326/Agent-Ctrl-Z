"""OWNER: Varuni
Roll back: undo everything the agent did in a stretch of time, in one go.

Pick how far back to go, see the full plan (nothing changes yet), then approve.
Several actions on the same event are combined, anything a person changed after the
agent is skipped with the reason, and each affected person gets one "it's fixed" note.

Run:  streamlit run app.py   (then pick "roll back" in the sidebar)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from core.session_rollback import NOTHING, plan_rollback, run_rollback
from repair.dashboard_helpers import escape_markdown
from shared import db

st.set_page_config(page_title="Roll back", page_icon="⏪", layout="wide")

WINDOWS = {"Last 15 minutes": 15, "Last hour": 60, "Last 3 hours": 180,
           "Today (last 24 hours)": 24 * 60, "This week": 7 * 24 * 60}
STRATEGIES = {"exact_restore": "Put back exactly", "group_repair": "Least-disruptive repair",
              "email_correction": "Stop or correct the email",
              "remove_created_event": "Remove the event it created",
              NOTHING: "Nothing to do"}

conn = db.connect()
st.title("⏪ Roll back")
st.caption("Undo everything your agent did in a stretch of time. You'll see the full plan "
           "first; nothing changes until you approve.")

choice = st.radio("How far back?", list(WINDOWS), index=1, horizontal=True)
if st.session_state.get("rollback_choice") != choice:      # a new choice = a new preview
    now = datetime.now(timezone.utc)
    st.session_state["rollback_choice"] = choice
    st.session_state["rollback_until"] = now.isoformat(timespec="seconds")
    st.session_state["rollback_since"] = (now - timedelta(minutes=WINDOWS[choice])
                                          ).isoformat(timespec="seconds")
since, until = st.session_state["rollback_since"], st.session_state["rollback_until"]
plan = plan_rollback(conn, since, until)

if not plan.steps:
    st.info("Your agent didn't do anything in that time that can be rolled back.")
else:
    st.subheader(plan.summary())
    for step in plan.steps:
        with st.container(border=True):
            st.markdown(f"**Your agent {escape_markdown(step.what_happened)}**")
            if step.will_run:
                st.success(f"**{STRATEGIES.get(step.strategy, step.strategy)}.** "
                           f"{escape_markdown(step.explanation)}")
                if step.will_notify:
                    st.caption("Will tell: " + escape_markdown(", ".join(step.will_notify)))
            else:
                st.warning(f"**Skipped.** {escape_markdown(step.skip_reason)}")

    if plan.to_run and st.button("⏪ Roll it all back", type="primary"):
        fresh = plan_rollback(conn, since, until)
        if fresh.fingerprint() != plan.fingerprint():
            st.error("Something changed while you were looking. Check the new plan above.")
        else:
            with st.spinner("Rolling back…"):
                st.session_state["rollback_report"] = run_rollback(conn, fresh).to_dict()
            st.session_state.pop("rollback_choice", None)
            st.rerun()

report = st.session_state.get("rollback_report")
if report:
    (st.error if report["failed"] else st.success)(escape_markdown(report["summary"]))
    for item in report["failed"]:
        st.caption("Failed: " + escape_markdown(item["details"]))
