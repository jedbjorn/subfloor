#!/usr/bin/env python3
"""Regression tests for the fresh-install shell/worktree contract."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "render"))
import compose  # noqa: E402


class FreshInstallShellContractTest(unittest.TestCase):
    def test_install_is_committed_before_first_shell_worktree(self) -> None:
        for relative in ("README.md", "docs/README.md"):
            text = (ROOT / relative).read_text()
            commit = text.index('git commit -m "chore: install subfloor"')
            enter = text.index("./sc enter", commit)
            self.assertLess(commit, enter, relative)

    def test_rendered_api_guidance_uses_path_launcher(self) -> None:
        rendered = compose.render_api(8837, "configured")
        self.assertIn("`sc mem`", rendered)
        self.assertNotIn("`./sc mem`", rendered)
        self.assertNotIn("./sc", compose.PARTICIPANT_RULES)
        boot = (
            ROOT / ".super-coder" / "templates" / "boot.md"
        ).read_text()
        self.assertNotIn("./sc", boot)
        dogfood = (
            ROOT / ".super-coder" / "scripts" / "seed_dogfood.py"
        ).read_text()
        self.assertNotIn("./sc mem", dogfood)

    def test_cartographer_targets_canonical_local_map_state(self) -> None:
        skill = (
            ROOT / ".super-coder" / "assets" / "skills" / "cartographer"
            / "SKILL.md"
        ).read_text()
        self.assertIn("$SC_ROOT/.sc-state/local/map/config.json", skill)
        self.assertIn("$SC_ROOT/.sc-state/map_extractors/", skill)
        self.assertIn("never a commit", skill)
        self.assertNotIn("**Commit** the config + hooks", skill)


if __name__ == "__main__":
    unittest.main()
