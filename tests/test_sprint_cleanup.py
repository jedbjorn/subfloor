"""Successful-Sprint cleanup scheduling, rollback, and replay gates."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ROOT / "tests")]

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
                self.lifecycle.arm(newer_sprint, 3)
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

    def test_newer_aborted_sprint_preserves_work_without_attempt(self):
        self._dirty_worktree()
        newer_sprint, _unit = self.create_sprint()
        calls = 0

        def aborts_after_fetch(_claim):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.lifecycle.arm(newer_sprint, 3)
                self.lifecycle.abort(
                    newer_sprint,
                    sprint_domain.LifecycleActor("planner", 3),
                    reason="preserve aborted fixture work",
                    terminal_outcome="aborted",
                )
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
                f"sprint:{self.sprint_id}:cleanup-failed:"
                f"target:{failed.cleanup_target_id}:generation:{failed.claim_generation}",
            ),
        ).fetchone()
        self.assertEqual(
            "git_common_dir_mismatch", json.loads(failure_event[0])["error_code"]
        )
        self.assertEqual((3, "re-enter"), tuple(failure_message[:2]))
        self.assertIn("cleanup-status", failure_message[2])
        self.assertIn("cleanup --sprint", failure_message[2])
        self.con.execute(
            "UPDATE sprint_cleanup_targets SET state='succeeded' "
            "WHERE sprint_id=? AND target_kind='worktree' AND state='pending'",
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
            (f"sprint:{second_sprint}:cleanup-completed",),
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
