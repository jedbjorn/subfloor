"""Stage 2 Sprint participant-conversation transaction contracts."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import sprint_participant_chats


@contextmanager
def substrate():
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
          shortname TEXT NOT NULL,
          flavor TEXT,
          user_id INTEGER REFERENCES users(user_id)
        );
        CREATE TABLE roadmap (
          feature_id INTEGER PRIMARY KEY,
          title TEXT NOT NULL
        );
        CREATE TABLE sprints (
          sprint_id INTEGER PRIMARY KEY,
          feature_id INTEGER NOT NULL REFERENCES roadmap(feature_id),
          originating_planner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
          conversation_generation TEXT NOT NULL,
          lifecycle TEXT NOT NULL,
          paused_at TEXT
        );
        CREATE TABLE sprint_specs (
          sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
          document_id INTEGER NOT NULL,
          bound_revision_sha256 TEXT NOT NULL,
          PRIMARY KEY(sprint_id,document_id)
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
          harness TEXT NOT NULL,
          model TEXT,
          effort TEXT,
          disposition TEXT NOT NULL,
          persistent_conversation_id TEXT REFERENCES conversations(conversation_id),
          current_conversation_id TEXT REFERENCES conversations(conversation_id),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
        CREATE TABLE sprint_work_units (
          work_unit_id INTEGER PRIMARY KEY,
          sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
          title TEXT NOT NULL,
          disposition TEXT NOT NULL,
          planned_wave INTEGER NOT NULL
        );
        INSERT INTO users VALUES (1);
        INSERT INTO roadmap VALUES (31,'Collaborative orchestration');
        INSERT INTO shells VALUES
          (10,'DEV1','dev',1),(20,'REV1','reviewer',1),(30,'PLN1','planner',1);
        INSERT INTO sprints
          (sprint_id,feature_id,originating_planner_shell_id,
           conversation_generation,lifecycle)
          VALUES (7,31,30,'0123456789abcdef0123456789abcdef','armed');
        INSERT INTO sprint_specs VALUES (7,46,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        INSERT INTO sprint_participants
          (participant_id,sprint_id,shell_id,role,harness,model,effort,disposition)
          VALUES (101,7,10,'developer','codex','gpt-test','high','active'),
                 (102,7,20,'reviewer','kimi','kimi-test','high','idle'),
                 (103,7,30,'planner','codex','planner-test','high','active');
        INSERT INTO sprint_work_units VALUES
          (700,7,'Participant chats','active',1),
          (701,7,'Already shipped','completed',0);
        """
    )
    try:
        yield con
    finally:
        con.close()


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
    return sprint_participant_chats.create_and_select(con, **values)


def test_arming_provisions_every_participant_without_a_wake() -> None:
    with substrate() as con:
        provisioned = sprint_participant_chats.provision_at_arming(con, 7)
        assert len(provisioned) == 3

        conversations = con.execute(
            "SELECT shell_id,conversation_scope,state "
            "FROM conversations ORDER BY shell_id"
        ).fetchall()
        assert [tuple(row) for row in conversations] == [
            (10, "sprint", "idle"),
            (20, "sprint", "idle"),
            (30, "sprint", "idle"),
        ]
        pointers = con.execute(
            "SELECT participant_id,persistent_conversation_id,"
            "current_conversation_id "
            "FROM sprint_participants ORDER BY participant_id"
        ).fetchall()
        assert [row["participant_id"] for row in pointers] == [101, 102, 103]
        assert all(
            row["persistent_conversation_id"] == row["current_conversation_id"]
            for row in pointers
        )
        assert {row["current_conversation_id"] for row in pointers} == set(provisioned)
        events = con.execute(
            "SELECT conversation_id,event_type,payload "
            "FROM conversation_events ORDER BY conversation_id"
        ).fetchall()
        assert len(events) == 3
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
            sprint_participant_chats.SprintConversationError,
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


def test_every_purpose_ignores_orphan_legacy_keys_and_replays_scoped_key() -> None:
    operations = {
        "work": "s7:p101:work",
        "fix": "review:55:fix:conversation",
        "merge": "review:56:merge:conversation",
        "fallback": "s7:p101:fallback:quota-1",
    }
    with substrate() as con:
        con.executemany(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,state,title,"
            "creation_idempotency_key,creation_request_hash,conversation_scope) "
            "VALUES (?,?,1,'codex','/historical','closed','Orphan',?,"
            "'historical-request','sprint')",
            (
                (f"cv_orphan_{purpose}", 10, key)
                for purpose, key in operations.items()
            ),
        )
        work = create(con, 101, "work", operations["work"])
        created = {"work": work}
        for purpose in ("fix", "merge", "fallback"):
            changes = {"parent_conversation_id": work}
            if purpose == "fallback":
                changes["context_packet"] = {"reason": "route exhausted"}
            created[purpose] = create(
                con, 101, purpose, operations[purpose], **changes
            )
            assert (
                create(con, 101, purpose, operations[purpose], **changes)
                == created[purpose]
            )

        assert len(set(created.values())) == 4
        keys = {
            row["purpose"]: row["creation_idempotency_key"]
            for row in con.execute(
                "SELECT link.purpose,c.creation_idempotency_key "
                "FROM sprint_participant_conversations link "
                "JOIN conversations c ON c.conversation_id=link.conversation_id"
            )
        }
        for purpose, operation_key in operations.items():
            assert keys[purpose] == sprint_participant_chats.conversation_idempotency_key(
                con, 101, purpose, operation_key
            )
            assert keys[purpose] != operation_key
            assert len(keys[purpose]) <= 255
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 8


def test_wake_reroutes_use_bounded_scoped_keys_and_replay_latest_route() -> None:
    with substrate() as con:
        work = create(con, 101, "work", "s7:p101:work")
        con.execute(
            "UPDATE conversations SET state='closed' WHERE conversation_id=?",
            (work,),
        )
        maximum_key = "k" * 255
        first = sprint_participant_chats.ensure_wake_conversation(
            con, 101, idempotency_key=maximum_key
        )
        assert first.created
        con.execute(
            "UPDATE conversations SET state='closed' WHERE conversation_id=?",
            (first.conversation_id,),
        )
        second = sprint_participant_chats.ensure_wake_conversation(
            con, 101, idempotency_key=maximum_key
        )
        assert second.created
        assert second.conversation_id != first.conversation_id

        replay = sprint_participant_chats.ensure_wake_conversation(
            con, 101, idempotency_key=maximum_key
        )
        assert replay == sprint_participant_chats.WakeConversationRoute(
            second.conversation_id, False
        )
        keys = [
            row[0]
            for row in con.execute(
                "SELECT c.creation_idempotency_key "
                "FROM sprint_participant_conversations link "
                "JOIN conversations c ON c.conversation_id=link.conversation_id "
                "WHERE link.sprint_participant_id=101 AND link.purpose='fallback' "
                "ORDER BY link.participant_conversation_id"
            )
        ]
        assert len(keys) == 2
        assert len(set(keys)) == 2
        assert all(key.startswith("sprint-generation:") for key in keys)
        assert all(len(key) <= 255 for key in keys)


def test_caller_transaction_rolls_back_every_row_when_pointer_selection_fails() -> None:
    with substrate() as con:
        con.execute(
            "CREATE TRIGGER reject_pointer BEFORE UPDATE OF current_conversation_id "
            "ON sprint_participants BEGIN SELECT RAISE(ABORT,'pointer fault'); END"
        )
        con.commit()

        with pytest.raises(sqlite3.IntegrityError, match="pointer fault"), con:
            create(con, 101, "work", "s7:p101:work")

        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert (
            con.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0] == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0]
            == 0
        )


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

        selected = sprint_participant_chats.select_work(con, 101)
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
            sprint_participant_chats.SprintConversationError,
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


def test_planner_fallback_generates_bounded_context_and_rejects_developer() -> None:
    with substrate() as con:
        provisioned = sprint_participant_chats.provision_at_arming(con, 7)

        fallback = sprint_participant_chats.create_planner_fallback(
            con,
            participant_id=103,
            reason="primary route quota exhausted",
            harness="kimi",
            model="kimi-fallback",
            effort="high",
            idempotency_key="s7:p103:fallback:quota-1",
        )

        row = con.execute(
            "SELECT c.harness,c.provider,l.parent_conversation_id,l.context_packet "
            "FROM conversations c JOIN sprint_participant_conversations l "
            "ON l.conversation_id=c.conversation_id WHERE c.conversation_id=?",
            (fallback,),
        ).fetchone()
        packet = json.loads(row["context_packet"])
        assert tuple(row)[:3] == ("kimi", "kimi", provisioned[2])
        assert packet == {
            "packet_version": 1,
            "reason": "primary route quota exhausted",
            "sprint": {
                "sprint_id": 7,
                "feature_id": 31,
                "feature_title": "Collaborative orchestration",
                "lifecycle": "armed",
            },
            "participant": {
                "participant_id": 103,
                "shell_id": 30,
                "shortname": "PLN1",
                "role": "planner",
                "disposition": "active",
                "previous_route": {
                    "harness": "codex",
                    "model": "planner-test",
                    "effort": "high",
                },
                "previous_conversation_id": provisioned[2],
            },
            "bound_specs": [
                {
                    "document_id": 46,
                    "revision_sha256": "a" * 64,
                }
            ],
            "open_work_units": [
                {
                    "work_unit_id": 700,
                    "title": "Participant chats",
                    "disposition": "active",
                }
            ],
        }
        assert (
            con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=103"
            ).fetchone()[0]
            == fallback
        )

        before = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        with pytest.raises(
            sprint_participant_chats.SprintConversationError,
            match="Planner-only",
        ):
            sprint_participant_chats.create_planner_fallback(
                con,
                participant_id=101,
                reason="not a planner",
                harness="kimi",
                model="kimi-fallback",
                effort="high",
                idempotency_key="s7:p101:fallback:invalid",
            )
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == before


def test_live_pill_projection_follows_current_pointer_until_terminal() -> None:
    with substrate() as con:
        provisioned = sprint_participant_chats.provision_at_arming(con, 7)
        work = con.execute(
            "SELECT persistent_conversation_id FROM sprint_participants "
            "WHERE participant_id=101"
        ).fetchone()[0]
        fix = create(
            con,
            101,
            "fix",
            "s7:p101:review:55:fix",
            parent_conversation_id=work,
        )
        shells = [{"shell_id": 10}, {"shell_id": 20}, {"shell_id": 30}]

        projected = sprint_participant_chats.attach_live_participations(con, shells)

        assert projected[0]["sprint"] == {
            "sprint_id": 7,
            "lifecycle": "armed",
            "role": "developer",
            "disposition": "active",
            "current_conversation_id": fix,
        }
        assert projected[1]["sprint"] == {
            "sprint_id": 7,
            "lifecycle": "armed",
            "role": "reviewer",
            "disposition": "idle",
            "current_conversation_id": provisioned[1],
        }

        con.execute("UPDATE sprints SET lifecycle='completed' WHERE sprint_id=7")
        terminal = sprint_participant_chats.attach_live_participations(
            con, [{"shell_id": 10}, {"shell_id": 20}]
        )
        assert terminal == [
            {"shell_id": 10, "sprint": None},
            {"shell_id": 20, "sprint": None},
        ]
