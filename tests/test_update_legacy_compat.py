#!/usr/bin/env python3
"""First-adoption bridge for updater behavior absent from an old fork."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import update  # noqa: E402
import update_compat  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Update Test",
    "GIT_AUTHOR_EMAIL": "update@example.invalid",
    "GIT_COMMITTER_NAME": "Update Test",
    "GIT_COMMITTER_EMAIL": "update@example.invalid",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


class LegacyUpdateCompatTest(unittest.TestCase):
    def make_ref_boundary(self, root: Path) -> tuple[str, str]:
        scripts = root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "update.py").write_text("# legacy updater\n")
        old_ref = commit(root, "legacy floor")
        (scripts / "update_compat.py").write_text("# bridge introduced\n")
        new_ref = commit(root, "bridge floor")
        return old_ref, new_ref

    def test_runs_once_when_previous_engine_lacked_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fork"
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            old_ref, new_ref = self.make_ref_boundary(root)
            state = root / ".sc-state"
            state.mkdir()
            engine_ref = state / "engine.ref"
            engine_ref_prev = state / "engine.ref.prev"
            marker = state / "local" / "update-compat-v1.done"
            engine_ref.write_text(new_ref + "\n")
            engine_ref_prev.write_text(old_ref + "\n")

            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                STATE_DIR=state,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=engine_ref_prev,
                MARKER=marker,
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ) as repair_dispatcher, mock.patch.object(
                update, "repair_git_worktrees"
            ) as repair, mock.patch.object(
                update, "refresh_installed_brokers"
            ) as refresh, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, update_compat.main())
                self.assertEqual(0, update_compat.main())

            self.assertEqual(
                repair_dispatcher.call_args_list,
                [mock.call(new_ref), mock.call(new_ref)],
            )
            repair.assert_called_once_with()
            refresh.assert_called_once_with()
            self.assertEqual(new_ref + "\n", marker.read_text())

    def test_new_updater_runs_bridge_before_repo_inspection(self) -> None:
        class Stop(Exception):
            pass

        with mock.patch.object(
            update, "run_update_compat", side_effect=Stop
        ) as bridge, mock.patch.object(
            update,
            "is_source_repo",
            side_effect=AssertionError("repo inspection preceded bridge"),
        ), self.assertRaises(Stop):
            update.main(["--no-fetch"])

        bridge.assert_called_once_with()

    def test_skips_when_previous_engine_already_had_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fork"
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            _, bridge_ref = self.make_ref_boundary(root)
            (root / "current.txt").write_text("newer floor\n")
            current_ref = commit(root, "post-bridge floor")
            state = root / ".sc-state"
            state.mkdir()
            engine_ref = state / "engine.ref"
            engine_ref_prev = state / "engine.ref.prev"
            marker = state / "local" / "update-compat-v1.done"
            engine_ref.write_text(current_ref + "\n")
            engine_ref_prev.write_text(bridge_ref + "\n")

            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                STATE_DIR=state,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=engine_ref_prev,
                MARKER=marker,
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ) as repair_dispatcher, mock.patch.object(
                update, "repair_git_worktrees"
            ) as repair, mock.patch.object(
                update, "refresh_installed_brokers"
            ) as refresh:
                self.assertEqual(0, update_compat.main())

            repair_dispatcher.assert_called_once_with(current_ref)
            repair.assert_not_called()
            refresh.assert_not_called()
            self.assertFalse(marker.exists())

    def test_materialized_map_setup_runs_bridge_in_fresh_process(self) -> None:
        """Model the dynamic seam used by the legacy updater after materialize."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fork"
            scripts = root / ".super-coder" / "scripts"
            scripts.mkdir(parents=True)
            git(root, "init", "-q", "-b", "main")
            (scripts / "update.py").write_text("# legacy updater\n")
            old_ref = commit(root, "legacy floor")

            shutil.copy2(SCRIPTS / "map_setup.py", scripts / "map_setup.py")
            shutil.copy2(
                SCRIPTS / "update_compat.py", scripts / "update_compat.py"
            )
            (scripts / "update.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "def _record(value):\n"
                "    path = Path(os.environ['SC_COMPAT_TEST_LOG'])\n"
                "    old = path.read_text() if path.exists() else ''\n"
                "    path.write_text(old + value + '\\n')\n"
                "def repair_git_worktrees(): _record('repair')\n"
                "def refresh_installed_brokers(): _record('brokers')\n"
                "def repair_callable_dispatcher(ref): _record('dispatcher')\n"
            )
            (scripts / "map_repo.py").write_text(
                "def main(): return 0\n"
            )
            (scripts / "cli_entry.py").write_text(
                "def run_cli(func, *args): return func(*args)\n"
            )
            current_ref = commit(root, "materialized bridge floor")

            state = root / ".sc-state"
            state.mkdir()
            (state / "engine.ref").write_text(current_ref + "\n")
            (state / "engine.ref.prev").write_text(old_ref + "\n")
            log = root / "compat.log"
            env = {**os.environ, "SC_COMPAT_TEST_LOG": str(log)}

            completed = subprocess.run(
                [sys.executable, str(scripts / "map_setup.py")],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("dispatcher\nrepair\nbrokers\n", log.read_text())
            self.assertEqual(
                current_ref + "\n",
                (state / "local" / "update-compat-v1.done").read_text(),
            )
            self.assertIn("legacy update bridge", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
