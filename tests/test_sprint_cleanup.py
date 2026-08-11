#!/usr/bin/env python3
"""Successful-Sprint cleanup scheduling, rollback, and replay gates."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ROOT / "tests")]

import sprint_cleanup  # noqa: E402
import sprint_close  # noqa: E402
import sprint_domain  # noqa: E402
from test_sprint_v2_domain import SprintDomainCase  # noqa: E402


TEST_ROOT = Path("/srv/super-coder")
TEST_COMMON_DIR = TEST_ROOT / ".git"


class SprintCleanupSchedulingTest(SprintDomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.cleanup = sprint_cleanup.SprintCleanupTargetStore(
            self.con,
            identity_provider=lambda: (TEST_ROOT, TEST_COMMON_DIR),
        )
        self.store = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=self.cleanup,
        )
        self.sprint_id, self.unit_id = self.create_sprint()
        self.store.arm(self.sprint_id, 3)

    def cleanup_rows(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT shell_id,target_kind,canonical_path,repository_root,"
            "git_common_dir,expected_base_branch,state,attempt_count,"
            "claim_generation FROM sprint_cleanup_targets WHERE sprint_id=? "
            "ORDER BY target_kind,canonical_path",
            (self.sprint_id,),
        ).fetchall()

    def test_direct_completion_schedules_exact_targets_and_replay_is_idle(self):
        changed = self.store.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="Fallback close",
            terminal_outcome="accepted",
        )
        self.assertTrue(changed)
        self.assertEqual(
            [
                (
                    None,
                    "artifact_dir",
                    f"{TEST_ROOT}/shared/sprints/sprint-{self.sprint_id}",
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    None,
                    "pending",
                    0,
                    0,
                ),
                (
                    1,
                    "worktree",
                    f"{TEST_ROOT}/.sc-worktrees/dev1",
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    "shell/dev1",
                    "pending",
                    0,
                    0,
                ),
                (
                    3,
                    "worktree",
                    f"{TEST_ROOT}/.sc-worktrees/pln1",
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    "shell/pln1",
                    "pending",
                    0,
                    0,
                ),
                (
                    2,
                    "worktree",
                    f"{TEST_ROOT}/.sc-worktrees/rev1",
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    "shell/rev1",
                    "pending",
                    0,
                    0,
                ),
            ],
            [tuple(row) for row in self.cleanup_rows()],
        )
        projection = self.cleanup.project(self.sprint_id)
        self.assertEqual(
            ("pending", 4, 3, 1, 4, 0, 0, 0),
            (
                projection.aggregate_state,
                projection.target_count,
                projection.worktree_count,
                projection.artifact_count,
                projection.pending_count,
                projection.running_count,
                projection.succeeded_count,
                projection.failed_count,
            ),
        )
        event = self.con.execute(
            "SELECT actor_kind,payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_scheduled'",
            (self.sprint_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        self.assertEqual("system", event["actor_kind"])
        self.assertEqual("pending", payload["aggregate_state"])
        self.assertEqual(4, payload["target_count"])
        self.assertEqual(3, len(payload["worktree_target_ids"]))
        self.assertEqual(1, len(payload["artifact_target_ids"]))

        self.assertFalse(
            self.store.transition(
                self.sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="Fallback close",
                terminal_outcome="accepted",
            )
        )
        self.assertEqual(4, len(self.cleanup_rows()))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='sprint.cleanup_scheduled'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_admin_participant_is_excluded_from_target_authority(self):
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'Admin','ADMIN','admin','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (6,'Bespoke','OPS1',NULL,'prompt',1)"
        )
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,5,'reviewer','codex')",
            (self.sprint_id,),
        )
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,6,'developer','codex')",
            (self.sprint_id,),
        )
        self.con.commit()

        targets = self.cleanup.prepare_targets(self.sprint_id)

        self.assertEqual(
            {1, 2, 3, 6},
            {target.shell_id for target in targets if target.shell_id},
        )
        self.assertIn(
            f"{TEST_ROOT}/.sc-worktrees/ops1",
            {target.canonical_path for target in targets},
        )
        self.assertNotIn(
            str(TEST_ROOT),
            {target.canonical_path for target in targets},
        )

    def test_soft_deleted_participant_keeps_exact_target_on_replay(self):
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=1")
        self.con.commit()

        self.assertTrue(
            self.store.transition(
                self.sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="Fallback close",
                terminal_outcome="accepted",
            )
        )
        rows = self.cleanup_rows()
        self.assertEqual(4, len(rows))
        self.assertEqual(
            [
                (
                    1,
                    "worktree",
                    f"{TEST_ROOT}/.sc-worktrees/dev1",
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    "shell/dev1",
                    "pending",
                    0,
                    0,
                )
            ],
            [tuple(row) for row in rows if row["shell_id"] == 1],
        )

        self.assertFalse(
            self.store.transition(
                self.sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="Fallback close",
                terminal_outcome="accepted",
            )
        )
        self.assertEqual(
            [tuple(row) for row in rows],
            [tuple(row) for row in self.cleanup_rows()],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='sprint.cleanup_scheduled'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_scheduling_failure_rolls_back_direct_completion(self):
        self.con.execute(
            "CREATE TRIGGER reject_cleanup_schedule BEFORE INSERT "
            "ON sprint_cleanup_targets BEGIN "
            "SELECT RAISE(ABORT,'reject cleanup schedule'); END"
        )
        self.con.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject cleanup schedule"):
            self.store.transition(
                self.sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="Fallback close",
                terminal_outcome="accepted",
            )

        self.assertEqual(
            ("armed", 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.completed') "
                    "FROM sprints WHERE sprint_id=?",
                    (self.sprint_id, self.sprint_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_schema_rejects_early_targets_and_identity_rewrites(self):
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "require completed lifecycle",
        ):
            self.con.execute(
                "INSERT INTO sprint_cleanup_targets "
                "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
                "git_common_dir,expected_base_branch) VALUES "
                "(?,1,'worktree','/repo/.sc-worktrees/dev1','/repo','/repo/.git',"
                "'shell/dev1')",
                (self.sprint_id,),
            )
        self.con.rollback()
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

        self.store.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="Fallback close",
            terminal_outcome="accepted",
        )
        before = self.con.execute(
            "SELECT canonical_path FROM sprint_cleanup_targets "
            "WHERE sprint_id=? AND shell_id=1",
            (self.sprint_id,),
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "identity is immutable"):
            self.con.execute(
                "UPDATE sprint_cleanup_targets SET canonical_path='/other' "
                "WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            )
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT canonical_path FROM sprint_cleanup_targets "
                "WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()[0],
        )

        target_id = int(
            self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET attempt_count=1,claim_generation=1 "
            "WHERE cleanup_target_id=?",
            (target_id,),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot decrease"):
            self.con.execute(
                "UPDATE sprint_cleanup_targets SET attempt_count=0 "
                "WHERE cleanup_target_id=?",
                (target_id,),
            )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE cleanup_target_id=?",
            (target_id,),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "are terminal"):
            self.con.execute(
                "UPDATE sprint_cleanup_targets SET state='pending' "
                "WHERE cleanup_target_id=?",
                (target_id,),
            )

    def test_transaction_rejects_participant_identity_drift(self):
        targets = self.cleanup.prepare_targets(self.sprint_id)
        self.con.execute(
            "UPDATE shells SET shortname='DEV-RENAMED' WHERE shell_id=1"
        )
        with self.assertRaisesRegex(
            sprint_cleanup.SprintCleanupInvariantError,
            "participant identities changed",
        ):
            with self.con:
                self.con.execute(
                    "UPDATE sprints SET lifecycle='completed',"
                    "terminal_outcome='accepted' WHERE sprint_id=?",
                    (self.sprint_id,),
                )
                self.cleanup.schedule_in_transaction(self.sprint_id, targets)
        self.assertEqual(
            ("armed", 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?) "
                    "FROM sprints WHERE sprint_id=?",
                    (self.sprint_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_conformance_completion_schedules_atomically_and_replays_exactly(self):
        close = sprint_close.SprintCloseStore(
            self.con,
            cleanup_store=self.cleanup,
        )
        kwargs = {
            "body": "Conformance passed.",
            "findings": [],
            "final_report": "Final integrated evidence.",
            "reason": "Reviewer approved",
            "terminal_outcome": "accepted",
            "idempotency_key": "cleanup-conformance",
        }
        first = close.record_conformance(self.sprint_id, 2, **kwargs)
        first_rows = [tuple(row) for row in self.cleanup_rows()]
        replay = close.record_conformance(self.sprint_id, 2, **kwargs)

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first_rows, [tuple(row) for row in self.cleanup_rows()])
        self.assertEqual(4, len(first_rows))
        self.assertEqual(
            (1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.completed'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='sprint.cleanup_scheduled')",
                    (self.sprint_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_conformance_schedule_failure_leaves_no_orphan_closeout_rows(self):
        self.con.execute(
            "CREATE TRIGGER reject_cleanup_schedule BEFORE INSERT "
            "ON sprint_cleanup_targets BEGIN "
            "SELECT RAISE(ABORT,'reject cleanup schedule'); END"
        )
        self.con.commit()
        close = sprint_close.SprintCloseStore(
            self.con,
            cleanup_store=self.cleanup,
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject cleanup schedule"):
            close.record_conformance(
                self.sprint_id,
                2,
                body="Conformance must roll back.",
                findings=[],
                final_report="No orphan report.",
                reason="Reviewer approved",
                terminal_outcome="accepted",
                idempotency_key="cleanup-rollback",
            )

        self.assertEqual(
            ("armed", 0, 0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=? "
                    "AND idempotency_key='cleanup-rollback:planner-completed'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type IN "
                    "('conformance.recorded','lifecycle.completed',"
                    "'sprint.cleanup_scheduled')) "
                    "FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,) * 5,
                ).fetchone()
            ),
        )

    def test_aggregate_projection_prefers_failure_then_pending_then_success(self):
        targets = self.cleanup.prepare_targets(self.sprint_id)
        with self.con:
            self.con.execute(
                "UPDATE sprints SET lifecycle='completed',terminal_outcome='accepted' "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            )
            self.cleanup.schedule_in_transaction(self.sprint_id, targets)
        ids = [
            int(row[0])
            for row in self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=? ORDER BY cleanup_target_id",
                (self.sprint_id,),
            )
        ]
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE cleanup_target_id IN (?,?,?)",
            ids[:3],
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',"
            "last_error_code='mutation_failed' WHERE cleanup_target_id=?",
            (ids[3],),
        )
        self.assertEqual("failed", self.cleanup.project(self.sprint_id).aggregate_state)
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='pending',last_error_code=NULL "
            "WHERE cleanup_target_id=?",
            (ids[3],),
        )
        self.assertEqual("pending", self.cleanup.project(self.sprint_id).aggregate_state)
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE cleanup_target_id=?",
            (ids[3],),
        )
        self.assertEqual("succeeded", self.cleanup.project(self.sprint_id).aggregate_state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
