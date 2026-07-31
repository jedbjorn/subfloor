"""Stage 2 Sprint participant-conversation transaction contracts."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import sprint_conversations


def substrate() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE users (
          user_id INTEGER PRIMARY KEY
        );
        CREATE TABLE shells (
          shell_id INTEGER PRIMARY KEY,
          shortname TEXT NOT NULL
        );
        CREATE TABLE sprints (
          sprint_id INTEGER PRIMARY KEY,
          owner_user_id INTEGER NOT NULL REFERENCES users(user_id),
          lifecycle TEXT NOT NULL
        );
        CREATE TABLE conversations (
          conversation_id TEXT PRIMARY KEY,
          shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
          owner_user_id INTEGER NOT NULL REFERENCES users(user_id),
          harness TEXT NOT NULL,
          provider TEXT,
          model TEXT,
          effort TEXT,
          worktree TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'idle',
          title TEXT,
          creation_idempotency_key TEXT NOT NULL,
          creation_request_hash TEXT NOT NULL,
          conversation_scope TEXT NOT NULL DEFAULT 'normal',
          UNIQUE(owner_user_id, creation_idempotency_key)
        );
        CREATE TABLE conversation_events (
          event_id INTEGER PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          sequence INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          payload TEXT NOT NULL,
          UNIQUE(conversation_id, sequence)
        );
        CREATE TABLE sprint_participants (
          participant_id INTEGER PRIMARY KEY,
          sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
          shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
          role TEXT NOT NULL,
          disposition TEXT NOT NULL,
          current_conversation_id TEXT REFERENCES conversations(conversation_id)
        );
        CREATE TABLE sprint_participant_conversations (
          participant_conversation_id INTEGER PRIMARY KEY,
          sprint_participant_id INTEGER NOT NULL
            REFERENCES sprint_participants(participant_id),
          conversation_id TEXT NOT NULL UNIQUE
            REFERENCES conversations(conversation_id),
          purpose TEXT NOT NULL,
          parent_conversation_id TEXT REFERENCES conversations(conversation_id),
          context_packet TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX one_work_conversation
          ON sprint_participant_conversations(sprint_participant_id)
          WHERE purpose='work';
        INSERT INTO users VALUES (1);
        INSERT INTO shells VALUES (10,'DEV1'),(20,'REV1');
        INSERT INTO sprints VALUES (7,1,'armed');
        INSERT INTO sprint_participants
          (participant_id,sprint_id,shell_id,role,disposition)
          VALUES (101,7,10,'developer','working'),
                 (102,7,20,'reviewer','waiting');
        """
    )
    return con


def create(
    con: sqlite3.Connection,
    participant_id: int,
    purpose: str,
    key: str,
    **changes,
) -> str:
    values = {
        "participant_id": participant_id,
        "owner_user_id": 1,
        "purpose": purpose,
        "harness": "codex",
        "provider": "openai",
        "model": "gpt-test",
        "effort": "high",
        "worktree": f"/worktrees/{participant_id}",
        "title": f"Sprint 7 {purpose}",
        "idempotency_key": key,
    }
    values.update(changes)
    return sprint_conversations.create_and_select(con, **values)


def test_arming_provisions_every_participant_without_a_wake() -> None:
    with substrate() as con:
        developer = create(con, 101, "work", "s7:p101:work")
        reviewer = create(con, 102, "work", "s7:p102:work")

        conversations = con.execute(
            "SELECT conversation_id,shell_id,conversation_scope,state "
            "FROM conversations ORDER BY shell_id"
        ).fetchall()
        assert [tuple(row) for row in conversations] == [
            (developer, 10, "sprint", "idle"),
            (reviewer, 20, "sprint", "idle"),
        ]
        pointers = con.execute(
            "SELECT participant_id,current_conversation_id "
            "FROM sprint_participants ORDER BY participant_id"
        ).fetchall()
        assert [tuple(row) for row in pointers] == [
            (101, developer),
            (102, reviewer),
        ]
        events = con.execute(
            "SELECT conversation_id,event_type,payload "
            "FROM conversation_events ORDER BY conversation_id"
        ).fetchall()
        assert len(events) == 2
        assert {json.loads(row["payload"])["purpose"] for row in events} == {"work"}
        assert all(row["event_type"] == "conversation.created" for row in events)


def test_replay_is_idempotent_and_conflicting_reuse_changes_nothing() -> None:
    with substrate() as con:
        work = create(con, 101, "work", "s7:p101:work")
        replay = create(con, 101, "work", "s7:p101:work")
        assert replay == work
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert (
            con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0]
            == 1
        )

        with pytest.raises(
            sprint_conversations.SprintConversationError,
            match="different request",
        ):
            create(
                con,
                101,
                "work",
                "s7:p101:work",
                title="Changed after retry",
            )
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=101"
            ).fetchone()[0]
            == work
        )
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_fix_conversation_becomes_current_without_closing_persistent_work() -> None:
    with substrate() as con:
        work = create(con, 101, "work", "s7:p101:work")
        fix = create(
            con,
            101,
            "fix",
            "s7:p101:review:55:fix",
            parent_conversation_id=work,
        )

        rows = con.execute(
            "SELECT c.conversation_id,c.state,l.purpose,l.parent_conversation_id "
            "FROM conversations c JOIN sprint_participant_conversations l "
            "ON l.conversation_id=c.conversation_id ORDER BY l.purpose"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (fix, "idle", "fix", work),
            (work, "idle", "work", None),
        ]
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=101"
            ).fetchone()[0]
            == fix
        )

        selected = sprint_conversations.select_work(con, 101)
        assert selected == work
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=101"
            ).fetchone()[0]
            == work
        )


def test_cross_participant_parent_is_rejected_without_partial_rows() -> None:
    with substrate() as con:
        developer = create(con, 101, "work", "s7:p101:work")
        reviewer = create(con, 102, "work", "s7:p102:work")

        with pytest.raises(
            sprint_conversations.SprintConversationError,
            match="does not belong",
        ):
            create(
                con,
                101,
                "merge",
                "s7:p101:review:9:merge",
                parent_conversation_id=reviewer,
            )

        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=101"
            ).fetchone()[0]
            == developer
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations "
                "WHERE purpose='merge'"
            ).fetchone()[0]
            == 0
        )


def test_fallback_replacement_retains_packet_route_and_history() -> None:
    packet = {
        "sprint_id": 7,
        "reason": "planner route exhausted",
        "pending": ["review unit 2"],
    }
    with substrate() as con:
        work = create(con, 101, "work", "s7:p101:work")
        fallback = create(
            con,
            101,
            "fallback",
            "s7:p101:fallback:1",
            parent_conversation_id=work,
            context_packet=packet,
            harness="kimi",
            provider="moonshot",
            model="kimi-test",
        )

        replacement = con.execute(
            "SELECT c.harness,c.provider,c.model,c.state,l.parent_conversation_id,"
            "l.context_packet FROM conversations c "
            "JOIN sprint_participant_conversations l "
            "ON l.conversation_id=c.conversation_id "
            "WHERE c.conversation_id=?",
            (fallback,),
        ).fetchone()
        assert tuple(replacement) == (
            "kimi",
            "moonshot",
            "kimi-test",
            "idle",
            work,
            json.dumps(packet, separators=(",", ":"), sort_keys=True),
        )
        assert (
            con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?", (work,)
            ).fetchone()[0]
            == "idle"
        )
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=101"
            ).fetchone()[0]
            == fallback
        )
