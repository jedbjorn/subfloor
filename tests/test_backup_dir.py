#!/usr/bin/env python3
"""Pin the per-fork backup dir contract (rebuild.py / rollback.py).

The hazard this guards: BACKUP_DIR was a fixed ~/db_backups/super-coder for
EVERY fork, pooling all forks' pre-update dumps — and rollback restores the
most recent dump, so a multi-fork update sweep could roll one fork back onto
another fork's DB. The contract now: the dir is keyed by the host repo's dir
name, and rollback shares rebuild's object rather than keeping a private copy
(a private copy of the path is exactly how the pooling happened).

Also pins the WAL-safety contract: backups go through sqlite3's online-backup
API, never a plain file copy — the DB runs journal_mode=WAL, so a copy2 of the
main file silently drops every un-checkpointed page (they live in the -wal
sidecar), i.e. the most recent writes are exactly what the restore point loses.

Run:
    python3 tests/test_backup_dir.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import rebuild  # noqa: E402
import rollback  # noqa: E402


class BackupDirTest(unittest.TestCase):
    def test_backup_dir_is_keyed_by_repo_dir_name(self):
        self.assertEqual(rebuild.BACKUP_DIR.name, ROOT.name,
                         "backups must be per-fork — a fixed name pools every "
                         "fork's dumps into one dir")
        self.assertEqual(rebuild.BACKUP_DIR.parent.name, "db_backups")

    def test_rollback_shares_rebuilds_dir(self):
        self.assertIs(rollback.BACKUP_DIR, rebuild.BACKUP_DIR,
                      "rollback must restore from the SAME per-fork dir rebuild "
                      "writes to — a private copy re-creates the pooling hazard")

    def test_no_hardcoded_fork_name_remains(self):
        for script in ("rebuild.py", "rollback.py"):
            src = (ROOT / ".super-coder" / "scripts" / script).read_text()
            self.assertNotIn('"db_backups" / "super-coder"', src,
                             f"{script}: the fixed-name path is the bug")


class WalSafeBackupTest(unittest.TestCase):
    def test_backup_captures_uncheckpointed_wal_pages(self):
        """A row committed to the -wal sidecar (autocheckpoint off, writer
        still connected) must appear in the backup — copy2 would miss it."""
        with tempfile.TemporaryDirectory() as td:
            live = Path(td) / "live.db"
            con = sqlite3.connect(live)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA wal_autocheckpoint=0")
                con.execute("CREATE TABLE t (x)")
                con.execute("INSERT INTO t VALUES (42)")
                con.commit()
                dst = Path(td) / "backup.db"
                rebuild.backup_db(dst, src=live)
                got = sqlite3.connect(dst).execute("SELECT x FROM t").fetchall()
                self.assertEqual(got, [(42,)],
                                 "backup missed WAL-resident writes — is it "
                                 "copying the file instead of using the "
                                 "online-backup API?")
            finally:
                con.close()

    def test_no_plain_file_copy_of_the_live_db_remains(self):
        for script in ("rebuild.py", "rollback.py"):
            src = (ROOT / ".super-coder" / "scripts" / script).read_text()
            self.assertNotIn("shutil.copy2(DB_PATH", src,
                             f"{script}: backing up the live WAL DB with a "
                             "file copy drops un-checkpointed writes")


if __name__ == "__main__":
    unittest.main()
