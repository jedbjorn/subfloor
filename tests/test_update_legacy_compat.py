#!/usr/bin/env python3
"""First-adoption bridge for updater behavior absent from an old fork."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import update  # noqa: E402
import update_compat  # noqa: E402
from skill_convergence_fixtures import (  # noqa: E402
    LOCAL_SKILL_DESCRIPTION,
    LOCAL_SKILL_NAME,
    TOMBSTONE_SKILLS,
    build_dirty_skill_fork,
)

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
    def test_installed_repo_reconciles_managed_host_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fork"
            engine = root / ".super-coder"
            engine.mkdir(parents=True)
            (engine / "instance.json").write_text('{"installed_at":"2026-08-09"}\n')
            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                ENGINE=engine,
            ), mock.patch.object(
                update_compat.sc_wrapper, "register_install", return_value="ready"
            ) as register, contextlib.redirect_stdout(io.StringIO()) as output:
                update_compat.reconcile_host_wrapper()

            register.assert_called_once_with(root)
            self.assertIn("managed host sc wrapper: ready", output.getvalue())

    def test_uninstalled_repo_does_not_claim_host_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fork"
            engine = root / ".super-coder"
            engine.mkdir(parents=True)
            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                ENGINE=engine,
            ), mock.patch.object(
                update_compat.sc_wrapper, "register_install"
            ) as register:
                update_compat.reconcile_host_wrapper()

            register.assert_not_called()

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
            skill_sweep_marker = state / "local" / "update-compat-skill-sweep.ref"
            engine_ref.write_text(new_ref + "\n")
            engine_ref_prev.write_text(old_ref + "\n")

            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                STATE_DIR=state,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=engine_ref_prev,
                MARKER=marker,
                SKILL_SWEEP_MARKER=skill_sweep_marker,
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ) as repair_dispatcher, mock.patch.object(
                update, "reconcile_linked_dispatchers"
            ) as reconcile_dispatchers, mock.patch.object(
                update, "reconcile_skill_projections", return_value={}
            ) as reconcile_skills, mock.patch.object(
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
            self.assertEqual(
                reconcile_dispatchers.call_args_list,
                [mock.call(new_ref), mock.call(new_ref)],
            )
            reconcile_skills.assert_called_once_with()
            self.assertEqual(new_ref + "\n", skill_sweep_marker.read_text())
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
            skill_sweep_marker = state / "local" / "update-compat-skill-sweep.ref"
            engine_ref.write_text(current_ref + "\n")
            engine_ref_prev.write_text(bridge_ref + "\n")

            with mock.patch.multiple(
                update_compat,
                REPO_ROOT=root,
                STATE_DIR=state,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=engine_ref_prev,
                MARKER=marker,
                SKILL_SWEEP_MARKER=skill_sweep_marker,
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ) as repair_dispatcher, mock.patch.object(
                update, "reconcile_linked_dispatchers"
            ) as reconcile_dispatchers, mock.patch.object(
                update, "reconcile_skill_projections", return_value={}
            ) as reconcile_skills, mock.patch.object(
                update, "repair_git_worktrees"
            ) as repair, mock.patch.object(
                update, "refresh_installed_brokers"
            ) as refresh:
                self.assertEqual(0, update_compat.main())

            repair_dispatcher.assert_called_once_with(current_ref)
            reconcile_dispatchers.assert_called_once_with(current_ref)
            reconcile_skills.assert_called_once_with()
            self.assertEqual(current_ref + "\n", skill_sweep_marker.read_text())
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
            shutil.copy2(SCRIPTS / "sc_wrapper.py", scripts / "sc_wrapper.py")
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
                "def reconcile_linked_dispatchers(ref): _record('worktrees')\n"
                "def reconcile_skill_projections(): _record('skills')\n"
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
            self.assertEqual(
                "dispatcher\nworktrees\nskills\nrepair\nbrokers\n", log.read_text()
            )
            self.assertEqual(
                current_ref + "\n",
                (state / "local" / "update-compat-skill-sweep.ref").read_text(),
            )
            self.assertEqual(
                current_ref + "\n",
                (state / "local" / "update-compat-v1.done").read_text(),
            )
            self.assertIn("legacy update bridge", completed.stdout)

    def test_pending_target_ref_drives_compat_before_pin_publication(self) -> None:
        pending = "b" * 40
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            engine_ref = state / "engine.ref"
            engine_ref.write_text("a" * 40 + "\n")
            skill_sweep_marker = state / "update-compat-skill-sweep.ref"
            skill_sweep_marker.write_text("a" * 40 + "\n")
            with mock.patch.multiple(
                update_compat,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=state / "engine.ref.prev",
                MARKER=state / "update-compat-v1.done",
                SKILL_SWEEP_MARKER=skill_sweep_marker,
            ), mock.patch.dict(
                os.environ, {"SC_UPDATE_TARGET_REF": pending}
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ) as repair_dispatcher, mock.patch.object(
                update, "reconcile_linked_dispatchers"
            ) as reconcile_dispatchers, mock.patch.object(
                update, "reconcile_skill_projections", return_value={}
            ) as reconcile_skills:
                self.assertEqual(0, update_compat.main())
                self.assertEqual(0, update_compat.main())
                skill_sweep_ref = skill_sweep_marker.read_text()

        self.assertEqual(
            repair_dispatcher.call_args_list, [mock.call(pending), mock.call(pending)]
        )
        reconcile_dispatchers.assert_not_called()
        reconcile_skills.assert_called_once_with()
        self.assertEqual(pending + "\n", skill_sweep_ref)

    def test_first_adoption_sweeps_main_and_dormant_skill_projections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = build_dirty_skill_fork(Path(td) / "downstream")
            with closing(sqlite3.connect(fixture.database)) as con:
                update.seed_skills.reconcile_tombstoned_skills(con)
                con.executemany(
                    "INSERT INTO documents "
                    "(feature_id,kind,seq,title,body,render_path) "
                    "VALUES (NULL,'doc',?,?,?,?)",
                    (
                        (9001, "Legacy owner", "owner", "docs_sc/shared.md"),
                        (9002, "Legacy duplicate", "duplicate", "docs_sc//shared.md"),
                    ),
                )
                con.commit()

            shell_authored = (
                fixture.root
                / ".sc-worktrees/dev9/.claude/skills/shell_notes/notes.txt"
            )
            shell_authored.parent.mkdir()
            shell_authored.write_text("shell-owned\n")
            real_projection = update.skill_projection.reconcile_existing_checkouts
            real_catalogue = update.flat.render_skills_catalogue

            def reconcile(con):
                return real_projection(con, repo_root=fixture.root)

            def render_catalogue(con):
                return real_catalogue(con, root=fixture.catalogue_root.parent)

            skill_sweep_marker = fixture.root / ".sc-state/local/skill-sweep.done"
            with mock.patch.multiple(
                update_compat,
                ENGINE_REF=fixture.root / ".sc-state/engine.ref",
                ENGINE_REF_PREV=fixture.root / ".sc-state/engine.ref.prev",
                MARKER=fixture.root / ".sc-state/local/update-compat-v1.done",
                SKILL_SWEEP_MARKER=skill_sweep_marker,
            ), mock.patch.object(
                update, "DB_PATH", fixture.database
            ), mock.patch.object(
                update.skill_projection,
                "reconcile_existing_checkouts",
                side_effect=reconcile,
            ) as projection, mock.patch.object(
                update.flat, "render_skills_catalogue", side_effect=render_catalogue
            ), mock.patch.object(
                update, "repair_callable_dispatcher"
            ), mock.patch.object(
                update, "reconcile_linked_dispatchers"
            ), mock.patch.object(
                update_compat, "needs_legacy_bridge", return_value=(False, None)
            ):
                self.assertEqual(0, update_compat.main())
                first_projection = {
                    str(path.relative_to(fixture.root)): path.read_bytes()
                    for root in fixture.native_skill_roots
                    for path in root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(0, update_compat.main())

            self.assertEqual(projection.call_count, 1)
            self.assertEqual(
                skill_sweep_marker.read_text(),
                (fixture.root / ".sc-state/engine.ref").read_text(),
            )
            self.assertEqual(shell_authored.read_text(), "shell-owned\n")
            self.assertFalse(
                fixture.catalogue_root.parent.joinpath("docs_sc/shared.md").exists()
            )
            self.assertEqual(
                {
                    str(path.relative_to(fixture.root)): path.read_bytes()
                    for root in fixture.native_skill_roots
                    for path in root.rglob("*")
                    if path.is_file()
                },
                first_projection,
            )
            expected_local = (
                (
                    f"---\nname: {LOCAL_SKILL_NAME}\n"
                    f"description: {LOCAL_SKILL_DESCRIPTION}\n---\n\n"
                ).encode()
                + fixture.expected_local_content
            )
            for skills_root in fixture.native_skill_roots:
                self.assertEqual(
                    skills_root.joinpath(LOCAL_SKILL_NAME, "SKILL.md").read_bytes(),
                    expected_local,
                )
                for retired in TOMBSTONE_SKILLS:
                    self.assertFalse(skills_root.joinpath(retired).exists())
            for control in fixture.control_files:
                self.assertEqual(control.read_bytes(), fixture.expected_control_file)


if __name__ == "__main__":
    unittest.main(verbosity=2)
