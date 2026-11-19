"""One shared SQLite database for the whole team (undo.db).

Who writes / reads each table:
  actions       -> written by Varuni, read by everyone
  flags         -> written by Varuni, read by Aryan's dashboard
  undo_results  -> written by Visista and Aryan, read by Varuni's undo router

JSON fields (before, after, affected_people, notify) are stored as text.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from shared.models import ActionRecord, Flag, UndoResult

DEFAULT_DB = Path(__file__).resolve().parent.parent / "undo.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id        TEXT PRIMARY KEY,
    timestamp        TEXT NOT NULL,
    tool             TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    before_json      TEXT,
    after_json       TEXT,
    affected_people  TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    status           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    flag_id      TEXT PRIMARY KEY,
    action_id    TEXT NOT NULL REFERENCES actions(action_id),
    flagged_by   TEXT NOT NULL,
    reason_text  TEXT NOT NULL,
    timestamp    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS undo_results (
    action_id      TEXT NOT NULL REFERENCES actions(action_id),
    fix_type       TEXT NOT NULL,
    success        INTEGER NOT NULL,
    details        TEXT NOT NULL,
    notify         TEXT NOT NULL,
    new_target_id  TEXT,
    timestamp      TEXT NOT NULL
);
"""


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DEFAULT_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------- actions ----------
def save_action(conn: sqlite3.Connection, a: ActionRecord) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
        (a.action_id, a.timestamp, a.tool, a.target_id,
         json.dumps(a.before), json.dumps(a.after),
         json.dumps(a.affected_people), a.instruction, a.status),
    )
    conn.commit()


def get_action(conn: sqlite3.Connection, action_id: str) -> Optional[ActionRecord]:
    row = conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
    if row is None:
        return None
    return ActionRecord(
        action_id=row["action_id"], timestamp=row["timestamp"], tool=row["tool"],
        target_id=row["target_id"], before=json.loads(row["before_json"]),
        after=json.loads(row["after_json"]),
        affected_people=json.loads(row["affected_people"]),
        instruction=row["instruction"], status=row["status"],
    )


def set_action_status(conn: sqlite3.Connection, action_id: str, status: str) -> None:
    conn.execute("UPDATE actions SET status=? WHERE action_id=?", (status, action_id))
    conn.commit()


def list_actions(conn: sqlite3.Connection) -> list[ActionRecord]:
    ids = [r["action_id"] for r in conn.execute("SELECT action_id FROM actions ORDER BY timestamp")]
    return [get_action(conn, i) for i in ids]


# ---------- flags ----------
def save_flag(conn: sqlite3.Connection, f: Flag) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO flags VALUES (?,?,?,?,?)",
        (f.flag_id, f.action_id, f.flagged_by, f.reason_text, f.timestamp),
    )
    conn.commit()


def list_flags(conn: sqlite3.Connection) -> list[Flag]:
    return [Flag(**dict(r)) for r in conn.execute("SELECT * FROM flags ORDER BY timestamp")]


# ---------- undo results ----------
def save_undo_result(conn: sqlite3.Connection, r: UndoResult) -> None:
    conn.execute(
        "INSERT INTO undo_results VALUES (?,?,?,?,?,?,?)",
        (r.action_id, r.fix_type, int(r.success), r.details,
         json.dumps(r.notify), r.new_target_id, r.timestamp),
    )
    conn.commit()


def list_undo_results(conn: sqlite3.Connection, action_id: str) -> list[UndoResult]:
    rows = conn.execute("SELECT * FROM undo_results WHERE action_id=?", (action_id,))
    return [
        UndoResult(action_id=r["action_id"], fix_type=r["fix_type"],
                   success=bool(r["success"]), details=r["details"],
                   notify=json.loads(r["notify"]), new_target_id=r["new_target_id"],
                   timestamp=r["timestamp"])
        for r in rows
    ]
