"""OWNER: Varuni

Receipt & Flag page: what an affected person sees when they open the link in
their receipt. Shows what the agent did and lets them flag it.

Opened as /receipt_flag?action=<id>&who=<email>&token=<signed token>
"""
import streamlit as st

from core.receipts import clean_flag_text, describe, submit_flag, valid_token
from shared import db

st.set_page_config(page_title="Receipt", page_icon="🧾")
st.title("What happened to your plans")

params = st.query_params
action_id, person, token = params.get("action", ""), params.get("who", ""), params.get("token", "")

conn = db.connect()
action = db.get_action(conn, action_id) if action_id else None

if not action or not valid_token(action_id, person, token):
    st.error("This link isn't valid. Please open it straight from your receipt.")
    st.stop()
if person not in action.affected_people:
    st.error("This receipt isn't for you.")
    st.stop()

st.write(f"An AI assistant **{describe(action)}**.")
st.caption("What the owner asked for:")
st.code(action.instruction, language=None)   # shown verbatim as plain text

already = [f for f in db.list_flags(conn) if f.action_id == action_id and f.flagged_by == person]

if action.status == "undone":
    results = db.list_undo_results(conn, action_id)
    st.success("This has been fixed. " + (results[-1].details if results else ""))
elif already:
    st.info("Thanks, you've flagged this. The owner has been told and will review it.")
elif action.status not in ("done", "flagged"):
    st.warning("The owner is already handling this one.")
else:
    with st.form("flag"):
        reason = st.text_area("Does this look wrong for you? Tell the owner why (optional).",
                              max_chars=500)
        if st.form_submit_button("Flag it", type="primary"):
            try:
                submit_flag(conn, action_id, person, clean_flag_text(reason), token)
                st.success("Thanks, the owner has been told and will review it.")
            except ValueError as exc:
                st.error(str(exc))
    st.caption("If it's fine, you don't need to do anything.")
