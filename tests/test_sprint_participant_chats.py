"""Active-registry Sprint wake-chat contracts after topology retirement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import active_chat_registry
import sprint_participant_chats
from conversation_broker import BrokerRun
from conversation_launch import ConversationLaunchPreparer


@contextmanager
def substrate(database: str = ":memory:"):
    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE users (user_id INTEGER PRIMARY KEY);
        CREATE TABLE shells (
          shell_id INTEGER PRIMARY KEY,
          shortname TEXT NOT NULL,
          flavor TEXT,
          user_id INTEGER REFERENCES users(user_id),
          is_deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE flavor_defaults (
          flavor TEXT NOT NULL,
          harness TEXT NOT NULL,
          model TEXT NOT NULL,
          is_default INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (flavor, harness)
        );
        CREATE TABLE roadmap (feature_id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE sprints (
          sprint_id INTEGER PRIMARY KEY,
          feature_id INTEGER NOT NULL REFERENCES roadmap(feature_id),
          originating_planner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
          conversation_generation TEXT NOT NULL,
          lifecycle TEXT NOT NULL,
          paused_at TEXT
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
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          closed_at TEXT,
          last_activity_at TEXT NOT NULL DEFAULT (datetime('now')),
          version INTEGER NOT NULL DEFAULT 1,
          UNIQUE(owner_user_id, creation_idempotency_key)
        );
        CREATE UNIQUE INDEX one_open_chat_per_shell
          ON conversations(shell_id) WHERE state<>'closed';
        CREATE TABLE active_shell_chats (
          shell_id INTEGER PRIMARY KEY REFERENCES shells(shell_id),
          chat_id TEXT NOT NULL UNIQUE REFERENCES conversations(conversation_id),
          process_pid INTEGER,
          process_start_ticks INTEGER,
          updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          CHECK (
            (process_pid IS NULL AND process_start_ticks IS NULL)
            OR
            (process_pid IS NOT NULL AND process_start_ticks IS NOT NULL)
          )
        );
        CREATE TRIGGER clear_active_chat_after_close
        AFTER UPDATE OF state ON conversations
        WHEN NEW.state='closed' AND OLD.state<>'closed'
        BEGIN
          DELETE FROM active_shell_chats WHERE chat_id=NEW.conversation_id;
        END;
        CREATE TABLE conversation_events (
          event_id INTEGER PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          sequence INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          payload TEXT NOT NULL,
          run_id INTEGER,
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
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE sprint_participant_conversations (
          participant_conversation_id INTEGER PRIMARY KEY,
          sprint_participant_id INTEGER NOT NULL
            REFERENCES sprint_participants(participant_id),
          conversation_id TEXT NOT NULL UNIQUE
            REFERENCES conversations(conversation_id),
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TRIGGER validate_sprint_conversation_link
        BEFORE INSERT ON sprint_participant_conversations
        WHEN NOT EXISTS (
          SELECT 1 FROM sprint_participants p
          JOIN conversations c ON c.conversation_id=NEW.conversation_id
          WHERE p.participant_id=NEW.sprint_participant_id
            AND p.shell_id=c.shell_id
            AND c.conversation_scope='sprint'
        )
        BEGIN
          SELECT RAISE(ABORT,'invalid Sprint participant conversation link');
        END;
        CREATE TRIGGER immutable_sprint_conversation_link
        BEFORE UPDATE ON sprint_participant_conversations
        BEGIN
          SELECT RAISE(ABORT,'Sprint participant conversation links are immutable');
        END;
        INSERT INTO users VALUES (1);
        INSERT INTO roadmap VALUES (31,'Collaborative orchestration');
        INSERT INTO shells VALUES
          (10,'DEV1','dev',1,0),(20,'REV1','reviewer',1,0),
          (30,'PLN1','planner',1,0);
        INSERT INTO flavor_defaults VALUES
          ('dev','codex','gpt-test',1),
          ('reviewer','kimi','kimi-test',1),
          ('planner','codex','planner-test',1);
        INSERT INTO sprints VALUES
          (7,31,30,'0123456789abcdef0123456789abcdef','armed',NULL);
        INSERT INTO sprint_participants
          (participant_id,sprint_id,shell_id,role,harness,model,effort,disposition)
          VALUES (101,7,10,'developer','codex','gpt-test','high','active'),
                 (102,7,20,'reviewer','kimi','kimi-test','high','idle'),
                 (103,7,30,'planner','codex','planner-test','high','active');
        """
    )
    try:
        yield con
    finally:
        con.close()


def create_wake(
    con: sqlite3.Connection,
    *,
    wake_id: int,
    participant_id: int = 101,
) -> str:
    con.execute("BEGIN")
    try:
        conversation_id = sprint_participant_chats.create_wake_conversation(
            con,
            wake_id=wake_id,
            sprint_id=7,
            participant_id=participant_id,
        )
    except Exception:
        con.rollback()
        raise
    con.commit()
    return conversation_id


@pytest.mark.parametrize(
    ("role", "label", "skill"),
    (
        ("developer", "Developer", "sprint_dev"),
        ("reviewer", "Reviewer", "sprint_rev"),
        ("planner", "Originating Planner", "sprint_pln"),
    ),
)
def test_wake_prompt_retries_only_unconfirmed_sprint_writes(
    role: str, label: str, skill: str
) -> None:
    prompt = sprint_participant_chats.wake_prompt(23, role)

    assert f"Sprint 23 handoff for your {label} role. Load `{skill}`." in prompt
    assert "If the handoff is not complete" not in prompt
    assert "run `sc sprint inbox --sprint 23` again" not in prompt
    assert (
        "If a Sprint command failed or did not confirm its durable write, "
        "retry that command."
    ) in prompt
    assert (
        "Do not re-check the inbox otherwise — new messages arrive as their own wakes."
    ) in prompt


def test_wake_creation_registers_one_chat_and_flat_history() -> None:
    with substrate() as con:
        conversation_id = create_wake(con, wake_id=41)

        conversation = con.execute(
            "SELECT shell_id,state,conversation_scope,creation_idempotency_key "
            "FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        assert tuple(conversation) == (
            10,
            "idle",
            "sprint",
            "generation:0123456789abcdef0123456789abcdef:wake:41",
        )
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT shell_id,chat_id FROM active_shell_chats"
            )
        ] == [(10, conversation_id)]
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT sprint_participant_id,conversation_id "
                "FROM sprint_participant_conversations"
            )
        ] == [(101, conversation_id)]

        payload = json.loads(
            con.execute(
                "SELECT payload FROM conversation_events WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        )
        assert payload == {
            "participant_id": 101,
            "scope": "sprint",
            "sprint_id": 7,
            "wake_id": 41,
        }


def test_replay_restores_registry_without_duplicating_history() -> None:
    with substrate() as con:
        conversation_id = create_wake(con, wake_id=42)
        con.execute("DELETE FROM active_shell_chats WHERE shell_id=10")
        con.commit()

        replayed = create_wake(con, wake_id=42)

        assert replayed == conversation_id
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM sprint_participant_conversations"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT chat_id FROM active_shell_chats WHERE shell_id=10"
        ).fetchone()[0] == conversation_id


def test_new_wake_requires_committed_close_then_preserves_history() -> None:
    with substrate() as con:
        first = create_wake(con, wake_id=43)

        with pytest.raises(
            sprint_participant_chats.WakeConversationBusy,
            match="another chat became active",
        ):
            create_wake(con, wake_id=44)
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1

        con.execute("BEGIN")
        closed = active_chat_registry.close_for_wake(con, 10)
        con.commit()
        assert closed is not None and closed.chat_id == first

        second = create_wake(con, wake_id=44)
        assert second != first
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT conversation_id,state FROM conversations ORDER BY rowid"
            )
        ] == [(first, "closed"), (second, "idle")]
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT conversation_id FROM sprint_participant_conversations "
                "ORDER BY participant_conversation_id"
            )
        ] == [(first,), (second,)]


def test_closed_planner_reenter_persists_canonical_default_route(tmp_path) -> None:
    db_path = tmp_path / "shell.db"
    with substrate(str(db_path)) as con:
        first = create_wake(con, wake_id=45, participant_id=103)
        con.execute("BEGIN")
        closed = active_chat_registry.close_for_wake(con, 30)
        con.execute(
            "UPDATE sprint_participants SET model=NULL,effort=NULL "
            "WHERE participant_id=103"
        )
        con.commit()
        assert closed is not None and closed.chat_id == first

        replacement = create_wake(con, wake_id=46, participant_id=103)
        stored = con.execute(
            "SELECT harness,provider,model,effort,worktree,title,"
            "creation_request_hash "
            "FROM conversations WHERE conversation_id=?",
            (replacement,),
        ).fetchone()

    broker_run = BrokerRun(
        run_id=7,
        conversation_id=replacement,
        message_id=8,
        shell_id=30,
        harness=stored["harness"],
        provider=stored["provider"],
        model=stored["model"],
        effort=stored["effort"],
        worktree=Path(stored["worktree"]),
        title=stored["title"],
        body="Pick up the Planner handoff",
        session_before=None,
        session_after=None,
        runner_ref=None,
        state="leased",
    )
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **_: SimpleNamespace(
            cwd=stored["worktree"],
            archive_id=42,
            harness="codex",
            model="planner-test",
            effort="high",
            env={},
        ),
        liveness=lambda: {"supported": True, "processes": []},
    )

    context, archive_id = preparer(broker_run)

    assert archive_id == 42
    assert context.model == "planner-test"
    assert context.effort == "high"
    assert context.env["SC_CONVERSATION_SURFACE"] == "sprint"
    assert tuple(
        stored[field] for field in ("harness", "provider", "model", "effort")
    ) == (
        "codex",
        "openai",
        "planner-test",
        "high",
    )
    expected_request = {
        "effort": "high",
        "harness": "codex",
        "model": "planner-test",
        "participant_id": 103,
        "provider": "openai",
        "sprint_id": 7,
        "wake_id": 46,
        "worktree": stored["worktree"],
    }
    assert stored["creation_request_hash"] == hashlib.sha256(
        json.dumps(expected_request, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def test_non_sprint_wake_ignores_prior_removed_harness_route() -> None:
    with substrate() as con:
        con.execute(
            "INSERT INTO conversations ("
            "conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
            "worktree,state,title,creation_idempotency_key,creation_request_hash,"
            "created_at,closed_at) VALUES ("
            "'cv_removed',10,1,'unsupported','example','legacy-chat','high',"
            "'/removed','closed','Removed history','removed','removed-hash',"
            "'2026-08-28 00:00:00','2026-08-28 00:01:00')"
        )
        con.commit()

        prepared = sprint_participant_chats.prepare_shell_wake_conversation(con, 10)

    assert (
        prepared.harness,
        prepared.provider,
        prepared.model,
        prepared.effort,
    ) == ("codex", "openai", "gpt-test", "high")


def test_explicit_participant_route_is_preserved_byte_for_byte() -> None:
    with substrate() as con:
        exact_model = "GPT-Test@Exact/Case"
        exact_effort = "xHigh"
        con.execute(
            "UPDATE sprint_participants SET model=?,effort=? "
            "WHERE participant_id=101",
            (exact_model, exact_effort),
        )
        con.commit()

        conversation_id = create_wake(con, wake_id=47)
        stored = con.execute(
            "SELECT model,effort FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()

        assert tuple(stored) == (exact_model, exact_effort)


def test_unresolvable_participant_route_creates_no_chat() -> None:
    with substrate() as con:
        con.execute(
            "UPDATE sprint_participants SET model=NULL,effort=NULL "
            "WHERE participant_id=103"
        )
        con.execute(
            "DELETE FROM flavor_defaults WHERE flavor='planner' AND harness='codex'"
        )
        con.commit()

        with pytest.raises(
            sprint_participant_chats.SprintConversationError,
            match=(
                "no model was supplied and no flavor default exists for it; "
                "supply an explicit model"
            ),
        ):
            create_wake(con, wake_id=48, participant_id=103)

        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM active_shell_chats").fetchone()[0] == 0


def test_flat_link_rejects_cross_shell_and_is_immutable() -> None:
    with substrate() as con:
        conversation_id = create_wake(con, wake_id=45)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid Sprint participant conversation link",
        ):
            con.execute(
                "INSERT INTO sprint_participant_conversations "
                "(sprint_participant_id,conversation_id) VALUES (?,?)",
                (102, conversation_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="Sprint participant conversation links are immutable",
        ):
            con.execute(
                "UPDATE sprint_participant_conversations "
                "SET sprint_participant_id=102 WHERE conversation_id=?",
                (conversation_id,),
            )


def test_live_projection_reads_registry_and_allows_zero_chat() -> None:
    with substrate() as con:
        developer_chat = create_wake(con, wake_id=46)
        shells = [{"shell_id": 10}, {"shell_id": 20}, {"shell_id": 30}]

        projected = sprint_participant_chats.attach_live_participations(con, shells)

        assert projected[0]["sprint"] == {
            "sprint_id": 7,
            "lifecycle": "armed",
            "role": "developer",
            "disposition": "active",
            "current_conversation_id": developer_chat,
        }
        assert projected[1]["sprint"]["current_conversation_id"] is None
        assert projected[2]["sprint"]["current_conversation_id"] is None

        con.execute("UPDATE sprints SET lifecycle='completed' WHERE sprint_id=7")
        terminal = sprint_participant_chats.attach_live_participations(
            con, [{"shell_id": 10}]
        )
        assert terminal == [{"shell_id": 10, "sprint": None}]
        assert con.execute(
            "SELECT chat_id FROM active_shell_chats WHERE shell_id=10"
        ).fetchone()[0] == developer_chat
