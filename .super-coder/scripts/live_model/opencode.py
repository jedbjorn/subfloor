"""opencode probe — ~/.local/share/opencode/opencode.db (SQLite, not files).

`session.directory` is the worktree, so the match is exact. Main vs subagent is
a ROW SHAPE: a subagent is its own `session` row with `parent_id` set, so the
main thread is `parent_id IS NULL` — in a captured run the parent session ends
on `qwen3.5:397b` while its child session ran `gpt-oss:120b`.

Model id: `message.data.modelID`, present on every assistant row the scout
counted (2939/2939). `providerID` sits beside it; we report `modelID` verbatim,
per the spec's "verbatim from the transcript" — the provider is a separate fact
and is not part of the model id.

The DB is opened READ-ONLY and must tolerate a live WAL: opencode is very
likely running while we read. On the scout's host this file is 1.0 GB, which is
why `probe`'s TTL cache is load-bearing rather than an optimisation — but the
query itself is indexed on (session_id, time_created, id) and touches only the
current session's rows.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import iso_utc, placeholder

HARNESS = "opencode"
DB = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode/opencode.db"


def read(worktree) -> "dict | None":
    worktree = str(worktree)
    db = DB
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # The CURRENT session for this worktree — newest main-thread session,
        # never a subagent's child row.
        sess = con.execute(
            "SELECT id FROM session WHERE directory=? AND parent_id IS NULL "
            "ORDER BY time_updated DESC, id DESC LIMIT 1", (worktree,)).fetchone()
        if sess is None:
            return None
        # Its last assistant message carrying an explicit model id. A fresh
        # session with nothing answered yet returns None — the current
        # session's honest answer, not the previous session's model.
        row = con.execute(
            "SELECT json_extract(data,'$.modelID'), json_extract(data,'$.time.created') "
            "FROM message WHERE session_id=? "
            "AND json_extract(data,'$.role')='assistant' "
            "AND json_extract(data,'$.modelID') IS NOT NULL "
            "ORDER BY time_created DESC, id DESC LIMIT 1", (sess[0],)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None or placeholder(row[0]):
        return None
    ms = row[1]
    return {"model": row[0],
            "observed_at": iso_utc(ms / 1000) if isinstance(ms, (int, float)) else None,
            "source": f"{db}#{sess[0]}"}
