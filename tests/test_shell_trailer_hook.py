"""prepare-commit-msg attribution: one shell trailer, generated, never blocking.

Feature #72 (spec #218, decision 7): the launcher exports SC_SHELL_NAME and
SC_SHELL_SHORTNAME; the tracked hook appends exactly one
`Co-Authored-By: <display_name> <shortname@subfloor.local>` trailer. Absent
variables mean no trailer. Drives the real hook against a scratch repo.

Run:
    python3 tests/test_shell_trailer_hook.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
HOOK = ENGINE / "hooks" / "prepare-commit-msg"
TRAILER = "Co-Authored-By: Code One <DEV9@subfloor.local>"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class ShellTrailerHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="sc-trailer-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        hooks = self.repo / "hooks"
        hooks.mkdir()
        shutil.copy2(HOOK, hooks / "prepare-commit-msg")
        self.base_env = {
            k: v for k, v in os.environ.items()
            if k not in ("SC_SHELL_NAME", "SC_SHELL_SHORTNAME")
        }
        self.base_env.update(GIT_ENV)
        self.git("init", "-q", "-b", "work")
        self.git("config", "core.hooksPath", str(hooks))
        (self.repo / "a.txt").write_text("one\n")
        self.git("add", "a.txt")

    def git(self, *args: str, env: dict | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True,
            capture_output=True, env=env or self.base_env,
        ).stdout

    def shell_env(self) -> dict:
        return {**self.base_env, "SC_SHELL_NAME": "Code One",
                "SC_SHELL_SHORTNAME": "DEV9"}

    def message(self) -> str:
        return self.git("log", "-1", "--format=%B")

    def test_launched_shell_commit_carries_exactly_one_trailer(self):
        self.git("commit", "-q", "-m", "feat: one", env=self.shell_env())
        body = self.message()
        self.assertEqual(body.count(TRAILER), 1)
        self.assertTrue(body.startswith("feat: one\n"))

    def test_amend_and_preexisting_trailer_never_duplicate(self):
        self.git("commit", "-q", "-m", f"feat: one\n\n{TRAILER}",
                 env=self.shell_env())
        self.assertEqual(self.message().count(TRAILER), 1)
        self.git("commit", "-q", "--amend", "--no-edit", env=self.shell_env())
        self.assertEqual(self.message().count(TRAILER), 1)

    def test_harness_trailer_and_shell_trailer_coexist(self):
        other = "Co-Authored-By: Some Harness <noreply@example.com>"
        self.git("commit", "-q", "-m", f"feat: one\n\n{other}",
                 env=self.shell_env())
        body = self.message()
        self.assertEqual(body.count(TRAILER), 1)
        self.assertEqual(body.count(other), 1)

    def test_bare_operator_gets_no_trailer(self):
        self.git("commit", "-q", "-m", "chore: bare")
        self.assertNotIn("subfloor.local", self.message())
        partial = {**self.base_env, "SC_SHELL_SHORTNAME": "DEV9"}
        (self.repo / "b.txt").write_text("two\n")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "chore: partial", env=partial)
        self.assertNotIn("subfloor.local", self.message())

    def test_launcher_exports_the_identity_the_hook_reads(self):
        run_py = (ENGINE / "scripts" / "run.py").read_text()
        self.assertEqual(run_py.count('env["SC_SHELL_NAME"] = full["display_name"] or ""'), 2)
        # Issue #1494: both exec seams pin the git ident vars to the booted
        # shell's identity, overriding whatever the environment inherited.
        self.assertEqual(run_py.count("env.update(shell_git_ident_env(full))"), 2)
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_sandbox_launcher_exports_no_repo_wide_git_identity(self):
        # Issue #1494: dispatch.sh must not forward a shared .git/config [user]
        # block as GIT_*_NAME/EMAIL — that export let the most recent launch
        # speak for every shell, or killed commits with an empty ident.
        dispatch = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertNotIn("GIT_AUTHOR_NAME", dispatch)
        self.assertNotIn("GIT_COMMITTER_NAME", dispatch)
        self.assertNotIn("git config user.name", dispatch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
