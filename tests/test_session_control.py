#!/usr/bin/env python3
"""Hermetic schema and state-machine tests for planner session control."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import session_control  # noqa: E402


def build_db() -> sqlite3.Connection:
    """Build a fresh engine DB from the baseline plus every migration."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def build_pre_session_control_db() -> sqlite3.Connection:
    """Build the dirty upgrade shape immediately before migration 0077."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration.name >= "0077_session_control_state.sql":
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed_shell(con: sqlite3.Connection, shell_id: int, shortname: str) -> int:
    con.execute(
        "INSERT OR IGNORE INTO users (user_id, username, is_active) VALUES (1, 'T', 1)"
    )
    con.execute(
        "INSERT INTO shells "
        "(shell_id, display_name, shortname, system_prompt, user_id) "
        "VALUES (?, ?, ?, 'test', 1)",
        (shell_id, shortname.upper(), shortname),
    )
    cur = con.execute(
        "INSERT INTO shell_memory_archives (shell_id, session_id, date) "
        "VALUES (?, ?, '2026-07-21')",
        (shell_id, f"session-{shell_id}"),
    )
    return cur.lastrowid


def insert_binding(
    con: sqlite3.Connection,
    *,
    archive_id: int,
    shell_id: int,
    harness: str = "codex",
    native_session_id: str | None = None,
    state: str = "idle",
    managed: int = 1,
) -> int:
    cur = con.execute(
        "INSERT INTO shell_session_bindings "
        "(archive_id, shell_id, harness, native_session_id, state, managed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (archive_id, shell_id, harness, native_session_id, state, managed),
    )
    return cur.lastrowid


class SessionControlSchemaTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.archive_id = seed_shell(self.con, 1, "plan1")

    def tearDown(self):
        self.con.close()

    def test_binding_defaults_and_foreign_keys(self):
        binding_id = insert_binding(
            self.con, archive_id=self.archive_id, shell_id=1, managed=0
        )
        row = self.con.execute(
            "SELECT * FROM shell_session_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        self.assertEqual(row["archive_id"], self.archive_id)
        self.assertEqual(row["shell_id"], 1)
        self.assertEqual(row["harness"], "codex")
        self.assertIsNone(row["native_session_id"])
        self.assertEqual(row["control_capabilities"], "{}")
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["managed"], 0)
        self.assertEqual(row["lease_generation"], 0)

        with self.assertRaises(sqlite3.IntegrityError):
            insert_binding(self.con, archive_id=999, shell_id=1, managed=0)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_binding(
                self.con, archive_id=self.archive_id, shell_id=999, managed=0
            )

    def test_binding_uniqueness_and_state_constraints(self):
        insert_binding(
            self.con,
            archive_id=self.archive_id,
            shell_id=1,
            native_session_id="thread-1",
        )
        archive_2 = seed_shell(self.con, 2, "plan2")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_binding(
                self.con,
                archive_id=archive_2,
                shell_id=2,
                native_session_id="thread-1",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            insert_binding(
                self.con,
                archive_id=archive_2,
                shell_id=2,
                state="lost",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE shell_session_bindings SET managed = 2 WHERE binding_id = 1"
            )

    def test_only_one_managed_binding_per_shell(self):
        insert_binding(self.con, archive_id=self.archive_id, shell_id=1)
        self.con.execute(
            "INSERT INTO shell_memory_archives (shell_id, session_id, date) "
            "VALUES (1, 'later-session', '2026-07-21')"
        )
        archive_2 = self.con.execute(
            "SELECT MAX(archive_id) FROM shell_memory_archives"
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            insert_binding(
                self.con,
                archive_id=archive_2,
                shell_id=1,
                native_session_id="thread-2",
            )
        # Historical/released rows remain valid when they are unmanaged.
        insert_binding(
            self.con,
            archive_id=archive_2,
            shell_id=1,
            native_session_id="thread-2",
            state="released",
            managed=0,
        )
        rows = self.con.execute(
            "SELECT state, managed FROM shell_session_bindings ORDER BY binding_id"
        ).fetchall()
        self.assertEqual(
            [(r["state"], r["managed"]) for r in rows], [("idle", 1), ("released", 0)]
        )

    def test_wake_job_defaults_uniqueness_and_foreign_keys(self):
        binding_id = insert_binding(self.con, archive_id=self.archive_id, shell_id=1)
        message_id = self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) "
            "VALUES (1, 1, 'wake')"
        ).lastrowid
        wake_id = self.con.execute(
            "INSERT INTO session_wake_jobs (binding_id, trigger_message_id) VALUES (?, ?)",
            (binding_id, message_id),
        ).lastrowid
        row = self.con.execute(
            "SELECT * FROM session_wake_jobs WHERE wake_id = ?", (wake_id,)
        ).fetchone()
        self.assertEqual(row["binding_id"], binding_id)
        self.assertEqual(row["trigger_message_id"], message_id)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempt_count"], 0)
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["finished_at"])
        self.assertIsNone(row["last_error"])

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO session_wake_jobs (binding_id, trigger_message_id) VALUES (?, ?)",
                (binding_id, message_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO session_wake_jobs (binding_id, trigger_message_id) VALUES (999, ?)",
                (message_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO session_wake_jobs (binding_id, trigger_message_id) VALUES (?, 999)",
                (binding_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE session_wake_jobs SET state = 'lost' WHERE wake_id = ?",
                (wake_id,),
            )

    def test_dispatch_indexes_exist(self):
        names = {
            row["name"]
            for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "idx_session_bindings_managed_shell",
                "idx_session_bindings_managed_state",
                "idx_session_wake_jobs_ready",
                "idx_session_wake_jobs_message",
            }.issubset(names)
        )


class SessionControlMigrationTest(unittest.TestCase):
    def test_dirty_pre_0077_database_upgrades_without_losing_messages(self):
        con = build_pre_session_control_db()
        self.addCleanup(con.close)
        archive_id = seed_shell(con, 1, "plan1")
        message_id = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) "
            "VALUES (1, 1, 'already unread before upgrade')"
        ).lastrowid
        con.commit()

        migration = MIGRATIONS / "0077_session_control_state.sql"
        con.executescript(migration.read_text())
        binding_id = insert_binding(
            con, archive_id=archive_id, shell_id=1, state="starting", managed=1
        )

        self.assertEqual(session_control.reconstruct_wake_jobs(con), 1)
        row = con.execute(
            "SELECT binding_id, trigger_message_id, state FROM session_wake_jobs"
        ).fetchone()
        self.assertEqual(tuple(row), (binding_id, message_id, "queued"))
        preserved = con.execute(
            "SELECT body, read_at FROM shell_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        self.assertEqual(tuple(preserved), ("already unread before upgrade", None))


class BindingStateMachineTest(unittest.TestCase):
    ALLOWED = {
        "starting": {"starting", "foreground", "idle", "dormant", "released", "error"},
        "foreground": {
            "foreground",
            "idle",
            "dispatching",
            "dormant",
            "released",
            "error",
        },
        "idle": {"idle", "foreground", "dispatching", "dormant", "released", "error"},
        "dispatching": {
            "dispatching",
            "foreground",
            "idle",
            "dormant",
            "released",
            "error",
        },
        "dormant": {
            "dormant",
            "starting",
            "foreground",
            "idle",
            "dispatching",
            "released",
            "error",
        },
        "released": {"released", "starting"},
        "error": {"error", "starting", "released"},
    }

    def test_every_allowed_and_forbidden_transition(self):
        self.assertEqual(set(self.ALLOWED), set(session_control.BINDING_STATES))
        for current in session_control.BINDING_STATES:
            for target in session_control.BINDING_STATES:
                expected = target in self.ALLOWED[current]
                self.assertEqual(
                    session_control.is_transition_allowed(current, target),
                    expected,
                    f"unexpected policy for {current} -> {target}",
                )
                if expected:
                    session_control.validate_transition(current, target)
                else:
                    with self.assertRaises(session_control.InvalidStateTransition):
                        session_control.validate_transition(current, target)

    def test_unknown_states_fail_closed(self):
        for current, target in (("lost", "idle"), ("idle", "lost")):
            with self.assertRaises(session_control.UnknownBindingState):
                session_control.is_transition_allowed(current, target)

    def test_compare_and_set_transition_and_stale_owner(self):
        con = build_db()
        try:
            archive_id = seed_shell(con, 1, "plan1")
            binding_id = insert_binding(
                con, archive_id=archive_id, shell_id=1, state="idle"
            )
            session_control.transition_binding(
                con, binding_id, expected="idle", target="dispatching"
            )
            row = con.execute(
                "SELECT state FROM shell_session_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            self.assertEqual(row["state"], "dispatching")
            with self.assertRaises(session_control.StaleBindingState) as caught:
                session_control.transition_binding(
                    con, binding_id, expected="idle", target="dormant"
                )
            self.assertEqual(caught.exception.actual, "dispatching")
            self.assertEqual(
                con.execute(
                    "SELECT state FROM shell_session_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()["state"],
                "dispatching",
            )
        finally:
            con.close()

    def test_invalid_transition_and_missing_binding_do_not_write(self):
        con = build_db()
        try:
            archive_id = seed_shell(con, 1, "plan1")
            binding_id = insert_binding(
                con, archive_id=archive_id, shell_id=1, state="released", managed=0
            )
            with self.assertRaises(session_control.InvalidStateTransition):
                session_control.transition_binding(
                    con, binding_id, expected="released", target="dispatching"
                )
            self.assertEqual(
                con.execute(
                    "SELECT state FROM shell_session_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()["state"],
                "released",
            )
            with self.assertRaises(session_control.BindingNotFound):
                session_control.transition_binding(
                    con, 999, expected="starting", target="idle"
                )
        finally:
            con.close()


class WakeReconstructionTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        archive_1 = seed_shell(self.con, 1, "plan1")
        archive_2 = seed_shell(self.con, 2, "plan2")
        self.binding_1 = insert_binding(
            self.con, archive_id=archive_1, shell_id=1, managed=1
        )
        self.binding_2 = insert_binding(
            self.con, archive_id=archive_2, shell_id=2, managed=0
        )

    def tearDown(self):
        self.con.close()

    def _message(self, to_shell_id: int, body: str, *, read: bool = False) -> int:
        cur = self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, read_at) "
            "VALUES (1, ?, ?, ?)",
            (to_shell_id, body, "2026-07-21 19:00:00" if read else None),
        )
        return cur.lastrowid

    def test_reconstructs_only_unread_managed_messages_and_is_idempotent(self):
        unread_managed = self._message(1, "managed unread")
        self._message(1, "managed read", read=True)
        self._message(2, "unmanaged unread")

        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 1)
        rows = self.con.execute(
            "SELECT binding_id, trigger_message_id, state, attempt_count "
            "FROM session_wake_jobs"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(self.binding_1, unread_managed, "queued", 0)],
        )
        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 0)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM session_wake_jobs").fetchone()[0], 1
        )

    def test_rescan_adds_new_messages_without_resetting_existing_job(self):
        first = self._message(1, "first")
        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 1)
        self.con.execute(
            "UPDATE session_wake_jobs SET state = 'failed', attempt_count = 3, "
            "last_error = 'provider down' WHERE trigger_message_id = ?",
            (first,),
        )
        second = self._message(1, "second")

        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 1)
        rows = self.con.execute(
            "SELECT trigger_message_id, state, attempt_count, last_error "
            "FROM session_wake_jobs ORDER BY trigger_message_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (first, "failed", 3, "provider down"),
                (second, "queued", 0, None),
            ],
        )

    def test_enabling_management_makes_existing_unread_message_reconstructible(self):
        message_id = self._message(2, "waiting before manage")
        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 0)
        self.con.execute(
            "UPDATE shell_session_bindings SET managed = 1 WHERE binding_id = ?",
            (self.binding_2,),
        )
        self.assertEqual(session_control.reconstruct_wake_jobs(self.con), 1)
        row = self.con.execute(
            "SELECT binding_id, trigger_message_id FROM session_wake_jobs"
        ).fetchone()
        self.assertEqual(tuple(row), (self.binding_2, message_id))


if __name__ == "__main__":
    unittest.main()
