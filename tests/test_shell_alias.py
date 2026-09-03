"""The `subfloor` operator command — bash + fish shell function install."""
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

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import shell_alias  # noqa: E402


class ShellAliasInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def bashrc(self) -> str:
        return (self.home / ".bashrc").read_text()

    def test_install_writes_bash_block_and_fish_files(self) -> None:
        (self.home / ".bashrc").write_text("export EDITOR=vi\n")
        lines = shell_alias.install(self.env)

        self.assertEqual(2, len(lines))
        self.assertIn("wrote", lines[0])
        self.assertIn("wrote", lines[1])
        text = self.bashrc()
        self.assertTrue(text.startswith("export EDITOR=vi\n"))
        self.assertIn(shell_alias.BASH_BEGIN, text)
        self.assertIn("subfloor() {", text)
        self.assertTrue(text.endswith(shell_alias.BASH_END + "\n"))
        fish = self.home / ".config/fish/functions/subfloor.fish"
        self.assertEqual(shell_alias.FISH_FUNCTION, fish.read_text())
        completion = self.home / ".config/fish/completions/subfloor.fish"
        self.assertEqual(shell_alias.FISH_COMPLETION, completion.read_text())
        self.assertEqual({"bash": "current", "fish": "current"}, shell_alias.status(self.env))
        self.assertTrue(shell_alias.is_current(self.env))

    def test_install_creates_bashrc_when_absent(self) -> None:
        shell_alias.install(self.env)
        self.assertTrue(self.bashrc().startswith(shell_alias.BASH_BEGIN))

    def test_install_is_idempotent(self) -> None:
        shell_alias.install(self.env)
        before = self.bashrc()
        lines = shell_alias.install(self.env)
        self.assertEqual(before, self.bashrc())
        self.assertTrue(all("already current" in line for line in lines), lines)

    def test_stale_block_is_replaced_in_place_not_duplicated(self) -> None:
        stale = shell_alias.BASH_FUNCTION.replace("alias v1)", "alias v0)").replace(
            "subfloor() {", "subfloor() { # old body"
        )
        (self.home / ".bashrc").write_text("# top\n" + stale + "# bottom\n")
        (self.home / ".config/fish/functions").mkdir(parents=True)
        (self.home / ".config/fish/functions/subfloor.fish").write_text(
            "# subfloor — managed by ./sc alias v0\nfunction subfloor\nend\n"
        )
        self.assertEqual({"bash": "stale", "fish": "stale"}, shell_alias.status(self.env))

        lines = shell_alias.install(self.env)

        self.assertTrue(all("refreshed" in line for line in lines), lines)
        text = self.bashrc()
        self.assertEqual(1, text.count("subfloor() {"))
        self.assertNotIn("# old body", text)
        self.assertIn("# top\n", text)
        self.assertIn("# bottom\n", text)
        self.assertTrue(shell_alias.is_current(self.env))

    def test_remove_strips_block_and_deletes_fish_files(self) -> None:
        (self.home / ".bashrc").write_text("alias ll='ls -l'\n")
        shell_alias.install(self.env)
        shell_alias.remove(self.env)
        self.assertEqual("alias ll='ls -l'\n", self.bashrc())
        self.assertFalse((self.home / ".config/fish/functions/subfloor.fish").exists())
        self.assertFalse((self.home / ".config/fish/completions/subfloor.fish").exists())
        self.assertEqual({"bash": "absent", "fish": "absent"}, shell_alias.status(self.env))

    def test_foreign_fish_function_is_never_overwritten(self) -> None:
        fish = self.home / ".config/fish/functions/subfloor.fish"
        fish.parent.mkdir(parents=True)
        fish.write_text("function subfloor\n    echo mine\nend\n")
        with self.assertRaises(shell_alias.AliasError):
            shell_alias.install(self.env)
        self.assertEqual("function subfloor\n    echo mine\nend\n", fish.read_text())
        self.assertIn("left alone", shell_alias.remove_fish(self.env))
        self.assertTrue(fish.exists())

    def test_symlinked_bashrc_is_refused(self) -> None:
        target = self.home / "real-bashrc"
        target.write_text("")
        (self.home / ".bashrc").symlink_to(target)
        with self.assertRaises(shell_alias.AliasError):
            shell_alias.install(self.env)

    def test_xdg_config_home_selects_the_fish_directory(self) -> None:
        xdg = Path(self.tmp.name) / "xdg"
        shell_alias.install({**self.env, "XDG_CONFIG_HOME": str(xdg)})
        self.assertTrue((xdg / "fish/functions/subfloor.fish").is_file())
        self.assertFalse((self.home / ".config").exists())

    def test_cli_status_exit_code_tracks_currency(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), unittest_env(self.env):
            self.assertEqual(1, shell_alias.main(["--status"]))
            self.assertEqual(0, shell_alias.main([]))
            self.assertEqual(0, shell_alias.main(["--status"]))
            self.assertEqual(0, shell_alias.main(["--remove"]))
            self.assertEqual(1, shell_alias.main(["--status"]))

    def test_cli_print_emits_each_shell_verbatim(self) -> None:
        for shell, expected in (("bash", shell_alias.BASH_FUNCTION), ("fish", shell_alias.FISH_FUNCTION)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, shell_alias.main(["--print", shell]))
            self.assertEqual(expected, out.getvalue())


@contextlib.contextmanager
def unittest_env(env: dict[str, str]):
    saved = {key: os.environ.get(key) for key in ("HOME", "XDG_CONFIG_HOME")}
    os.environ.pop("XDG_CONFIG_HOME", None)
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ShellFunctionBehaviourTest(unittest.TestCase):
    """The rendered functions resolve the enclosing checkout and forward args."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.checkout = base / "fork"
        (self.checkout / ".sc-state").mkdir(parents=True)
        (self.checkout / "sub" / "deeper").mkdir(parents=True)
        launcher = self.checkout / "sc"
        # The function never changes directory: like ./sc itself, the launcher
        # resolves its checkout from its own path, and cwd-relative verbs keep cwd.
        launcher.write_text(
            '#!/bin/sh\nprintf "root=%s args=%s\\n" "$(cd "$(dirname "$0")" && pwd -P)" "$*"\n'
        )
        launcher.chmod(0o755)
        self.outside = base / "elsewhere"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_bash(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        script = shell_alias.BASH_FUNCTION + "\nsubfloor " + " ".join(args)
        return subprocess.run(["bash", "-c", script], cwd=cwd, text=True, capture_output=True)

    def test_bash_function_runs_the_enclosing_launcher_from_a_subdirectory(self) -> None:
        result = self.run_bash(self.checkout / "sub" / "deeper", "enter", "cc", "--harness", "claude")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"root={self.checkout.resolve()} args=enter cc --harness claude\n",
            result.stdout,
        )

    def test_bash_function_fails_outside_a_checkout(self) -> None:
        result = self.run_bash(self.outside, "help")
        self.assertEqual(1, result.returncode)
        self.assertIn("no Subfloor checkout", result.stderr)
        self.assertEqual("", result.stdout)

    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_fish_function_runs_the_enclosing_launcher_from_a_subdirectory(self) -> None:
        env = {"HOME": self.tmp.name, "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg")}
        shell_alias.install(env)
        result = subprocess.run(
            ["fish", "-c", "subfloor enter cc --harness claude"],
            cwd=self.checkout / "sub",
            env={**os.environ, **env},
            text=True,
            capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"root={self.checkout.resolve()} args=enter cc --harness claude\n",
            result.stdout,
        )
        outside = subprocess.run(
            ["fish", "-c", "subfloor help"],
            cwd=self.outside,
            env={**os.environ, **env},
            text=True,
            capture_output=True,
        )
        self.assertEqual(1, outside.returncode)
        self.assertIn("no Subfloor checkout", outside.stderr)


class OperatorSurfaceTest(unittest.TestCase):
    def test_dispatcher_exposes_alias_and_make_cleanup(self) -> None:
        dispatcher = (SCRIPTS / "dispatch.sh").read_text()
        self.assertIn('alias)        exec "$PY" "$S/shell_alias.py" "$@" ;;', dispatcher)
        self.assertIn('make-cleanup) exec "$PY" "$S/make_cleanup.py" "$@" ;;', dispatcher)

    def test_make_surface_is_gone_from_the_engine(self) -> None:
        self.assertFalse((ROOT / "Makefile").exists())
        self.assertFalse((ROOT / ".super-coder" / "aliases.mk").exists())
        import engine_manifest  # noqa: PLC0415

        self.assertNotIn(".super-coder/aliases.mk", engine_manifest.ENGINE_PATHS)
        installer = (SCRIPTS / "install.py").read_text()
        self.assertNotIn("wire_make_aliases", installer)
        self.assertNotIn("make dos-", installer)

    def test_help_is_slim_and_names_the_subfloor_verbs(self) -> None:
        env = {**os.environ, "SC_DISPATCH": str(SCRIPTS / "dispatch.sh")}
        result = subprocess.run(
            [str(ROOT / "sc"), "help"], cwd=ROOT, env=env, text=True, capture_output=True
        )
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertLess(len(lines), 40, "top-level help must stay a one-screen chart")
        self.assertTrue(lines[0].startswith("Subfloor"))
        for verb in ("enter", "launch", "restart", "down", "update", "test", "url", "help"):
            self.assertIn(f"subfloor {verb}", result.stdout)
        self.assertIn("make-cleanup", result.stdout)
        self.assertNotIn("make dos-", result.stdout)
        self.assertNotIn("Fedora", result.stdout)
        full = subprocess.run(
            [str(ROOT / "sc"), "help", "--all"], cwd=ROOT, env=env, text=True, capture_output=True
        )
        self.assertEqual(0, full.returncode, full.stderr)
        self.assertGreater(len(full.stdout.splitlines()), len(lines))
        self.assertIn("./sc alias", full.stdout)
        self.assertIn("./sc enter [shortname]", full.stdout)


if __name__ == "__main__":
    unittest.main()
