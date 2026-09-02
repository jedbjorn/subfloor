#!/usr/bin/env python3
"""Feature #24 conversation schema, transition, and rebuild contracts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
FOUNDATION = MIGRATIONS / "0132_conversation_foundation.sql"
GIT_TARGETS = MIGRATIONS / "0142_conversation_git_targets.sql"
ACTIVE_REGISTRY = MIGRATIONS / "0162_active_chat_registry.sql"
REAPER_IDENTITY = MIGRATIONS / "0163_conversation_run_process_identity.sql"
TOPOLOGY_RETIREMENT = MIGRATIONS / "0168_retire_sprint_conversation_topology.sql"
LIVE_NATIVE_ROUTES = MIGRATIONS / "0238_final_schema_rebaseline.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import conversation_state  # noqa: E402
import db_driver  # noqa: E402
import migrate  # noqa: E402
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
        self.con.commit()
        self.serial = 0

    def tearDown(self) -> None:
        self.con.close()

    def next_key(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}-{self.serial}"

    def test_thinking_route_migration_preserves_legacy_and_gates_v2(self) -> None:
        conversation = self.add_conversation()
        legacy = self.con.execute(
            "SELECT route_contract_version,route_binding FROM conversations "
            "WHERE conversation_id=?", (conversation,),
        ).fetchone()
        preset = self.con.execute(
            "SELECT effort FROM flavor_defaults LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(legacy), (1, None))
        self.assertEqual(preset["effort"], None)

        values = (
            1, 1, "codex", "/tmp/v2", "v2-null", "hash-v2-null",
        )
        with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "conversation route contract and binding disagree"):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash,"
                "route_contract_version,route_binding) "
                "VALUES (?,?,?,?,?,?,2,NULL)", values,
            )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM conversations "
                "WHERE creation_idempotency_key='v2-null'"
            ).fetchone()[0],
            0,
        )

        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash,"
            "route_contract_version,route_binding) "
            "VALUES (?,?,?,?,?,?,2,'{}')",
            (2, 1, "codex", "/tmp/v2", "v2-bound", "hash-v2-bound"),
        )
        with self.assertRaisesRegex(
                sqlite3.IntegrityError, "conversation route identity is immutable"):
            self.con.execute(
                "UPDATE conversations SET effort='low' "
                "WHERE creation_idempotency_key='v2-bound'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE flavor_defaults SET effort=' HIGH ' "
                "WHERE rowid=(SELECT rowid FROM flavor_defaults LIMIT 1)"
            )

    def test_live_native_migration_preserves_v2_and_admits_only_bound_v3(self) -> None:
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,model,worktree,"
            "creation_idempotency_key,creation_request_hash,"
            "route_contract_version,route_binding) "
            "VALUES ('cv_v2_preserved',1,1,'codex','gpt-test','/tmp/v2',"
            "'v2-preserved','hash-v2-preserved',2,'{\"contract_version\":2}')"
        )
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,"
            "creation_idempotency_key,creation_request_hash,"
            "route_contract_version,route_binding) "
            "VALUES ('cv_v3_exact',2,1,'opencode','ollama-cloud/glm-5.2',"
            "'MAX.Future','/tmp/v3','v3-exact','hash-v3-exact',3,"
            "'{\"contract_version\":3}')"
        )

        rows = self.con.execute(
            "SELECT conversation_id,route_contract_version,route_binding "
            "FROM conversations WHERE conversation_id IN "
            "('cv_v2_preserved','cv_v3_exact') ORDER BY conversation_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("cv_v2_preserved", 2, '{"contract_version":2}'),
                ("cv_v3_exact", 3, '{"contract_version":3}'),
            ],
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "conversation route contract and binding disagree",
        ):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,model,worktree,"
                "creation_idempotency_key,creation_request_hash,"
                "route_contract_version,route_binding) "
                "VALUES (3,1,'opencode','ollama-cloud/glm-5.2','/tmp/v3-null',"
                "'v3-null','hash-v3-null',3,NULL)"
            )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM conversations "
                "WHERE creation_idempotency_key='v3-null'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def add_conversation(
        self,
        *,
        shell_id: int = 1,
        state: str = "idle",
    ) -> str:
        key = self.next_key("conversation")
        closed_at = "2026-07-29 00:00:00" if state == "closed" else None
        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,"
            "state,closed_at,creation_idempotency_key,creation_request_hash) "
            "VALUES (?,1,?,?,?,?,?,?)",
            (
                shell_id,
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
    def test_live_native_migration_preserves_routes_and_dependent_registry(
        self,
    ) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        apply_schema(con, through="0234_reseed_ci_fallback_authority.sql")
        con.execute("INSERT INTO users (user_id,username) VALUES (41,'migration')")
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (41,'Migration','mig41','dev','prompt',41)"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (42,'Migration v3','mig42','dev','prompt',41)"
        )
        con.execute(
            "INSERT OR REPLACE INTO flavor_defaults "
            "(flavor,harness,model,is_default,effort) VALUES "
            "('dev','opencode','ollama-cloud/glm-5.2',0,'high')"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,"
            "creation_idempotency_key,creation_request_hash,"
            "route_contract_version,route_binding) VALUES "
            "('cv_before_v3',41,41,'opencode','ollama-cloud/glm-5.2','high',"
            "'/tmp/migration','before-v3','before-v3-hash',2,"
            "'{\"contract_version\":2}')"
        )
        con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) "
            "VALUES (41,'cv_before_v3')"
        )
        before = tuple(con.execute(
            "SELECT * FROM conversations WHERE conversation_id='cv_before_v3'"
        ).fetchone())
        con.commit()

        migrate.apply(con, LIVE_NATIVE_ROUTES)

        self.assertEqual(
            tuple(con.execute(
                "SELECT * FROM conversations "
                "WHERE conversation_id='cv_before_v3'"
            ).fetchone()),
            before,
        )
        self.assertEqual(
            tuple(con.execute(
                "SELECT shell_id,chat_id FROM active_shell_chats"
            ).fetchone()),
            (41, "cv_before_v3"),
        )
        self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIsNotNone(con.execute(
            "SELECT 1 FROM schema_migrations WHERE filename=?",
            (LIVE_NATIVE_ROUTES.name,),
        ).fetchone())

        con.execute(
            "UPDATE flavor_defaults SET effort='MAX.Future' "
            "WHERE flavor='dev' AND harness='opencode'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO flavor_defaults "
                "(flavor,harness,model,effort) "
                "VALUES ('strict-lower','claude','sonnet','MAX.Future')"
            )
        con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,model,effort,worktree,"
            "creation_idempotency_key,creation_request_hash,"
            "route_contract_version,route_binding) VALUES "
            "(42,41,'opencode','ollama-cloud/glm-5.2','MAX.Future',"
            "'/tmp/migration-v3','after-v3','after-v3-hash',3,"
            "'{\"contract_version\":3}')"
        )

    def test_installed_fork_upgrades_from_pre_foundation_schema(self) -> None:
        con = sqlite3.connect(":memory:")
        apply_schema(con, through="0129_reseed_api_identity_wording.sql")
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

    def test_reaper_identity_migration_preserves_legacy_live_run(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(con, through="0162_active_chat_registry.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (9,'legacy')")
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (9,'Legacy','legacy','dev','prompt',9)"
            )
            con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,state,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (9,9,'codex','/tmp/legacy','running','legacy','hash')"
            )
            conversation_id = con.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE creation_idempotency_key='legacy'"
            ).fetchone()[0]
            message_id = con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'user','9','prompt','live','message','hash','running')",
                (conversation_id,),
            ).lastrowid
            run_id = con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at) VALUES "
                "(?,9,?,'running','legacy-broker','2999-01-01 00:00:00',"
                "'2026-08-02 00:00:00')",
                (conversation_id, message_id),
            ).lastrowid

            con.executescript(REAPER_IDENTITY.read_text())

            row = con.execute(
                "SELECT state,process_pid,process_start_ticks,process_group_id,"
                "reaper_last_signal,reaper_signaled_at FROM conversation_runs "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self.assertEqual(
                row,
                ("running", None, None, None, None, None),
            )

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
                "(shell_id,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (9,9,'codex','/tmp/legacy','legacy','hash')"
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
                through="0134_conversation_stars.sql",
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
                "(conversation_id,shell_id,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES ('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',9,9,"
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

    def test_active_registry_migration_converges_legacy_open_chats(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(
                con,
                through="0161_reseed_flags_output_guidance.sql",
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
                "(conversation_id,shell_id,owner_user_id,harness,worktree,"
                "conversation_scope,creation_idempotency_key,"
                "creation_request_hash,state,last_activity_at) VALUES "
                "('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',9,9,'codex',"
                "'/tmp/legacy','normal','legacy-normal','hash-normal','queued',"
                "'2026-08-02 10:00:00'),"
                "('cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',9,9,'codex',"
                "'/tmp/legacy','sprint','legacy-running','hash-running','running',"
                "'2026-08-02 11:00:00'),"
                "('cv_cccccccccccccccccccccccccccccccc',9,9,'codex',"
                "'/tmp/legacy','sprint','legacy-newest','hash-newest','idle',"
                "'2026-08-02 12:00:00')"
            )
            con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) VALUES "
                "('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','user','9','prompt',"
                "'queued','queued-message','hash-queued','queued'),"
                "('cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','user','9','prompt',"
                "'running','running-message','hash-running','running'),"
                "('cv_cccccccccccccccccccccccccccccccc','user','9','prompt',"
                "'active','active-message','hash-active','queued')"
            )
            con.execute(
                "INSERT INTO conversation_outbox "
                "(conversation_id,message_id,state,claim_owner,claimed_at,"
                "lease_expires_at) "
                "SELECT conversation_id,message_id,'pending',NULL,NULL,NULL "
                "FROM conversation_messages "
                "WHERE conversation_id IN ("
                "'cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'cv_cccccccccccccccccccccccccccccccc')"
            )
            con.execute(
                "INSERT INTO conversation_outbox "
                "(conversation_id,message_id,state,claim_owner,claimed_at,"
                "lease_expires_at) "
                "SELECT conversation_id,message_id,'claimed','broker',"
                "'2026-08-02 11:01:00','2026-08-02 11:06:00' "
                "FROM conversation_messages WHERE conversation_id="
                "'cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'"
            )

            con.executescript(ACTIVE_REGISTRY.read_text())
            con.executescript(ACTIVE_REGISTRY.read_text())

            states = {
                row["conversation_id"]: row["state"]
                for row in con.execute(
                    "SELECT conversation_id,state FROM conversations"
                )
            }
            active = con.execute(
                "SELECT shell_id,chat_id,process_pid,process_start_ticks "
                "FROM active_shell_chats"
            ).fetchall()
            self.assertEqual(
                states,
                {
                    "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "closed",
                    "cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": "closed",
                    "cv_cccccccccccccccccccccccccccccccc": "idle",
                },
            )
            self.assertEqual(
                [tuple(row) for row in active],
                [
                    (
                        9,
                        "cv_cccccccccccccccccccccccccccccccc",
                        None,
                        None,
                    )
                ],
            )
            queued_work = [
                tuple(row)
                for row in con.execute(
                    "SELECT m.conversation_id,m.state,o.state,"
                    "m.completed_at IS NOT NULL,o.claim_owner "
                    "FROM conversation_messages m "
                    "JOIN conversation_outbox o USING (message_id) "
                    "ORDER BY m.conversation_id"
                )
            ]
            self.assertEqual(
                queued_work,
                [
                    (
                        "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "cancelled",
                        "cancelled",
                        1,
                        None,
                    ),
                    (
                        "cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "cancelled",
                        "cancelled",
                        1,
                        None,
                    ),
                    (
                        "cv_cccccccccccccccccccccccccccccccc",
                        "queued",
                        "pending",
                        0,
                        None,
                    ),
                ],
            )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "active chat must be open and belong to its shell",
            ):
                con.execute(
                    "UPDATE active_shell_chats SET chat_id="
                    "'cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE shell_id=9"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE active_shell_chats SET process_pid=123 "
                    "WHERE shell_id=9"
                )

            con.execute(
                "UPDATE conversations SET state='closed',closed_at=datetime('now') "
                "WHERE conversation_id='cv_cccccccccccccccccccccccccccccccc'"
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM active_shell_chats").fetchone()[0],
                0,
            )

    def test_topology_retirement_converges_dirty_links_and_universal_fence(self):
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0167_reseed_sprint_authority_split.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (9,'legacy')")
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (9,'Legacy','legacy','dev','prompt',9)"
            )
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title) VALUES ('Legacy Sprint')"
                ).lastrowid
            )
            sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                    "VALUES (?,9,1)",
                    (feature_id,),
                ).lastrowid
            )
            participant_id = int(
                con.execute(
                    "INSERT INTO sprint_participants "
                    "(sprint_id,shell_id,role,harness) "
                    "VALUES (?,9,'developer','codex')",
                    (sprint_id,),
                ).lastrowid
            )
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,worktree,"
                "creation_idempotency_key,creation_request_hash,conversation_scope) "
                "VALUES ('cv_legacy_sprint',9,9,'codex','/tmp/legacy',"
                "'legacy-sprint','legacy-hash','sprint')"
            )
            link_id = int(
                con.execute(
                    "INSERT INTO sprint_participant_conversations "
                    "(sprint_participant_id,conversation_id,purpose) "
                    "VALUES (?,'cv_legacy_sprint','work')",
                    (participant_id,),
                ).lastrowid
            )
            con.execute(
                "UPDATE sprint_participants SET persistent_conversation_id="
                "'cv_legacy_sprint',current_conversation_id='cv_legacy_sprint' "
                "WHERE participant_id=?",
                (participant_id,),
            )
            con.execute(
                "INSERT INTO active_shell_chats (shell_id,chat_id) "
                "VALUES (9,'cv_legacy_sprint')"
            )

            con.executescript(TOPOLOGY_RETIREMENT.read_text())

            self.assertEqual(
                set(),
                {"persistent_conversation_id", "current_conversation_id"}
                & {
                    row[1]
                    for row in con.execute("PRAGMA table_info(sprint_participants)")
                },
            )
            self.assertEqual(
                set(),
                {"purpose", "parent_conversation_id", "context_packet"}
                & {
                    row[1]
                    for row in con.execute(
                        "PRAGMA table_info(sprint_participant_conversations)"
                    )
                },
            )
            self.assertEqual(
                (link_id, participant_id, "cv_legacy_sprint"),
                tuple(
                    con.execute(
                        "SELECT participant_conversation_id,"
                        "sprint_participant_id,conversation_id "
                        "FROM sprint_participant_conversations"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                [], [tuple(row) for row in con.execute("PRAGMA foreign_key_check")]
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO conversations "
                    "(conversation_id,shell_id,owner_user_id,harness,worktree,"
                    "creation_idempotency_key,creation_request_hash,"
                    "conversation_scope) VALUES "
                    "('cv_second_sprint',9,9,'codex','/tmp/legacy',"
                    "'second-sprint','second-hash','sprint')"
                )

    def test_conversation_requires_direct_user_owner(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,harness,worktree,creation_idempotency_key,"
                "creation_request_hash) "
                "VALUES (1,'codex','/tmp/x','bad','hash')"
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

    def test_typed_error_names_edge_and_legal_targets(self) -> None:
        with self.assertRaises(
            conversation_state.ConversationStateError
        ) as raised:
            conversation_state.require_transition(
                "conversation", "closed", "running"
            )
        self.assertIn("closed -> running", str(raised.exception))
        self.assertIn("allowed: idle", str(raised.exception))
        with self.assertRaises(
            conversation_state.ConversationStateError
        ) as raised:
            conversation_state.require_transition(
                "message", "completed", "queued"
            )
        self.assertIn("completed -> queued", str(raised.exception))
        self.assertIn("terminal", str(raised.exception))


class FenceAndEventTest(ConversationDbCase):
    def test_open_chat_migration_closes_older_legacy_rows(self) -> None:
        self.con.execute("DROP INDEX idx_conversations_one_open_shell")
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


class SnapshotPolicyTest(ConversationDbCase):
    TABLES = (
        "conversations",
        "active_shell_chats",
        "conversation_git_targets",
        "conversation_boot_snapshots",
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

    def test_snapshot_round_trip_preserves_queue_and_recovery_evidence(
            self) -> None:
        conversation = self.add_conversation()
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (1,?)",
            (conversation,),
        )
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
                "SELECT shell_id,chat_id,process_pid,process_start_ticks "
                "FROM active_shell_chats"
            ).fetchone(),
            (1, conversation, None, None),
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


class BootSnapshotSchemaCase(ConversationDbCase):
    """Spec #163 conversation_boot_snapshots schema contract."""

    MIGRATION = MIGRATIONS / "0224_conversation_boot_snapshots.sql"
    DIGEST = hashlib.sha256(b"boot content").hexdigest()

    def bind(self, conversation: str, **overrides) -> None:
        row = {
            "conversation_id": conversation,
            "content": "boot content",
            "content_sha256": self.DIGEST,
            "content_bytes": len(b"boot content"),
            "format_version": 1,
            "binding_origin": "new_conversation",
        }
        row.update(overrides)
        self.con.execute(
            "INSERT INTO conversation_boot_snapshots "
            "(conversation_id,content,content_sha256,content_bytes,"
            "format_version,binding_origin) "
            "VALUES (?,?,?,?,?,?)",
            (
                row["conversation_id"],
                row["content"],
                row["content_sha256"],
                row["content_bytes"],
                row["format_version"],
                row["binding_origin"],
            ),
        )

    def test_round_trip_and_bound_at_default(self) -> None:
        conversation = self.add_conversation()
        self.bind(conversation)
        row = self.con.execute(
            "SELECT content,content_sha256,content_bytes,format_version,"
            "binding_origin,bound_at FROM conversation_boot_snapshots "
            "WHERE conversation_id=?",
            (conversation,),
        ).fetchone()
        self.assertEqual(
            tuple(row)[:5],
            ("boot content", self.DIGEST, 12, 1, "new_conversation"),
        )
        self.assertIsNotNone(row[5])

    def test_one_snapshot_per_conversation(self) -> None:
        conversation = self.add_conversation()
        self.bind(conversation)
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, binding_origin="legacy_first_resume")

    def test_digest_must_be_lowercase_hex_sha256(self) -> None:
        conversation = self.add_conversation()
        for bad in ("x" * 64, "A" + self.DIGEST[1:], self.DIGEST[:63]):
            with self.subTest(digest=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.bind(conversation, content_sha256=bad)

    def test_content_is_bounded_and_non_empty(self) -> None:
        conversation = self.add_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, content="", content_bytes=0)
        oversized = "a" * (1048576 + 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(
                conversation,
                content=oversized,
                content_bytes=len(oversized),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, content="ok", content_bytes=1048577)

    def test_content_bytes_agrees_with_utf8_bytes(self) -> None:
        conversation = self.add_conversation()
        text = "boot ünïcode"  # 13 chars, 15 UTF-8 bytes
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, content=text, content_bytes=len(text))
        self.bind(
            conversation,
            content=text,
            content_bytes=len(text.encode("utf-8")),
        )

    def test_format_version_and_origin_are_constrained(self) -> None:
        conversation = self.add_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, format_version=0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.bind(conversation, binding_origin="fresh_render")

    def test_snapshot_is_immutable(self) -> None:
        conversation = self.add_conversation()
        self.bind(conversation)
        for column, value in (
            ("content", "tampered"),
            ("content_sha256", "f" * 64),
            ("content_bytes", 99),
            ("format_version", 2),
            ("binding_origin", "legacy_first_resume"),
            ("bound_at", "1999-01-01 00:00:00"),
        ):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.con.execute(
                        "UPDATE conversation_boot_snapshots "
                        f"SET {column}=? WHERE conversation_id=?",
                        (value, conversation),
                    )

    def test_snapshot_follows_conversation_delete_cascade(self) -> None:
        conversation = self.add_conversation()
        self.bind(conversation)
        self.con.execute(
            "DELETE FROM conversations WHERE conversation_id=?",
            (conversation,),
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT 1 FROM conversation_boot_snapshots "
                "WHERE conversation_id=?",
                (conversation,),
            ).fetchone(),
        )

    def test_migration_leaves_legacy_conversations_unbound(self) -> None:
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        apply_schema(legacy, through="0223_model_default_effort_binding.sql")
        legacy.execute(
            "INSERT INTO users (user_id, username) VALUES (1,'operator')"
        )
        legacy.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev1','dev','prompt',1)"
        )
        legacy.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/tmp/worktree-1','idle','legacy','legacy-h')"
        )
        legacy.executescript(self.MIGRATION.read_text())
        legacy.execute("PRAGMA foreign_keys=ON")
        conversation = legacy.execute(
            "SELECT conversation_id FROM conversations"
        ).fetchone()[0]
        self.assertIsNone(
            legacy.execute(
                "SELECT 1 FROM conversation_boot_snapshots "
                "WHERE conversation_id=?",
                (conversation,),
            ).fetchone(),
        )
        self.assertEqual(
            tuple(
                legacy.execute(
                    "SELECT state,harness FROM conversations"
                ).fetchone()
            ),
            ("idle", "codex"),
        )
        self.assertEqual(
            legacy.execute("PRAGMA foreign_key_check").fetchall(), []
        )
        legacy.close()


if __name__ == "__main__":
    unittest.main()
