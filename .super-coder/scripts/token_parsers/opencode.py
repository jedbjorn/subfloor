"""opencode parser — ~/.local/share/opencode/opencode.db, `session` table.

The easiest source: per-session totals pre-aggregated in real columns, a
native `title`, and a `directory` column for the repo filter. Plain
read-only SQL — no JSONL walking.

The `model` column is JSON ({"id","providerID","variant"}): provider comes
straight from providerID; a non-default variant folds into the model label.

Caveat (spec doc #11): the token columns are NOT NULL DEFAULT 0, so "not
exposed" and "measured zero" are indistinguishable at that layer — trust
opencode's numbers as-is (status stays 'ok').

Sub-sessions (parent_id set — spawned subagents) are real spend in the same
directory and get their own rows; the ref is the session id either way.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import in_repo, iso_utc, row

HARNESS = "opencode"
PARSER_VERSION = "1"
DB = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode/opencode.db"


def _model_label(raw: "str | None", log) -> tuple:
    """(model, provider) from the JSON model column."""
    if not raw:
        return None, None
    try:
        m = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        log(f"opencode: unparseable model column {raw!r}")
        return str(raw), None
    mid = m.get("id")
    variant = m.get("variant")
    if mid and variant and variant != "default":
        mid = f"{mid} ({variant})"
    return mid, m.get("providerID")


def sweep(repo_root, since_epoch, log) -> list[dict]:
    if not DB.exists():
        return []
    rows: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        log(f"opencode: cannot open {DB}: {e}")
        return []
    try:
        cur = con.execute(
            "SELECT id, directory, title, model, time_created, time_updated, "
            "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
            "tokens_cache_write FROM session")
        for s in cur:
            if not in_repo(s["directory"], repo_root):
                continue
            updated = (s["time_updated"] or 0) / 1000  # epoch ms → s
            last = since_epoch(s["id"])
            if last is not None and updated <= last:
                continue
            model, provider = _model_label(s["model"], log)
            rows.append(row(
                harness=HARNESS, ref=s["id"], parser_version=PARSER_VERSION,
                provider=provider, model=model, title=s["title"],
                started_at=iso_utc((s["time_created"] or 0) / 1000 or None),
                ended_at=iso_utc(updated or None),
                input_tokens=s["tokens_input"], output_tokens=s["tokens_output"],
                cache_read_tokens=s["tokens_cache_read"],
                cache_write_tokens=s["tokens_cache_write"],
                reasoning_tokens=s["tokens_reasoning"],
                cwd=s["directory"]))
    except sqlite3.Error as e:
        log(f"opencode: session table read failed: {e} — format drift?")
    finally:
        con.close()
    return rows
