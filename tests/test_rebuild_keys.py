#!/usr/bin/env python3
"""Tests that `./sc rebuild` preserves shell api_keys instead of rotating them.

content.sql never serializes api_key (secret in a git-tracked file), so a
rebuild reloads every shell NULL-keyed. Before #265 the post-rebuild backfill
then minted brand-new keys — an implicit rotation that orphaned the
SC_API_TOKEN run.py injected into every live session at boot and 401'd all mem
traffic engine-wide until each session re-entered. rebuild.py now reads the
keys from the outgoing DB before deleting it and restores them after the
content load; the backfill mints only shells that had no key.

These build a throwaway engine DB, exercise the real read_existing_keys /
restore_keys seam around a simulated rebuild (schema + migrations + NULL-keyed
content reload), and assert old keys survive verbatim while new shells still
get minted.

Run:
    python3 tests/test_rebuild_keys.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import rebuild  # noqa: E402
import backfill_shell_api_keys  # noqa: E402

KEY_1 = "tok_shell1_KEEP_ME_across_rebuild_0000000000"
KEY_2 = "tok_shell2_KEEP_ME_across_rebuild_1111111111"
ROTATED_AT = "2026-07-01 00:00:00"


def apply_engine_schema(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.commit()
    con.close()


def insert_shell(con, shell_id: int, shortname: str, api_key=None) -> None:
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
        "system_prompt, user_id, is_shared, has_identity, bootstrapped, "
        "api_key, api_key_rotated_at) "
        "VALUES (?, ?, ?, 'test', 'sp', 1, 0, 1, 1, ?, ?)",
        (shell_id, shortname.upper(), shortname, api_key,
         ROTATED_AT if api_key else None),
    )


class RebuildKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        self._orig_db_path = rebuild.DB_PATH
        rebuild.DB_PATH = self.db

    def tearDown(self):
        rebuild.DB_PATH = self._orig_db_path
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def build_keyed_db(self):
        apply_engine_schema(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        insert_shell(con, 1, "aa", KEY_1)
        insert_shell(con, 2, "bb", KEY_2)
        con.commit()
        con.close()

    def keys_by_shell(self) -> dict:
        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT shell_id, api_key, api_key_rotated_at FROM shells").fetchall()
        con.close()
        return {r[0]: (r[1], r[2]) for r in rows}

    def test_keys_survive_rebuild(self):
        """Old shells keep their exact keys; a new shell gets minted."""
        self.build_keyed_db()
        keys = rebuild.read_existing_keys()
        self.assertEqual(set(keys), {1, 2})

        # Simulate the rebuild: delete the DB, re-apply schema + migrations,
        # reload content — shells come back NULL-keyed (content.sql never
        # carries api_key), plus one shell that did not exist before.
        self.db.unlink()
        apply_engine_schema(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        insert_shell(con, 1, "aa")
        insert_shell(con, 2, "bb")
        insert_shell(con, 3, "cc")
        con.commit()
        con.close()

        restored = rebuild.restore_keys(keys)
        self.assertEqual(restored, 2)
        backfill_shell_api_keys.backfill(str(self.db))

        after = self.keys_by_shell()
        self.assertEqual(after[1], (KEY_1, ROTATED_AT))
        self.assertEqual(after[2], (KEY_2, ROTATED_AT))
        self.assertIsNotNone(after[3][0], "new shell not minted")
        self.assertNotIn(after[3][0], {KEY_1, KEY_2})

    def test_dropped_shell_key_is_dropped(self):
        """A shell absent from the new content does not resurrect its key."""
        self.build_keyed_db()
        keys = rebuild.read_existing_keys()

        self.db.unlink()
        apply_engine_schema(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        insert_shell(con, 1, "aa")  # shell 2 gone
        con.commit()
        con.close()

        self.assertEqual(rebuild.restore_keys(keys), 1)
        after = self.keys_by_shell()
        self.assertEqual(after[1], (KEY_1, ROTATED_AT))
        self.assertNotIn(2, after)

    def test_no_db_reads_empty(self):
        """First build: no outgoing DB, nothing to preserve."""
        self.assertEqual(rebuild.read_existing_keys(), {})
        self.assertEqual(rebuild.restore_keys({}), 0)

    def test_pre_key_db_reads_empty(self):
        """A pre-0027 DB (no api_key column) yields empty — mint-all path."""
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA.read_text())  # baseline only, no migrations
        con.commit()
        con.close()
        self.assertEqual(rebuild.read_existing_keys(), {})

    def test_restore_never_clobbers_loaded_key(self):
        """If a shell already has a key post-load, the carried key loses."""
        self.build_keyed_db()
        keys = rebuild.read_existing_keys()

        self.db.unlink()
        apply_engine_schema(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        insert_shell(con, 1, "aa", "tok_already_present_wins_2222222222222222")
        con.commit()
        con.close()

        self.assertEqual(rebuild.restore_keys(keys), 0)
        after = self.keys_by_shell()
        self.assertEqual(after[1][0], "tok_already_present_wins_2222222222222222")

    def test_missing_shells_table_reads_empty(self):
        """A DB with no shells table at all is the other genuinely-keyless
        shape — mint-all, not abort."""
        sqlite3.connect(self.db).close()   # zero-byte DB file, no tables
        self.assertEqual(rebuild.read_existing_keys(), {})

    def test_locked_db_aborts_instead_of_rotating(self):
        """#279: a keyed DB that can't be READ (e.g. `database is locked`
        after the busy timeout — a >5s exclusive lock from VACUUM/checkpoint
        racing `sc verify`) must ABORT the rebuild, not return {} and let the
        backfill silently rotate every key. Simulated at the driver seam; the
        real 5s-lock reproduction is documented on the issue."""
        self.build_keyed_db()
        with mock.patch.object(
                rebuild.db_driver, "connect",
                side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(SystemExit) as cm:
                rebuild.read_existing_keys()
        self.assertIn("aborting", str(cm.exception))
        # the outgoing DB is untouched — nothing was deleted or rotated
        self.assertEqual(self.keys_by_shell()[1], (KEY_1, ROTATED_AT))


if __name__ == "__main__":
    unittest.main()
