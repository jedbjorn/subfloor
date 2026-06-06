#!/usr/bin/env python3
"""Tests for the Shell Inbox (shell_messages) SQL surface.

Stdlib `unittest` — no pytest, matching the engine's no-dependency style. Each
test builds a throwaway DB the way the engine ships it (schema.sql + every
migration in filename order, mirroring rebuild.py/migrate.py), seeds two shells,
and exercises the §4 statements from the spec: send / check / mark-read, happy
and negative paths.

Run:
    python3 tests/test_shell_messaging.py
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

# §4 statements, verbatim shape (params bound). `<self>` is a real bound param
# here rather than a textual splice, but the resolved SQL is identical.
CHECK_SQL = (
    "SELECT m.message_id, s.shortname AS from_shortname, m.body, m.created_at "
    "FROM shell_messages m "
    "JOIN shells s ON s.shell_id = m.from_shell_id "
    "WHERE m.to_shell_id = :self AND m.read_at IS NULL "
    "ORDER BY m.created_at LIMIT :limit"
)
SEND_SQL = (
    "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) VALUES ("
    "  :self,"
    "  (SELECT shell_id FROM shells "
    "     WHERE shortname = :to_shortname AND COALESCE(is_deleted,0) = 0),"
    "  :body)"
)
MARK_READ_SQL = (
    "UPDATE shell_messages SET read_at = datetime('now') "
    "WHERE message_id = :id AND to_shell_id = :self AND read_at IS NULL"
)


def build_db() -> sqlite3.Connection:
    """Fresh in-memory DB: schema.sql + every migration, FK enforcement on."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(path.read_text())
    # Migrations may toggle PRAGMA foreign_keys; assert the surface under
    # enforcement (the skill runs with `PRAGMA foreign_keys=ON`).
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed_shells(con: sqlite3.Connection) -> None:
    con.executescript(
        "INSERT INTO shells (shell_id, display_name, shortname, system_prompt) "
        "VALUES (1, 'Dev', 'dev1', 'x'), (2, 'Review', 'rev1', 'x');"
    )
    con.commit()


class ShellMessagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        seed_shells(self.con)

    def tearDown(self) -> None:
        self.con.close()

    # ── table shape ─────────────────────────────────────────────────────────
    def test_table_and_index_exist(self) -> None:
        t = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shell_messages'"
        ).fetchone()
        self.assertIsNotNone(t, "shell_messages table missing after schema+migrations")
        idx = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_shell_messages_to_unread'"
        ).fetchone()
        self.assertIsNotNone(idx, "composite unread index missing")

    # ── send → check → mark-read happy path ─────────────────────────────────
    def test_send_check_mark_read_roundtrip(self) -> None:
        cur = self.con.execute(
            SEND_SQL, {"self": 1, "to_shortname": "rev1", "body": "**spec** ready"}
        )
        self.con.commit()
        self.assertEqual(cur.rowcount, 1)
        msg_id = cur.lastrowid

        # rev1 checks its inbox — the message is there, markdown verbatim.
        rows = self.con.execute(CHECK_SQL, {"self": 2, "limit": 50}).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], msg_id)
        self.assertEqual(rows[0]["from_shortname"], "dev1")
        self.assertEqual(rows[0]["body"], "**spec** ready")

        # sender's inbox is empty (it's recipient-scoped).
        self.assertEqual(self.con.execute(CHECK_SQL, {"self": 1, "limit": 50}).fetchall(), [])

        # rev1 marks it read → disappears from the inbox.
        cur = self.con.execute(MARK_READ_SQL, {"id": msg_id, "self": 2})
        self.con.commit()
        self.assertEqual(cur.rowcount, 1)
        self.assertEqual(self.con.execute(CHECK_SQL, {"self": 2, "limit": 50}).fetchall(), [])

    # ── negative: empty body → CHECK fires ──────────────────────────────────
    def test_empty_body_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                SEND_SQL, {"self": 1, "to_shortname": "rev1", "body": ""}
            )

    # ── negative: unknown recipient → INSERT fails (NOT NULL / FK) ───────────
    def test_unknown_recipient_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                SEND_SQL, {"self": 1, "to_shortname": "nope", "body": "hi"}
            )

    # ── negative: a deleted recipient is unreachable ────────────────────────
    def test_deleted_recipient_rejected(self) -> None:
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shortname='rev1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                SEND_SQL, {"self": 1, "to_shortname": "rev1", "body": "hi"}
            )

    # ── access control: cannot mark-read another shell's message ────────────
    def test_mark_read_foreign_message_noop(self) -> None:
        cur = self.con.execute(
            SEND_SQL, {"self": 1, "to_shortname": "rev1", "body": "for rev1"}
        )
        self.con.commit()
        msg_id = cur.lastrowid
        # dev1 (self=1) tries to mark rev1's inbound read → 0 rows, still unread.
        cur = self.con.execute(MARK_READ_SQL, {"id": msg_id, "self": 1})
        self.con.commit()
        self.assertEqual(cur.rowcount, 0)
        self.assertEqual(len(self.con.execute(CHECK_SQL, {"self": 2, "limit": 50}).fetchall()), 1)

    # ── idempotency: re-marking a read message is a no-op ───────────────────
    def test_mark_read_idempotent(self) -> None:
        cur = self.con.execute(
            SEND_SQL, {"self": 1, "to_shortname": "rev1", "body": "once"}
        )
        self.con.commit()
        msg_id = cur.lastrowid
        first = self.con.execute(MARK_READ_SQL, {"id": msg_id, "self": 2})
        self.con.commit()
        self.assertEqual(first.rowcount, 1)
        second = self.con.execute(MARK_READ_SQL, {"id": msg_id, "self": 2})
        self.con.commit()
        self.assertEqual(second.rowcount, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
