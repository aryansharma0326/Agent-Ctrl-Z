"""OWNER: Varuni
Flag inbox: every flag people sent, already read and sorted, so the owner can decide
in one click instead of reading every note.

For each flagged action it shows what the agent did, the recommendation (undo it,
look yourself, or keep it) and why, the exact undo plan, and one line per person.

Untrusted input: flag text is written by people outside the team. It is only shown
as inert text inside "Show what they wrote", after sanitizing, and only labels (never
the text) affect the recommendation. The undo itself is planned by fixed code.

Run:  python -m fake_data.seed   (demo data)
      streamlit run app.py        (then pick "flag inbox" in the sidebar)
"""
from __future__ import annotations

import streamlit as st

from core import llm
from core.flag_triage import SUSPICIOUS, triage_all
from core.undo_router import route_undo
from repair.dashboard_helpers import dismiss_flags, escape_markdown, sanitize_untrusted
from shared import db

st.set_page_config(page_title="Flag inbox", page_icon="📥", layout="wide")

LABEL_CHIPS = {
    "conflict": "🟠 `Clashes with their plans`",
    "wrong_time": "🟠 `Bad time`",
    "wrong_people": "🔴 `Wrong people`",
    "wrong_details": "🟠 `Wrong details`",
    "bring_back": "🔴 `Still needed`",
    "fine": "🟢 `Says it's fine`",
    "unclear": "⚪ `Unclear`",
    SUSPICIOUS: "🟣 `Looks like injected orders`",
}
VERDICTS = {
    "undo": ("Recommended: undo it", st.error),
    "look": ("Recommended: read the flags yourself", st.warning),
    "keep": ("Recommended: keep it, it was right", st.success),
}
SOURCES = {"model": "read by AI", "rules": "read by keyword rules", "guard": "not read by AI"}

conn = db.connect()
results = st.session_state.setdefault("inbox_results", {})

st.title("📥 Flag inbox")
st.caption("What the people affected by your agent said, already sorted. "
           + ("Flags are read by an AI model that can only pick a label."
              if llm.available() else
              "Flags are read by keyword rules (set GROQ_API_KEY to let an AI read them)."))

items = triage_all(conn)
counts = {k: sum(i.recommendation == k for i in items) for k in VERDICTS}
c1, c2, c3 = st.columns(3)
c1.metric("To undo", counts["undo"])
c2.metric("To look at", counts["look"])
c3.metric("To keep", counts["keep"])

if not items:
    st.info("No flags waiting for you. 🎉")

for item in items:
    aid = item.action_id
    title, box = VERDICTS[item.recommendation]
    with st.container(border=True):
        st.markdown(f"**Your agent {escape_markdown(item.what_happened)}**")
        box(f"**{title}.** {escape_markdown(item.reason)}")

        if item.plan:
            st.caption("What the undo will do: " + escape_markdown(item.plan["explanation"]))

        for f in item.flags:
            st.markdown(f"{LABEL_CHIPS.get(f.label, f.label)} **{escape_markdown(f.flagged_by)}**"
                        f" · {escape_markdown(f.summary)} *({SOURCES[f.source]})*")

        with st.expander("Show what they wrote"):
            st.caption("Written by other people. Shown as plain text, never followed.")
            for f in db.list_flags(conn):
                if f.action_id == aid:
                    st.text(f"{f.flagged_by}:")
                    st.code(sanitize_untrusted(f.reason_text) or "(no reason given)",
                            language=None)

        undo_col, keep_col, _ = st.columns([2, 2, 6])
        can_undo = bool(item.plan)
        if undo_col.button("↩️ Undo it", key=f"inbox_undo_{aid}", disabled=not can_undo,
                           type="primary" if item.recommendation == "undo" else "secondary"):
            with st.spinner("Undoing…"):
                results[aid] = route_undo(conn, aid).to_dict()
            st.rerun()
        if keep_col.button("✋ Keep it", key=f"inbox_keep_{aid}",
                           help="The agent's action was right: keep it and clear the flags.",
                           type="primary" if item.recommendation == "keep" else "secondary"):
            dismiss_flags(conn, aid)
            st.toast("Kept. Nothing was undone.")
            st.rerun()

for aid, r in list(results.items()):
    (st.success if r["success"] else st.error)(
        f"**Undo {'worked' if r['success'] else 'did not finish'}**: "
        f"{escape_markdown(r['details'])}  \nTold it's fixed: "
        f"{escape_markdown(', '.join(r['notify']) or 'nobody')}")
