"""Successful-Sprint artifact cleanup scheduling, rollback, and replay gates."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [
    str(ENGINE / "api"),
    str(ENGINE / "scripts"),
    str(ROOT / "tests"),
]

import sprint_cleanup
import sprint_close
import sprint_domain
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
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now') WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()

    def artifact_path(self, sprint_id: int | None = None) -> str:
        return f"{TEST_ROOT}/shared/sprints/sprint-{sprint_id or self.sprint_id}"

    def cleanup_rows(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT shell_id,target_kind,canonical_path,repository_root,"
            "git_common_dir,expected_base_branch,state,attempt_count,"
            "claim_generation FROM sprint_cleanup_targets WHERE sprint_id=? "
            "ORDER BY target_kind,canonical_path",
            (self.sprint_id,),
        ).fetchall()

    def complete(self, reason: str = "Fallback close") -> bool:
        return self.store.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason=reason,
            terminal_outcome="accepted",
        )

    def insert_legacy_worktree_row(self) -> int:
        """Write the row shape an older engine left behind for one shell."""
        row_id = self.con.execute(
            "INSERT INTO sprint_cleanup_targets "
            "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
            "git_common_dir,expected_base_branch) VALUES "
            "(?,1,'worktree',?,?,?,'shell/dev1')",
            (
                self.sprint_id,
                f"{TEST_ROOT}/.sc-worktrees/dev1",
                str(TEST_ROOT),
                str(TEST_COMMON_DIR),
            ),
        ).lastrowid
        self.con.commit()
        return int(row_id)

    def test_direct_completion_schedules_one_artifact_target_and_replays_idle(self):
        self.assertTrue(self.complete())
        self.assertEqual(
            [
                (
                    None,
                    "artifact_dir",
                    self.artifact_path(),
                    str(TEST_ROOT),
                    str(TEST_COMMON_DIR),
                    None,
                    "pending",
                    0,
                    0,
                )
            ],
            [tuple(row) for row in self.cleanup_rows()],
        )
        projection = self.cleanup.project(self.sprint_id)
        self.assertEqual(
            ("pending", 1, 1, 0, 0, 0),
            (
                projection.aggregate_state,
                projection.target_count,
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
        self.assertEqual(1, payload["target_count"])
        self.assertEqual(1, len(payload["artifact_target_ids"]))
        self.assertNotIn("worktree_target_ids", payload)

        self.assertFalse(self.complete())
        self.assertEqual(1, len(self.cleanup_rows()))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='sprint.cleanup_scheduled'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_pause_abort_and_abort_replay_never_schedule_cleanup(self):
        paused = self.store.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture pause must preserve every Sprint artifact",
        )

        self.assertTrue(paused.changed)
        self.assertEqual(
            ("paused", 0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='sprint.cleanup_scheduled'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.completed') "
                    "FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,) * 4,
                ).fetchone()
            ),
        )

        aborted = self.store.abort(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture abort must preserve every Sprint artifact",
            terminal_outcome="aborted",
        )
        replay = self.store.abort(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture abort must preserve every Sprint artifact",
            terminal_outcome="aborted",
        )

        self.assertTrue(aborted.changed)
        self.assertFalse(replay.changed)
        self.assertEqual(
            ("aborted", 0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,"
                    "(SELECT COUNT(*) FROM sprint_cleanup_targets WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='sprint.cleanup_scheduled'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.completed') "
                    "FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,) * 4,
                ).fetchone()
            ),
        )

    def test_prepare_targets_never_consults_sprint_participants(self):
        # Cleanup no longer touches shells, so a Sprint with no managed
        # (or only Admin) participants still resolves its one artifact target.
        participantless = self.sprint_id + 1000
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participants WHERE sprint_id=?",
                (participantless,),
            ).fetchone()[0],
        )

        targets = self.cleanup.prepare_targets(participantless)

        self.assertEqual(
            [("artifact_dir", self.artifact_path(participantless))],
            [(target.target_kind, target.canonical_path) for target in targets],
        )

    def test_scheduling_failure_rolls_back_direct_completion(self):
        self.con.execute(
            "CREATE TRIGGER reject_cleanup_schedule BEFORE INSERT "
            "ON sprint_cleanup_targets BEGIN "
            "SELECT RAISE(ABORT,'reject cleanup schedule'); END"
        )
        self.con.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject cleanup schedule"):
            self.complete()

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
                "(sprint_id,target_kind,canonical_path,repository_root,"
                "git_common_dir) VALUES "
                "(?,'artifact_dir','/repo/shared/sprints/sprint-1','/repo',"
                "'/repo/.git')",
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

        self.complete()
        target_id, before = self.con.execute(
            "SELECT cleanup_target_id,canonical_path FROM sprint_cleanup_targets "
            "WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "identity is immutable"):
            self.con.execute(
                "UPDATE sprint_cleanup_targets SET canonical_path='/other' "
                "WHERE cleanup_target_id=?",
                (target_id,),
            )
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT canonical_path FROM sprint_cleanup_targets "
                "WHERE cleanup_target_id=?",
                (target_id,),
            ).fetchone()[0],
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

    def test_transaction_rejects_a_target_prepared_for_another_sprint(self):
        other_sprint, _unit_id = self.create_sprint()
        targets = self.cleanup.prepare_targets(other_sprint)

        with (
            self.assertRaisesRegex(
                sprint_cleanup.SprintCleanupInvariantError,
                "artifact target changed",
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
        self.assertEqual(1, len(first_rows))
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
        self.complete()
        target_id = int(
            self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("pending", self.cleanup.project(self.sprint_id).aggregate_state)
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',"
            "last_error_code='artifact_delete_failed' WHERE cleanup_target_id=?",
            (target_id,),
        )
        self.assertEqual("failed", self.cleanup.project(self.sprint_id).aggregate_state)
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='running',lease_owner='fixture',"
            "lease_expires_at='2099-01-01 00:00:00',last_error_code=NULL "
            "WHERE cleanup_target_id=?",
            (target_id,),
        )
        self.assertEqual(
            "pending", self.cleanup.project(self.sprint_id).aggregate_state
        )
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded',lease_owner=NULL,"
            "lease_expires_at=NULL WHERE cleanup_target_id=?",
            (target_id,),
        )
        self.assertEqual(
            "succeeded", self.cleanup.project(self.sprint_id).aggregate_state
        )

    def test_legacy_worktree_row_is_never_claimed_counted_or_blocking(self):
        self.complete()
        legacy_id = self.insert_legacy_worktree_row()

        projection = self.cleanup.project(self.sprint_id)
        self.assertEqual((1, 1, "pending"), (
            projection.target_count,
            projection.pending_count,
            projection.aggregate_state,
        ))
        status = sprint_cleanup.SprintCleanupRecoveryStore(
            self.con, target_store=self.cleanup
        ).status(self.sprint_id, 3)
        self.assertEqual(
            [("artifact_dir", f"shared/sprints/sprint-{self.sprint_id}")],
            [(row["target_kind"], row["path_label"]) for row in status["targets"]],
        )

        claim = self.cleanup.claim_next("legacy-fixture")
        self.assertEqual("artifact_dir", claim.target_kind)
        self.assertNotEqual(legacy_id, claim.cleanup_target_id)
        self.assertTrue(self.cleanup.mark_succeeded(claim, {"existed": False}))

        self.assertIsNone(self.cleanup.claim_next("legacy-fixture"))
        self.assertEqual(
            "succeeded", self.cleanup.project(self.sprint_id).aggregate_state
        )
        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT state FROM sprint_cleanup_targets WHERE cleanup_target_id=?",
                (legacy_id,),
            ).fetchone()[0],
        )


class SprintCleanupExecutorTest(SprintDomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repository = Path(self.tmp.name) / "repository"
        self._git(Path(self.tmp.name), "init", str(self.repository))
        self._git(self.repository, "config", "user.name", "Sprint Fixture")
        self._git(self.repository, "config", "user.email", "fixture@example.test")
        (self.repository / ".gitignore").write_text(
            "shared/sprints/\n", encoding="utf-8"
        )
        self._git(self.repository, "add", ".gitignore")
        self._git(self.repository, "commit", "-m", "initial")
        self._git(self.repository, "branch", "-M", "main")

        self.now = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)
        self.common_dir = (self.repository / ".git").resolve()
        self.cleanup = sprint_cleanup.SprintCleanupTargetStore(
            self.con,
            identity_provider=lambda: (self.repository.resolve(), self.common_dir),
            clock=lambda: self.now,
        )
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda _harness: None,
            cleanup_store=self.cleanup,
        )
        self.sprint_id, self.unit_id = self.create_sprint()
        self.lifecycle.arm(self.sprint_id, 3)
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now') WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
        self.lifecycle.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="fixture completion",
            terminal_outcome="accepted",
        )
        self.artifact = (
            self.repository / "shared" / "sprints" / f"sprint-{self.sprint_id}"
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

    def _executor(self, **kwargs) -> sprint_cleanup.SprintCleanupExecutor:
        return sprint_cleanup.SprintCleanupExecutor(
            self.cleanup, lease_seconds=60, **kwargs
        )

    def _artifact_row(self) -> sqlite3.Row:
        return self.con.execute(
            "SELECT * FROM sprint_cleanup_targets WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()

    def _populate_artifact(self) -> None:
        self.artifact.mkdir(parents=True)
        (self.artifact / "one.txt").write_text("one\n", encoding="utf-8")
        (self.artifact / "nested").mkdir()
        (self.artifact / "nested" / "two.txt").write_text("two\n", encoding="utf-8")

    def _adjacent_sprint(self) -> Path:
        adjacent = self.repository / "shared" / "sprints" / "sprint-999"
        adjacent.mkdir(parents=True, exist_ok=True)
        (adjacent / "keep.txt").write_text("keep\n", encoding="utf-8")
        return adjacent

    def test_artifact_deletion_is_exact_and_records_bounded_count(self):
        self._populate_artifact()
        adjacent = self._adjacent_sprint()

        receipt = self._executor().run_next("fixture")

        self.assertEqual("succeeded", receipt.state)
        self.assertFalse(self.artifact.exists())
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        row = self._artifact_row()
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

    def test_absent_artifact_target_succeeds_once_without_adjacent_mutation(self):
        adjacent = self._adjacent_sprint()

        receipt = self._executor().run_next("fixture")
        replay = self._executor().run_next("fixture")

        self.assertEqual(("succeeded", 1), (receipt.state, receipt.attempt_count))
        self.assertEqual("idle", replay.state)
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        row = self._artifact_row()
        self.assertEqual(("succeeded", 1), (row["state"], row["attempt_count"]))
        self.assertEqual(
            {"existed": False, "entry_count": 0, "entry_count_truncated": False},
            json.loads(row["before_evidence"]),
        )
        self.assertEqual(
            {
                "existed": False,
                "removed_entry_count": 0,
                "entry_count_truncated": False,
            },
            json.loads(row["after_evidence"]),
        )

    def test_artifact_delete_failure_retries_to_exact_convergence(self):
        self.artifact.mkdir(parents=True)
        sentinel = self.artifact / "retry.txt"
        sentinel.write_text("retry\n", encoding="utf-8")
        adjacent = self._adjacent_sprint()

        with mock.patch.object(
            sprint_cleanup.shutil,
            "rmtree",
            side_effect=OSError("injected artifact refusal"),
        ):
            first = self._executor().run_next("fixture")

        self.assertEqual(
            ("pending", "artifact_delete_failed", 1),
            (first.state, first.code, first.attempt_count),
        )
        self.assertEqual("retry\n", sentinel.read_text())
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        failed_row = self.con.execute(
            "SELECT state,attempt_count,last_error_code,after_evidence "
            "FROM sprint_cleanup_targets WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual(
            ("pending", 1, "artifact_delete_failed", None),
            tuple(failed_row),
        )

        self.now += timedelta(seconds=6)
        second = self._executor().run_next("fixture")

        self.assertEqual(("succeeded", 2), (second.state, second.attempt_count))
        self.assertFalse(self.artifact.exists())
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        row = self._artifact_row()
        retry = json.loads(row["after_evidence"])["retry_evidence"]
        self.assertEqual(
            ("succeeded", 2, None, 1, "artifact_delete_failed"),
            (
                row["state"],
                row["attempt_count"],
                row["last_error_code"],
                retry["failed_attempts"],
                retry["last_error_code"],
            ),
        )

    def test_symlinked_artifact_parent_refuses_escape_and_preserves_bytes(self):
        outside = Path(self.tmp.name) / "outside"
        (outside / "sprints" / f"sprint-{self.sprint_id}").mkdir(parents=True)
        sentinel = outside / "sprints" / f"sprint-{self.sprint_id}" / "survive.txt"
        sentinel.write_text("outside authority\n", encoding="utf-8")
        (self.repository / "shared").symlink_to(outside, target_is_directory=True)

        receipt = self._executor().run_next("fixture")

        self.assertEqual(
            ("failed", "symlink_component_refused", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual("outside authority\n", sentinel.read_text())
        row = self._artifact_row()
        self.assertEqual(
            ("failed", 0, "symlink_component_refused", None),
            (
                row["state"],
                row["attempt_count"],
                row["last_error_code"],
                row["before_evidence"],
            ),
        )

    def test_symlinked_artifact_target_is_refused_before_mutation(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "survive.txt"
        sentinel.write_text("outside authority\n", encoding="utf-8")
        self.artifact.parent.mkdir(parents=True)
        self.artifact.symlink_to(outside, target_is_directory=True)

        receipt = self._executor().run_next("fixture")

        self.assertEqual(
            ("failed", "artifact_symlink_refused", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertTrue(self.artifact.is_symlink())
        self.assertEqual("outside authority\n", sentinel.read_text())

    def test_unreadable_repository_identity_fails_closed_and_preserves_bytes(self):
        self._populate_artifact()
        (self.repository / ".git" / "HEAD").unlink()

        receipt = self._executor().run_next("fixture")

        self.assertEqual(
            ("failed", "repository_identity_unreadable", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual("one\n", (self.artifact / "one.txt").read_text())
        row = self._artifact_row()
        self.assertEqual(
            ("failed", 0, "repository_identity_unreadable", None),
            (
                row["state"],
                row["attempt_count"],
                row["last_error_code"],
                row["before_evidence"],
            ),
        )

    def test_busy_repository_lock_defers_without_consuming_an_attempt(self):
        self._populate_artifact()
        with (self.common_dir / "sc-sprint-cleanup.lock").open("a+") as holder:
            flock(holder.fileno(), LOCK_EX | LOCK_NB)
            receipt = sprint_cleanup.SprintCleanupExecutor(
                self.cleanup, lease_seconds=60, lock_timeout=0.1
            ).run_next("fixture")

        self.assertEqual(
            ("waiting", "repository_lock_busy", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual("one\n", (self.artifact / "one.txt").read_text())
        row = self._artifact_row()
        self.assertEqual(
            ("pending", 0, "repository_lock_busy", None),
            (
                row["state"],
                row["attempt_count"],
                row["waiting_reason"],
                row["last_error_code"],
            ),
        )

    def test_reclaimed_generation_cannot_mutate_or_write_terminal_state(self):
        self._populate_artifact()
        first = self.cleanup.claim_next("first", lease_seconds=10)
        self.assertIsNotNone(first)
        self.now += timedelta(seconds=11)
        second = self.cleanup.claim_next("second", lease_seconds=60)
        self.assertIsNotNone(second)

        receipt = self._executor().execute(first)

        self.assertEqual(("stale", "claim_superseded"), (receipt.state, receipt.code))
        self.assertTrue((self.artifact / "one.txt").is_file())
        row = self._artifact_row()
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

    def test_substituted_claim_path_is_refused_before_mutation(self):
        self._populate_artifact()
        adjacent = self._adjacent_sprint()
        claim = self.cleanup.claim_next("fixture", lease_seconds=60)
        self.assertIsInstance(claim, sprint_cleanup.CleanupClaim)

        receipt = self._executor().execute(
            replace(claim, canonical_path=str(adjacent))
        )

        self.assertEqual(
            ("failed", "artifact_path_mismatch", 0),
            (receipt.state, receipt.code, receipt.attempt_count),
        )
        self.assertEqual("keep\n", (adjacent / "keep.txt").read_text())
        self.assertEqual("one\n", (self.artifact / "one.txt").read_text())

    def test_simultaneous_claims_produce_one_fenced_winner(self):
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
                result = store.claim_next(owner, lease_seconds=60)
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
                "FROM sprint_cleanup_targets WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()
        self.assertEqual(("running", winners[0][0], 1, 0), tuple(row))


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
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now') WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
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

    def target_id(self, sprint_id: int | None = None) -> int:
        return int(
            self.con.execute(
                "SELECT cleanup_target_id FROM sprint_cleanup_targets "
                "WHERE sprint_id=?",
                (sprint_id or self.sprint_id,),
            ).fetchone()[0]
        )

    def failure_key(self, claim: sprint_cleanup.CleanupClaim) -> str:
        return (
            f"_sc:system:sprint:{claim.sprint_id}:cleanup-failed:"
            f"target:{claim.cleanup_target_id}:"
            f"generation:{claim.claim_generation}"
        )

    def cleanup_receipts(self) -> list[sqlite3.Row]:
        """Every system cleanup receipt, ignoring ordinary lifecycle wakes."""
        return self.con.execute(
            "SELECT receiver_shell_id,declared_type,body,idempotency_key "
            "FROM wake_message WHERE idempotency_key LIKE '\\_sc:system:%:cleanup-%' "
            "ESCAPE '\\' ORDER BY message_id"
        ).fetchall()

    def test_status_is_participant_bounded_and_rejects_unrelated_shell(self):
        target_id = self.target_id()
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',"
            "last_error_code='artifact_delete_failed',last_error_detail=?,"
            "before_evidence=? WHERE cleanup_target_id=?",
            (
                f"command failed under {TEST_ROOT}/secret",
                json.dumps(
                    {
                        "existed": True,
                        "entry_count": 2,
                        "entry_count_truncated": False,
                        "secret_sample": ["?? secret-name.txt"],
                    }
                ),
                target_id,
            ),
        )
        self.con.commit()

        status = self.recovery.status(self.sprint_id, 1)

        self.assertEqual(
            ("failed", 1, 0, 0, 0, 1),
            (
                status["aggregate_state"],
                status["target_count"],
                status["pending_count"],
                status["running_count"],
                status["succeeded_count"],
                status["failed_count"],
            ),
        )
        self.assertEqual(1, len(status["targets"]))
        target = status["targets"][0]
        self.assertEqual(
            (f"shared/sprints/sprint-{self.sprint_id}", "artifact_dir"),
            (target["path_label"], target["target_kind"]),
        )
        self.assertEqual(
            {"existed": True, "entry_count": 2, "entry_count_truncated": False},
            target["before"],
        )
        self.assertEqual({"code": "artifact_delete_failed"}, target["error"])
        self.assertNotIn(str(TEST_ROOT), json.dumps(status))
        self.assertNotIn("secret-name", json.dumps(status))

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
        target_id = self.target_id()
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='failed',attempt_count=3,"
            "last_error_code='artifact_delete_failed',"
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
                "artifact_delete_failed",
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

    def _legacy_sprint(self) -> int:
        """Complete one Sprint the way an engine without cleanup scheduling did."""
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
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now') WHERE sprint_id=?",
            (legacy_sprint,),
        )
        self.con.commit()
        lifecycle.transition(
            legacy_sprint,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="historical completion before cleanup scheduling",
            terminal_outcome="accepted",
        )
        return legacy_sprint

    def test_only_fnb_can_adopt_one_completed_legacy_sprint(self):
        legacy_sprint = self._legacy_sprint()

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
            ("adopted_legacy", True, False, 1, "pending"),
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
            [("artifact_dir", f"{TEST_ROOT}/shared/sprints/sprint-{legacy_sprint}")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT target_kind,canonical_path FROM sprint_cleanup_targets "
                    "WHERE sprint_id=?",
                    (legacy_sprint,),
                )
            ],
        )
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

    def test_success_records_its_event_and_sends_no_wake(self):
        claim = self.targets.claim_next("silent-success")
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
        self.assertEqual(
            ("succeeded", evidence),
            (target["state"], json.loads(target["after_evidence"])),
        )
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"aggregate_state": "succeeded", "succeeded_count": 1, "target_count": 1},
            json.loads(events[0]["payload"]),
        )
        self.assertEqual([], self.cleanup_receipts())

    def test_failure_sends_exactly_one_planner_receipt_with_the_retry_command(self):
        claim = self.targets.claim_next("failure-fixture")
        self.assertIsNotNone(claim)

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "artifact_path_mismatch",
                "stored artifact identity changed",
            )
        )
        self.assertFalse(
            self.targets.fail_safety(
                claim,
                "artifact_path_mismatch",
                "stored artifact identity changed",
            )
        )

        events = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_failed'",
            (self.sprint_id,),
        ).fetchall()
        receipts = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (self.failure_key(claim),),
        ).fetchall()
        self.assertEqual(1, len(events))
        self.assertEqual(
            {
                "aggregate_state": "failed",
                "attempt_count": 0,
                "claim_generation": claim.claim_generation,
                "cleanup_target_id": claim.cleanup_target_id,
                "error_code": "artifact_path_mismatch",
                "path_label": f"shared/sprints/sprint-{self.sprint_id}",
                "target_kind": "artifact_dir",
            },
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(1, len(receipts))
        self.assertEqual((3, "re-enter"), tuple(receipts[0][:2]))
        body = receipts[0]["body"]
        self.assertIn(
            f"Sprint {self.sprint_id} artifact cleanup failed for "
            f"shared/sprints/sprint-{self.sprint_id} "
            "(error_code=artifact_path_mismatch).",
            body,
        )
        self.assertIn(f"sc sprint cleanup-status --sprint {self.sprint_id}", body)
        self.assertIn(
            f"sc sprint cleanup --sprint {self.sprint_id} --key <stable-retry-key>",
            body,
        )
        self.assertNotIn("worktree", body)
        self.assertEqual(1, len(self.cleanup_receipts()))

    def test_failure_routes_once_to_fnb_when_the_planner_is_inactive(self):
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=3")
        self.con.commit()
        claim = self.targets.claim_next("inactive-planner-failure")

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "artifact_not_directory",
                "stored artifact target is not a directory",
            )
        )

        receipts = self.con.execute(
            "SELECT receiver_shell_id,declared_type,body FROM wake_message "
            "WHERE idempotency_key=?",
            (self.failure_key(claim),),
        ).fetchall()
        self.assertEqual(1, len(receipts))
        self.assertEqual((5, "re-enter"), tuple(receipts[0][:2]))
        self.assertIn("FnB fallback receipt", receipts[0]["body"])
        self.assertIn("cleanup-status", receipts[0]["body"])

    def test_failure_commits_once_when_planner_and_fnb_are_inactive(self):
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id IN (3,5)")
        self.con.commit()
        claim = self.targets.claim_next("no-receiver-failure")

        self.assertTrue(
            self.targets.fail_safety(
                claim,
                "artifact_not_directory",
                "stored artifact target is not a directory",
            )
        )

        target = self.con.execute(
            "SELECT state,last_error_code,last_error_detail "
            "FROM sprint_cleanup_targets WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        self.assertEqual(
            (
                "failed",
                "artifact_not_directory",
                "stored artifact target is not a directory",
            ),
            tuple(target),
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='sprint.cleanup_failed'",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            [],
            self.con.execute(
                "SELECT receiver_shell_id FROM wake_message WHERE idempotency_key=?",
                (self.failure_key(claim),),
            ).fetchall(),
        )

    def test_terminal_failure_receipt_recovers_from_malformed_key_collision(self):
        claim = self.targets.claim_next("malformed-failure-collision")
        system_key = self.failure_key(claim)
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
                "artifact_path_mismatch",
                "stored artifact identity changed",
            )
        )
        self.assertFalse(
            self.targets.fail_safety(
                claim,
                "artifact_path_mismatch",
                "stored artifact identity changed",
            )
        )

        messages = self.con.execute(
            "SELECT message_id,receiver_shell_id,body,idempotency_key "
            "FROM wake_message WHERE idempotency_key IN (?,?) ORDER BY message_id",
            (system_key, fallback_key),
        ).fetchall()
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='sprint.cleanup_failed'",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(2, len(messages))
        self.assertEqual(
            (malformed_id, 3, "malformed engine-wide conflict", system_key),
            tuple(messages[0]),
        )
        self.assertEqual(
            (3, fallback_key),
            (messages[1]["receiver_shell_id"], messages[1]["idempotency_key"]),
        )
        self.assertIn("artifact cleanup failed", messages[1]["body"])
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

    def test_retry_after_failure_converges_to_a_silent_success(self):
        failed = self.targets.claim_next("failure-fixture")
        self.assertTrue(
            self.targets.fail_safety(
                failed,
                "artifact_path_mismatch",
                "stored artifact identity changed",
            )
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message WHERE idempotency_key=?",
                (self.failure_key(failed),),
            ).fetchone()[0],
        )

        self.recovery.recover(
            self.sprint_id,
            3,
            idempotency_key="terminal-receipt-test-retry",
            adopt_legacy=False,
        )
        retried = self.targets.claim_next("success-fixture")
        self.assertIsNotNone(retried)
        self.assertTrue(self.targets.mark_succeeded(retried, {"existed": False}))

        completed_event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.cleanup_completed'",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual(
            ("succeeded", 1),
            (
                json.loads(completed_event[0])["aggregate_state"],
                json.loads(completed_event[0])["target_count"],
            ),
        )
        self.assertEqual(1, len(self.cleanup_receipts()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
