#!/usr/bin/env python3
"""Feature #24 conversation schema, transition, and rebuild contracts."""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
FOUNDATION = MIGRATIONS / "0132_conversation_foundation.sql"
GIT_TARGETS = MIGRATIONS / "0142_conversation_git_targets.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import conversation_state  # noqa: E402
import db_driver  # noqa: E402
import snapshot  # noqa: E402


def apply_schema(con: sqlite3.Connection, *, through: str | None = None) -> None:
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through is not None and migration.name > through:
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class ConversationDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self.con.execute(
            "INSERT INTO users (user_id, username) VALUES (1,'operator')"
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev1','dev','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (2,'Review','rev1','reviewer','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (3,'Planner','plan1','planner','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Conductor','con1','conductor','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (24,'doc','SPRINT: conversation test','x')"
        )
        self.con.commit()
        self.serial = 0

    def tearDown(self) -> None:
        self.con.close()

    def next_key(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}-{self.serial}"

    def add_conversation(
        self,
        *,
        shell_id: int = 1,
        state: str = "idle",
        mode: str = "normal",
    ) -> str:
        key = self.next_key("conversation")
        closed_at = "2026-07-29 00:00:00" if state == "closed" else None
        if mode == "normal":
            owner_user_id, sprint_doc_id = 1, None
        else:
            owner_user_id, sprint_doc_id = None, 24
        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,mode,owner_user_id,sprint_doc_id,harness,worktree,"
            "state,closed_at,creation_idempotency_key,creation_request_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                shell_id,
                mode,
                owner_user_id,
                sprint_doc_id,
                "codex",
                f"/tmp/worktree-{shell_id}",
                state,
                closed_at,
                key,
                f"hash-{key}",
            ),
        )
        return self.con.execute(
            "SELECT conversation_id FROM conversations "
            "WHERE creation_idempotency_key=?",
            (key,),
        ).fetchone()[0]

    def add_message(
        self,
        conversation_id: str,
        *,
        state: str = "accepted",
        caused_by_message_id: int | None = None,
    ) -> int:
        key = self.next_key("message")
        completed_at = (
            "2026-07-29 00:00:00"
            if state in {"completed", "failed", "cancelled"}
            else None
        )
        return self.con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,caused_by_message_id,state,"
            "completed_at) "
            "VALUES (?,'user','1','prompt','hello',?,?,?,?,?)",
            (
                conversation_id,
                key,
                f"hash-{key}",
                caused_by_message_id,
                state,
                completed_at,
            ),
        ).lastrowid

    def add_sprint(self) -> int:
        self.con.execute(
            "INSERT OR IGNORE INTO sprints "
            "(sprint_doc_id,state,legacy) VALUES (24,'declared',1)"
        )
        return 24

    def add_unit(self, *, seq: str = "U1") -> int:
        self.add_sprint()
        return self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title) VALUES (24,?,'Unit')",
            (seq,),
        ).lastrowid

    def add_directive(self, *, unit_id: int | None = None) -> int:
        self.add_sprint()
        return self.con.execute(
            "INSERT INTO directives "
            "(issuer_flavor,kind,target,sprint_doc_id,unit_id) "
            "VALUES ('system','stall','conductor',24,?)",
            (unit_id,),
        ).lastrowid

    def add_shell_message(self, *, kind: str = "result") -> int:
        self.add_sprint()
        return self.con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id,to_shell_id,body,kind,sprint_doc_id) "
            "VALUES (1,4,'Sprint evidence',?,24)",
            (kind,),
        ).lastrowid

    def add_binding(
        self,
        conversation_id: str,
        *,
        role: str = "conductor",
        slot: str | None = None,
        unit_id: int | None = None,
        source_directive_id: int | None = None,
        source_message_id: int | None = None,
        required_result_kind: str | None = None,
        state: str = "pending",
        outcome: str | None = None,
        result_message_id: int | None = None,
    ) -> int:
        self.add_sprint()
        return self.con.execute(
            "INSERT INTO sprint_conversation_bindings "
            "(conversation_id,sprint_doc_id,role,lifecycle,slot,unit_id,"
            "source_directive_id,source_message_id,required_result_kind,"
            "state,outcome,result_message_id,started_at,completed_at) "
            "VALUES (?,24,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                role,
                "persistent" if role == "conductor" else "one_shot",
                slot
                or {
                    "conductor": "con1",
                    "planner": "plan1",
                    "developer": "dev1",
                    "reviewer": "rev1",
                    "conformance": "rev1",
                }[role],
                unit_id,
                source_directive_id,
                source_message_id,
                required_result_kind,
                state,
                outcome,
                result_message_id,
                "2026-07-30 00:01:00" if state == "active" else None,
                "2026-07-30 00:02:00" if state == "terminal" else None,
            ),
        ).lastrowid

    def add_run(
        self,
        conversation_id: str,
        message_id: int,
        *,
        shell_id: int = 1,
        state: str = "leased",
    ) -> int:
        started_at = (
            "2026-07-29 00:00:00"
            if state in {"starting", "running"}
            else None
        )
        ended_at = (
            "2026-07-29 00:01:00"
            if state in {"succeeded", "failed", "cancelled", "unknown"}
            else None
        )
        return self.con.execute(
            "INSERT INTO conversation_runs "
            "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
            "lease_expires_at,started_at,ended_at) "
            "VALUES (?,?,?,?,?,'2026-07-29 00:10:00',?,?)",
            (
                conversation_id,
                shell_id,
                message_id,
                state,
                self.next_key("broker"),
                started_at,
                ended_at,
            ),
        ).lastrowid

    def add_outbox(
        self,
        conversation_id: str,
        message_id: int,
        *,
        state: str = "pending",
        run_id: int | None = None,
    ) -> int:
        claimed = state in {"claimed", "dispatched"}
        return self.con.execute(
            "INSERT INTO conversation_outbox "
            "(conversation_id,message_id,state,claim_owner,claimed_at,"
            "lease_expires_at,run_id,dispatched_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                message_id,
                state,
                "broker" if claimed else None,
                "2026-07-29 00:00:00" if claimed else None,
                "2026-07-29 00:10:00" if claimed else None,
                run_id if state == "dispatched" else None,
                "2026-07-29 00:01:00"
                if state == "dispatched"
                else None,
            ),
        ).lastrowid


class MigrationAndShapeTest(ConversationDbCase):
    def test_installed_fork_upgrades_from_pre_foundation_schema(self) -> None:
        con = sqlite3.connect(":memory:")
        apply_schema(con, through="0131_report_only_sprint_units.sql")
        self.assertIsNone(con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='conversations'"
        ).fetchone())
        con.executescript(FOUNDATION.read_text())
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "conversations",
            "conversation_messages",
            "conversation_runs",
            "conversation_events",
            "conversation_outbox",
        }.issubset(tables))
        con.close()

    def test_star_migration_defaults_legacy_rows_and_enforces_boolean_values(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(
                con,
                through="0133_one_open_normal_conversation_per_shell.sql",
            )
            con.execute(
                "INSERT INTO users (user_id,username) VALUES (9,'legacy')"
            )
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (9,'Legacy','legacy','dev','prompt',9)"
            )
            con.execute(
                "INSERT INTO conversations "
                "(shell_id,mode,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (9,'normal',9,'codex','/tmp/legacy','legacy','hash')"
            )

            con.executescript(
                (
                    MIGRATIONS / "0134_conversation_stars.sql"
                ).read_text()
            )

            self.assertEqual(
                con.execute(
                    "SELECT starred FROM conversations "
                    "WHERE creation_idempotency_key='legacy'"
                ).fetchone()[0],
                0,
            )
            con.execute(
                "UPDATE conversations SET starred=1 "
                "WHERE creation_idempotency_key='legacy'"
            )
            self.assertEqual(
                con.execute(
                    "SELECT starred FROM conversations "
                    "WHERE creation_idempotency_key='legacy'"
                ).fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE conversations SET starred=2 "
                    "WHERE creation_idempotency_key='legacy'"
                )

    def test_git_target_migration_upgrades_existing_conversations(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(
                con,
                through="0141_rebuild_completed_sprint_cancellations.sql",
            )
            con.execute(
                "INSERT INTO users (user_id,username) VALUES (9,'legacy')"
            )
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (9,'Legacy','legacy','dev','prompt',9)"
            )
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,mode,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES ('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',9,'normal',9,"
                "'codex','/tmp/legacy','legacy','hash')"
            )

            con.executescript(GIT_TARGETS.read_text())

            indexes = {
                row[1]
                for row in con.execute(
                    "PRAGMA index_list('conversation_git_targets')"
                )
            }
            self.assertTrue(
                {
                    "idx_conversation_git_targets_pr",
                    "idx_conversation_git_targets_local",
                    "idx_conversation_git_targets_recent",
                    "idx_conversation_git_targets_head",
                    "idx_conversation_git_targets_pr_lookup",
                }.issubset(indexes)
            )
            first = "1" * 40
            target_id = con.execute(
                "INSERT INTO conversation_git_targets "
                "(conversation_id,branch_name,base_ref,first_head_sha,"
                "latest_head_sha) VALUES "
                "('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','feature/reused',"
                "'origin/main',?,?) RETURNING target_id",
                (first, first),
            ).fetchone()[0]
            con.execute(
                "UPDATE conversation_git_targets SET latest_head_sha=? "
                "WHERE target_id=?",
                ("2" * 40, target_id),
            )
            con.execute(
                "UPDATE conversation_git_targets SET pr_number=821,"
                "pr_head_sha=?,pr_state='OPEN',"
                "remote_refreshed_at=datetime('now') WHERE target_id=?",
                ("2" * 40, target_id),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "PR identity is immutable",
            ):
                con.execute(
                    "UPDATE conversation_git_targets SET pr_number=822 "
                    "WHERE target_id=?",
                    (target_id,),
                )
            second = "3" * 40
            con.execute(
                "INSERT INTO conversation_git_targets "
                "(conversation_id,branch_name,base_ref,first_head_sha,"
                "latest_head_sha,pr_number) VALUES "
                "('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','feature/reused',"
                "'origin/main',?,?,822)",
                (second, second),
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM conversation_git_targets "
                    "WHERE branch_name='feature/reused'"
                ).fetchone()[0],
                2,
            )

    def test_binding_migration_preserves_reserved_sprint_conversation(
        self,
    ) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(
                con,
                through="0134_conversation_stars.sql",
            )
            con.execute(
                "INSERT INTO users (user_id,username) VALUES (9,'legacy')"
            )
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (9,'Conductor','con9','conductor','prompt',9)"
            )
            con.execute(
                "INSERT INTO documents "
                "(document_id,kind,title,body) "
                "VALUES (99,'doc','SPRINT: reserved','x')"
            )
            con.execute(
                "INSERT INTO sprints "
                "(sprint_doc_id,state,legacy) VALUES (99,'declared',1)"
            )
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,mode,sprint_doc_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES ('cv_reserved',9,'sprint',99,'opencode','/tmp/con9',"
                "'reserved','hash')"
            )

            con.executescript(
                (
                    MIGRATIONS / "0135_sprint_conversation_bindings.sql"
                ).read_text()
            )

            self.assertEqual(
                con.execute(
                    "SELECT conversation_id FROM conversations"
                ).fetchone()[0],
                "cv_reserved",
            )
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' "
                    "AND name='idx_conversations_live_sprint'"
                ).fetchone()
            )
            con.execute(
                "INSERT INTO sprint_conversation_bindings "
                "(conversation_id,sprint_doc_id,role,lifecycle,slot) "
                "VALUES ('cv_reserved',99,'conductor','persistent','con9')"
            )
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,mode,sprint_doc_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES ('cv_second',9,'sprint',99,'opencode','/tmp/con9',"
                "'second','hash')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO sprint_conversation_bindings "
                    "(conversation_id,sprint_doc_id,role,lifecycle,slot) "
                    "VALUES ('cv_second',99,'conductor','persistent','con9')"
                )

    def test_normal_and_reserved_sprint_ownership_shapes(self) -> None:
        normal = self.add_conversation()
        sprint = self.add_conversation(mode="sprint")
        self.assertTrue(normal.startswith("cv_"))
        self.assertTrue(sprint.startswith("cv_"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,mode,harness,worktree,creation_idempotency_key,"
                "creation_request_hash) "
                "VALUES (1,'normal','codex','/tmp/x','bad','hash')"
            )

    def test_sprint_binding_scopes_conductor_and_worker_uniqueness(self) -> None:
        self.add_sprint()
        conductor = self.add_conversation(shell_id=4, mode="sprint")
        conductor_binding = self.add_binding(conductor)

        unit_id = self.add_unit()
        directive_id = self.add_directive(unit_id=unit_id)
        worker = self.add_conversation(shell_id=1, mode="sprint")
        worker_binding = self.add_binding(
            worker,
            role="developer",
            slot="dev1",
            unit_id=unit_id,
            source_directive_id=directive_id,
            required_result_kind="unit-report",
        )

        second_conductor = self.add_conversation(shell_id=4, mode="sprint")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(second_conductor)

        duplicate = self.add_conversation(shell_id=1, mode="sprint")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(
                duplicate,
                role="developer",
                slot="dev1",
                unit_id=unit_id,
                source_directive_id=directive_id,
                required_result_kind="unit-report",
            )

        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state='active',started_at=datetime('now') "
            "WHERE binding_id IN (?,?)",
            (conductor_binding, worker_binding),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprint_conversation_bindings "
                "SET state='terminal',outcome='closed',"
                "completed_at=datetime('now') WHERE binding_id=?",
                (conductor_binding,),
            )
        self.con.execute(
            "UPDATE conversations "
            "SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id IN (?,?)",
            (conductor, worker),
        )
        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state='terminal',outcome='closed',completed_at=datetime('now') "
            "WHERE binding_id=?",
            (conductor_binding,),
        )
        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state='terminal',outcome='failed',completed_at=datetime('now') "
            "WHERE binding_id=?",
            (worker_binding,),
        )

        self.add_binding(second_conductor)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(
                duplicate,
                role="developer",
                slot="dev1",
                unit_id=unit_id,
                source_directive_id=directive_id,
                required_result_kind="unit-report",
            )

        self.assertEqual(
            [
                row[0]
                for row in self.con.execute(
                    "SELECT role FROM sprint_conversation_bindings "
                    "ORDER BY binding_id"
                ).fetchall()
            ],
            ["conductor", "developer", "conductor"],
        )

    def test_sprint_binding_rejects_wrong_mode_role_and_source_scope(
        self,
    ) -> None:
        normal = self.add_conversation(shell_id=4)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(normal)

        wrong_role = self.add_conversation(shell_id=1, mode="sprint")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(wrong_role, role="planner")

        self.add_sprint()
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (25,'doc','SPRINT: other','x')"
        )
        self.con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy) VALUES (25,'declared',1)"
        )
        other_directive = self.con.execute(
            "INSERT INTO directives "
            "(issuer_flavor,kind,target,sprint_doc_id) "
            "VALUES ('system','stall','conductor',25)"
        ).lastrowid
        planner = self.add_conversation(shell_id=3, mode="sprint")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_binding(
                planner,
                role="planner",
                slot="plan1",
                source_directive_id=other_directive,
                required_result_kind="answer",
            )

    def test_sprint_binding_allows_source_to_release_another_sprint_unit(
        self,
    ) -> None:
        source_unit_id = self.add_unit(seq="U1")
        assignment_unit_id = self.add_unit(seq="U2")
        directive_id = self.add_directive(unit_id=source_unit_id)
        conversation = self.add_conversation(shell_id=1, mode="sprint")

        binding_id = self.add_binding(
            conversation,
            role="developer",
            slot="dev1",
            unit_id=assignment_unit_id,
            source_directive_id=directive_id,
            required_result_kind="unit-report",
        )

        self.assertEqual(
            tuple(self.con.execute(
                "SELECT sprint_doc_id,unit_id,source_directive_id "
                "FROM sprint_conversation_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()),
            (24, assignment_unit_id, directive_id),
        )

    def test_succeeded_assignment_requires_scoped_result_and_is_immutable(
        self,
    ) -> None:
        unit_id = self.add_unit()
        directive_id = self.add_directive(unit_id=unit_id)
        conversation = self.add_conversation(shell_id=1, mode="sprint")
        binding_id = self.add_binding(
            conversation,
            role="developer",
            slot="dev1",
            unit_id=unit_id,
            source_directive_id=directive_id,
            required_result_kind="unit-report",
        )
        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state='active',started_at=datetime('now') "
            "WHERE binding_id=?",
            (binding_id,),
        )

        wrong_kind = self.add_shell_message(kind="task")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprint_conversation_bindings "
                "SET state='terminal',outcome='succeeded',"
                "result_message_id=?,completed_at=datetime('now') "
                "WHERE binding_id=?",
                (wrong_kind, binding_id),
            )

        result_message = self.add_shell_message()
        self.con.execute(
            "UPDATE conversations "
            "SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (conversation,),
        )
        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state='terminal',outcome='succeeded',"
            "result_message_id=?,completed_at=datetime('now') "
            "WHERE binding_id=?",
            (result_message, binding_id),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprint_conversation_bindings "
                "SET outcome='failed',result_message_id=NULL "
                "WHERE binding_id=?",
                (binding_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "DELETE FROM sprint_conversation_bindings WHERE binding_id=?",
                (binding_id,),
            )

    def test_conversation_and_message_idempotency_are_scoped(self) -> None:
        conversation = self.add_conversation()
        row = self.con.execute(
            "SELECT owner_user_id,creation_idempotency_key "
            "FROM conversations WHERE conversation_id=?",
            (conversation,),
        ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (2,?,'claude','/tmp/other',?,'different-hash')",
                tuple(row),
            )

        key = self.next_key("idem")
        self.con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash) "
            "VALUES (?,'user','1','prompt','one',?,'hash-one')",
            (conversation, key),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash) "
                "VALUES (?,'user','1','prompt','two',?,'hash-two')",
                (conversation, key),
            )
        other = self.add_conversation(shell_id=2)
        self.con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash) "
            "VALUES (?,'user','1','prompt','other',?,'hash-other')",
            (other, key),
        )

    def test_outbox_is_one_dispatch_intent_per_message(self) -> None:
        conversation = self.add_conversation()
        message = self.add_message(conversation)
        self.add_outbox(conversation, message)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_outbox(conversation, message)

    def test_cross_conversation_links_are_refused(self) -> None:
        first = self.add_conversation()
        second = self.add_conversation(shell_id=2)
        message = self.add_message(first)
        self.add_message(first, caused_by_message_id=message)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_message(second, caused_by_message_id=message)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_run(second, message, shell_id=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_outbox(second, message)

        run = self.add_run(first, message)
        second_message = self.add_message(second)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_outbox(
                second,
                second_message,
                state="dispatched",
                run_id=run,
            )
        other_message = self.add_message(first)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_outbox(
                first,
                other_message,
                state="dispatched",
                run_id=run,
            )


class TransitionMatrixTest(ConversationDbCase):
    def update_conversation(self, row_id: str, target: str) -> None:
        self.con.execute(
            "UPDATE conversations SET state=?,closed_at=? "
            "WHERE conversation_id=?",
            (
                target,
                "2026-07-29 00:02:00" if target == "closed" else None,
                row_id,
            ),
        )

    def conversation_at(self, state: str) -> str:
        row_id = self.add_conversation()
        paths = {
            "idle": (),
            "queued": ("queued",),
            "running": ("queued", "running"),
            "waiting": ("queued", "running", "waiting"),
            "error": ("queued", "running", "error"),
            "closed": ("closed",),
        }
        for target in paths[state]:
            self.update_conversation(row_id, target)
        return row_id

    def update_message(self, row_id: int, target: str) -> None:
        self.con.execute(
            "UPDATE conversation_messages SET state=?,completed_at=? "
            "WHERE message_id=?",
            (
                target,
                "2026-07-29 00:02:00"
                if target in {"completed", "failed", "cancelled"}
                else None,
                row_id,
            ),
        )

    def message_at(self, state: str) -> int:
        row_id = self.add_message(self.add_conversation())
        paths = {
            "accepted": (),
            "queued": ("queued",),
            "running": ("queued", "running"),
            "completed": ("completed",),
            "failed": ("failed",),
            "cancelled": ("cancelled",),
        }
        for target in paths[state]:
            self.update_message(row_id, target)
        return row_id

    def update_run(self, row_id: int, target: str) -> None:
        started_at = (
            "2026-07-29 00:00:00"
            if target in {"starting", "running"}
            else None
        )
        ended_at = (
            "2026-07-29 00:02:00"
            if target in {"succeeded", "failed", "cancelled", "unknown"}
            else None
        )
        self.con.execute(
            "UPDATE conversation_runs "
            "SET state=?,started_at=?,ended_at=? WHERE run_id=?",
            (target, started_at, ended_at, row_id),
        )

    def run_at(self, state: str) -> int:
        conversation = self.add_conversation()
        message = self.add_message(conversation)
        row_id = self.add_run(conversation, message)
        paths = {
            "leased": (),
            "starting": ("starting",),
            "running": ("starting", "running"),
            "succeeded": ("starting", "running", "succeeded"),
            "failed": ("failed",),
            "cancelled": ("cancelled",),
            "unknown": ("unknown",),
        }
        for target in paths[state]:
            self.update_run(row_id, target)
        return row_id

    def terminal_run_for_message(
        self,
        conversation_id: str,
        message_id: int,
    ) -> int:
        run_id = self.add_run(conversation_id, message_id)
        self.update_run(run_id, "failed")
        return run_id

    def update_outbox(
        self,
        row_id: int,
        target: str,
        *,
        run_id: int | None = None,
    ) -> None:
        claimed = target in {"claimed", "dispatched"}
        self.con.execute(
            "UPDATE conversation_outbox "
            "SET state=?,claim_owner=?,claimed_at=?,lease_expires_at=?,"
            "run_id=?,dispatched_at=? WHERE outbox_id=?",
            (
                target,
                "broker" if claimed else None,
                "2026-07-29 00:00:00" if claimed else None,
                "2026-07-29 00:10:00" if claimed else None,
                run_id if target == "dispatched" else None,
                "2026-07-29 00:02:00"
                if target == "dispatched"
                else None,
                row_id,
            ),
        )

    def outbox_at(
        self,
        state: str,
    ) -> tuple[int, str, int, int | None]:
        conversation = self.add_conversation()
        message = self.add_message(conversation)
        row_id = self.add_outbox(conversation, message)
        run_id = None
        if state == "claimed":
            self.update_outbox(row_id, "claimed")
        elif state == "dispatched":
            self.update_outbox(row_id, "claimed")
            run_id = self.terminal_run_for_message(conversation, message)
            self.update_outbox(row_id, "dispatched", run_id=run_id)
        elif state == "cancelled":
            self.update_outbox(row_id, "cancelled")
        return row_id, conversation, message, run_id

    def update_sprint_binding(self, row_id: int, target: str) -> None:
        if target == "terminal":
            self.con.execute(
                "UPDATE conversations "
                "SET state='closed',closed_at='2026-07-30 00:02:00' "
                "WHERE conversation_id=("
                "SELECT conversation_id "
                "FROM sprint_conversation_bindings WHERE binding_id=?"
                ")",
                (row_id,),
            )
        self.con.execute(
            "UPDATE sprint_conversation_bindings "
            "SET state=?,outcome=?,started_at=?,completed_at=? "
            "WHERE binding_id=?",
            (
                target,
                "closed" if target == "terminal" else None,
                "2026-07-30 00:01:00"
                if target in {"active", "terminal"}
                else None,
                "2026-07-30 00:02:00"
                if target == "terminal"
                else None,
                row_id,
            ),
        )

    def sprint_binding_at(self, state: str) -> int:
        conversation = self.add_conversation(shell_id=4, mode="sprint")
        row_id = self.add_binding(conversation)
        paths = {
            "pending": (),
            "active": ("active",),
            "terminal": ("active", "terminal"),
        }
        for target in paths[state]:
            self.update_sprint_binding(row_id, target)
        return row_id

    def assert_matrix(
        self,
        machine: str,
        factory,
        updater,
    ) -> None:
        transitions = conversation_state.MACHINES[machine]
        for old, allowed in transitions.items():
            for new in transitions:
                with self.subTest(machine=machine, edge=f"{old}->{new}"):
                    self.con.execute("SAVEPOINT transition_case")
                    try:
                        made = factory(old)
                        row_id = (
                            made[0] if isinstance(made, tuple) else made
                        )
                        legal = new == old or new in allowed
                        try:
                            updater(row_id, new, made)
                        except sqlite3.IntegrityError:
                            db_allowed = False
                        else:
                            db_allowed = True
                        self.assertEqual(db_allowed, legal)
                        self.assertEqual(
                            conversation_state.transition_allowed(
                                machine, old, new
                            ),
                            legal,
                        )
                    finally:
                        self.con.execute("ROLLBACK TO transition_case")
                        self.con.execute("RELEASE transition_case")

    def test_exhaustive_conversation_edges_match_helper(self) -> None:
        self.assert_matrix(
            "conversation",
            self.conversation_at,
            lambda row_id, target, _made: self.update_conversation(
                row_id, target
            ),
        )

    def test_exhaustive_message_edges_match_helper(self) -> None:
        self.assert_matrix(
            "message",
            self.message_at,
            lambda row_id, target, _made: self.update_message(row_id, target),
        )

    def test_exhaustive_run_edges_match_helper(self) -> None:
        self.assert_matrix(
            "run",
            self.run_at,
            lambda row_id, target, _made: self.update_run(row_id, target),
        )

    def test_exhaustive_outbox_edges_match_helper(self) -> None:
        def update(row_id, target, made):
            _row_id, conversation, message, existing_run = made
            run_id = existing_run
            if target == "dispatched" and run_id is None:
                run_id = self.terminal_run_for_message(
                    conversation, message
                )
            self.update_outbox(row_id, target, run_id=run_id)

        self.assert_matrix("outbox", self.outbox_at, update)

    def test_exhaustive_sprint_binding_edges_match_helper(self) -> None:
        self.assert_matrix(
            "sprint_binding",
            self.sprint_binding_at,
            lambda row_id, target, _made: self.update_sprint_binding(
                row_id,
                target,
            ),
        )

    def test_typed_error_names_edge_and_legal_targets(self) -> None:
        with self.assertRaises(
            conversation_state.ConversationStateError
        ) as raised:
            conversation_state.require_transition(
                "conversation", "closed", "idle"
            )
        self.assertIn("closed -> idle", str(raised.exception))
        self.assertIn("terminal", str(raised.exception))


class FenceAndEventTest(ConversationDbCase):
    def test_open_chat_migration_closes_older_legacy_rows(self) -> None:
        self.con.execute("DROP INDEX idx_conversations_live_normal_shell")
        older = self.add_conversation(shell_id=1)
        self.con.execute(
            "UPDATE conversations SET last_activity_at='2026-01-01 00:00:00' "
            "WHERE conversation_id=?",
            (older,),
        )
        newer = self.add_conversation(shell_id=1)
        self.con.execute(
            "UPDATE conversations SET last_activity_at='2026-07-29 00:00:00' "
            "WHERE conversation_id=?",
            (newer,),
        )

        self.con.executescript(
            (ENGINE / "migrations"
             / "0133_one_open_normal_conversation_per_shell.sql").read_text()
        )

        states = dict(
            self.con.execute(
                "SELECT conversation_id,state FROM conversations "
                "WHERE shell_id=1"
            ).fetchall()
        )
        self.assertEqual(states[older], "closed")
        self.assertEqual(states[newer], "idle")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_conversation(shell_id=1)

    def test_one_live_run_per_conversation_and_shell(self) -> None:
        first = self.add_conversation()
        first_message = self.add_message(first)
        first_run = self.add_run(first, first_message)

        second_message = self.add_message(first)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_run(first, second_message)

        # The run fence remains independent of the newer open-chat fence. A
        # closed historical row supplies a second same-shell conversation
        # without violating the one-open-normal-conversation invariant.
        second = self.add_conversation(state="closed")
        second_message = self.add_message(second)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_run(second, second_message)

        self.con.execute(
            "UPDATE conversation_runs "
            "SET state='failed',ended_at=datetime('now') WHERE run_id=?",
            (first_run,),
        )
        self.add_run(second, second_message)

    def test_one_open_normal_conversation_per_shell(self) -> None:
        self.add_conversation(shell_id=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_conversation(shell_id=1)
        self.add_conversation(shell_id=1, state="closed")
        self.add_conversation(shell_id=2)

    def test_different_shells_may_run_concurrently(self) -> None:
        first = self.add_conversation(shell_id=1)
        second = self.add_conversation(shell_id=2)
        self.add_run(first, self.add_message(first), shell_id=1)
        self.add_run(second, self.add_message(second), shell_id=2)

    def test_events_are_ordered_append_only_and_scoped(self) -> None:
        conversation = self.add_conversation()
        message = self.add_message(conversation)
        run = self.add_run(conversation, message)
        self.con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id,run_id) "
            "VALUES (?,1,'run.started',?, ?, ?)",
            (conversation, json.dumps({"ok": True}), message, run),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type) "
                "VALUES (?,1,'duplicate')",
                (conversation,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE conversation_events SET event_type='changed'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM conversation_events")

        other = self.add_conversation(shell_id=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,message_id) "
                "VALUES (?,1,'wrong.message',?)",
                (other, message),
            )

    def test_bounded_message_and_event_payloads(self) -> None:
        conversation = self.add_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash) "
                "VALUES (?,'user','1','prompt',?,?,'hash')",
                (
                    conversation,
                    "x" * (1048576 + 1),
                    self.next_key("large-message"),
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,payload) "
                "VALUES (?,1,'bad.payload','[]')",
                (conversation,),
            )


class SprintBindingConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        con = db_driver.connect(self.db_path)
        apply_schema(con)
        con.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Developer','dev1','dev','prompt',1)"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Conductor','con1','conductor','prompt',1)"
        )
        con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (24,'doc','SPRINT: concurrent bindings','x')"
        )
        con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy) VALUES (24,'declared',1)"
        )
        self.unit_id = con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title) VALUES (24,'U1','Unit')"
        ).lastrowid
        self.directive_id = con.execute(
            "INSERT INTO directives "
            "(issuer_flavor,kind,target,sprint_doc_id,unit_id) "
            "VALUES ('system','stall','conductor',24,?)",
            (self.unit_id,),
        ).lastrowid
        con.commit()
        con.close()

    def race_bindings(
        self,
        *,
        role: str,
        shell_id: int,
        unit_id: int | None,
        source_directive_id: int | None,
    ) -> list[bool]:
        barrier = threading.Barrier(3)

        def create(owner: str) -> bool:
            con = db_driver.connect(self.db_path)
            try:
                barrier.wait(timeout=5)
                try:
                    with db_driver.write_transaction(
                        con,
                        "test.sprint_binding",
                    ):
                        conversation_id = f"cv_{owner}"
                        con.execute(
                            "INSERT INTO conversations "
                            "(conversation_id,shell_id,mode,sprint_doc_id,"
                            "harness,worktree,creation_idempotency_key,"
                            "creation_request_hash) "
                            "VALUES (?,?, 'sprint',24,?,?,?,?)",
                            (
                                conversation_id,
                                shell_id,
                                "opencode" if role == "conductor" else "codex",
                                f"/tmp/{owner}",
                                f"create-{owner}",
                                f"hash-{owner}",
                            ),
                        )
                        con.execute(
                            "INSERT INTO sprint_conversation_bindings "
                            "(conversation_id,sprint_doc_id,role,lifecycle,"
                            "slot,unit_id,source_directive_id,"
                            "required_result_kind) "
                            "VALUES (?,24,?,?,?,?,?,?)",
                            (
                                conversation_id,
                                role,
                                "persistent"
                                if role == "conductor"
                                else "one_shot",
                                "con1" if role == "conductor" else "dev1",
                                unit_id,
                                source_directive_id,
                                None
                                if role == "conductor"
                                else "unit-report",
                            ),
                        )
                except sqlite3.IntegrityError:
                    return False
                return True
            finally:
                con.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            workers = [
                pool.submit(create, f"broker-{index}")
                for index in (1, 2)
            ]
            barrier.wait(timeout=5)
            return [worker.result(timeout=10) for worker in workers]

    def assert_single_binding(self, results: list[bool], role: str) -> None:
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(results), 1)
        con = db_driver.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM sprint_conversation_bindings "
                    "WHERE role=?",
                    (role,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM conversations "
                    "WHERE mode='sprint'"
                ).fetchone()[0],
                1,
            )
        finally:
            con.close()

    def test_parallel_conductor_bindings_create_exactly_one(self) -> None:
        results = self.race_bindings(
            role="conductor",
            shell_id=4,
            unit_id=None,
            source_directive_id=None,
        )
        self.assert_single_binding(results, "conductor")

    def test_parallel_assignment_bindings_create_exactly_one(self) -> None:
        results = self.race_bindings(
            role="developer",
            shell_id=1,
            unit_id=self.unit_id,
            source_directive_id=self.directive_id,
        )
        self.assert_single_binding(results, "developer")


class SnapshotPolicyTest(ConversationDbCase):
    TABLES = (
        "conversations",
        "conversation_git_targets",
        "sprint_conversation_bindings",
        "sprint_cancellations",
        "conversation_messages",
        "conversation_runs",
        "conversation_events",
        "conversation_outbox",
    )

    def test_conversation_tables_are_snapshotted_in_dependency_order(self) -> None:
        positions = [
            snapshot.PER_INSTANCE_TABLES.index(table)
            for table in self.TABLES
        ]
        self.assertEqual(positions, sorted(positions))

    def test_snapshot_round_trip_preserves_sprint_binding(self) -> None:
        self.add_sprint()
        conversation = self.add_conversation(shell_id=4, mode="sprint")
        self.add_binding(conversation, slot="con1")

        body = ["PRAGMA foreign_keys=OFF;", "BEGIN;"]
        for table in ("conversations", "sprint_conversation_bindings"):
            body.extend(snapshot.dump_table(self.con, table))
        body.extend(["COMMIT;", "PRAGMA foreign_keys=ON;"])

        target = sqlite3.connect(":memory:")
        apply_schema(target)
        target.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        target.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Conductor','con1','conductor','prompt',1)"
        )
        target.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (24,'doc','SPRINT: conversation test','x')"
        )
        target.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy) VALUES (24,'declared',1)"
        )
        target.executescript("\n".join(body))

        self.assertEqual(
            target.execute(
                "SELECT conversation_id,sprint_doc_id,role,lifecycle,slot,"
                "state FROM sprint_conversation_bindings"
            ).fetchone(),
            (conversation, 24, "conductor", "persistent", "con1", "pending"),
        )
        self.assertEqual(
            target.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        target.close()

    def test_snapshot_round_trip_preserves_queue_and_recovery_evidence(
            self) -> None:
        conversation = self.add_conversation()
        message = self.add_message(conversation, state="queued")
        run = self.add_run(
            conversation, message, state="unknown"
        )
        self.con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id,run_id) "
            "VALUES (?,1,'run.failed','{\"delivery\":\"unknown\"}',?,?)",
            (conversation, message, run),
        )
        self.add_outbox(conversation, message)

        body = ["PRAGMA foreign_keys=OFF;", "BEGIN;"]
        for table in self.TABLES:
            body.extend(snapshot.dump_table(self.con, table))
        body.extend(["COMMIT;", "PRAGMA foreign_keys=ON;"])

        target = sqlite3.connect(":memory:")
        apply_schema(target)
        target.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        target.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev1','dev','prompt',1)"
        )
        target.executescript("\n".join(body))
        self.assertEqual(
            target.execute(
                "SELECT state,harness,worktree FROM conversations"
            ).fetchone(),
            ("idle", "codex", "/tmp/worktree-1"),
        )
        self.assertEqual(
            target.execute(
                "SELECT state,body FROM conversation_messages"
            ).fetchone(),
            ("queued", "hello"),
        )
        self.assertEqual(
            target.execute(
                "SELECT state,error_detail FROM conversation_runs"
            ).fetchone(),
            ("unknown", None),
        )
        self.assertEqual(
            target.execute(
                "SELECT event_type,payload FROM conversation_events"
            ).fetchone(),
            ("run.failed", '{"delivery":"unknown"}'),
        )
        self.assertEqual(
            target.execute(
                "SELECT state FROM conversation_outbox"
            ).fetchone()[0],
            "pending",
        )
        self.assertEqual(target.execute(
            "PRAGMA foreign_key_check"
        ).fetchall(), [])
        target.close()


if __name__ == "__main__":
    unittest.main()
