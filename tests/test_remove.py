"""Safety and end-to-end coverage for ``./sc remove``."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import install
import remove as remove_mod


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


class RemoveFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "host-project"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Remove Test")
        git(self.repo, "config", "user.email", "remove@noreply.local")

        self.engine = self.repo / ".super-coder"
        (self.engine / "hooks").mkdir(parents=True)
        (self.engine / "scripts").mkdir()
        (self.engine / "engine.manifest").write_text("{}\n")
        (self.engine / "instance.json").write_text('{"installed_at":"2026-07-31"}\n')
        (self.repo / "sc").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "README.md").write_text("# keep me\n")
        (self.repo / "CLAUDE.md").write_text("generated\n")
        (self.repo / "docs_sc").mkdir()
        (self.repo / "docs_sc" / "generated.md").write_text("generated\n")
        (self.repo / "shared").mkdir()
        (self.repo / "shared" / "user-notes.txt").write_text("preserve\n")
        workflow = self.repo / ".github/workflows/subfloor-visual-qa.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "# managed-by: subfloor — visual-qa shim v3\nname: visual qa\n"
        )

        state = self.repo / ".sc-state"
        state.mkdir()
        (state / "engine.ref").write_text("a" * 40 + "\n")
        (state / "content.sql").write_text("generated\n")
        (self.repo / ".gitignore").write_text("*.keep\n" + install._GITIGNORE_BLOCK)
        (self.repo / "Makefile").write_text(
            "test:\n\t@echo host\n" + install.APPENDED_ALIASES_BLOCK
        )
        git(self.repo, "config", "core.hooksPath", str(self.engine / "hooks"))
        git(
            self.repo,
            "remote",
            "add",
            "super-coder",
            "https://github.com/jedbjorn/subfloor.git",
        )
        git(
            self.repo,
            "remote",
            "add",
            "mirror",
            "https://example.com/acme/project.git",
        )

        self.db = self.engine / "shell_db.db"
        self.writer = sqlite3.connect(self.db)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("CREATE TABLE kept (value INTEGER)")
        self.writer.execute("INSERT INTO kept VALUES (42)")
        self.writer.commit()

        self.patchers = [
            mock.patch.object(remove_mod, "REPO_ROOT", self.repo),
            mock.patch.object(remove_mod, "ENGINE", self.engine),
            mock.patch.object(remove_mod, "STATE_DIR", state),
            mock.patch.object(remove_mod, "DB_PATH", self.db),
            mock.patch.object(
                remove_mod,
                "BACKUP_ROOT",
                state / "db_backups" / "removal",
            ),
            mock.patch.object(remove_mod, "validate_target", return_value=self.repo),
            mock.patch.object(remove_mod, "managed_worktrees", return_value=[]),
            mock.patch.object(remove_mod, "engine_drift", return_value=({}, [])),
            mock.patch.object(remove_mod, "quiesce_runtime"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.writer.close()
        self.tmp.cleanup()

    def removal_dir(self) -> Path:
        roots = list((self.repo / ".sc-state/db_backups/removal").iterdir())
        self.assertEqual(len(roots), 1)
        return roots[0]


class EndToEndRemoveTest(RemoveFixture):
    def test_verified_wal_backup_and_repo_cleanup(self) -> None:
        self.assertEqual(remove_mod.main(["--yes"]), 0)

        destination = self.removal_dir()
        backup = next(destination.glob("shell_db.removal.*.db"))
        con = sqlite3.connect(backup)
        try:
            self.assertEqual(con.execute("SELECT value FROM kept").fetchall(), [(42,)])
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            con.close()

        manifest = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "removed")
        self.assertEqual(manifest["database"]["integrity_check"], "ok")
        self.assertEqual(len(manifest["database"]["sha256"]), 64)
        self.assertEqual(manifest["engine_ref"], "a" * 40)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((destination / "manifest.json").stat().st_mode), 0o600
        )

        self.assertFalse(self.engine.exists())
        self.assertFalse((self.repo / "sc").exists())
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        self.assertFalse((self.repo / "docs_sc").exists())
        self.assertFalse(
            (self.repo / ".github/workflows/subfloor-visual-qa.yml").exists()
        )
        self.assertEqual((self.repo / "README.md").read_text(), "# keep me\n")
        self.assertEqual(
            (self.repo / "shared/user-notes.txt").read_text(), "preserve\n"
        )

        makefile = (self.repo / "Makefile").read_text()
        self.assertIn("echo host", makefile)
        self.assertNotIn("aliases.mk", makefile)
        gitignore = (self.repo / ".gitignore").read_text()
        self.assertIn("*.keep", gitignore)
        self.assertIn(remove_mod.BACKUP_IGNORE, gitignore)
        self.assertNotIn("/.super-coder/", gitignore)

        hooks = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(hooks.returncode, 0)
        self.assertNotIn("super-coder", git(self.repo, "remote").stdout.splitlines())
        self.assertIn("mirror", git(self.repo, "remote").stdout.splitlines())

    def test_no_database_is_reported_truthfully(self) -> None:
        self.writer.close()
        self.writer = sqlite3.connect(":memory:")
        for suffix in ("", "-wal", "-shm"):
            (Path(str(self.db) + suffix)).unlink(missing_ok=True)

        self.assertEqual(remove_mod.main(["--yes"]), 0)
        manifest = json.loads((self.removal_dir() / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "removed")
        self.assertIsNone(manifest["database"])

    def test_dry_run_changes_nothing(self) -> None:
        self.assertEqual(remove_mod.main(["--dry-run"]), 0)
        self.assertTrue(self.engine.exists())
        self.assertTrue((self.repo / "sc").exists())
        self.assertFalse((self.repo / ".sc-state/db_backups/removal").exists())
        remove_mod.quiesce_runtime.assert_not_called()


class RemoveSafetyGateTest(RemoveFixture):
    def test_dirty_worktree_refuses_before_quiesce(self) -> None:
        worktree = self.repo / ".sc-worktrees/dev1"
        worktree.mkdir(parents=True)
        with (
            mock.patch.object(remove_mod, "managed_worktrees", return_value=[worktree]),
            mock.patch.object(remove_mod, "dirty_worktrees", return_value=[worktree]),
        ):
            self.assertEqual(remove_mod.main(["--yes"]), 1)
        self.assertTrue(self.engine.exists())
        remove_mod.quiesce_runtime.assert_not_called()

    def test_unwritable_backup_refuses_before_quiesce(self) -> None:
        with mock.patch.object(
            remove_mod,
            "new_backup_dir",
            side_effect=PermissionError("read-only fixture"),
        ):
            self.assertEqual(remove_mod.main(["--yes"]), 1)
        self.assertTrue(self.engine.exists())
        remove_mod.quiesce_runtime.assert_not_called()

    def test_runtime_shutdown_failure_keeps_live_installation(self) -> None:
        remove_mod.quiesce_runtime.side_effect = remove_mod.RemoveError(
            "runtime fixture stayed live"
        )
        self.assertEqual(remove_mod.main(["--yes"]), 1)
        self.assertTrue(self.engine.exists())
        self.assertTrue(self.db.exists())
        self.assertTrue((self.repo / "sc").exists())

    def test_backup_failure_after_shutdown_keeps_live_installation(self) -> None:
        with mock.patch.object(
            remove_mod,
            "backup_database",
            side_effect=sqlite3.OperationalError("backup fixture failed"),
        ):
            self.assertEqual(remove_mod.main(["--yes"]), 1)
        remove_mod.quiesce_runtime.assert_called_once_with(self.repo)
        self.assertTrue(self.engine.exists())
        self.assertTrue(self.db.exists())
        self.assertTrue((self.repo / "sc").exists())

    def test_cleanup_failure_marks_verified_backup_partial(self) -> None:
        with mock.patch.object(
            remove_mod,
            "remove_installation",
            side_effect=OSError("cleanup fixture failed"),
        ):
            self.assertEqual(remove_mod.main(["--yes"]), 1)
        manifest = json.loads((self.removal_dir() / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "partial")
        self.assertIn("cleanup fixture failed", manifest["errors"])
        self.assertTrue(self.engine.exists())

    def test_symlink_is_unlinked_without_following_target(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe\n")
        link = self.repo / "linked-generated"
        link.symlink_to(outside, target_is_directory=True)

        self.assertTrue(remove_mod._remove_path(link, self.repo))
        self.assertFalse(link.exists())
        self.assertEqual((outside / "keep").read_text(), "safe\n")


class TargetValidationTest(unittest.TestCase):
    def test_source_repo_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "source"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(
                repo,
                "remote",
                "add",
                "origin",
                "https://github.com/jedbjorn/subfloor.git",
            )
            with self.assertRaises(remove_mod.RemoveError):
                remove_mod.validate_target(repo)

    def test_linked_worktree_marker_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "linked"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: elsewhere\n")
            with self.assertRaises(remove_mod.RemoveError):
                remove_mod.validate_target(repo)


class RuntimeQuiescenceTest(unittest.TestCase):
    def test_foreground_listener_blocks_removal_after_down(self) -> None:
        repo = Path("/tmp/remove-runtime-fixture")
        results = [
            subprocess.CompletedProcess(["sc", "down"], 0, "stopped\n", ""),
            subprocess.CompletedProcess(["ports.py", "port"], 0, "8837\n", ""),
        ]
        connection = mock.Mock()
        with (
            mock.patch.object(remove_mod, "stop_running_jobs"),
            mock.patch.object(remove_mod, "_run", side_effect=results),
            mock.patch.object(remove_mod.shutil, "which", return_value=None),
            mock.patch.object(
                remove_mod.socket, "create_connection", return_value=connection
            ),
            self.assertRaises(remove_mod.RemoveError),
        ):
            remove_mod.quiesce_runtime(repo)
        connection.close.assert_called_once_with()

    def test_no_docker_host_without_pg_can_prove_quiescence_by_listener(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            engine = repo / ".super-coder"
            engine.mkdir()
            (engine / "instance.json").write_text("{}\n")
            results = [
                subprocess.CompletedProcess(["sc", "down"], 1, "", "no docker"),
                subprocess.CompletedProcess(["ports.py", "port"], 0, "8837\n", ""),
            ]
            with (
                mock.patch.object(remove_mod, "stop_running_jobs"),
                mock.patch.object(remove_mod, "_run", side_effect=results),
                mock.patch.object(remove_mod.shutil, "which", return_value=None),
                mock.patch.object(
                    remove_mod.socket,
                    "create_connection",
                    side_effect=ConnectionRefusedError,
                ),
            ):
                remove_mod.quiesce_runtime(repo)

    def test_no_docker_host_with_pg_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            engine = repo / ".super-coder"
            engine.mkdir()
            (engine / "instance.json").write_text('{"pg": {}}\n')
            result = subprocess.CompletedProcess(["sc", "down"], 1, "", "no docker")
            with (
                mock.patch.object(remove_mod, "stop_running_jobs"),
                mock.patch.object(remove_mod, "_run", return_value=result),
                mock.patch.object(remove_mod.shutil, "which", return_value=None),
                self.assertRaises(remove_mod.RemoveError),
            ):
                remove_mod.quiesce_runtime(repo)


class WiringTest(unittest.TestCase):
    def test_dispatcher_and_make_alias_are_public(self) -> None:
        dispatcher = (ROOT / "sc").read_text()
        aliases = (ROOT / ".super-coder/aliases.mk").read_text()
        self.assertIn('remove)       if sc_help_form "$@"; then', dispatcher)
        self.assertIn('exec "$PY" "$S/remove.py" "$@"', dispatcher)
        self.assertIn("dos-remove:           ; $(SC) remove $(ARGS)", aliases)
        self.assertIn("dos-remove", aliases)


if __name__ == "__main__":
    unittest.main(verbosity=2)
