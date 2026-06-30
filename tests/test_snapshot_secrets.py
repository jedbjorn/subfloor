#!/usr/bin/env python3
"""Tests that `./sc snapshot` never serializes live credentials to content.sql.

content.sql is git-tracked. The `shells` table carries each shell's `api_key`
(its bearer token, provisioned by the server's startup backfill) and the `users`
table carries `password_*` auth fields. A snapshot taken while keys are
provisioned must NOT write those into the committed file — and the bare
`token_urlsafe` format is not caught by the gitleaks default ruleset, so the
serializer is the only line of defense. These build a throwaway engine DB with a
keyed shell + a user with password fields, run the real `snapshot.dump_table`,
and assert the secret values (and their column names) never appear in the dump.

Run:
    python3 tests/test_snapshot_secrets.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import snapshot  # noqa: E402

API_KEY = "tok_live_deadbeefcafe0123456789ABCDEFghijklmno"   # 43-char-ish secret
PW_HASH = "pwhash_SECRET_must_not_ship_to_git_000000000"
PW_SALT = "pwsalt_SECRET_must_not_ship_to_git_111111111"


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active, password_hash, password_salt) "
        "VALUES (1, 'T', 1, ?, ?)", (PW_HASH, PW_SALT))
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (1, 'TC', 'tc', 'test', 'sp', 1, 0, 1, 1, ?)", (API_KEY,))
    con.commit()
    con.close()


class SnapshotSecretsTest(unittest.TestCase):
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

    def _dump(self, table: str) -> str:
        return "\n".join(snapshot.dump_table(self.con, table))

    def test_shells_dump_omits_api_key(self):
        out = self._dump("shells")
        self.assertNotIn(API_KEY, out, "api_key token serialized into content.sql")
        self.assertNotIn("api_key", out, "api_key column present in the shells INSERT")
        # sanity: the shell row IS dumped (non-secret columns survive)
        self.assertIn("INSERT INTO shells", out)
        self.assertIn("'tc'", out)

    def test_users_dump_omits_passwords(self):
        out = self._dump("users")
        self.assertNotIn(PW_HASH, out)
        self.assertNotIn(PW_SALT, out)
        self.assertNotIn("password_hash", out)
        self.assertNotIn("password_salt", out)
        self.assertIn("INSERT INTO users", out)

    def test_sensitive_set_matches_live_schema(self):
        # Guard against a future column rename leaving a secret unguarded: every
        # name in SENSITIVE_COLUMNS must still exist on its table.
        for table, cols in snapshot.SENSITIVE_COLUMNS.items():
            live = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
            missing = cols - live
            self.assertFalse(missing, f"{table}: SENSITIVE_COLUMNS names not in schema: {missing}")


if __name__ == "__main__":
    unittest.main()
