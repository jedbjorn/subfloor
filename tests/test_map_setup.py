#!/usr/bin/env python3
"""Regression coverage for home-owned map hook wiring."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import map_setup  # noqa: E402


class HomeHookWiringTest(unittest.TestCase):
    def test_external_project_never_owns_the_hooks_path(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            hooks = home / ".super-coder" / "hooks"
            hooks.mkdir(parents=True)
            (hooks / "post-checkout").write_text("#!/bin/sh\n")

            with mock.patch.object(map_setup, "HOME_ROOT", home), \
                    mock.patch.object(map_setup, "HOOKS_DIR", hooks), \
                    mock.patch.object(map_setup, "HOOKS_ABS", str(hooks)), \
                    mock.patch.object(map_setup, "_is_git_repo", return_value=True), \
                    mock.patch.object(map_setup.subprocess, "run") as run:
                self.assertTrue(map_setup.wire_hooks())

        run.assert_called_once_with(
            ["git", "-C", str(home), "config", "core.hooksPath", str(hooks)],
            check=True,
        )


class ArgumentPreflightTest(unittest.TestCase):
    """Help and typos must answer before any setup phase runs (issue #1659)."""

    def test_help_and_typos_reach_no_setup_phase(self):
        tripwire = mock.Mock(side_effect=AssertionError("setup phase ran"))
        for argv, code in ((["--help"], 0), (["-h"], 0), (["--wat"], 2)):
            with self.subTest(argv=argv), \
                    mock.patch.object(map_setup, "run_update_compat", tripwire), \
                    mock.patch.object(map_setup, "wire_hooks", tripwire), \
                    mock.patch.object(map_setup.map_repo, "main", tripwire), \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertEqual(code, map_setup.main(argv))
        tripwire.assert_not_called()


class UpdateBridgeSeatTest(unittest.TestCase):
    def test_sandbox_seat_skips_host_update_bridge(self):
        with mock.patch.dict(map_setup.os.environ, {"SC_SANDBOX": "1"}), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            map_setup.run_update_compat()
        run.assert_not_called()

    def test_host_seat_runs_update_bridge(self):
        with mock.patch.dict(map_setup.os.environ, {}, clear=True), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            run.return_value.returncode = 0
            map_setup.run_update_compat()
        run.assert_called_once()

    def test_host_shell_skips_owner_bridge(self):
        with mock.patch.dict(os.environ, {"SC_API_TOKEN": "shell-token"}, clear=True), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            map_setup.run_update_compat()
        run.assert_not_called()

    def test_operator_update_preserves_bridge_with_inherited_shell_token(self):
        with mock.patch.dict(os.environ, {
            "SC_API_TOKEN": "shell-token", "SC_ADMIN": "1",
        }, clear=True), mock.patch.object(map_setup.subprocess, "run") as run:
            run.return_value.returncode = 0
            map_setup.run_update_compat()
        run.assert_called_once()

    def test_operator_bridge_failure_still_aborts(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(SystemExit, "compatibility phase failed"):
                map_setup.run_update_compat()

    def test_host_shell_first_setup_wires_hooks_and_maps_without_bridge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / ".super-coder/scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(ENGINE / "scripts/map_setup.py", scripts / "map_setup.py")
            (scripts / "update_compat.py").write_text(
                "raise PermissionError('cannot read private state owner metadata')\n"
            )
            (scripts / "map_repo.py").write_text(
                "from pathlib import Path\n"
                "def main():\n"
                "    Path(__file__).parents[2].joinpath('mapped').touch()\n"
                "    return 0\n"
            )
            (scripts / "cli_entry.py").write_text(
                "def run_cli(func, *args): return func(*args)\n"
            )
            hooks = root / ".super-coder/hooks"
            hooks.mkdir()
            hook = hooks / "post-checkout"
            hook.write_text("#!/bin/sh\n")
            hook.chmod(0o644)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            env = {**os.environ, "SC_API_TOKEN": "shell-token"}
            for key in ("SC_SANDBOX", "SC_ADMIN"):
                env.pop(key, None)
            result = subprocess.run(
                [sys.executable, str(scripts / "map_setup.py")],
                env=env, capture_output=True, text=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "mapped").exists())
            self.assertTrue(hook.stat().st_mode & 0o111)
            configured = subprocess.run(
                ["git", "-C", str(root), "config", "--get", "core.hooksPath"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(str(hooks), configured.stdout.strip())


if __name__ == "__main__":
    unittest.main()
