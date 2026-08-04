"""Regression coverage for engine-owned user-global boot pointers."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import global_pointer  # noqa: E402


class GlobalPointerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.engine = root / ".super-coder"
        self.home = root / "home"
        (self.engine / "templates").mkdir(parents=True)
        (self.engine / "adapters").mkdir()
        self.home.mkdir()
        self.template = "# Boot pointer\n\nStatic instructions.\n"
        (self.engine / "templates" / "global_pointer.md").write_text(
            self.template
        )

    def add_adapter(self, harness: str, pointer: str | None) -> None:
        adapter_dir = self.engine / "adapters" / harness
        adapter_dir.mkdir()
        config = {"harness": harness}
        if pointer is not None:
            config["global_pointer"] = pointer
        (adapter_dir / "adapter.json").write_text(json.dumps(config))

    @property
    def desired(self) -> str:
        return (
            "# Boot pointer\n"
            f"{global_pointer.SENTINEL}\n"
            "\nStatic instructions.\n"
        )

    def test_fresh_write_has_exact_managed_content(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        target.parent.mkdir()

        written = global_pointer.write_global_pointers(
            self.engine, home=self.home, environ={}
        )

        self.assertEqual(written, [target])
        self.assertEqual(target.read_text(), self.desired)
        self.assertEqual(target.read_text().splitlines()[1], global_pointer.SENTINEL)

    def test_identical_content_is_a_true_no_op(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        target.parent.mkdir()
        target.write_text(self.desired)

        with mock.patch.object(global_pointer, "_atomic_replace") as replace:
            written = global_pointer.write_global_pointers(
                self.engine, home=self.home, environ={}
            )

        self.assertEqual(written, [])
        replace.assert_not_called()
        self.assertEqual(target.read_text(), self.desired)

    def test_managed_drift_is_rewritten_without_backup(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        target.parent.mkdir()
        target.write_text(self.desired.replace("Static", "Hand-edited"))

        written = global_pointer.write_global_pointers(
            self.engine, home=self.home, environ={}
        )

        self.assertEqual(written, [target])
        self.assertEqual(target.read_text(), self.desired)
        self.assertFalse(target.with_name("CLAUDE.md.pre-sc.bak").exists())

    def test_unmanaged_adoption_backs_up_exactly_once(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        backup = target.with_name("CLAUDE.md.pre-sc.bak")
        target.parent.mkdir()
        target.write_text("operator original\n")

        first = io.StringIO()
        with mock.patch("sys.stdout", first):
            global_pointer.write_global_pointers(
                self.engine, home=self.home, environ={}
            )
        self.assertEqual(target.read_text(), self.desired)
        self.assertEqual(backup.read_text(), "operator original\n")
        self.assertIn(str(backup), first.getvalue())

        target.write_text("operator replacement\n")
        second = io.StringIO()
        with mock.patch("sys.stdout", second):
            global_pointer.write_global_pointers(
                self.engine, home=self.home, environ={}
            )
        self.assertEqual(target.read_text(), self.desired)
        self.assertEqual(backup.read_text(), "operator original\n")
        self.assertNotIn("backup →", second.getvalue())

    def test_missing_parent_directory_skips_without_creating_it(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")

        written = global_pointer.write_global_pointers(
            self.engine, home=self.home, environ={}
        )

        self.assertEqual(written, [])
        self.assertFalse((self.home / ".claude").exists())

    def test_is_sandbox_skips_all_writes(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        target.parent.mkdir()

        written = global_pointer.write_global_pointers(
            self.engine, home=self.home, environ={"IS_SANDBOX": "1"}
        )

        self.assertEqual(written, [])
        self.assertFalse(target.exists())

    def test_symlink_is_left_untouched_with_warning(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        source = self.home / "operator-pointer.md"
        target.parent.mkdir()
        source.write_text("operator symlink target\n")
        target.symlink_to(source)
        output = io.StringIO()

        with mock.patch("sys.stdout", output):
            written = global_pointer.write_global_pointers(
                self.engine, home=self.home, environ={}
            )

        self.assertEqual(written, [])
        self.assertTrue(target.is_symlink())
        self.assertEqual(source.read_text(), "operator symlink target\n")
        self.assertIn(f"left symlink untouched → {target}", output.getvalue())

    def test_write_failure_warns_and_does_not_raise(self) -> None:
        self.add_adapter("claude", ".claude/CLAUDE.md")
        target = self.home / ".claude" / "CLAUDE.md"
        target.parent.mkdir()
        output = io.StringIO()

        with (
            mock.patch.object(
                global_pointer,
                "_atomic_replace",
                side_effect=PermissionError("read-only home"),
            ),
            mock.patch("sys.stdout", output),
        ):
            written = global_pointer.write_global_pointers(
                self.engine, home=self.home, environ={}
            )

        self.assertEqual(written, [])
        self.assertFalse(target.exists())
        self.assertIn(f"skipped {target} (read-only home)", output.getvalue())

    def test_codex_home_override_replaces_default_dot_codex_root(self) -> None:
        self.add_adapter("codex", ".codex/AGENTS.md")
        codex_home = self.home / "custom-codex"
        codex_home.mkdir()
        target = codex_home / "AGENTS.md"

        written = global_pointer.write_global_pointers(
            self.engine,
            home=self.home,
            environ={"CODEX_HOME": str(codex_home)},
        )

        self.assertEqual(written, [target])
        self.assertEqual(target.read_text(), self.desired)
        self.assertFalse((self.home / ".codex").exists())


class AdapterDeclarationTest(unittest.TestCase):
    def test_only_verified_harnesses_declare_global_pointers(self) -> None:
        expected = {
            "claude": ".claude/CLAUDE.md",
            "codex": ".codex/AGENTS.md",
            "opencode": ".config/opencode/AGENTS.md",
            "kimi": None,
            "vibe": None,
        }
        actual = {}
        for harness in expected:
            config = json.loads(
                (ROOT / ".super-coder" / "adapters" / harness / "adapter.json").read_text()
            )
            actual[harness] = config.get("global_pointer")

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
