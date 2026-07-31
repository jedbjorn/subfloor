"""Feature #26 durable conversation Git-target observations."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
TESTS = ROOT / "tests"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(TESTS))

import conversation_git_targets  # noqa: E402
import snapshot  # noqa: E402
from review_fixtures import ReviewRepository  # noqa: E402


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class ConversationGitTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReviewRepository()
        self.addCleanup(self.fixture.cleanup)
        self.db_path = self.fixture.root / "shell.db"
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev','dev','prompt',1)"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES ('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,1,"
            "'codex',?,'create','hash')",
            (str(self.fixture.repo),),
        )
        con.commit()
        con.close()
        self.conversation_id = "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def rows(self):
        con = self.connect()
        try:
            return con.execute(
                "SELECT * FROM conversation_git_targets "
                "ORDER BY first_seen_at,target_id"
            ).fetchall()
        finally:
            con.close()

    def test_observation_is_idempotent_and_updates_bounded_head_history(
        self,
    ) -> None:
        first_head = self.fixture.git("rev-parse", "HEAD")
        first_time = datetime(2026, 7, 30, 19, 0, tzinfo=timezone.utc)
        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
                now=first_time,
            )
        )

        self.fixture.write("second.txt", "second\n")
        latest_head = self.fixture.commit("second")
        second_time = datetime(2026, 7, 30, 19, 1, tzinfo=timezone.utc)
        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
                now=second_time,
            )
        )

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch_name"], "main")
        self.assertEqual(rows[0]["base_ref"], "origin/main")
        self.assertEqual(rows[0]["first_head_sha"], first_head)
        self.assertEqual(rows[0]["latest_head_sha"], latest_head)
        self.assertEqual(rows[0]["first_seen_at"], "2026-07-30 19:00:00")
        self.assertEqual(rows[0]["last_seen_at"], "2026-07-30 19:01:00")

    def test_pr_association_never_gets_repurposed_by_later_local_work(
        self,
    ) -> None:
        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
            )
        )
        con = self.connect()
        con.execute(
            "UPDATE conversation_git_targets SET pr_number=821,"
            "pr_head_sha=latest_head_sha,pr_state='OPEN',"
            "remote_refreshed_at=datetime('now')"
        )
        con.commit()
        con.close()

        self.fixture.write("reuse.txt", "new delivery\n")
        reused_head = self.fixture.commit("reuse branch")
        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
            )
        )

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        pr_target = next(row for row in rows if row["pr_number"] == 821)
        local_target = next(row for row in rows if row["pr_number"] is None)
        self.assertNotEqual(pr_target["target_id"], local_target["target_id"])
        self.assertEqual(local_target["first_head_sha"], reused_head)

    def test_git_reads_happen_before_the_write_transaction(self) -> None:
        def unlocked_runner(*args, **kwargs):
            con = sqlite3.connect(self.db_path, timeout=0.1)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.rollback()
            finally:
                con.close()
            command = args[0]
            self.assertEqual(command[:3], ["git", "-C", str(self.fixture.repo)])
            return self.fixture.git_result(*command[3:], check=False)

        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
                runner=unlocked_runner,
            )
        )

    def test_every_git_failure_is_non_blocking_and_writes_nothing(self) -> None:
        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["git"], 2)

        self.assertFalse(
            conversation_git_targets.safely_observe_and_persist(
                self.db_path,
                self.conversation_id,
                runner=timeout,
            )
        )
        self.assertEqual(self.rows(), [])
        con = self.connect()
        try:
            self.assertEqual(
                con.execute(
                    "SELECT state FROM conversations WHERE conversation_id=?",
                    (self.conversation_id,),
                ).fetchone()[0],
                "idle",
            )
        finally:
            con.close()

    def test_snapshot_round_trip_retains_targets_after_branch_cleanup(
        self,
    ) -> None:
        self.assertTrue(
            conversation_git_targets.observe_and_persist(
                self.db_path,
                self.conversation_id,
            )
        )
        con = self.connect()
        try:
            con.execute(
                "UPDATE conversation_git_targets SET pr_number=821,"
                "pr_head_sha=latest_head_sha,pr_state='MERGED',"
                "merge_sha=?,merged_at='2026-07-30 19:30:00',"
                "remote_refreshed_at='2026-07-30 19:31:00'",
                ("f" * 40,),
            )
            con.commit()
            statements = snapshot.dump_table(
                con,
                "conversation_git_targets",
            )
        finally:
            con.close()

        target = sqlite3.connect(":memory:")
        target.row_factory = sqlite3.Row
        apply_schema(target)
        target.execute(
            "INSERT INTO users (user_id,username) VALUES (1,'operator')"
        )
        target.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev','dev','prompt',1)"
        )
        target.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES ('cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,1,"
            "'codex','/missing/after-cleanup','create','hash')"
        )
        target.executescript(
            "\n".join(["BEGIN;", *statements, "COMMIT;"])
        )
        retained = target.execute(
            "SELECT pr_number,pr_state,merge_sha "
            "FROM conversation_git_targets"
        ).fetchone()
        self.assertEqual(tuple(retained), (821, "MERGED", "f" * 40))
        self.assertEqual(target.execute(
            "PRAGMA foreign_key_check"
        ).fetchall(), [])
        target.close()
