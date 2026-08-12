"""Successful-Sprint cleanup scheduling, rollback, and replay gates."""

from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [
    str(ENGINE / "api"),
    str(ENGINE / "scripts"),
    str(ROOT / "tests"),
]

import server
import sprint_cleanup
import sprint_cli
import sprint_close
import sprint_domain
import sprint_message_delivery
import sprint_runtime
import run
from conversation_adapters import NativeTurn, NormalizedEvent
from conversation_broker import ConversationBroker
from conversation_launch import ConversationLaunchPreparer
from test_sprint_v2_domain import SprintDomainCase

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
        self.con.execute("UPDATE shells SET shortname='DEV-RENAMED' WHERE shell_id=1")
        with (
            self.assertRaisesRegex(
                sprint_cleanup.SprintCleanupInvariantError,
                "participant identities changed",
            ),
            self.con,
        ):
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
        self.assertEqual(
            "pending", self.cleanup.project(self.sprint_id).aggregate_state
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE cleanup_target_id=?",
            (ids[3],),
        )
        self.assertEqual(
            "succeeded", self.cleanup.project(self.sprint_id).aggregate_state
        )

    def test_arm_rejects_unresolved_prior_cleanup_without_new_sprint_writes(self):
        self.store.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture completion",
            terminal_outcome="accepted",
        )
        newer_sprint, _unit_id = self.create_sprint()

        with self.assertRaises(sprint_domain.SprintCleanupConflictError) as raised:
            self.store.arm(newer_sprint, 3)

        self.assertEqual(
            {
                "code": "prior_cleanup_unresolved",
                "prior_sprint_id": self.sprint_id,
                "cleanup_target_id": raised.exception.details["cleanup_target_id"],
                "target_state": "pending",
                "path_label": ".sc-worktrees/dev1",
                "last_safe_fact": "cleanup_pending",
                "status_command": (
                    f"sc sprint cleanup-status --sprint {self.sprint_id}"
                ),
                "retry_command": (
                    f"sc sprint cleanup --sprint {self.sprint_id} "
                    "--key <stable-retry-key>"
                ),
            },
            raised.exception.details,
        )
        command = shlex.split(raised.exception.details["retry_command"])
        parsed = sprint_cli.build_parser().parse_args(command[2:])
        self.assertEqual(
            ("cleanup", self.sprint_id, "<stable-retry-key>", False),
            (parsed.command, parsed.sprint, parsed.key, parsed.adopt_legacy),
        )
        self.assertEqual(
            1,
            str(raised.exception).count("--key <stable-retry-key>"),
        )
        self.assertEqual(
            ("prepared", 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.armed') "
                    "FROM sprints WHERE sprint_id=?",
                    (newer_sprint, newer_sprint, newer_sprint),
                ).fetchone()
            ),
        )

        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (self.sprint_id,),
        )
        self.con.commit()
        self.assertEqual(2, len(self.store.arm(newer_sprint, 3)))

    def test_arm_revalidates_cleanup_gate_inside_write_transaction(self):
        newer_sprint, _unit_id = self.create_sprint()
        blocker = sprint_cleanup.UnresolvedCleanupTarget(
            cleanup_target_id=91,
            sprint_id=self.sprint_id,
            shell_id=1,
            state="running",
            path_label=".sc-worktrees/dev1",
            last_safe_fact="cleanup_claim_active",
        )
        self.cleanup.unresolved_worktree = mock.Mock(
            side_effect=(None, blocker)
        )

        with self.assertRaises(sprint_domain.SprintCleanupConflictError):
            self.store.arm(newer_sprint, 3)

        self.assertEqual(2, self.cleanup.unresolved_worktree.call_count)
        self.assertEqual(
            ("prepared", 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,(SELECT COUNT(*) FROM wake_message "
                    "WHERE sprint_id=?) FROM sprints WHERE sprint_id=?",
                    (newer_sprint, newer_sprint),
                ).fetchone()
            ),
        )

    def test_older_prepared_sprint_cannot_arm_over_later_cleanup(self):
        self.store.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture completion",
            terminal_outcome="accepted",
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
        older_sprint, _older_unit = self.create_sprint()
        later_sprint, _later_unit = self.create_sprint()
        self.store.arm(later_sprint, 3)
        self.store.transition(
            later_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="later Sprint completed first",
            terminal_outcome="accepted",
        )

        with self.assertRaises(sprint_domain.SprintCleanupConflictError) as raised:
            self.store.arm(older_sprint, 3)

        self.assertEqual(later_sprint, raised.exception.details["prior_sprint_id"])
        self.assertLess(older_sprint, later_sprint)
        self.assertEqual(
            ("prepared", 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.armed') "
                    "FROM sprints WHERE sprint_id=?",
                    (older_sprint, older_sprint, older_sprint),
                ).fetchone()
            ),
        )

    def test_cleanup_arm_conflict_projects_actionable_details(self):
        blocker = sprint_cleanup.UnresolvedCleanupTarget(
            cleanup_target_id=91,
            sprint_id=self.sprint_id,
            shell_id=1,
            state="running",
            path_label=".sc-worktrees/dev1",
            last_safe_fact="cleanup_claim_active",
        )
        conflict = sprint_domain.SprintCleanupConflictError(blocker)
        handler = object.__new__(server.Handler)
        handler._send = lambda status, body: (status, body)

        token_status, token_body = handler._sprint_error(conflict)
        board_status, board_body = handler._sprint_board_mutation_error(conflict)

        self.assertEqual(409, token_status)
        self.assertEqual(conflict.details, token_body["details"])
        self.assertEqual(
            f"sc sprint cleanup --sprint {self.sprint_id} "
            "--key <stable-retry-key>",
            token_body["details"]["retry_command"],
        )
        self.assertEqual(409, board_status)
        self.assertEqual("lifecycle_conflict", board_body["error"]["code"])
        self.assertEqual(conflict.details, board_body["error"]["details"])
        self.assertEqual(
            token_body["details"]["retry_command"],
            board_body["error"]["details"]["retry_command"],
        )

    def test_resume_rejects_later_completed_cleanup_then_succeeds_when_clear(self):
        def paused_counts():
            return tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.reconciled'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.armed')",
                    (paused_sprint, paused_sprint, paused_sprint),
                ).fetchone()
            )

        def completed_targets():
            return [
                tuple(row)
                for row in self.con.execute(
                    "SELECT cleanup_target_id,state,attempt_count,claim_generation,"
                    "lease_owner,waiting_reason FROM sprint_cleanup_targets "
                    "WHERE sprint_id=? AND target_kind='worktree' "
                    "ORDER BY cleanup_target_id",
                    (completed_sprint,),
                )
            ]

        paused_sprint = self.sprint_id
        self.store.pause(
            paused_sprint,
            sprint_domain.LifecycleActor("participant", 1),
            reason="later Sprint temporarily owns the participant slots",
        )
        completed_sprint, _unit_id = self.create_sprint()
        self.store.arm(completed_sprint, 3)
        self.store.transition(
            completed_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="later Sprint completed first",
            terminal_outcome="accepted",
        )
        before_targets = completed_targets()
        self.assertEqual(
            [("pending", 0, 0, None, None)] * 3,
            [target[1:] for target in before_targets],
        )
        before_counts = paused_counts()
        reconciled = False

        def reconcile(_con):
            nonlocal reconciled
            reconciled = True

        with self.assertRaises(sprint_domain.SprintCleanupConflictError) as raised:
            self.store.resume(
                paused_sprint,
                sprint_domain.LifecycleActor("planner", 3),
                reason="resume after later Sprint",
                reconcile_in_transaction=reconcile,
            )

        self.assertEqual(completed_sprint, raised.exception.details["prior_sprint_id"])
        self.assertFalse(reconciled)
        self.assertEqual(
            "paused",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (paused_sprint,),
            ).fetchone()[0],
        )
        self.assertEqual(before_counts, paused_counts())
        self.assertEqual(before_targets, completed_targets())

        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (completed_sprint,),
        )
        self.con.commit()
        receipt = self.store.resume(
            paused_sprint,
            sprint_domain.LifecycleActor("planner", 3),
            reason="cleanup authority is terminal",
            reconcile_in_transaction=reconcile,
        )

        self.assertTrue(receipt.changed)
        self.assertTrue(reconciled)
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (paused_sprint,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (before_counts[1] + 1, before_counts[2] + 1),
            paused_counts()[1:],
        )


class SprintCleanupExecutorTest(SprintDomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repository = Path(self.tmp.name) / "repository"
        self.remote = Path(self.tmp.name) / "remote.git"
        self._git(Path(self.tmp.name), "init", "--bare", str(self.remote))
        self._git(Path(self.tmp.name), "init", str(self.repository))
        self._git(self.repository, "config", "user.name", "Sprint Fixture")
        self._git(self.repository, "config", "user.email", "fixture@example.test")
        (self.repository / ".gitignore").write_text(
            ".sc-worktrees/\nkeep.cache\nshared/sprints/\n",
            encoding="utf-8",
        )
        (self.repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git(self.repository, "add", ".gitignore", "tracked.txt")
        self._git(self.repository, "commit", "-m", "initial")
        self._git(self.repository, "branch", "-M", "main")
        self._git(self.repository, "remote", "add", "origin", str(self.remote))
        self._git(self.repository, "push", "-u", "origin", "main")
        self._git(self.repository, "branch", "remote-preserved")
        self._git(self.repository, "push", "origin", "remote-preserved")
        self.worktree = self.repository / ".sc-worktrees" / "dev1"
        self._git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "shell/dev1",
            str(self.worktree),
            "main",
        )
        self._git(self.worktree, "config", "user.name", "Sprint Fixture")
        self._git(self.worktree, "config", "user.email", "fixture@example.test")

        self.now = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)
        common_dir = (self.repository / ".git").resolve()
        self.cleanup = sprint_cleanup.SprintCleanupTargetStore(
            self.con,
            identity_provider=lambda: (self.repository.resolve(), common_dir),
            clock=lambda: self.now,
        )
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=self.cleanup,
        )
        self.sprint_id, self.unit_id = self.create_sprint()
        self.lifecycle.arm(self.sprint_id, 3)
        self.lifecycle.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture completion",
            terminal_outcome="accepted",
        )

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed in {cwd}: "
                f"{result.stderr or result.stdout}"
            )
        return result

    def _executor(
        self,
        *,
        liveness=None,
        branch_pruner=None,
    ) -> sprint_cleanup.SprintCleanupExecutor:
        return sprint_cleanup.SprintCleanupExecutor(
            self.cleanup,
            liveness_probe=liveness or (lambda _claim: "dormant"),
            branch_pruner=branch_pruner
            or (
                lambda _repo: {
                    "candidates": 0,
                    "deleted": [],
                    "failed": [],
                }
            ),
            lease_seconds=60,
        )

    def _dirty_worktree(self) -> None:
        self._git(self.worktree, "checkout", "-b", "feat/disposable")
        (self.worktree / "local-commit.txt").write_text("local\n", encoding="utf-8")
        self._git(self.worktree, "add", "local-commit.txt")
        self._git(self.worktree, "commit", "-m", "local-only")
        (self.worktree / "tracked.txt").write_text("staged dirt\n", encoding="utf-8")
        self._git(self.worktree, "add", "tracked.txt")
        (self.worktree / "untracked.txt").write_text("discard\n", encoding="utf-8")
        nested = self.worktree / "nested-repository"
        nested.mkdir()
        self._git(nested, "init")
        (nested / "only-local.txt").write_text("discard nested\n", encoding="utf-8")
        (self.worktree / "keep.cache").write_text(
            "ignored survives\n", encoding="utf-8"
        )
        (self.repository / "outside.txt").write_text(
            "adjacent survives\n", encoding="utf-8"
        )

    def _advance_remote_main(self) -> str:
        (self.repository / "tracked.txt").write_text(
            "refreshed main\n", encoding="utf-8"
        )
        self._git(self.repository, "add", "tracked.txt")
        self._git(self.repository, "commit", "-m", "advance main")
        self._git(self.repository, "push", "origin", "main")
        return self._git(self.repository, "rev-parse", "HEAD").stdout.strip()

    def _worktree_row(self) -> sqlite3.Row:
        return self.con.execute(
            "SELECT * FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
            (self.sprint_id,),
        ).fetchone()

    def test_reset_discards_nested_and_dirty_state_at_refreshed_main(self):
        self._dirty_worktree()
        refreshed_main = self._advance_remote_main()
        pruned_from = []

        def prune(repo):
            pruned_from.append(
                (
                    repo,
                    self._git(self.worktree, "branch", "--show-current").stdout.strip(),
                )
            )
            return {"candidates": 0, "deleted": [], "failed": []}

        receipt = self._executor(branch_pruner=prune).run_next("fixture", shell_id=1)

        self.assertEqual("succeeded", receipt.state)
        self.assertEqual(
            "shell/dev1",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertEqual(
            refreshed_main, self._git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        )
        self.assertEqual("", self._git(self.worktree, "status", "--porcelain").stdout)
        self.assertEqual(
            "refreshed main\n", (self.worktree / "tracked.txt").read_text()
        )
        self.assertFalse((self.worktree / "untracked.txt").exists())
        self.assertFalse((self.worktree / "nested-repository").exists())
        self.assertTrue((self.worktree / "keep.cache").is_file())
        self.assertEqual(
            "adjacent survives\n", (self.repository / "outside.txt").read_text()
        )
        self.assertNotEqual(
            "",
            self._git(
                self.repository, "ls-remote", "origin", "refs/heads/remote-preserved"
            ).stdout,
        )
        self.assertEqual([(self.repository, "shell/dev1")], pruned_from)
        row = self._worktree_row()
        before = json.loads(row["before_evidence"])
        after = json.loads(row["after_evidence"])
        self.assertEqual(
            ("succeeded", 1, 1),
            (row["state"], row["attempt_count"], row["claim_generation"]),
        )
        self.assertGreater(before["status_count"], 0)
        self.assertEqual(0, after["status_count"])
        self.assertEqual(refreshed_main, after["refreshed_main_sha"])

    def test_substituted_repository_fails_closed_and_preserves_bytes(self):
        self._git(self.repository, "worktree", "remove", "--force", str(self.worktree))
        self.worktree.mkdir(parents=True)
        self._git(self.worktree, "init")
        sentinel = self.worktree / "substituted.txt"
        sentinel.write_text("must survive\n", encoding="utf-8")

        receipt = self._executor().run_next("fixture", shell_id=1)

        self.assertEqual(
            ("failed", "git_common_dir_mismatch"), (receipt.state, receipt.code)
        )
        self.assertEqual("must survive\n", sentinel.read_text())
        row = self._worktree_row()
        self.assertEqual(
            ("failed", 0, "git_common_dir_mismatch"),
            (row["state"], row["attempt_count"], row["last_error_code"]),
        )
        self.assertIsNone(row["before_evidence"])

    def test_reclaimed_generation_cannot_mutate_or_write_terminal_state(self):
        self._dirty_worktree()
        first = self.cleanup.claim_next("first", shell_id=1, lease_seconds=10)
        self.assertIsNotNone(first)
        self.now += timedelta(seconds=11)
        second = self.cleanup.claim_next("second", shell_id=1, lease_seconds=60)
        self.assertIsNotNone(second)

        receipt = self._executor().execute(first)

        self.assertEqual(("stale", "claim_superseded"), (receipt.state, receipt.code))
        self.assertTrue((self.worktree / "untracked.txt").is_file())
        row = self._worktree_row()
        self.assertEqual(
            ("running", "second", 2, 0),
            (
                row["state"],
                row["lease_owner"],
                row["claim_generation"],
                row["attempt_count"],
            ),
        )
        self.assertIsNone(row["last_error_code"])

    def test_post_lock_revalidation_refuses_newer_sprint_without_git_mutation(self):
        self._dirty_worktree()
        newer_sprint, _unit = self.create_sprint()
        calls = 0

        def liveness(_claim):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.con.execute(
                    "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
                    (newer_sprint,),
                )
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                    "VALUES (?,'lifecycle.armed','fnb',3,'{}')",
                    (newer_sprint,),
                )
                self.con.commit()
            return "dormant"

        receipt = self._executor(liveness=liveness).run_next("fixture", shell_id=1)

        self.assertEqual(
            ("failed", "newer_sprint_owns_target"), (receipt.state, receipt.code)
        )
        self.assertTrue((self.worktree / "untracked.txt").is_file())
        self.assertEqual(
            "feat/disposable",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        row = self._worktree_row()
        self.assertEqual(("failed", 0), (row["state"], row["attempt_count"]))
        self.assertIsNotNone(row["before_evidence"])

    def test_cleanup_refuses_lower_id_sprint_armed_after_authority(self):
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
        older_sprint, _older_unit = self.create_sprint()
        later_sprint, _later_unit = self.create_sprint()
        self.lifecycle.arm(later_sprint, 3)
        self.lifecycle.transition(
            later_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="later Sprint completed first",
            terminal_outcome="accepted",
        )
        with self.assertRaises(sprint_domain.SprintCleanupConflictError):
            self.lifecycle.arm(older_sprint, 3)
        self.con.execute(
            "UPDATE sprints SET lifecycle='armed',armed_at=datetime('now') "
            "WHERE sprint_id=?",
            (older_sprint,),
        )
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (?,'lifecycle.armed','fnb',3,'{}')",
            (older_sprint,),
        )
        self.con.commit()
        self._dirty_worktree()

        receipt = self._executor().run_next("fixture", shell_id=1)

        self.assertLess(older_sprint, later_sprint)
        self.assertEqual(
            ("failed", "newer_sprint_owns_target", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual(
            "feat/disposable",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertEqual("staged dirt\n", (self.worktree / "tracked.txt").read_text())
        self.assertTrue((self.worktree / "untracked.txt").is_file())
        row = self.con.execute(
            "SELECT state,attempt_count,last_error_code "
            "FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
            (later_sprint,),
        ).fetchone()
        self.assertEqual(
            ("failed", 0, "newer_sprint_owns_target"),
            tuple(row),
        )

    def test_newer_aborted_sprint_preserves_work_without_attempt(self):
        self._dirty_worktree()
        newer_sprint, _unit = self.create_sprint()
        calls = 0

        def aborts_after_fetch(_claim):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.con.execute(
                    "UPDATE sprints SET lifecycle='aborted',"
                    "terminal_outcome='aborted' WHERE sprint_id=?",
                    (newer_sprint,),
                )
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                    "VALUES (?,'lifecycle.aborted','fnb',3,'{}')",
                    (newer_sprint,),
                )
                self.con.commit()
            return "dormant"

        receipt = self._executor(liveness=aborts_after_fetch).run_next(
            "fixture", shell_id=1
        )

        self.assertEqual(
            ("failed", "newer_sprint_owns_target", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual(
            "feat/disposable",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertEqual("staged dirt\n", (self.worktree / "tracked.txt").read_text())
        self.assertEqual(
            "discard\n", (self.worktree / "untracked.txt").read_text()
        )
        self.assertEqual(
            "discard nested\n",
            (self.worktree / "nested-repository" / "only-local.txt").read_text(),
        )
        row = self._worktree_row()
        self.assertEqual(
            ("failed", 0, "newer_sprint_owns_target"),
            (
                row["state"],
                row["attempt_count"],
                row["last_error_code"],
            ),
        )
        self.assertIsNotNone(row["before_evidence"])

    def test_live_target_waits_without_consuming_attempt(self):
        receipt = self._executor(liveness=lambda _claim: "live").run_next(
            "fixture",
            shell_id=1,
        )

        self.assertEqual(
            ("waiting", "waiting_for_run_exit"), (receipt.state, receipt.code)
        )
        row = self._worktree_row()
        self.assertEqual(
            ("pending", 0, "waiting_for_run_exit"),
            (row["state"], row["attempt_count"], row["waiting_reason"]),
        )
        self.assertIsNone(row["last_error_code"])

    def test_runtime_pulse_defers_live_target_without_mutation_attempt(self):
        runtime = sprint_runtime.SprintRuntimeService(
            ":memory:", owner="runtime-fixture"
        )
        switch = mock.Mock()
        switch.tick.return_value = False
        monitored = mock.Mock(changed=False)
        executor = self._executor(liveness=lambda _claim: "live")

        with mock.patch.object(
            sprint_runtime.activity_monitor.ActivityMonitor,
            "tick",
            return_value=monitored,
        ), mock.patch.object(runtime, "_switch", return_value=switch), mock.patch.object(
            runtime, "_deliver_wakes", return_value=False
        ), mock.patch.object(runtime, "_record_heartbeat"), mock.patch.object(
            sprint_runtime.sprint_cleanup,
            "SprintCleanupExecutor",
            return_value=executor,
        ):
            changed = runtime._pulse(self.con, startup=False)

        self.assertTrue(changed)
        switch.tick.assert_called_once_with()
        row = self._worktree_row()
        self.assertEqual(
            ("pending", 0, "waiting_for_run_exit", 1),
            (
                row["state"],
                row["attempt_count"],
                row["waiting_reason"],
                row["claim_generation"],
            ),
        )
        self.assertEqual(
            "",
            self._git(self.worktree, "status", "--porcelain").stdout,
        )

    def test_runtime_pulses_advance_past_live_target_to_dormant_worktree(self):
        planner_worktree = self.repository / ".sc-worktrees" / "pln1"
        self._git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "shell/pln1",
            str(planner_worktree),
            "main",
        )
        (self.worktree / "live-state.txt").write_text("preserve\n", encoding="utf-8")
        (planner_worktree / "dormant-state.txt").write_text(
            "discard\n", encoding="utf-8"
        )
        runtime = sprint_runtime.SprintRuntimeService(
            ":memory:", owner="runtime-fixture"
        )
        switch = mock.Mock()
        switch.tick.return_value = False
        executor = self._executor(
            liveness=lambda claim: "live" if claim.shell_id == 1 else "dormant"
        )

        with mock.patch.object(
            sprint_runtime.activity_monitor.ActivityMonitor,
            "tick",
            return_value=mock.Mock(changed=False),
        ), mock.patch.object(runtime, "_switch", return_value=switch), mock.patch.object(
            runtime, "_deliver_wakes", return_value=False
        ), mock.patch.object(runtime, "_record_heartbeat"), mock.patch.object(
            sprint_runtime.sprint_cleanup,
            "SprintCleanupExecutor",
            return_value=executor,
        ):
            first_changed = runtime._pulse(self.con, startup=False)
            self.now += timedelta(seconds=5)
            second_changed = runtime._pulse(self.con, startup=False)

        rows = {
            row["shell_id"]: row
            for row in self.con.execute(
                "SELECT shell_id,target_kind,state,attempt_count,claim_generation,"
                "lease_expires_at,waiting_reason FROM sprint_cleanup_targets "
                "WHERE sprint_id=? ORDER BY cleanup_target_id",
                (self.sprint_id,),
            )
            if row["target_kind"] == "worktree"
        }
        artifact = self.con.execute(
            "SELECT state,claim_generation FROM sprint_cleanup_targets "
            "WHERE sprint_id=? AND target_kind='artifact_dir'",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual((True, True), (first_changed, second_changed))
        self.assertEqual(
            ("pending", 0, 1, "2026-08-11 20:00:05", "waiting_for_run_exit"),
            tuple(rows[1][field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "lease_expires_at",
                "waiting_reason",
            )),
        )
        self.assertEqual(
            ("succeeded", 1, 1),
            tuple(rows[3][field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
            )),
        )
        self.assertEqual(("pending", 0), tuple(artifact))
        self.assertTrue((self.worktree / "live-state.txt").is_file())
        self.assertFalse((planner_worktree / "dormant-state.txt").exists())

    def test_runtime_fairness_advances_dormant_retry_before_live_waiter(self):
        planner_worktree = self.repository / ".sc-worktrees" / "pln1"
        reviewer_worktree = self.repository / ".sc-worktrees" / "rev1"
        self._git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "shell/pln1",
            str(planner_worktree),
            "main",
        )
        self._git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "shell/rev1",
            str(reviewer_worktree),
            "main",
        )
        (self.worktree / "live-state.txt").write_text("preserve\n", encoding="utf-8")
        (planner_worktree / "retry-state.txt").write_text(
            "discard\n", encoding="utf-8"
        )
        (reviewer_worktree / "dormant-state.txt").write_text(
            "discard\n", encoding="utf-8"
        )

        class FailPlannerOnce(sprint_cleanup.SprintCleanupExecutor):
            failed = False

            def _git(self, repo, *args, code, timeout=None):
                if (
                    Path(repo) == planner_worktree
                    and args[:2] == ("clean", "-ffd")
                    and not self.failed
                ):
                    self.failed = True
                    raise sprint_cleanup.SprintCleanupMutationError(
                        "clean_current_failed",
                        "injected dormant retry",
                    )
                return super()._git(repo, *args, code=code, timeout=timeout)

        executor = FailPlannerOnce(
            self.cleanup,
            liveness_probe=lambda claim: (
                "live" if claim.shell_id == 1 else "dormant"
            ),
            branch_pruner=lambda _repo: {
                "candidates": 0,
                "deleted": [],
                "failed": [],
            },
            lease_seconds=60,
        )
        runtime = sprint_runtime.SprintRuntimeService(
            ":memory:", owner="runtime-fixture"
        )
        switch = mock.Mock()
        switch.tick.return_value = False

        with mock.patch.object(
            sprint_runtime.activity_monitor.ActivityMonitor,
            "tick",
            return_value=mock.Mock(changed=False),
        ), mock.patch.object(runtime, "_switch", return_value=switch), mock.patch.object(
            runtime, "_deliver_wakes", return_value=False
        ), mock.patch.object(runtime, "_record_heartbeat"), mock.patch.object(
            sprint_runtime.sprint_cleanup,
            "SprintCleanupExecutor",
            return_value=executor,
        ):
            changed = []
            for _pulse in range(4):
                changed.append(runtime._pulse(self.con, startup=False))
                self.now += timedelta(seconds=5)

        rows = {
            row["shell_id"]: row
            for row in self.con.execute(
                "SELECT shell_id,target_kind,state,attempt_count,claim_generation,"
                "waiting_reason,last_error_code FROM sprint_cleanup_targets "
                "WHERE sprint_id=? ORDER BY cleanup_target_id",
                (self.sprint_id,),
            )
            if row["target_kind"] == "worktree"
        }
        artifact = self.con.execute(
            "SELECT state,claim_generation FROM sprint_cleanup_targets "
            "WHERE sprint_id=? AND target_kind='artifact_dir'",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual([True, True, True, True], changed)
        self.assertEqual(
            ("pending", 0, 1, "waiting_for_run_exit", None),
            tuple(rows[1][field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "waiting_reason",
                "last_error_code",
            )),
        )
        self.assertEqual(
            ("succeeded", 2, 2, None, None),
            tuple(rows[3][field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "waiting_reason",
                "last_error_code",
            )),
        )
        self.assertEqual(
            ("succeeded", 1, 1),
            tuple(rows[2][field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
            )),
        )
        self.assertEqual(("pending", 0), tuple(artifact))
        self.assertTrue((self.worktree / "live-state.txt").is_file())
        self.assertFalse((planner_worktree / "retry-state.txt").exists())
        self.assertFalse((reviewer_worktree / "dormant-state.txt").exists())

    def test_runtime_startup_stays_ready_during_blocked_cleanup(self):
        database = Path(self.tmp.name) / "runtime-startup.db"
        with sqlite3.connect(database) as target:
            self.con.backup(target)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        claims: list[sprint_cleanup.CleanupClaim] = []

        class BlockingCleanupExecutor:
            def __init__(self, store):
                self.store = store

            def run_next(self, owner):
                claim = self.store.claim_next(owner, lease_seconds=60)
                if claim is None:
                    return sprint_cleanup.CleanupExecutionReceipt(None, None, "idle")
                claims.append(claim)
                started.set()
                release.wait(timeout=10)
                changed = self.store.mark_succeeded(claim, {"blocked": False})
                finished.set()
                return sprint_cleanup.CleanupExecutionReceipt(
                    claim.cleanup_target_id,
                    claim.sprint_id,
                    "succeeded" if changed else "stale",
                    claim_generation=claim.claim_generation,
                    attempt_count=0,
                )

        runtime = sprint_runtime.SprintRuntimeService(
            database,
            owner="startup-runtime",
            pulse_seconds=0.05,
        )
        self.addCleanup(release.set)
        self.addCleanup(runtime.stop)

        with mock.patch.object(
            sprint_runtime.sprint_cleanup,
            "SprintCleanupExecutor",
            BlockingCleanupExecutor,
        ), mock.patch.object(
            server.conversation_broker,
            "start_service",
            return_value=mock.Mock(interrupt=mock.Mock()),
        ), mock.patch.object(
            server.conversation_reaper, "start_service"
        ), mock.patch.object(
            server.sprint_runtime,
            "start_service",
            side_effect=lambda *_args, **_kwargs: (runtime.start() or runtime),
        ), mock.patch.object(server.sprint_pr_watcher, "start_service"):
            server.start_runtime_services()
            self.assertTrue(started.wait(timeout=1))
            blocked_at = time.monotonic()
            time.sleep(5.2)
            self.assertGreaterEqual(time.monotonic() - blocked_at, 5)
            with sqlite3.connect(database) as con:
                con.row_factory = sqlite3.Row
                health = sprint_runtime.runtime_status(con)
                row = con.execute(
                    "SELECT state,lease_owner,claim_generation,attempt_count "
                    "FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
                    (self.sprint_id,),
                ).fetchone()
            self.assertTrue(runtime.wait_ready(timeout=0))
            self.assertTrue(runtime.is_alive())
            self.assertEqual("live", health["state"])
            self.assertEqual(1, len(claims))
            self.assertEqual(
                ("running", "startup-runtime:cleanup", 1, 0),
                tuple(row),
            )
            worker = runtime._cleanup_thread
            self.assertIsInstance(worker, threading.Thread)
            self.assertFalse(worker.daemon)
            runtime.stop()
            runtime.join(timeout=1)
            self.assertFalse(runtime.is_alive())
            self.assertTrue(worker.is_alive())
            release.set()
            self.assertTrue(finished.wait(timeout=1))
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())

        with sqlite3.connect(database) as con:
            final = con.execute(
                "SELECT state,lease_owner,claim_generation,attempt_count "
                "FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()
        self.assertEqual(("succeeded", None, 1, 0), tuple(final))

    def test_render_only_launcher_preserves_pending_dirty_worktree(self):
        self._dirty_worktree()
        before_row = tuple(
            self._worktree_row()[field]
            for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "lease_owner",
                "lease_expires_at",
                "waiting_reason",
            )
        )
        before_branch = self._git(
            self.worktree, "branch", "--show-current"
        ).stdout.strip()
        before_head = self._git(self.worktree, "rev-parse", "HEAD").stdout.strip()

        with mock.patch.dict(run.os.environ, {"RENDER_ONLY": "1"}), mock.patch.object(
            sprint_cleanup, "SprintCleanupExecutor"
        ) as executor_class:
            run.cleanup_before_launch(
                self.con,
                {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"},
            )

        executor_class.assert_not_called()
        after_row = tuple(
            self._worktree_row()[field]
            for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "lease_owner",
                "lease_expires_at",
                "waiting_reason",
            )
        )
        self.assertEqual(
            ("pending", 0, 0, None, None, None),
            before_row,
        )
        self.assertEqual(before_row, after_row)
        self.assertEqual(
            ("feat/disposable", before_head),
            (
                self._git(self.worktree, "branch", "--show-current").stdout.strip(),
                self._git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
            ),
        )
        self.assertEqual("feat/disposable", before_branch)
        self.assertEqual("staged dirt\n", (self.worktree / "tracked.txt").read_text())
        self.assertEqual("discard\n", (self.worktree / "untracked.txt").read_text())
        self.assertEqual(
            "discard nested\n",
            (self.worktree / "nested-repository" / "only-local.txt").read_text(),
        )

    def test_real_launcher_resets_pending_dirty_worktree(self):
        self._dirty_worktree()
        executor = self._executor()

        with mock.patch.dict(run.os.environ, {}, clear=True), mock.patch.object(
            sprint_cleanup,
            "SprintCleanupExecutor",
            return_value=executor,
        ) as executor_class:
            run.cleanup_before_launch(
                self.con,
                {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"},
            )

        executor_class.assert_called_once()
        row = self._worktree_row()
        self.assertEqual(
            ("succeeded", 1, 1, None, None),
            tuple(row[field] for field in (
                "state",
                "attempt_count",
                "claim_generation",
                "lease_owner",
                "lease_expires_at",
            )),
        )
        self.assertEqual(
            "shell/dev1",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertEqual(
            self._git(self.repository, "rev-parse", "origin/main").stdout.strip(),
            self._git(self.worktree, "rev-parse", "HEAD").stdout.strip(),
        )
        self.assertEqual(
            "",
            self._git(self.worktree, "status", "--porcelain").stdout,
        )
        self.assertFalse((self.worktree / "local-commit.txt").exists())
        self.assertFalse((self.worktree / "untracked.txt").exists())
        self.assertFalse((self.worktree / "nested-repository").exists())

    def test_broker_leased_turn_cleans_then_binds_archive_and_dispatches(self):
        self._dirty_worktree()
        database = Path(self.tmp.name) / "broker-launch.db"
        with sqlite3.connect(database) as target:
            self.con.backup(target)
        with sqlite3.connect(database) as con:
            conversation_id = con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,provider,model,effort,worktree,"
                "state,creation_idempotency_key,creation_request_hash) "
                "VALUES (1,1,'codex','openai','gpt-test','high',?,'queued',"
                "'cleanup-launch','cleanup-launch-hash') RETURNING conversation_id",
                (str(self.worktree),),
            ).fetchone()[0]
            con.execute(
                "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (1,?)",
                (conversation_id,),
            )
            message_id = con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'user','1','prompt','clean then dispatch',"
                "'cleanup-message','cleanup-message-hash','queued')",
                (conversation_id,),
            ).lastrowid
            con.execute(
                "INSERT INTO conversation_outbox (conversation_id,message_id) "
                "VALUES (?,?)",
                (conversation_id, message_id),
            )
            con.commit()

        leased_ids: list[int] = []

        def prepare(**kwargs):
            leased_ids.append(kwargs["current_leased_run_id"])
            with sqlite3.connect(database) as con:
                con.row_factory = sqlite3.Row
                run.cleanup_before_launch(
                    con,
                    {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"},
                    current_leased_run_id=kwargs["current_leased_run_id"],
                )
                archive_id = con.execute(
                    "INSERT INTO shell_memory_archives "
                    "(shell_id,session_id,date,full_narrative) "
                    "VALUES (1,'9001','2026-08-12','prepared')"
                ).lastrowid
                con.commit()
            return SimpleNamespace(
                cwd=str(self.worktree),
                archive_id=archive_id,
                harness="codex",
                model="gpt-test",
                effort="high",
                env={},
            )

        class DispatchAdapter:
            harness = "codex"

            def __init__(self):
                self.started = 0

            def start(self, context, _message):
                self.started += 1
                return NativeTurn(
                    "codex",
                    "native-session",
                    "native-run",
                    context.checked_worktree(),
                )

            def stream(self, _turn):
                yield NormalizedEvent(
                    "session.started", {"session_ref": "native-session"}
                )
                yield NormalizedEvent("run.started", {"status": "running"})
                yield NormalizedEvent("run.completed", {"status": "completed"})

            def close(self):
                return None

        adapter = DispatchAdapter()
        preparer = ConversationLaunchPreparer(
            database,
            prepare_launch=prepare,
            liveness=lambda: {"supported": True, "processes": []},
        )
        broker = ConversationBroker(
            database,
            adapter_factory=lambda _harness: adapter,
            launch_preparer=preparer,
            owner="cleanup-launch-broker",
            heartbeat_seconds=60,
            recovery_seconds=60,
        )
        snapshot = {
            "supported": True,
            "repo": {"root": str(self.repository.resolve())},
            "indeterminate": False,
            "active_other_shells": [],
            "claimed_pids": {},
        }
        try:
            with mock.patch.object(
                sprint_cleanup.shell_liveness, "compute", return_value=snapshot
            ), mock.patch.object(
                sprint_cleanup.git_prune,
                "prune",
                return_value={"candidates": 0, "deleted": [], "failed": []},
            ):
                broker.start()
                self.assertTrue(broker.wait_started(timeout=2))
                broker.notify()
                deadline = time.monotonic() + 5
                durable = None
                while time.monotonic() < deadline:
                    with sqlite3.connect(database) as con:
                        durable = con.execute(
                            "SELECT run_id,state,archive_id FROM conversation_runs "
                            "WHERE trigger_message_id=?",
                            (message_id,),
                        ).fetchone()
                    if durable is not None and durable[1] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(durable)
                self.assertEqual("succeeded", durable[1])
        finally:
            broker.stop()
            broker.join(timeout=2)
            self.assertFalse(broker.is_alive())
            self.assertTrue(broker.wait_idle(timeout=2))

        with sqlite3.connect(database) as con:
            cleanup_row = con.execute(
                "SELECT state,attempt_count,claim_generation,waiting_reason "
                "FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()
            durable = con.execute(
                "SELECT run.run_id,run.state,run.archive_id,"
                "run.harness_session_after,archive.session_id "
                "FROM conversation_runs run "
                "JOIN shell_memory_archives archive "
                "ON archive.archive_id=run.archive_id "
                "WHERE run.trigger_message_id=?",
                (message_id,),
            ).fetchone()
        self.assertEqual([durable[0]], leased_ids)
        self.assertEqual(("succeeded", 1, 1, None), tuple(cleanup_row))
        self.assertEqual(
            ("succeeded", "native-session"),
            (durable[1], durable[3]),
        )
        self.assertEqual((1, "9001"), (durable[2], durable[4]))
        self.assertEqual(1, adapter.started)
        self.assertEqual(
            ("shell/dev1", ""),
            (
                self._git(self.worktree, "branch", "--show-current").stdout.strip(),
                self._git(self.worktree, "status", "--porcelain").stdout,
            ),
        )
        self.assertFalse((self.worktree / "untracked.txt").exists())

    def test_launcher_excludes_only_its_exact_leased_run(self):
        self._dirty_worktree()
        self.con.execute("DROP INDEX idx_conversation_runs_live_conversation")
        self.con.execute("DROP INDEX idx_conversation_runs_live_shell")
        conversation_id = self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex',?,'running','lease-race','lease-race-hash') "
            "RETURNING conversation_id",
            (str(self.worktree),),
        ).fetchone()[0]

        def leased_run(key: str) -> int:
            message_id = self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'user','1','prompt',?,?,?,'running')",
                (conversation_id, key, key, f"{key}-hash"),
            ).lastrowid
            return int(self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,heartbeat_at) "
                "VALUES (?,1,?,'leased',?,'2099-01-01 00:00:00',datetime('now'))",
                (conversation_id, message_id, key),
            ).lastrowid)

        current_run_id = leased_run("current-launch")
        other_run_id = leased_run("different-owner")
        self.con.commit()

        with self.assertRaises(run.LaunchError):
            run.cleanup_before_launch(
                self.con,
                {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"},
                current_leased_run_id=current_run_id,
            )

        row = self._worktree_row()
        self.assertEqual(
            ("pending", 0, 1, "waiting_for_run_exit"),
            (
                row["state"],
                row["attempt_count"],
                row["claim_generation"],
                row["waiting_reason"],
            ),
        )
        self.assertEqual(current_run_id + 1, other_run_id)
        self.assertEqual(
            "feat/disposable",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertEqual("staged dirt\n", (self.worktree / "tracked.txt").read_text())
        self.assertEqual("discard\n", (self.worktree / "untracked.txt").read_text())

    def test_launcher_refuses_target_held_by_runtime_claim(self):
        claim = self.cleanup.claim_next(
            "sprint-runtime:fixture:cleanup",
            shell_id=1,
            lease_seconds=60,
        )
        self.assertIsInstance(claim, sprint_cleanup.CleanupClaim)
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET lease_expires_at='2099-01-01 00:00:00' "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        )
        self.con.commit()
        self.assertEqual(1, claim.claim_generation)

        with self.assertRaises(run.LaunchError) as raised:
            run.cleanup_before_launch(
                self.con,
                {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"},
            )

        self.assertIn("launcher_result=idle", str(raised.exception))
        self.assertIn("last_safe_fact=cleanup_claim_active", str(raised.exception))
        row = self._worktree_row()
        self.assertEqual(
            ("running", "sprint-runtime:fixture:cleanup", 1, 0),
            (
                row["state"],
                row["lease_owner"],
                row["claim_generation"],
                row["attempt_count"],
            ),
        )

    def test_simultaneous_launcher_and_runtime_claim_one_fenced_winner(self):
        database = Path(self.tmp.name) / "claims.db"
        with sqlite3.connect(database) as target:
            self.con.backup(target)

        barrier = threading.Barrier(2)
        claims: list[tuple[str, sprint_cleanup.CleanupClaim | None]] = []
        claims_lock = threading.Lock()

        def claim(owner: str) -> None:
            with sqlite3.connect(database, timeout=5) as con:
                con.row_factory = sqlite3.Row
                store = sprint_cleanup.SprintCleanupTargetStore(
                    con,
                    clock=lambda: self.now,
                )
                barrier.wait(timeout=5)
                result = store.claim_next(owner, shell_id=1, lease_seconds=60)
                with claims_lock:
                    claims.append((owner, result))

        threads = [
            threading.Thread(target=claim, args=("launcher:fixture",)),
            threading.Thread(target=claim, args=("sprint-runtime:fixture:cleanup",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([False, False], [thread.is_alive() for thread in threads])
        winners = [(owner, item) for owner, item in claims if item is not None]
        losers = [(owner, item) for owner, item in claims if item is None]
        self.assertEqual((1, 1), (len(winners), len(losers)))
        with sqlite3.connect(database) as con:
            row = con.execute(
                "SELECT state,lease_owner,claim_generation,attempt_count "
                "FROM sprint_cleanup_targets WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()
        self.assertEqual(
            ("running", winners[0][0], 1, 0),
            tuple(row),
        )

    def test_post_fetch_live_race_preserves_all_three_mutation_attempts(self):
        self._dirty_worktree()
        calls = 0

        def becomes_live(_claim):
            nonlocal calls
            calls += 1
            return "live" if calls == 2 else "dormant"

        waiting = self._executor(liveness=becomes_live).run_next("fixture", shell_id=1)

        self.assertEqual(
            ("waiting", "waiting_for_run_exit", 0),
            (waiting.state, waiting.code, waiting.attempt_count),
        )
        row = self._worktree_row()
        self.assertEqual(
            ("pending", 0, "waiting_for_run_exit", None),
            (
                row["state"],
                row["attempt_count"],
                row["waiting_reason"],
                row["last_error_code"],
            ),
        )
        self.assertEqual(
            "feat/disposable",
            self._git(self.worktree, "branch", "--show-current").stdout.strip(),
        )
        self.assertTrue((self.worktree / "untracked.txt").is_file())

        class FailEveryClean(sprint_cleanup.SprintCleanupExecutor):
            def _git(self, repo, *args, code, timeout=None):
                if args[:2] == ("clean", "-ffd"):
                    raise sprint_cleanup.SprintCleanupMutationError(
                        "clean_current_failed",
                        "injected destructive failure",
                    )
                return super()._git(repo, *args, code=code, timeout=timeout)

        failing = FailEveryClean(
            self.cleanup,
            liveness_probe=lambda _claim: "dormant",
            branch_pruner=lambda _repo: {
                "candidates": 0,
                "deleted": [],
                "failed": [],
            },
            lease_seconds=60,
        )
        self.now += timedelta(seconds=6)
        failures = []
        for _attempt in range(3):
            failures.append(failing.run_next("fixture", shell_id=1))
            self.now += timedelta(seconds=6)

        self.assertEqual(
            ["pending", "pending", "failed"], [item.state for item in failures]
        )
        self.assertEqual([1, 2, 3], [item.attempt_count for item in failures])
        self.assertTrue(all(item.code == "clean_current_failed" for item in failures))
        row = self._worktree_row()
        self.assertEqual(
            ("failed", 3, "clean_current_failed"),
            (row["state"], row["attempt_count"], row["last_error_code"]),
        )
        self.assertTrue((self.worktree / "untracked.txt").is_file())

    def test_fetch_failures_backoff_without_spending_destructive_budget(self):
        self._git(self.repository, "remote", "remove", "origin")
        executor = self._executor()
        receipts = []
        for _attempt in range(3):
            receipts.append(executor.run_next("fixture", shell_id=1))
            self.now += timedelta(seconds=6)

        self.assertEqual(
            ["pending", "pending", "pending"], [item.state for item in receipts]
        )
        self.assertEqual([0, 0, 0], [item.attempt_count for item in receipts])
        self.assertTrue(all(item.code == "fetch_failed" for item in receipts))
        row = self._worktree_row()
        self.assertEqual(
            ("pending", 0, "retry_backoff", "fetch_failed"),
            (
                row["state"],
                row["attempt_count"],
                row["waiting_reason"],
                row["last_error_code"],
            ),
        )
        self.assertEqual(
            3,
            row["claim_generation"],
        )
        self.assertEqual(
            "missing remote origin",
            row["last_error_detail"],
        )
        self.assertEqual(
            (None, None),
            (row["before_evidence"], row["after_evidence"]),
        )

    def test_partial_git_mutation_retries_to_convergence(self):
        self._dirty_worktree()
        refreshed_main = self._advance_remote_main()

        class FailFirstClean(sprint_cleanup.SprintCleanupExecutor):
            failed = False

            def _git(self, repo, *args, code, timeout=None):
                if args[:2] == ("clean", "-ffd") and not self.failed:
                    self.failed = True
                    raise sprint_cleanup.SprintCleanupMutationError(
                        "clean_current_failed",
                        "injected partial mutation",
                    )
                return super()._git(repo, *args, code=code, timeout=timeout)

        first_executor = FailFirstClean(
            self.cleanup,
            liveness_probe=lambda _claim: "dormant",
            branch_pruner=lambda _repo: {
                "candidates": 0,
                "deleted": [],
                "failed": [],
            },
            lease_seconds=60,
        )
        first = first_executor.run_next("fixture", shell_id=1)
        self.now += timedelta(seconds=6)
        second = self._executor().run_next("fixture", shell_id=1)

        self.assertEqual(
            ("pending", "clean_current_failed", 1),
            (first.state, first.code, first.attempt_count),
        )
        self.assertEqual(("succeeded", 2), (second.state, second.attempt_count))
        self.assertEqual(
            refreshed_main, self._git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        )
        self.assertFalse((self.worktree / "nested-repository").exists())
        row = self._worktree_row()
        retry = json.loads(row["after_evidence"])["retry_evidence"]
        self.assertEqual(
            (1, "clean_current_failed", "injected partial mutation"),
            (
                retry["failed_attempts"],
                retry["last_error_code"],
                retry["last_error_detail"],
            ),
        )
        self.assertIsNone(row["last_error_code"])

    def test_artifact_deletion_is_exact_and_records_bounded_count(self):
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (self.sprint_id,),
        )
        self.con.commit()
        artifact = self.repository / "shared" / "sprints" / f"sprint-{self.sprint_id}"
        artifact.mkdir(parents=True)
        (artifact / "one.txt").write_text("one\n", encoding="utf-8")
        (artifact / "nested").mkdir()
        (artifact / "nested" / "two.txt").write_text("two\n", encoding="utf-8")
        adjacent = artifact.parent / "sprint-999"
        adjacent.mkdir()
        (adjacent / "keep.txt").write_text("keep\n", encoding="utf-8")

        receipt = self._executor().run_next("fixture")

        self.assertEqual("succeeded", receipt.state)
        self.assertFalse(artifact.exists())
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        row = self.con.execute(
            "SELECT state,attempt_count,before_evidence,after_evidence "
            "FROM sprint_cleanup_targets WHERE sprint_id=? "
            "AND target_kind='artifact_dir'",
            (self.sprint_id,),
        ).fetchone()
        before = json.loads(row["before_evidence"])
        after = json.loads(row["after_evidence"])
        self.assertEqual(
            ("succeeded", 1, 3),
            (row["state"], row["attempt_count"], before["entry_count"]),
        )
        self.assertEqual(
            (True, 3, False),
            (
                after["existed"],
                after["removed_entry_count"],
                after["entry_count_truncated"],
            ),
        )


class SprintCleanupRecoveryTest(SprintDomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (5, "FnB", "FNB", "admin", "prompt"),
                (6, "Outside developer", "DEV2", "dev", "prompt"),
            ),
        )
        self.con.commit()
        self.targets = sprint_cleanup.SprintCleanupTargetStore(
            self.con,
            identity_provider=lambda: (TEST_ROOT, TEST_COMMON_DIR),
        )
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=self.targets,
        )
        self.sprint_id, self.unit_id = self.create_sprint()
        self.lifecycle.arm(self.sprint_id, 3)
        self.lifecycle.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="recovery fixture",
            terminal_outcome="accepted",
        )
        self.recovery = sprint_cleanup.SprintCleanupRecoveryStore(
            self.con,
            target_store=self.targets,
        )

    def test_status_is_participant_bounded_and_rejects_unrelated_shell(self):
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET before_evidence=? "
            "WHERE sprint_id=? AND shell_id=1",
            (
                json.dumps(
                    {
                        "branch": "feat/local",
                        "head": "a" * 40,
                        "status_count": 1,
                        "status_sample": ["?? secret-name.txt"],
                    }
                ),
                self.sprint_id,
            ),
        )
        self.con.commit()

        failed_id = int(
            self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=? AND shell_id=2",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',"
            "last_error_code='clean_current_failed',last_error_detail=? "
            "WHERE cleanup_target_id=?",
            (f"command failed under {TEST_ROOT}/secret", failed_id),
        )
        self.con.commit()

        status = self.recovery.status(self.sprint_id, 1)

        self.assertEqual(
            ("failed", 4, 3, 0, 0, 1),
            (
                status["aggregate_state"],
                status["target_count"],
                status["pending_count"],
                status["running_count"],
                status["succeeded_count"],
                status["failed_count"],
            ),
        )
        developer = next(
            row for row in status["targets"] if row["shell"]["shell_id"] == 1
        )
        self.assertEqual(".sc-worktrees/dev1", developer["path_label"])
        self.assertEqual(
            {"branch": "feat/local", "head": "a" * 40, "status_count": 1},
            developer["before"],
        )
        self.assertNotIn(str(TEST_ROOT), json.dumps(status))
        self.assertNotIn("secret-name", json.dumps(status))
        self.assertEqual(
            {"code": "clean_current_failed"},
            next(
                row
                for row in status["targets"]
                if row["shell"]["shell_id"] == 2
            )["error"],
        )
        with self.assertRaisesRegex(
            sprint_cleanup.SprintCleanupRequestError,
            "only a Sprint participant or FnB",
        ) as refused:
            self.recovery.status(self.sprint_id, 6)
        self.assertEqual(
            (403, "cleanup_status_forbidden"),
            (refused.exception.status, refused.exception.code),
        )

    def test_planner_retry_is_idempotent_and_preserves_failure_evidence(self):
        target_id = int(
            self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=? AND shell_id=1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',attempt_count=3,"
            "last_error_code='clean_current_failed',"
            "last_error_detail='bounded failure' WHERE cleanup_target_id=?",
            (target_id,),
        )
        self.con.commit()

        first = self.recovery.recover(
            self.sprint_id,
            3,
            idempotency_key="retry-cleanup-fixture",
            adopt_legacy=False,
        )
        replay = self.recovery.recover(
            self.sprint_id,
            3,
            idempotency_key="retry-cleanup-fixture",
            adopt_legacy=False,
        )

        self.assertEqual(
            (True, False, "requeued", (target_id,), "pending"),
            (
                first.created,
                replay.created,
                first.action,
                first.target_ids,
                first.projection.aggregate_state,
            ),
        )
        self.assertEqual(first.request_id, replay.request_id)
        row = self.con.execute(
            "SELECT state,attempt_count,waiting_reason,last_error_code,"
            "last_error_detail FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (target_id,),
        ).fetchone()
        self.assertEqual(
            (
                "pending",
                3,
                "manual_retry",
                "clean_current_failed",
                "bounded failure",
            ),
            tuple(row),
        )
        self.assertEqual(
            (1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_cleanup_requests "
                    " WHERE idempotency_key='retry-cleanup-fixture'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    " AND event_type='sprint.cleanup_requeued')",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )
        with self.assertRaisesRegex(
            sprint_cleanup.SprintCleanupRequestError,
            "reused with different input",
        ) as conflict:
            self.recovery.recover(
                self.sprint_id,
                3,
                idempotency_key="retry-cleanup-fixture",
                adopt_legacy=True,
            )
        self.assertEqual("idempotency_key_reused", conflict.exception.code)

    def test_only_fnb_can_adopt_one_completed_legacy_sprint(self):
        legacy_sprint, _unit = self.create_sprint()
        legacy_lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=mock.Mock(
                prepare_targets=mock.Mock(return_value=()),
                schedule_in_transaction=mock.Mock(return_value=None),
                unresolved_worktree=mock.Mock(return_value=None),
            ),
        )
        legacy_lifecycle.arm(legacy_sprint, 3)
        legacy_lifecycle.transition(
            legacy_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="historical completion before cleanup scheduling",
            terminal_outcome="accepted",
        )

        with self.assertRaisesRegex(
            sprint_cleanup.SprintCleanupRequestError,
            "only FnB",
        ) as refused:
            self.recovery.recover(
                legacy_sprint,
                3,
                idempotency_key="planner-cannot-adopt",
                adopt_legacy=True,
            )
        self.assertEqual("legacy_adoption_forbidden", refused.exception.code)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?",
                (legacy_sprint,),
            ).fetchone()[0],
        )

        adopted = self.recovery.recover(
            legacy_sprint,
            5,
            idempotency_key="fnb-adopts-one-legacy-sprint",
            adopt_legacy=True,
        )
        replay = self.recovery.recover(
            legacy_sprint,
            5,
            idempotency_key="fnb-adopts-one-legacy-sprint",
            adopt_legacy=True,
        )

        self.assertEqual(
            ("adopted_legacy", True, False, 4, "pending"),
            (
                adopted.action,
                adopted.created,
                replay.created,
                len(adopted.target_ids),
                adopted.projection.aggregate_state,
            ),
        )
        self.assertEqual(adopted.target_ids, replay.target_ids)
        self.assertEqual(
            (1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_cleanup_requests "
                    " WHERE idempotency_key='fnb-adopts-one-legacy-sprint'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    " AND event_type='sprint.cleanup_adopted')",
                    (legacy_sprint,),
                ).fetchone()
            ),
        )

    def _adopt_legacy_with_inactive_planner(self, key: str) -> int:
        legacy_sprint, _unit = self.create_sprint()
        lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=mock.Mock(
                prepare_targets=mock.Mock(return_value=()),
                schedule_in_transaction=mock.Mock(return_value=None),
                unresolved_worktree=mock.Mock(return_value=None),
            ),
        )
        lifecycle.arm(legacy_sprint, 3)
        lifecycle.transition(
            legacy_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="historical completion before cleanup scheduling",
            terminal_outcome="accepted",
        )
        receipt = self.recovery.recover(
            legacy_sprint,
            5,
            idempotency_key=key,
            adopt_legacy=True,
        )
        self.assertEqual((True, "adopted_legacy", 4), (
            receipt.created,
            receipt.action,
            len(receipt.target_ids),
        ))
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id<>? AND state='pending'",
            (legacy_sprint,),
        )
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=3")
        self.con.commit()
        return legacy_sprint

    def test_legacy_success_routes_once_to_fnb_when_planner_is_inactive(self):
        sprint_id = self._adopt_legacy_with_inactive_planner(
            "fnb-adopts-legacy-success"
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (sprint_id,),
        )
        self.con.commit()
        claim = self.targets.claim_next("inactive-planner-success")
        self.assertEqual("artifact_dir", claim.target_kind)

        self.assertTrue(self.targets.mark_succeeded(claim, {"existed": False}))
        self.assertFalse(self.targets.mark_succeeded(claim, {"existed": False}))

        target = self.con.execute(
            "SELECT state,after_evidence FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (sprint_id,),
        ).fetchall()
        notices = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (f"_sc:system:sprint:{sprint_id}:cleanup-completed",),
        ).fetchall()
        self.assertEqual(("succeeded", {"existed": False}), (
            target["state"],
            json.loads(target["after_evidence"]),
        ))
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"aggregate_state": "succeeded", "succeeded_count": 4, "target_count": 4},
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(1, len(notices))
        self.assertEqual((5, "re-enter"), tuple(notices[0][:2]))
        self.assertIn("FnB fallback receipt", notices[0]["body"])
        self.assertIn("worktrees are reusable", notices[0]["body"])

    def test_legacy_failure_routes_once_to_fnb_when_planner_is_inactive(self):
        sprint_id = self._adopt_legacy_with_inactive_planner(
            "fnb-adopts-legacy-failure"
        )
        claim = self.targets.claim_next("inactive-planner-failure", shell_id=1)
        self.assertEqual("worktree", claim.target_kind)

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )
        self.assertFalse(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )

        target = self.con.execute(
            "SELECT state,last_error_code,last_error_detail "
            "FROM sprint_cleanup_targets WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_failed'",
            (sprint_id,),
        ).fetchall()
        key = (
            f"_sc:system:sprint:{sprint_id}:cleanup-failed:"
            f"target:{claim.cleanup_target_id}:"
            f"generation:{claim.claim_generation}"
        )
        notices = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchall()
        self.assertEqual(
            (
                "failed",
                "git_common_dir_mismatch",
                "stored repository identity changed",
            ),
            tuple(target),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            "git_common_dir_mismatch",
            json.loads(events[0]["payload"])["error_code"],
        )
        self.assertEqual(1, len(notices))
        self.assertEqual((5, "re-enter"), tuple(notices[0][:2]))
        self.assertIn("FnB fallback receipt", notices[0]["body"])
        self.assertIn("cleanup-status", notices[0]["body"])

    def test_participant_key_cannot_suppress_terminal_cleanup_receipt(self):
        participant_key = f"sprint:{self.sprint_id}:cleanup-completed"
        system_key = f"_sc:system:sprint:{self.sprint_id}:cleanup-completed"
        participant = sprint_message_delivery.SprintMessageStore(self.con).relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="ordinary participant notification",
            idempotency_key=participant_key,
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "cannot use the reserved System namespace",
        ):
            sprint_message_delivery.SprintMessageStore(self.con).relay(
                self.sprint_id,
                from_shell_id=1,
                to_shortname="PLN1",
                body="attempt to occupy the cleanup receipt identity",
                idempotency_key=system_key,
            )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (self.sprint_id,),
        )
        self.con.commit()
        claim = self.targets.claim_next("participant-key-collision")
        self.assertEqual("artifact_dir", claim.target_kind)

        evidence = {"existed": True, "removed_entry_count": 1}
        self.assertTrue(self.targets.mark_succeeded(claim, evidence))
        self.assertFalse(self.targets.mark_succeeded(claim, evidence))

        target = self.con.execute(
            "SELECT state,after_evidence FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (self.sprint_id,),
        ).fetchall()
        participant_messages = self.con.execute(
            "SELECT sprint_id,sender_shell_id,receiver_shell_id,body "
            "FROM wake_message WHERE idempotency_key=?",
            (participant_key,),
        ).fetchall()
        receipts = self.con.execute(
            "SELECT message_id,sprint_id,sender_shell_id,receiver_shell_id,"
            "declared_type,body FROM wake_message WHERE idempotency_key=?",
            (system_key,),
        ).fetchall()
        self.assertEqual(
            ("succeeded", evidence),
            (target["state"], json.loads(target["after_evidence"])),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"aggregate_state": "succeeded", "succeeded_count": 4, "target_count": 4},
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(
            [(self.sprint_id, 1, 3, "ordinary participant notification")],
            [tuple(row) for row in participant_messages],
        )
        self.assertEqual(1, len(receipts))
        self.assertEqual((None, None, 3, "re-enter"), tuple(receipts[0][1:5]))
        self.assertIn("cleanup completed", receipts[0]["body"])
        self.assertIn("worktrees are reusable", receipts[0]["body"])
        self.assertEqual(
            [(participant.message_id, 1), (receipts[0]["message_id"], 1)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,COUNT(*) FROM sprint_wake_messages "
                    "WHERE message_id IN (?,?) GROUP BY message_id ORDER BY message_id",
                    (participant.message_id, receipts[0]["message_id"]),
                )
            ],
        )

    def test_terminal_state_and_event_commit_across_reserved_key_collision(self):
        system_key = f"_sc:system:sprint:{self.sprint_id}:cleanup-completed"
        fallback_key = f"{system_key}:collision:1"
        conflict = sprint_message_delivery.SprintMessageStore(
            self.con
        ).send_to_shell(
            3,
            message_kind="notification",
            body="pre-existing engine-wide conflict",
            idempotency_key=system_key,
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (self.sprint_id,),
        )
        self.con.commit()
        claim = self.targets.claim_next("reserved-key-collision")
        self.assertEqual("artifact_dir", claim.target_kind)

        evidence = {"existed": True, "removed_entry_count": 2}
        self.assertTrue(self.targets.mark_succeeded(claim, evidence))
        self.assertFalse(self.targets.mark_succeeded(claim, evidence))

        target = self.con.execute(
            "SELECT state,after_evidence FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (self.sprint_id,),
        ).fetchall()
        messages = self.con.execute(
            "SELECT message_id,receiver_shell_id,body,idempotency_key "
            "FROM wake_message WHERE idempotency_key IN (?,?) ORDER BY message_id",
            (system_key, fallback_key),
        ).fetchall()
        self.assertEqual(
            ("succeeded", evidence),
            (target["state"], json.loads(target["after_evidence"])),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"aggregate_state": "succeeded", "succeeded_count": 4, "target_count": 4},
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(2, len(messages))
        self.assertEqual(
            [
                (
                    conflict.message_id,
                    3,
                    "pre-existing engine-wide conflict",
                    system_key,
                ),
                (
                    messages[1]["message_id"],
                    3,
                    f"Sprint {self.sprint_id} cleanup completed. "
                    "cleanup_state=succeeded; target_count=4. Its managed "
                    "participant worktrees are reusable.",
                    fallback_key,
                ),
            ],
            [tuple(row) for row in messages],
        )
        self.assertEqual(
            [
                (conflict.message_id, 3, "pending"),
                (messages[1]["message_id"], 3, "pending"),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wm.message_id,w.receiver_shell_id,w.state "
                    "FROM sprint_wake_messages wm JOIN sprint_wake_outbox w "
                    "USING (wake_id) WHERE wm.message_id IN (?,?) "
                    "ORDER BY wm.message_id",
                    (conflict.message_id, messages[1]["message_id"]),
                )
            ],
        )

    def test_terminal_failure_receipt_recovers_from_malformed_key_collision(self):
        claim = self.targets.claim_next("malformed-failure-collision", shell_id=1)
        self.assertEqual("worktree", claim.target_kind)
        system_key = (
            f"_sc:system:sprint:{self.sprint_id}:cleanup-failed:"
            f"target:{claim.cleanup_target_id}:generation:{claim.claim_generation}"
        )
        fallback_key = f"{system_key}:collision:1"
        malformed_id = int(
            self.con.execute(
                "INSERT INTO wake_message "
                "(receiver_shell_id,message_kind,body,declared_type,actionable,"
                "idempotency_key) VALUES (3,'notification',?, 're-enter',0,?)",
                ("malformed engine-wide conflict", system_key),
            ).lastrowid
        )
        self.con.commit()

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )
        self.assertFalse(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )

        target = self.con.execute(
            "SELECT state,last_error_code,last_error_detail "
            "FROM sprint_cleanup_targets WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_failed'",
            (self.sprint_id,),
        ).fetchall()
        messages = self.con.execute(
            "SELECT message_id,receiver_shell_id,body,idempotency_key "
            "FROM wake_message WHERE idempotency_key IN (?,?) ORDER BY message_id",
            (system_key, fallback_key),
        ).fetchall()
        self.assertEqual(
            (
                "failed",
                "git_common_dir_mismatch",
                "stored repository identity changed",
            ),
            tuple(target),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {
                "aggregate_state": "failed",
                "attempt_count": 0,
                "claim_generation": claim.claim_generation,
                "cleanup_target_id": claim.cleanup_target_id,
                "error_code": "git_common_dir_mismatch",
                "path_label": ".sc-worktrees/dev1",
                "target_kind": "worktree",
            },
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(2, len(messages))
        self.assertEqual(
            (malformed_id, 3, "malformed engine-wide conflict", system_key),
            tuple(messages[0]),
        )
        self.assertEqual(
            (messages[1]["message_id"], 3, fallback_key),
            (
                messages[1]["message_id"],
                messages[1]["receiver_shell_id"],
                messages[1]["idempotency_key"],
            ),
        )
        self.assertIn("cleanup failed", messages[1]["body"])
        self.assertIn("cleanup-status", messages[1]["body"])
        self.assertEqual(
            [(messages[1]["message_id"], 3, "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wm.message_id,w.receiver_shell_id,w.state "
                    "FROM sprint_wake_messages wm JOIN sprint_wake_outbox w "
                    "USING (wake_id) WHERE wm.message_id IN (?,?) "
                    "ORDER BY wm.message_id",
                    (malformed_id, messages[1]["message_id"]),
                )
            ],
        )

    def test_legacy_success_commits_once_when_planner_and_fnb_are_inactive(self):
        sprint_id = self._adopt_legacy_with_inactive_planner(
            "no-receiver-adopts-legacy-success"
        )
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=5")
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (sprint_id,),
        )
        self.con.commit()
        claim = self.targets.claim_next("no-receiver-success")
        self.assertEqual("artifact_dir", claim.target_kind)

        self.assertTrue(self.targets.mark_succeeded(claim, {"existed": False}))
        self.assertFalse(self.targets.mark_succeeded(claim, {"existed": False}))

        target = self.con.execute(
            "SELECT state,after_evidence FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (sprint_id,),
        ).fetchall()
        notices = self.con.execute(
            "SELECT receiver_shell_id FROM wake_message WHERE idempotency_key=?",
            (f"_sc:system:sprint:{sprint_id}:cleanup-completed",),
        ).fetchall()
        self.assertEqual(
            ("succeeded", {"existed": False}),
            (target["state"], json.loads(target["after_evidence"])),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"aggregate_state": "succeeded", "succeeded_count": 4, "target_count": 4},
            json.loads(events[0]["payload"]),
        )
        self.assertEqual([], notices)

    def test_legacy_failure_commits_once_when_planner_and_fnb_are_inactive(self):
        sprint_id = self._adopt_legacy_with_inactive_planner(
            "no-receiver-adopts-legacy-failure"
        )
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=5")
        self.con.commit()
        claim = self.targets.claim_next("no-receiver-failure", shell_id=1)
        self.assertEqual("worktree", claim.target_kind)

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )
        self.assertFalse(
            self.targets.fail_safety(
                claim,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )

        target = self.con.execute(
            "SELECT state,last_error_code,last_error_detail "
            "FROM sprint_cleanup_targets WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_failed'",
            (sprint_id,),
        ).fetchall()
        key = (
            f"_sc:system:sprint:{sprint_id}:cleanup-failed:"
            f"target:{claim.cleanup_target_id}:"
            f"generation:{claim.claim_generation}"
        )
        notices = self.con.execute(
            "SELECT receiver_shell_id FROM wake_message WHERE idempotency_key=?",
            (key,),
        ).fetchall()
        self.assertEqual(
            (
                "failed",
                "git_common_dir_mismatch",
                "stored repository identity changed",
            ),
            tuple(target),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {
                "aggregate_state": "failed",
                "attempt_count": 0,
                "claim_generation": claim.claim_generation,
                "cleanup_target_id": claim.cleanup_target_id,
                "error_code": "git_common_dir_mismatch",
                "path_label": ".sc-worktrees/dev1",
                "target_kind": "worktree",
            },
            json.loads(events[0]["payload"]),
        )
        self.assertEqual([], notices)

    def test_terminal_transitions_emit_exact_planner_receipts_atomically(self):
        failed = self.targets.claim_next("failure-fixture", shell_id=1)
        self.assertIsNotNone(failed)
        self.assertTrue(
            self.targets.fail_safety(
                failed,
                "git_common_dir_mismatch",
                "stored repository identity changed",
            )
        )
        failure_event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_failed'",
            (self.sprint_id,),
        ).fetchone()
        failure_message = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (
                f"_sc:system:sprint:{self.sprint_id}:cleanup-failed:"
                f"target:{failed.cleanup_target_id}:generation:{failed.claim_generation}",
            ),
        ).fetchone()
        self.assertEqual(
            "git_common_dir_mismatch", json.loads(failure_event[0])["error_code"]
        )
        self.assertEqual((3, "re-enter"), tuple(failure_message[:2]))
        self.assertIn("cleanup-status", failure_message[2])
        self.assertIn("cleanup --sprint", failure_message[2])
        self.recovery.recover(
            self.sprint_id,
            3,
            idempotency_key="terminal-receipt-test-retry",
            adopt_legacy=False,
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()

        second_sprint, _unit = self.create_sprint()
        self.lifecycle.arm(second_sprint, 3)
        self.lifecycle.transition(
            second_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="success receipt fixture",
            terminal_outcome="accepted",
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree'",
            (second_sprint,),
        )
        self.con.commit()
        artifact = self.targets.claim_next("success-fixture")
        self.assertIsNotNone(artifact)
        self.assertEqual("artifact_dir", artifact.target_kind)
        self.assertTrue(self.targets.mark_succeeded(artifact, {"existed": False}))

        completed_event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (second_sprint,),
        ).fetchone()
        completed_message = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (f"_sc:system:sprint:{second_sprint}:cleanup-completed",),
        ).fetchone()
        self.assertEqual(
            ("succeeded", 4),
            (
                json.loads(completed_event[0])["aggregate_state"],
                json.loads(completed_event[0])["target_count"],
            ),
        )
        self.assertEqual((3, "re-enter"), tuple(completed_message[:2]))
        self.assertIn("worktrees are reusable", completed_message[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
