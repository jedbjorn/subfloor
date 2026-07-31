#!/usr/bin/env python3
"""Stage 1 gates for the Sprints v2 domain and lifecycle switch."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
FOUNDATION = MIGRATIONS / "0146_sprint_v2_domain.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import sprint_domain  # noqa: E402


def apply_schema(con: sqlite3.Connection, *, through: str | None = None) -> None:
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through is not None and migration.name > through:
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintDomainCase(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
                (4, "Other planner", "PLN2", "planner", "prompt"),
            ),
        )
        self.con.commit()
        self.store = sprint_domain.SprintLifecycleStore(self.con)
        self.serial = 0

    def create_sprint(
        self,
        *,
        merge_grant: bool = True,
        approval_verdict: str = "pass",
    ) -> tuple[int, int]:
        self.serial += 1
        serial = self.serial
        feature_id = self.con.execute(
            "INSERT INTO roadmap (title,roadmap_status) VALUES (?,'in_progress')",
            (f"Feature {serial}",),
        ).lastrowid
        body = f"governing spec revision {serial}"
        document_id = self.con.execute(
            "INSERT INTO documents (feature_id,kind,seq,title,body) "
            "VALUES (?,'spec',1,?,?)",
            (feature_id, f"Spec {serial}", body),
        ).lastrowid
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = self.con.execute(
            "INSERT INTO sprint_spec_approvals "
            "(document_id,revision_sha256,reviewer_shell_id,verdict) "
            "VALUES (?,?,2,?)",
            (document_id, revision, approval_verdict),
        ).lastrowid
        sprint_id = self.con.execute(
            "INSERT INTO sprints "
            "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
            "VALUES (?,3,?)",
            (feature_id, 1 if merge_grant else 0),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (sprint_id, document_id, revision, approval_id),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,?,?,?,?,?)",
            (
                (sprint_id, 3, "planner", "codex", "planner-model", "high"),
                (sprint_id, 1, "developer", "codex", "dev-model", "high"),
                (sprint_id, 2, "reviewer", "kimi", "review-model", "high"),
            ),
        )
        work_unit_id = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
            "VALUES (?,1,2,'Foundation','Ship the durable foundation')",
            (sprint_id,),
        ).lastrowid
        self.con.commit()
        return int(sprint_id), int(work_unit_id)


class MigrationAndShapeTest(SprintDomainCase):
    def test_upgrade_from_zero_sprint_baseline_creates_v2_domain(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(con, through="0145_reseed_generic_guidance.sql")
            self.assertEqual(
                [],
                con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'sprint%'"
                ).fetchall(),
            )
            con.executescript(FOUNDATION.read_text())
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "sprints",
                    "sprint_specs",
                    "sprint_participants",
                    "sprint_work_units",
                    "sprint_messages",
                    "sprint_wake_outbox",
                    "sprint_wake_attempts",
                    "sprint_registered_prs",
                    "sprint_pr_transitions",
                    "sprint_judgments",
                    "sprint_reports",
                    "sprint_events",
                }.issubset(tables)
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_append_only_evidence_rejects_mutation_and_preserves_row(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        event = self.con.execute(
            "SELECT event_id,event_type FROM sprint_events WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        self.assertEqual("lifecycle.armed", event["event_type"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.con.execute(
                "UPDATE sprint_events SET event_type='rewritten' WHERE event_id=?",
                (event["event_id"],),
            )
        self.assertEqual(
            "lifecycle.armed",
            self.con.execute(
                "SELECT event_type FROM sprint_events WHERE event_id=?",
                (event["event_id"],),
            ).fetchone()[0],
        )


class LifecycleTest(SprintDomainCase):
    def test_sprint_insert_must_start_prepared(self) -> None:
        sprint_id, _ = self.create_sprint()
        feature_id = self.con.execute(
            "SELECT feature_id FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()[0]

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "Sprint inserts must start prepared"
        ):
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,lifecycle,"
                "merge_grant_enabled) VALUES (?,3,'armed',1)",
                (feature_id,),
            )

        self.assertEqual(
            [(sprint_id, "prepared")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT sprint_id,lifecycle FROM sprints ORDER BY sprint_id"
                )
            ],
        )

    def test_arm_atomically_provisions_every_participant_conversation(self) -> None:
        sprint_id, _ = self.create_sprint()

        self.store.arm(sprint_id, 3)

        participants = self.con.execute(
            "SELECT participant_id,persistent_conversation_id,"
            "current_conversation_id FROM sprint_participants "
            "WHERE sprint_id=? ORDER BY participant_id",
            (sprint_id,),
        ).fetchall()
        self.assertEqual(3, len(participants))
        self.assertTrue(
            all(
                row["persistent_conversation_id"] == row["current_conversation_id"]
                for row in participants
            )
        )
        conversation_ids = {row["current_conversation_id"] for row in participants}
        self.assertEqual(
            [("sprint", "idle", 3)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT conversation_scope,state,COUNT(*) "
                    "FROM conversations WHERE conversation_id IN (?,?,?) "
                    "GROUP BY conversation_scope,state",
                    tuple(sorted(conversation_ids)),
                )
            ],
        )
        self.assertEqual(
            [("work", 3)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT purpose,COUNT(*) FROM sprint_participant_conversations "
                    "GROUP BY purpose"
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM conversation_outbox").fetchone()[0],
            "creating placeholders must not launch or wake a harness",
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='lifecycle.armed'",
            (sprint_id,),
        ).fetchone()
        self.assertEqual(
            conversation_ids,
            set(json.loads(event["payload"])["initial_conversation_ids"]),
        )

        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,title,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/normal','Normal chat','normal-1','hash')"
        )
        self.assertEqual(
            [("normal", 1), ("sprint", 3)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT conversation_scope,COUNT(*) FROM conversations "
                    "GROUP BY conversation_scope ORDER BY conversation_scope"
                )
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,title,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (1,1,'codex','/normal-2','Other normal chat',"
                "'normal-2','hash')"
            )

    def test_arm_rolls_back_conversations_when_initial_release_fails(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.con.execute(
            "CREATE TRIGGER reject_initial_message BEFORE INSERT ON sprint_messages "
            "BEGIN SELECT RAISE(ABORT,'release fault'); END"
        )
        self.con.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "release fault"):
            self.store.arm(sprint_id, 3)

        self.assertEqual(
            "prepared",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0],
        )
        self.assertEqual(
            [(None, None), (None, None), (None, None)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT persistent_conversation_id,current_conversation_id "
                    "FROM sprint_participants WHERE sprint_id=? "
                    "ORDER BY participant_id",
                    (sprint_id,),
                )
            ],
        )

    def test_armed_sprint_merge_grant_is_immutable(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "Sprint merge grant is immutable after arming",
        ):
            self.con.execute(
                "UPDATE sprints SET merge_grant_enabled=0 WHERE sprint_id=?",
                (sprint_id,),
            )

        self.assertEqual(
            ("armed", 1),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,merge_grant_enabled FROM sprints "
                    "WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )

    def test_arm_atomically_releases_only_dependency_free_work(self) -> None:
        sprint_id, first_unit = self.create_sprint()
        blocked_unit = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
            "planned_wave) VALUES (?,1,2,'Blocked','Wait for foundation',1)",
            (sprint_id,),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (sprint_id, blocked_unit, first_unit),
        )
        self.con.commit()

        wake_ids = self.store.arm(sprint_id, 3)

        self.assertEqual(1, len(wake_ids))
        self.assertEqual(
            ("armed", 2),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,version FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(first_unit, "ready"), (blocked_unit, "planned")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
        )
        message = self.con.execute(
            "SELECT work_unit_id,message_kind,actionable,disposition,body "
            "FROM sprint_messages WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        self.assertEqual(first_unit, message["work_unit_id"])
        self.assertEqual("work_assignment", message["message_kind"])
        self.assertEqual(1, message["actionable"])
        self.assertEqual("pending", message["disposition"])
        self.assertEqual(
            "Foundation\n\nShip the durable foundation", message["body"]
        )
        self.assertEqual(
            [(wake_ids[0], message["work_unit_id"])],
            [
                (row["wake_id"], first_unit)
                for row in self.con.execute(
                    "SELECT w.wake_id FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "JOIN sprint_messages m USING (message_id) "
                    "WHERE w.sprint_id=? AND m.work_unit_id=?",
                    (sprint_id, first_unit),
                )
            ],
        )

    def test_single_armed_invariant_rolls_back_second_release(self) -> None:
        first, _ = self.create_sprint()
        second, second_unit = self.create_sprint()
        self.store.arm(first, 3)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.arm(second, 3)

        self.assertEqual(
            "prepared",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (second,)
            ).fetchone()[0],
        )
        self.assertEqual(
            "planned",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (second_unit,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_messages WHERE sprint_id=?", (second,)
            ).fetchone()[0],
        )

    def test_arm_coalesces_ready_messages_for_the_same_participant(self) -> None:
        sprint_id, first_unit = self.create_sprint()
        second_unit = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
            "VALUES (?,1,2,'Second','Ship second')",
            (sprint_id,),
        ).lastrowid
        self.con.commit()

        wake_ids = self.store.arm(sprint_id, 3)

        self.assertEqual(1, len(wake_ids))
        self.assertEqual(
            [(first_unit,), (second_unit,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT m.work_unit_id FROM sprint_wake_messages wm "
                    "JOIN sprint_messages m USING (message_id) "
                    "WHERE wm.wake_id=? ORDER BY m.message_id",
                    (wake_ids[0],),
                )
            ],
        )

    def test_arm_rejects_wrong_planner_and_unapproved_spec_without_effect(self) -> None:
        sprint_id, unit_id = self.create_sprint(approval_verdict="fail")
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.arm(sprint_id, 4)
        with self.assertRaises(sprint_domain.SprintInvariantError):
            self.store.arm(sprint_id, 3)
        self.assertEqual(
            ("prepared", "planned", 0),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
                self.con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (unit_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            ),
        )

    def test_arm_rejects_spec_edited_after_approval_without_effect(self) -> None:
        sprint_id, unit_id = self.create_sprint()
        self.con.execute(
            "UPDATE documents SET body='edited after QAQC' "
            "WHERE document_id=(SELECT document_id FROM sprint_specs "
            "WHERE sprint_id=?)",
            (sprint_id,),
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "exact, passing spec approval"
        ):
            self.store.arm(sprint_id, 3)

        self.assertEqual(
            ("prepared", "planned", 0),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
                self.con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (unit_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_messages WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            ),
        )

    def test_transition_authority_and_database_backstop(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.transition(
                sprint_id,
                "paused",
                sprint_domain.LifecycleActor("participant", 4),
                reason="not assigned",
            )
        self.assertTrue(
            self.store.transition(
                sprint_id,
                "paused",
                sprint_domain.LifecycleActor("participant", 1),
                reason="integrity threat",
            )
        )
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.transition(
                sprint_id,
                "armed",
                sprint_domain.LifecycleActor("participant", 1),
            )
        self.assertTrue(
            self.store.transition(
                sprint_id,
                "armed",
                sprint_domain.LifecycleActor("fnb"),
            )
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "illegal Sprint lifecycle transition"
        ):
            self.con.execute(
                "UPDATE sprints SET lifecycle='prepared' WHERE sprint_id=?",
                (sprint_id,),
            )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )

    def test_terminal_wake_failure_auto_pauses_with_evidence(self) -> None:
        sprint_id, _ = self.create_sprint()
        wake_id = self.store.arm(sprint_id, 3)[0]

        self.assertEqual(1, self.store.record_wake_failure(wake_id, "fault one"))
        self.assertEqual(2, self.store.record_wake_failure(wake_id, "fault two"))
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.assertEqual(3, self.store.record_wake_failure(wake_id, "fault three"))

        self.assertEqual(
            ("paused", "failed", 3),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,w.state,w.attempt_count FROM sprints s "
                    "JOIN sprint_wake_outbox w USING (sprint_id) "
                    "WHERE w.wake_id=?",
                    (wake_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(1, "fault one"), (2, "fault two"), (3, "fault three")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,error_detail FROM sprint_wake_attempts "
                    "WHERE wake_id=? ORDER BY attempt_number",
                    (wake_id,),
                )
            ],
        )
        report = self.con.execute(
            "SELECT report_kind,body FROM sprint_reports WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        self.assertEqual("pause", report["report_kind"])
        self.assertIn('"reason": "wake_delivery_exhausted"', report["body"])
        self.assertEqual(
            "lifecycle.paused",
            self.con.execute(
                "SELECT event_type FROM sprint_events WHERE sprint_id=? "
                "ORDER BY event_id DESC LIMIT 1",
                (sprint_id,),
            ).fetchone()[0],
        )


class ArmedServiceSwitchTest(SprintDomainCase):
    def test_non_armed_states_invoke_zero_poll_or_dispatch_callbacks(self) -> None:
        sprint_id, _ = self.create_sprint()
        calls: list[tuple[int, str]] = []
        switch = sprint_domain.ArmedServiceSwitch(
            self.store,
            (lambda active, trigger: calls.append((active, trigger)),),
        )

        self.assertFalse(switch.recover_on_startup())
        self.assertFalse(switch.tick())
        self.assertEqual([], calls)
        self.store.arm(sprint_id, 3)
        self.assertTrue(switch.tick())
        self.assertEqual([(sprint_id, "pulse")], calls)
        self.store.transition(
            sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="hold",
        )
        self.assertFalse(switch.tick())
        self.assertEqual([(sprint_id, "pulse")], calls)

    def test_restart_recovers_armed_sprint_from_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sprint.db"
            with closing(db_driver.connect(path)) as seed:
                apply_schema(seed)
                seed.execute(
                    "INSERT INTO users (user_id,username) VALUES (1,'operator')"
                )
                seed.executemany(
                    "INSERT INTO shells "
                    "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                    "VALUES (?,?,?,?,?,1)",
                    (
                        (1, "Developer", "DEV1", "dev", "prompt"),
                        (2, "Reviewer", "REV1", "reviewer", "prompt"),
                        (3, "Planner", "PLN1", "planner", "prompt"),
                    ),
                )
                seed.execute(
                    "INSERT INTO roadmap (feature_id,title,roadmap_status) "
                    "VALUES (1,'Feature','in_progress')"
                )
                body = "spec"
                revision = hashlib.sha256(body.encode()).hexdigest()
                seed.execute(
                    "INSERT INTO documents "
                    "(document_id,feature_id,kind,seq,title,body) "
                    "VALUES (1,1,'spec',1,'Spec',?)",
                    (body,),
                )
                seed.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(approval_id,document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (1,1,?,2,'pass')",
                    (revision,),
                )
                seed.execute(
                    "INSERT INTO sprints "
                    "(sprint_id,feature_id,originating_planner_shell_id,"
                    "merge_grant_enabled) VALUES (1,1,3,1)"
                )
                seed.execute(
                    "INSERT INTO sprint_specs VALUES (1,1,?,1,datetime('now'))",
                    (revision,),
                )
                seed.executemany(
                    "INSERT INTO sprint_participants "
                    "(sprint_id,shell_id,role,harness) VALUES (1,?,?,?)",
                    ((3, "planner", "codex"), (1, "developer", "codex"),
                     (2, "reviewer", "kimi")),
                )
                seed.execute(
                    "INSERT INTO sprint_work_units "
                    "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                    "VALUES (1,1,2,'Unit','Output')"
                )
                seed.commit()
                sprint_domain.SprintLifecycleStore(seed).arm(1, 3)

            calls: list[tuple[int, str]] = []
            with closing(db_driver.connect(path)) as restarted:
                switch = sprint_domain.ArmedServiceSwitch(
                    sprint_domain.SprintLifecycleStore(restarted),
                    (lambda active, trigger: calls.append((active, trigger)),),
                )
                self.assertTrue(switch.recover_on_startup())
            self.assertEqual([(1, "startup")], calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
