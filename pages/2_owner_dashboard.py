"""OWNER: Aryan
Owner Dashboard: review what the agent did, see who flagged it, preview the
least-disruptive repair, then approve the undo or dismiss the flag.

Run:  python -m fake_data.seed   (demo data)
      streamlit run app.py        (then pick "owner dashboard" in the sidebar)

Untrusted input: Flag.reason_text is written by people outside the team. It is
only ever displayed as inert text (st.code, after sanitize_untrusted) and is
never passed to the repair planner, the undo router or any LLM.
"""
from __future__ import annotations

import streamlit as st

from core.undo_router import route_undo
from repair.dashboard_helpers import (DashboardItem, describe_change, dismiss_flags,
                                      escape_markdown, load_items, metrics, preview_state,
                                      sanitize_untrusted)
from repair.group_repair import CASE_A, CASE_B, CASE_C, CASE_D, plan_group_repair
from shared import db

st.set_page_config(page_title="Owner Dashboard", page_icon="🛡️", layout="wide")

CASE_LABELS = {
    CASE_A: ("Exact restore", "green"),
    CASE_B: ("Reschedule to avoid double-booking", "orange"),
    CASE_C: ("Keep group meeting + makeup sync", "violet"),
    CASE_D: ("No change", "gray"),
}
STATUS_LABELS = {"done": "✅ done", "flagged": "🚩 flagged", "undone": "↩️ undone",
                 "undo_failed": "⚠️ undo failed"}


def md(text: object) -> str:
    """Escape anything that came from data before handing it to st.markdown."""
    return escape_markdown(text)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.caption(f"Database: `{db.DEFAULT_DB.name}`")
    with st.expander("Demo tools"):
        st.write("Reset undo.db and the fake calendar to the six demo stories.")
        if st.checkbox("I understand this wipes current actions and flags"):
            if st.button("Reload demo data"):
                from fake_data.seed import seed
                seed(db.connect())
                st.session_state.pop("undo_results", None)
                st.rerun()

conn = db.connect()
items = load_items(conn)
results: dict = st.session_state.setdefault("undo_results", {})

# ------------------------------------------------------------------ header + metrics
st.title("🛡️ Owner Dashboard")
st.caption("Everything your AI agent did on your behalf, what people flagged, and a "
           "least-disruptive way to fix it.")

cols = st.columns(4)
for col, (label, value) in zip(cols, metrics(items).items()):
    col.metric(label, value)

pending = [i for i in items if i.pending]
reviewed = [i for i in items if i.flags and not i.pending]

tab_pending, tab_reviewed, tab_all = st.tabs(
    [f"🚩 Needs review ({len(pending)})", f"Reviewed ({len(reviewed)})",
     f"All actions ({len(items)})"])


# ------------------------------------------------------------------ pieces
def render_change(item: DashboardItem) -> None:
    a = item.action
    verb, before, after = describe_change(a)
    st.markdown(f"**Owner asked:** “{md(a.instruction)}”  \n"
                f"**Agent:** {md(verb)} ({md(a.tool)}) · {md(a.timestamp)}")
    b, arrow, aft = st.columns([10, 1, 10])
    with b:
        st.caption("Before")
        st.text(before)
    arrow.markdown("<div style='text-align:center;padding-top:1.6rem'>→</div>",
                   unsafe_allow_html=True)   # static markup only, no data
    with aft:
        st.caption("After")
        st.text(after)
    if a.affected_people:
        st.caption("Affected: " + md(", ".join(a.affected_people)))


def render_flags(item: DashboardItem) -> None:
    for f in item.flags:
        st.markdown(f"🚩 **Flagged by** {md(f.flagged_by)} · {md(f.timestamp)}")
        st.caption("⚠️ Untrusted external input: shown verbatim, never executed, never "
                   "sent to the AI.")
        st.code(sanitize_untrusted(f.reason_text), language="text", wrap_lines=True)


def render_preview(item: DashboardItem) -> None:
    with st.expander("🔍 Preview proposed repair (dry run, nothing is changed)"):
        try:
            plan = plan_group_repair(item.action, preview_state(item))
        except Exception as exc:
            st.warning(f"Couldn't build a repair preview: {md(exc)}")
            return
        label, color = CASE_LABELS.get(plan.case, (plan.case, "gray"))
        st.markdown(f":{color}-badge[{label}]  "
                    + ("✅ would fix it" if plan.success else "⏸️ would not change the calendar"))
        st.write(md(plan.details))

        c1, c2, c3 = st.columns(3)
        c1.caption("Original slot")
        c1.text(plan.original_slot or "—")
        c2.caption("Proposed slot")
        c2.text(plan.proposed_slot or "—")
        c3.caption("Calendar write")
        c3.text({"create": "Create new event", "update": "Update existing event"}
                .get(plan.operation, "None"))

        st.caption("Conflict check at the original time")
        if plan.conflicts:
            st.dataframe([{"Person": c.person, "Now busy with": c.summary, "When": c.when}
                          for c in plan.conflicts], hide_index=True, width="stretch")
        elif plan.original_slot:
            st.text("No conflicts: everyone is still free.")
        else:
            st.text("Not applicable.")

        st.caption("Would notify (only people whose schedule changes)")
        st.text(", ".join(plan.notify) or "Nobody")


def render_result(action_id: str) -> None:
    r = results.get(action_id)
    if not r:
        return
    box = st.success if r["success"] else st.error
    box(f"**Undo {'succeeded' if r['success'] else 'did not complete'}** "
        f"({md(r['fix_type'])})  \n{md(r['details'])}")
    st.caption("Notified: " + md(", ".join(r["notify"]) or "nobody")
               + (f" · new event id {md(r['new_target_id'])}" if r.get("new_target_id") else ""))


# ------------------------------------------------------------------ needs review
with tab_pending:
    if not pending:
        st.info("No pending flags. Nice! 🎉")
    for item in pending:
        aid = item.action.action_id
        with st.container(border=True):
            render_change(item)
            render_flags(item)
            render_preview(item)
            approve, dismiss, _ = st.columns([2, 2, 6])
            if approve.button("↩️ Approve Undo", key=f"approve_{aid}", type="primary"):
                with st.spinner("Undoing…"):
                    try:
                        results[aid] = route_undo(conn, aid).to_dict()
                    except Exception as exc:
                        results[aid] = {"success": False, "fix_type": "router_error",
                                        "details": f"Undo router raised: {exc}",
                                        "notify": [], "new_target_id": None}
            if dismiss.button("✋ Dismiss Flag", key=f"dismiss_{aid}",
                              help="The agent's action was correct: keep it and clear the flag."):
                dismiss_flags(conn, aid)
                st.toast("Flag dismissed; nothing was undone.")
                st.rerun()
            render_result(aid)

# ------------------------------------------------------------------ reviewed
with tab_reviewed:
    if not reviewed:
        st.caption("Nothing reviewed yet.")
    for item in reviewed:
        with st.container(border=True):
            st.markdown(f"**{STATUS_LABELS.get(item.action.status, md(item.action.status))}**")
            render_change(item)
            render_flags(item)
            for r in item.results:
                (st.success if r.success else st.error)(
                    f"{md(r.fix_type)}: {md(r.details)}  \nNotified: "
                    f"{md(', '.join(r.notify) or 'nobody')}")
            render_result(item.action.action_id)

# ------------------------------------------------------------------ all actions
with tab_all:
    st.dataframe(
        [{"Time": i.action.timestamp, "Instruction": i.action.instruction,
          "Tool": i.action.tool, "Target": i.action.target_id,
          "Status": STATUS_LABELS.get(i.action.status, i.action.status),
          "Flags": len(i.flags)} for i in items],
        hide_index=True, width="stretch")
