"""Regression tests for the fresh-install shell/worktree contract."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "render"))
import compose


class FreshInstallShellContractTest(unittest.TestCase):
    def test_install_is_committed_before_first_shell_worktree(self) -> None:
        for relative in ("README.md", "docs/README.md"):
            text = (ROOT / relative).read_text()
            commit = text.index(
                'git commit --no-verify -m "chore: install subfloor"'
            )
            # This install's happy path launches and enters via the bare-metal
            # host commands, not the sandbox make aliases. docs/README.md
            # spells them with backticks — strip inline-code marks first.
            # Invariant: the install commit precedes the first worktree-creating
            # step (./sc enter). ./sc launch only starts host services.
            plain = text.replace("`", "")
            commit_line = plain.index("git add -A && git commit --no-verify")
            enter = plain.index("./sc enter", commit_line)
            self.assertLess(commit_line, enter, relative)

    def test_rendered_api_guidance_uses_path_launcher(self) -> None:
        rendered = compose.render_api(8837, "configured")
        self.assertIn("`sc mem`", rendered)
        self.assertNotIn("`./sc mem`", rendered)
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
        self.assertIn("`.sc-state/local/map/config.json`", skill)
        self.assertIn("`.sc-state/map_extractors/<name>.py`", skill)
        self.assertNotIn("SC_ROOT", skill)
        self.assertIn("never a commit", skill)
        self.assertNotIn("**Commit** the config + hooks", skill)


if __name__ == "__main__":
    unittest.main()
