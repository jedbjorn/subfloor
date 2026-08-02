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


if __name__ == "__main__":
    unittest.main()
