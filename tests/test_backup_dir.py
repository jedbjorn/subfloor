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

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import db_backup  # noqa: E402
import rebuild  # noqa: E402
import rollback  # noqa: E402
import update  # noqa: E402


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

    def test_explicit_writable_override_wins_without_touching_fallbacks(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            override = base / "explicit"
            selected = db_backup.select_backup_dir(
                base / "repo",
                {"HOME": str(base / "home"),
                 "SC_DB_BACKUP_DIR": str(override)},
            )
            self.assertEqual(selected, override)
            self.assertTrue(override.is_dir())
            self.assertFalse((base / "home" / "db_backups").exists())

    def test_unwritable_override_and_home_fall_through_to_repo_local(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            override = Path(td) / "override"
            home = Path(td) / "home" / "db_backups" / repo.name
            local = repo / ".sc-state" / "db_backups"
            probed: list[Path] = []

            def probe(path: Path) -> None:
                probed.append(path)
                if path != local:
                    raise PermissionError("read-only fixture")
                path.mkdir(parents=True)

            with mock.patch.object(db_backup, "_probe_writable",
                                   side_effect=probe):
                selected = db_backup.select_backup_dir(
                    repo,
                    {"HOME": str(Path(td) / "home"),
                     "SC_DB_BACKUP_DIR": str(override)},
                )
            self.assertEqual(selected, local)
            self.assertEqual(probed, [override, home, local])

    def test_no_writable_destination_names_the_supported_override(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(
                 db_backup, "_probe_writable",
                 side_effect=PermissionError("read-only fixture")
             ):
            with self.assertRaises(db_backup.BackupDestinationError) as caught:
                db_backup.select_backup_dir(
                    Path(td) / "repo", {"HOME": str(Path(td) / "home")}
                )
            self.assertIn("Set SC_DB_BACKUP_DIR", str(caught.exception))

    def test_restore_discovery_keeps_prior_home_backup_visible_after_override(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            home_backup = base / "home" / "db_backups" / repo.name
            override = base / "new-override"
            home_backup.mkdir(parents=True)
            override.mkdir()
            expected = home_backup / "shell_db.prerebuild.20300101_000000.db"
            expected.touch()

            found = db_backup.latest_backup(
                repo,
                "shell_db.prerebuild.*.db",
                {"HOME": str(base / "home"),
                 "SC_DB_BACKUP_DIR": str(override)},
            )

            self.assertEqual(found, expected)

    def test_rollback_prefers_update_pair_over_newer_verify_backup(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            backups = base / "home" / "db_backups" / repo.name
            backups.mkdir(parents=True)
            paired = backups / "shell_db.preupdate.20260801_230000.db"
            verify = backups / "shell_db.prerebuild.20260801_233000.db"
            paired.touch()
            verify.touch()
            os.utime(paired, ns=(1, 1))
            os.utime(verify, ns=(2, 2))
            with mock.patch.object(rollback, "REPO_ROOT", repo), mock.patch.dict(
                os.environ,
                {"HOME": str(base / "home"), "SC_DB_BACKUP_DIR": ""},
            ):
                found = rollback.latest_db_restore_point()
            self.assertEqual(found, paired)

    def test_rollback_uses_legacy_prerebuild_when_no_update_pair_exists(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            backups = base / "home" / "db_backups" / repo.name
            backups.mkdir(parents=True)
            legacy = backups / "shell_db.prerebuild.20260731_120000.db"
            legacy.touch()
            with mock.patch.object(rollback, "REPO_ROOT", repo), mock.patch.dict(
                os.environ,
                {"HOME": str(base / "home"), "SC_DB_BACKUP_DIR": ""},
            ):
                found = rollback.latest_db_restore_point()
            self.assertEqual(found, legacy)

    def test_update_backup_is_classed_as_engine_paired_preupdate(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "shell_db.db"
            db.write_bytes(b"live")
            with mock.patch.object(update, "DB_PATH", db), mock.patch.object(
                update.rebuild_mod, "backup_existing"
            ) as backup, mock.patch.object(update.migrate_mod, "migrate"):
                update.migrate_or_rebuild()
            backup.assert_called_once_with(prefix="preupdate")

    def test_first_new_verify_promotes_pre_prefix_update_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            state = root / ".sc-state"
            backups = state / "db_backups"
            backups.mkdir(parents=True)
            state.joinpath("engine.ref.prev").write_text("old-engine\n")
            live = root / "shell_db.db"
            with sqlite3.connect(live) as con:
                con.execute("CREATE TABLE marker (value TEXT)")
                con.execute("INSERT INTO marker VALUES ('current-before-verify')")
            legacy = backups / "shell_db.prerebuild.20260801_230000.db"
            with sqlite3.connect(legacy) as con:
                con.execute("CREATE TABLE marker (value TEXT)")
                con.execute("INSERT INTO marker VALUES ('pre-update-pair')")

            with mock.patch.multiple(
                rebuild, REPO_ROOT=root, DB_PATH=live
            ), mock.patch.object(
                rebuild, "backup_dir", return_value=backups
            ), mock.patch.dict(
                os.environ,
                {"HOME": str(Path(td) / "unwritable-home"), "SC_DB_BACKUP_DIR": ""},
            ):
                rebuild.backup_existing()

            promoted = backups / "shell_db.preupdate.20260801_230000.db"
            self.assertTrue(promoted.is_file())
            with sqlite3.connect(promoted) as con:
                self.assertEqual(
                    con.execute("SELECT value FROM marker").fetchone()[0],
                    "pre-update-pair",
                )
            latest_verify = next(
                path
                for path in backups.glob("shell_db.prerebuild.*.db")
                if path != legacy
            )
            with sqlite3.connect(latest_verify) as con:
                self.assertEqual(
                    con.execute("SELECT value FROM marker").fetchone()[0],
                    "current-before-verify",
                )


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

    def test_shared_backup_keeps_five_and_preserves_the_live_row(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            live = base / "live.db"
            out = base / "backups"
            out.mkdir()
            with sqlite3.connect(live) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("CREATE TABLE t (x)")
                con.execute("INSERT INTO t VALUES (42)")
            for i in range(5):
                (out / f"shell_db.prerestart.20000101_00000{i}.db").touch()

            written = db_backup.backup_database(
                live, out, "prerestart", keep=5
            )

            self.assertIsNotNone(written)
            backups = sorted(out.glob("shell_db.prerestart.*.db"))
            self.assertEqual(len(backups), 5)
            self.assertNotIn(out / "shell_db.prerestart.20000101_000000.db",
                             backups)
            with sqlite3.connect(written) as con:
                self.assertEqual(
                    con.execute("SELECT x FROM t").fetchall(),
                    [(42,)],
                )


if __name__ == "__main__":
    unittest.main()
