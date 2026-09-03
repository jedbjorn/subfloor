"""`./sc make-cleanup` — retiring a fork's make dos-* wiring."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import make_cleanup  # noqa: E402


class MakeCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "fork"
        (self.repo / ".super-coder").mkdir(parents=True)
        self.aliases = self.repo / ".super-coder" / "aliases.mk"
        self.makefile = self.repo / "Makefile"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nothing_to_do_on_a_clean_fork(self) -> None:
        self.assertEqual([], make_cleanup.plan(self.repo))
        self.assertEqual([], make_cleanup.apply(self.repo))

    def test_installer_one_liner_makefile_is_deleted(self) -> None:
        self.makefile.write_text(make_cleanup.INSTALLER_MAKEFILE)
        self.aliases.write_text("dos-e: ; ./sc enter\n")

        self.assertEqual(
            [("delete-makefile", str(self.makefile)), ("delete-aliases", str(self.aliases))],
            make_cleanup.plan(self.repo),
        )
        report = make_cleanup.apply(self.repo)

        self.assertFalse(self.makefile.exists())
        self.assertFalse(self.aliases.exists())
        self.assertEqual(2, len(report))
        self.assertEqual([], make_cleanup.plan(self.repo))

    def test_fork_owned_makefile_keeps_its_targets_and_loses_the_include(self) -> None:
        own = "test:\n\t@echo host\n\ndeploy:\n\t@echo ship\n"
        self.makefile.write_text(own + make_cleanup.APPENDED_ALIASES_BLOCK)

        self.assertEqual([("unwire-makefile", str(self.makefile))], make_cleanup.plan(self.repo))
        make_cleanup.apply(self.repo)

        text = self.makefile.read_text()
        self.assertIn("@echo host", text)
        self.assertIn("@echo ship", text)
        self.assertNotIn("aliases.mk", text)
        self.assertNotIn("designs-OS", text)
        self.assertFalse(make_cleanup.makefile_still_wired(self.repo))

    def test_bare_hard_include_line_is_removed_too(self) -> None:
        self.makefile.write_text("all:\n\t@true\n  include .super-coder/aliases.mk\n")
        make_cleanup.apply(self.repo)
        self.assertEqual("all:\n\t@true\n", self.makefile.read_text())

    def test_makefile_without_include_is_left_untouched(self) -> None:
        self.makefile.write_text("all:\n\t@true\n")
        self.assertEqual([], make_cleanup.plan(self.repo))
        self.assertFalse(make_cleanup.cleanup_makefile(self.repo))
        self.assertEqual("all:\n\t@true\n", self.makefile.read_text())

    def test_dry_run_writes_nothing(self) -> None:
        self.makefile.write_text(make_cleanup.INSTALLER_MAKEFILE)
        self.aliases.write_text("x\n")
        out = io.StringIO()
        with mock.patch.object(make_cleanup, "REPO_ROOT", self.repo), contextlib.redirect_stdout(out):
            self.assertEqual(0, make_cleanup.main(["--dry-run"]))
        self.assertIn("would:", out.getvalue())
        self.assertTrue(self.makefile.exists())
        self.assertTrue(self.aliases.exists())

    def test_cli_apply_reports_and_points_at_subfloor(self) -> None:
        self.makefile.write_text("own:\n\t@true\n" + make_cleanup.APPENDED_ALIASES_BLOCK)
        self.aliases.write_text("x\n")
        out = io.StringIO()
        with mock.patch.object(make_cleanup, "REPO_ROOT", self.repo), contextlib.redirect_stdout(out):
            self.assertEqual(0, make_cleanup.main([]))
        text = out.getvalue()
        self.assertIn("removed the aliases.mk include", text)
        self.assertIn("deleted", text)
        self.assertIn("Makefile kept", text)
        self.assertIn("subfloor <verb>", text)
        self.assertFalse(self.aliases.exists())

    def test_remove_delegates_makefile_unwiring_here(self) -> None:
        import remove as remove_mod  # noqa: PLC0415

        self.makefile.write_text("own:\n\t@true\n" + make_cleanup.APPENDED_ALIASES_BLOCK)
        self.assertTrue(remove_mod.cleanup_makefile(self.repo))
        self.assertEqual("own:\n\t@true\n", self.makefile.read_text())


if __name__ == "__main__":
    unittest.main()
