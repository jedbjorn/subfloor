#!/usr/bin/env python3
"""Schema properties of migrations 0096 + 0097 — the quota registry + windows.

Each test here pins a decision the migrations make that a plausible "cleanup"
would undo:

- the window upsert key really is idempotent for account-wide rows (the reason
  it is an expression index over COALESCE(scope,'') and not the plain UNIQUE
  the spec's Data Model literally names — SQLite compares NULLs as distinct, so
  the plain form does not constrain the common path at all);
- the registry has an identity key, so a re-probe advances an account instead
  of minting a duplicate;
- an unrecognized window_kind is STORED, not rejected — the spec requires it be
  "rendered under its raw duration, not dropped", so a tidy CHECK over the five
  known kinds would be data loss on exactly the endpoint drift this feature
  expects;
- status IS closed, because those five values are ours;
- 0097 REALLY DROPPED the five identity columns, with a positive control — an
  assertion that a column is absent is worthless unless it can fail, and this
  one is the schema half of "no operator email is ever written again".

Run:
    python3 tests/test_quota_schema.py
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

UPSERT = (
    "INSERT INTO harness_quota_window "
    "(account_pk, window_kind, scope, used_percent, captured_at, status) "
    "VALUES (?, ?, ?, ?, ?, 'ok') "
    "ON CONFLICT(account_pk, window_kind, COALESCE(scope, '')) "
    "DO UPDATE SET used_percent=excluded.used_percent, "
    "captured_at=excluded.captured_at"
)


class QuotaSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA.read_text())
        for p in sorted(MIGRATIONS.glob("*.sql")):
            self.con.executescript(p.read_text())
        self.con.execute("PRAGMA foreign_keys=ON")
        # Three columns is the whole registry row after 0097.
        self.con.execute(
            "INSERT INTO harness_quota_account (account_pk, provider, "
            "account_ref) VALUES (1, 'anthropic', 'uuid-abc')")

    def tearDown(self):
        self.con.close()
        for p in sorted(self.tmp.glob("*")):
            p.unlink()
        self.tmp.rmdir()

    def _windows(self):
        return self.con.execute(
            "SELECT window_kind, scope, used_percent FROM harness_quota_window "
            "ORDER BY window_pk").fetchall()

    def test_upsert_is_idempotent_for_account_wide_windows(self):
        """scope IS NULL is the COMMON path — session, weekly and five_hour are
        all account-wide. Two probes must leave one row, updated."""
        for percent in (10.0, 20.0):
            self.con.execute(
                UPSERT, (1, "weekly", None, percent, "2026-07-25T00:00:00Z"))
        self.assertEqual(self._windows(), [("weekly", None, 20.0)])

    def test_upsert_is_idempotent_for_scoped_windows(self):
        for percent in (10.0, 30.0):
            self.con.execute(
                UPSERT, (1, "weekly_scoped", "opus", percent,
                         "2026-07-25T00:00:00Z"))
        self.assertEqual(self._windows(), [("weekly_scoped", "opus", 30.0)])

    def test_account_wide_and_scoped_windows_coexist(self):
        """COALESCE folds NULL to '' for uniqueness only — it must not collide a
        NULL scope with a genuinely empty or differently-scoped one."""
        self.con.execute(UPSERT, (1, "weekly", None, 10.0, "t"))
        self.con.execute(UPSERT, (1, "weekly", "opus", 20.0, "t"))
        self.assertEqual(
            self._windows(), [("weekly", None, 10.0), ("weekly", "opus", 20.0)])

    def test_registry_rejects_duplicate_account(self):
        """A re-probe reuses the existing row; it never mints a second one.

        This is the ONLY job account_ref has left after 0097 — it is why the
        column survived a migration that dropped everything around it."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO harness_quota_account (provider, account_ref) "
                "VALUES ('anthropic', 'uuid-abc')")

    def test_same_ref_on_a_different_provider_is_a_different_account(self):
        self.con.execute(
            "INSERT INTO harness_quota_account (provider, account_ref) "
            "VALUES ('openai', 'uuid-abc')")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM harness_quota_account").fetchone()[0], 2)

    def test_0097_dropped_every_identity_column(self):
        """The registry is (account_pk, provider, account_ref) and nothing else.

        POSITIVE CONTROL FIRST, and it is the point of the test: `provider` is
        asserted PRESENT through the same PRAGMA read that reports the others
        absent. Without it a typo'd table name, a PRAGMA that returned nothing,
        or a migration that never ran would all pass this as a clean bill of
        health — an absence assertion that cannot fail is not evidence."""
        cols = {r[1] for r in
                self.con.execute("PRAGMA table_info(harness_quota_account)")}
        self.assertEqual(cols, {"account_pk", "provider", "account_ref"})
        for gone in ("account_label", "plan", "first_seen", "last_seen",
                     "is_current"):
            self.assertNotIn(gone, cols)

    def test_the_last_seen_index_went_with_its_column(self):
        """idx_hqa_last_seen drove the 7-day activity filter, which no longer
        exists. SQLite also refuses to drop an indexed column, so an 0097 that
        left this behind would not have applied at all — the migration order is
        load-bearing, not stylistic."""
        idx = {r[1] for r in
               self.con.execute("PRAGMA index_list(harness_quota_account)")}
        self.assertNotIn("idx_hqa_last_seen", idx)
        # Positive control: the UNIQUE(provider, account_ref) auto-index is
        # still there, so this PRAGMA does report indexes that exist.
        self.assertTrue(any(name.startswith("sqlite_autoindex_harness_quota_account")
                            for name in idx), idx)

    def test_unrecognized_window_kind_is_stored_not_rejected(self):
        """Endpoint drift must degrade to a row rendered under its raw duration,
        never to a dropped row. A CHECK over the five known kinds would turn
        this insert into data loss."""
        self.con.execute(UPSERT, (1, "900_SECOND", "900 SECOND", 5.0, "t"))
        self.assertEqual(self._windows(), [("900_SECOND", "900 SECOND", 5.0)])

    def test_status_is_a_closed_set(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO harness_quota_window (account_pk, window_kind, "
                "captured_at, status) VALUES (1, 'weekly', 't', 'probably_ok')")

    def test_window_requires_a_real_account(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO harness_quota_window (account_pk, window_kind, "
                "captured_at) VALUES (99, 'weekly', 't')")

    def test_counts_and_percent_are_independently_nullable(self):
        """Anthropic and OpenAI report percentage only; Moonshot reports counts
        only. Neither is ever back-computed from the other, so both sides must
        be storable as NULL."""
        self.con.execute(UPSERT, (1, "session", None, 22.0, "t"))
        self.con.execute(
            "INSERT INTO harness_quota_window (account_pk, window_kind, scope, "
            "used, limit_value, captured_at) "
            "VALUES (1, 'five_hour', NULL, 120, 500, 't')")
        rows = self.con.execute(
            "SELECT window_kind, used_percent, used, limit_value "
            "FROM harness_quota_window ORDER BY window_pk").fetchall()
        self.assertEqual(
            rows, [("session", 22.0, None, None), ("five_hour", None, 120, 500)])


if __name__ == "__main__":
    unittest.main()
