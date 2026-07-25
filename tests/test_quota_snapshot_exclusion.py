#!/usr/bin/env python3
"""Asserts the quota tables never reach a freshly rendered content.sql.

content.sql is git-tracked. `harness_quota_account.account_label` holds the
operator's FULL email address — decision #67 withdrew the earlier redaction
because the address never reaches the repository, which makes snapshot
exclusion the ONLY thing keeping that true. So the guarantee is asserted here
rather than assumed from `PER_INSTANCE_TABLES`.

The distinction matters. A test that asserts `"harness_quota_account" not in
snapshot.PER_INSTANCE_TABLES` is just the allowlist read twice: it stays green
if the renderer grows a second source of tables, dumps a table the allowlist
never named, or is refactored to decide inclusion some other way. This builds a
throwaway engine DB with real rows in both quota tables, runs the REAL
`snapshot.main()` against it, and greps the file that comes out. Whatever path
put a table into that file, the assertion sees it.

The paths `main()` writes are redirected into a temp dir (the live DB, the
output content.sql, the legacy-copy cleanup, and the map serializer, which
otherwise writes the repo's own map_content.sql). The render loop itself —
`PER_INSTANCE_TABLES`, `dump_table`, `SENSITIVE_COLUMNS`, the row filters — is
untouched and real.

Run:
    python3 tests/test_quota_snapshot_exclusion.py
"""
from __future__ import annotations

import os
import shutil
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
import snapshot  # noqa: E402

QUOTA_TABLES = ("harness_quota_account", "harness_quota_window")

# Full address on purpose: this is the exact value decision #67 accepted into
# the DB, so it is the exact value that must not appear in a tracked file.
SENTINEL_EMAIL = "operator.sentinel@must-never-reach-git.test"
SENTINEL_REF = "acct_sentinel_0123456789"
SENTINEL_PLAN = "sentinel_plan"


def build_engine_db(path: Path) -> None:
    """Schema + every migration, then rows in the quota tables and one row in a
    table that IS serialized (the positive control)."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
        "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
        "VALUES (1, 'TC', 'tc', 'test', 'sp', 1, 0, 1, 1)")
    con.execute(
        "INSERT INTO harness_quota_account (account_pk, provider, account_ref, "
        "account_label, plan, first_seen, last_seen, is_current) "
        "VALUES (1, 'openai', ?, ?, ?, '2026-07-25T00:00:00Z', "
        "'2026-07-25T00:00:00Z', 1)",
        (SENTINEL_REF, SENTINEL_EMAIL, SENTINEL_PLAN))
    con.execute(
        "INSERT INTO harness_quota_window (account_pk, window_kind, scope, "
        "used_percent, captured_at, status, probe_version) "
        "VALUES (1, 'weekly', NULL, 42.0, '2026-07-25T00:00:00Z', 'ok', '1')")
    con.commit()
    con.close()


class QuotaSnapshotExclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        self.out = self.tmp / "state" / "content.sql"
        build_engine_db(self.db)
        self.rendered = self._render()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self) -> str:
        """Run the real snapshot.main() with its write targets redirected."""
        with mock.patch.multiple(
            snapshot,
            DB_PATH=self.db,
            OUT_PATH=self.out,
            LEGACY_PATH=self.tmp / "legacy" / "content.sql",
            REPO_ROOT=self.tmp,          # main() prints OUT_PATH.relative_to(REPO_ROOT)
            snapshot_map=lambda: None,   # else it serializes the REPO's map db
        ), mock.patch.object(
            snapshot.artifact_policy, "prepare_local_state", lambda: []
        ), mock.patch.dict(os.environ, {"SC_ADMIN": "1"}):
            self.assertEqual(snapshot.main(), 0)
        return self.out.read_text()

    def test_render_is_real(self):
        """Guard the guard: the assertions below are only worth anything if the
        renderer actually produced a populated content.sql. A silently empty or
        failed render would make every `assertNotIn` pass for free."""
        self.assertIn("INSERT INTO shells", self.rendered)
        self.assertIn("'tc'", self.rendered)

    def test_quota_tables_absent_from_content_sql(self):
        for table in QUOTA_TABLES:
            self.assertNotIn(
                table, self.rendered,
                f"{table} was serialized into content.sql — it holds operator "
                "account state and content.sql is git-tracked")

    def test_operator_email_absent_from_content_sql(self):
        """The value, not just the table name. Catches the table reaching the
        file under an alias, a join, or a future per-column dumper."""
        self.assertNotIn(SENTINEL_EMAIL, self.rendered)
        self.assertNotIn(SENTINEL_EMAIL.split("@", 1)[0], self.rendered)
        self.assertNotIn(SENTINEL_REF, self.rendered)
        self.assertNotIn(SENTINEL_PLAN, self.rendered)

    def test_rows_are_present_in_the_db_that_was_rendered(self):
        """The exclusion must be the renderer's doing, not an empty table. If a
        future migration edit dropped these rows on the floor, the assertions
        above would pass while proving nothing."""
        con = sqlite3.connect(self.db)
        try:
            for table in QUOTA_TABLES:
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 1, f"{table} had no row to exclude")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
