#!/usr/bin/env python3
"""Interface schema tests (sprint 25 seq 4, spec #20 task #80).

Builds a throwaway engine DB from the real schema.sql + the full migration
chain and asserts the 0078 surface: every Interface table exists with its key
columns, the sprint_doc_id columns landed, and the uniqueness invariants the
occupancy model requires actually hold at the DB layer:

- one non-ended Interface session per shell (ended rows free the slot)
- one live generation per shell
- one current writer lease per session
- one live wake batch per binding
- one unreleased binding per planner, and per sprint
- unique (binding, message) wake work
- idempotency keys unique per (actor, operation, key)

Run:
    python3 tests/test_interface_schema.py
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

TABLES = [
    "interface_generations", "interface_sessions", "interface_writer_leases",
    "interface_input_state", "interface_idempotency_keys",
    "sprint_planner_bindings", "planner_wake_items", "planner_wake_batches",
    "planner_action_receipts", "pr_poll_runs", "pr_poll_observations",
    "planner_alerts",
]


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    for sid in (1, 2):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
            "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (?,?,?,'test','sp',1,0,1,1)", (sid, f"S{sid}", f"s{sid}"))
    con.execute(
        "INSERT INTO documents (document_id, kind, title) "
        "VALUES (1,'doc','SPRINT: test')")
    con.commit()
    con.close()


class InterfaceSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _session(self, shell_id=1, generation=1, occupancy="reserved"):
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation) "
            "VALUES (?,?)", (shell_id, generation))
        cur = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy) "
            "VALUES (?,?,?)", (shell_id, generation, occupancy))
        return cur.lastrowid

    def _binding(self, planner=2, doc=1, released=False):
        sid = self._session(shell_id=planner, generation=10 + planner,
                            occupancy="occupied")
        cur = self.con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
            " generation, released_at) VALUES (?,?,?,?,?,?)",
            (doc, planner, sid, planner, 10 + planner,
             "2026-01-01" if released else None))
        return cur.lastrowid

    def test_tables_and_columns_exist(self):
        for table in TABLES:
            row = self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            self.assertIsNotNone(row, f"missing table {table}")
        msg_cols = {r[1] for r in self.con.execute(
            "PRAGMA table_info(shell_messages)")}
        self.assertIn("sprint_doc_id", msg_cols)
        wp_cols = {r[1] for r in self.con.execute(
            "PRAGMA table_info(watched_prs)")}
        self.assertIn("sprint_doc_id", wp_cols)

    def test_one_non_ended_session_per_shell(self):
        self._session(shell_id=1, generation=1, occupancy="occupied")
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._session(shell_id=1, generation=2, occupancy="reserved")
        self.con.rollback()
        # Ending the live session frees the slot.
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended', ended_at="
            "datetime('now') WHERE shell_id=1 AND generation=1")
        self._session(shell_id=1, generation=2, occupancy="reserved")

    def test_one_live_generation_per_shell(self):
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO interface_generations (shell_id, generation) "
                "VALUES (1,2)")
        self.con.rollback()
        self.con.execute(
            "UPDATE interface_generations SET ended_at=datetime('now') "
            "WHERE shell_id=1 AND generation=1")
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,2)")

    def test_one_current_writer_per_session(self):
        sid = self._session(occupancy="occupied")
        self.con.execute(
            "INSERT INTO interface_writer_leases "
            "(session_id, shell_id, generation, client_id, token_hash) "
            "VALUES (?,1,1,'c1','h1')", (sid,))
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO interface_writer_leases "
                "(session_id, shell_id, generation, client_id, token_hash) "
                "VALUES (?,1,1,'c2','h2')", (sid,))
        self.con.rollback()
        # Revoking frees the writer slot (takeover).
        self.con.execute(
            "UPDATE interface_writer_leases SET revoked_at=datetime('now') "
            "WHERE session_id=?", (sid,))
        self.con.execute(
            "INSERT INTO interface_writer_leases "
            "(session_id, shell_id, generation, client_id, token_hash) "
            "VALUES (?,1,1,'c2','h2')", (sid,))

    def test_one_live_batch_per_binding(self):
        bid = self._binding()
        self.con.execute(
            "INSERT INTO planner_wake_batches (binding_id, shell_id, "
            "generation) VALUES (?,2,12)", (bid,))
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO planner_wake_batches (binding_id, shell_id, "
                "generation) VALUES (?,2,12)", (bid,))
        self.con.rollback()
        self.con.execute(
            "UPDATE planner_wake_batches SET state='complete' "
            "WHERE binding_id=?", (bid,))
        self.con.execute(
            "INSERT INTO planner_wake_batches (binding_id, shell_id, "
            "generation) VALUES (?,2,12)", (bid,))

    def test_one_unreleased_binding_per_planner_and_sprint(self):
        bid = self._binding(planner=2, doc=1)
        sess, gen = self.con.execute(
            "SELECT session_id, generation FROM sprint_planner_bindings "
            "WHERE binding_id=?", (bid,)).fetchone()
        self.con.commit()
        # Same planner, same live session, second armed binding → planner slot.
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO sprint_planner_bindings "
                "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
                " generation) VALUES (1,2,?,?,?)", (sess, 2, gen))
        self.con.rollback()
        # A different planner arming the SAME sprint doc → sprint slot.
        other = self._session(shell_id=1, generation=11, occupancy="occupied")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO sprint_planner_bindings "
                "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
                " generation) VALUES (1,1,?,1,11)", (other,))
        self.con.rollback()
        # Released bindings free both slots.
        self.con.execute(
            "UPDATE sprint_planner_bindings SET released_at=datetime('now')")
        self.con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
            " generation) VALUES (1,2,?,?,?)", (sess, 2, gen))

    def test_wake_item_unique_per_binding_message(self):
        bid = self._binding()
        self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, "
            "kind, sprint_doc_id) VALUES (1,2,'wake','task',1)")
        mid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.con.execute(
            "INSERT INTO planner_wake_items (binding_id, message_id) "
            "VALUES (?,?)", (bid, mid))
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO planner_wake_items (binding_id, message_id) "
                "VALUES (?,?)", (bid, mid))
        self.con.rollback()

    def test_idempotency_key_scope_unique(self):
        self.con.execute(
            "INSERT INTO interface_idempotency_keys "
            "(actor_scope, operation, idem_key, request_hash, expires_at) "
            "VALUES ('operator','sessions.create','k1','h','2030-01-01')")
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO interface_idempotency_keys "
                "(actor_scope, operation, idem_key, request_hash, expires_at) "
                "VALUES ('operator','sessions.create','k1','h2','2030-01-01')")
        self.con.rollback()
        # Same key under a different operation is a different slot.
        self.con.execute(
            "INSERT INTO interface_idempotency_keys "
            "(actor_scope, operation, idem_key, request_hash, expires_at) "
            "VALUES ('operator','sessions.stop','k1','h','2030-01-01')")

    def test_alert_dedupe_while_open(self):
        self.con.execute(
            "INSERT INTO planner_alerts (severity, reason, dedupe_key) "
            "VALUES ('critical','crash','-|1|crash')")
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO planner_alerts (severity, reason, dedupe_key) "
                "VALUES ('critical','crash','-|1|crash')")
        self.con.rollback()
        self.con.execute("UPDATE planner_alerts SET resolved_at=datetime('now')")
        self.con.execute(
            "INSERT INTO planner_alerts (severity, reason, dedupe_key) "
            "VALUES ('critical','crash','-|1|crash')")


if __name__ == "__main__":
    unittest.main()
