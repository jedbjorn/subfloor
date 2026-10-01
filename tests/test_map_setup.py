#!/usr/bin/env python3
"""Regression coverage for home-owned map hook wiring."""
from __future__ import annotations

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


class SandboxUpdateBridgeTest(unittest.TestCase):
    def test_sandbox_seat_skips_host_update_bridge(self):
        with mock.patch.dict(map_setup.os.environ, {"SC_SANDBOX": "1"}), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            map_setup.run_update_compat()
        run.assert_not_called()

    def test_host_seat_runs_update_bridge(self):
        with mock.patch.dict(map_setup.os.environ, {}, clear=False), \
                mock.patch.object(map_setup.subprocess, "run") as run:
            map_setup.os.environ.pop("SC_SANDBOX", None)
            run.return_value.returncode = 0
            map_setup.run_update_compat()
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
