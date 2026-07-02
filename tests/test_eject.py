#!/usr/bin/env python3
"""Tests for `./sc eject` (scripts/eject.py) — the one-way divergence door.

The pieces with teeth: the .gitignore surgery (the /.super-coder/ rule must go,
the runtime files must STAY ignored, and re-running must not stack blocks), and
the refusal surface (update + rollback must hard-stop on the ejected marker —
an ejected fork that still materializes upstream over its edited engine would
be the exact data loss eject exists to prevent).

Run:
    python3 tests/test_eject.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import eject  # noqa: E402
import update  # noqa: E402

FORK_GITIGNORE = """\
# my fork's own rules
*.log
.env

# super-coder — rebuilt/derived; never commit
/.super-coder/
/CLAUDE.md
/AGENTS.md
/.sc-worktrees/
/.sc-state/engine.ref.prev
"""


class GitignoreEjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = eject.REPO_ROOT
        eject.REPO_ROOT = self.root

    def tearDown(self):
        eject.REPO_ROOT = self._orig
        self.tmp.cleanup()

    def _lines(self) -> list[str]:
        return (self.root / ".gitignore").read_text().splitlines()

    def test_drops_engine_rule_keeps_everything_else(self):
        (self.root / ".gitignore").write_text(FORK_GITIGNORE)
        status = eject._gitignore_eject()
        self.assertIn("dropped", status)
        lines = [ln.strip() for ln in self._lines()]
        self.assertNotIn("/.super-coder/", lines,
                         "the engine-dir rule must be gone — git must see the engine")
        for kept in ("*.log", ".env", "/CLAUDE.md", "/.sc-worktrees/"):
            self.assertIn(kept, lines, f"unrelated rule '{kept}' must survive")

    def test_runtime_files_stay_ignored(self):
        (self.root / ".gitignore").write_text(FORK_GITIGNORE)
        eject._gitignore_eject()
        lines = [ln.strip() for ln in self._lines()]
        for runtime in ("/.super-coder/shell_db.db", "/.super-coder/instance.json",
                        "/.super-coder/run/", "/.super-coder/logs/",
                        "/.super-coder/engine.manifest"):
            self.assertIn(runtime, lines,
                          f"'{runtime}' must stay ignored after eject — it is "
                          f"per-instance runtime, never fork source")

    def test_idempotent(self):
        (self.root / ".gitignore").write_text(FORK_GITIGNORE)
        eject._gitignore_eject()
        once = (self.root / ".gitignore").read_text()
        eject._gitignore_eject()
        self.assertEqual((self.root / ".gitignore").read_text(), once,
                         "re-running must not stack a second runtime block")

    def test_no_gitignore_at_all(self):
        status = eject._gitignore_eject()
        self.assertIn("wrote", status)
        self.assertIn("/.super-coder/shell_db.db",
                      [ln.strip() for ln in self._lines()])


class EjectedMarkerRefusalTest(unittest.TestCase):
    """update/rollback consult EJECTED_MARKER + is_source_repo(); the guard
    itself is a two-line early-exit in each main(). Assert the wiring exists —
    the marker path is shared and the exits mention 'eject'."""

    def test_update_knows_the_marker(self):
        self.assertEqual(update.EJECTED_MARKER.name, "ejected")
        self.assertEqual(update.EJECTED_MARKER.parent.name, ".sc-state")

    def test_update_main_guards_on_marker(self):
        src = (ROOT / ".super-coder" / "scripts" / "update.py").read_text()
        self.assertIn("EJECTED_MARKER.exists()", src)
        self.assertIn("has EJECTED", src)

    def test_rollback_main_guards_on_marker(self):
        src = (ROOT / ".super-coder" / "scripts" / "rollback.py").read_text()
        self.assertIn("EJECTED_MARKER.exists()", src)

    def test_eject_and_update_agree_on_marker_path(self):
        self.assertEqual(str(eject.EJECTED_MARKER), str(update.EJECTED_MARKER))


if __name__ == "__main__":
    unittest.main()
