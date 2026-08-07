"""Live probes for branch-guard.sh (#317).

The guard blocks default-branch edits, but a gitignored target (the shared/
handoff dir pattern) can never land on a branch — blocking it forced shells to
side-step the hook via Bash `cp` to complete a documented workflow. These tests
drive the real script against a scratch repo.

The scratch repo deliberately lives under $HOME, NOT /tmp — the guard's scratch
exemption allows /tmp/* outright, which would short-circuit every case here.

Run:
    python3 tests/test_branch_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".super-coder" / "scripts" / "branch-guard.sh"
HOOK = ROOT / ".super-coder" / "hooks" / "pre-commit"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class BranchGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(tempfile.mkdtemp(prefix="sc-bg-test-", dir=Path.home()))
        run = lambda *a: subprocess.run(a, cwd=cls.repo, check=True,
                                        capture_output=True,
                                        env={**os.environ, **GIT_ENV})
        run("git", "init", "-q", "-b", "main")
        (cls.repo / ".gitignore").write_text("shared/\n")
        (cls.repo / "shared" / "specs").mkdir(parents=True)
        (cls.repo / "src").mkdir()
        (cls.repo / "src" / "app.py").write_text("x = 1\n")
        run("git", "add", ".gitignore", "src/app.py")
        run("git", "commit", "-q", "-m", "init")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def guard(self, target: str) -> subprocess.CompletedProcess:
        """Run the guard the way the claude PreToolUse hook does: JSON on stdin,
        cwd inside the repo, no admin/shared-dir/TMPDIR escape hatches."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("SC_SHELL_FLAVOR", "SC_SHARED_DIRS", "TMPDIR",
                            "SC_PROTECTED_BRANCHES", "SC_SHELL_WORKTREE")}
        payload = json.dumps({"tool_input": {"file_path": target}})
        return subprocess.run(["bash", str(GUARD)], input=payload, text=True,
                              cwd=self.repo, env=env, capture_output=True,
                              check=False)

    def pre_commit(self, **markers: str) -> subprocess.CompletedProcess:
        """Run the universal commit backstop with an explicit caller context."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "SC_ENGINE_DIR", "SC_SHELL_FLAVOR", "SC_SHARED_DIRS", "TMPDIR",
                "SC_PROTECTED_BRANCHES", "SC_SHELL_WORKTREE",
            )
        }
        env.update(markers)
        return subprocess.run(["bash", str(HOOK)], text=True, cwd=self.repo,
                              env=env, capture_output=True, check=False)

    def test_gitignored_target_allowed_on_protected_branch(self):
        # the #317 case: shared/ is gitignored — a write there can't land on main
        r = self.guard(str(self.repo / "shared" / "specs" / "handoff.md"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_tracked_area_target_blocked_on_protected_branch(self):
        r = self.guard(str(self.repo / "src" / "new_module.py"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("protected branch 'main'", r.stderr)

    def test_tracked_area_target_allowed_on_feature_branch(self):
        subprocess.run(["git", "checkout", "-q", "-b", "feat/x"],
                       cwd=self.repo, check=True, capture_output=True)
        try:
            r = self.guard(str(self.repo / "src" / "new_module.py"))
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            subprocess.run(["git", "checkout", "-q", "main"],
                           cwd=self.repo, check=True, capture_output=True)

    def test_bare_operator_gets_deliberate_recovery(self):
        result = self.pre_commit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Create a feature branch first", result.stderr)
        self.assertIn("git commit command with --no-verify", result.stderr)

    def test_launched_shell_markers_suppress_bypass_recipe(self):
        for markers in (
            {"SC_SHELL_FLAVOR": "vibe"},
            {"SC_SHELL_WORKTREE": str(self.repo)},
        ):
            with self.subTest(markers=markers):
                result = self.pre_commit(**markers)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Create a feature branch first", result.stderr)
                self.assertNotIn("no-verify", result.stderr)

    def test_no_verify_bypasses_the_installed_hook_for_operator_commit(self):
        repo = Path(tempfile.mkdtemp(prefix="sc-bg-bypass-", dir=Path.home()))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        engine = repo / ".super-coder"
        (engine / "hooks").mkdir(parents=True)
        (engine / "scripts").mkdir()
        shutil.copy2(HOOK, engine / "hooks" / "pre-commit")
        shutil.copy2(GUARD, engine / "scripts" / "branch-guard.sh")
        operator_env = {
            k: v for k, v in os.environ.items()
            if k not in ("SC_ENGINE_DIR", "SC_SHELL_FLAVOR", "SC_SHELL_WORKTREE")
        }
        operator_env.update(GIT_ENV)

        def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *args], cwd=repo, check=check, text=True,
                capture_output=True, env=operator_env,
            )

        git("init", "-q", "-b", "main")
        git("config", "core.hooksPath", str(engine / "hooks"))
        (repo / "tracked.txt").write_text("one\n")
        git("add", "tracked.txt")
        blocked = git("commit", "-m", "blocked", check=False)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("--no-verify", blocked.stderr)

        committed = git("commit", "--no-verify", "-m", "allowed", check=False)
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertEqual(git("rev-list", "--count", "HEAD").stdout.strip(), "1")


class OperatorDocumentationTest(unittest.TestCase):
    INSTALL_COMMIT = (
        'git add -A && git commit --no-verify -m "chore: install subfloor"'
    )
    UPDATE_COMMIT = (
        "git add .sc-state/engine.ref sc && git commit --no-verify "
        '-m "chore: update subfloor"'
    )
    DOC_PATHS = (ROOT / "README.md", ROOT / "docs" / "README.md",
                 ROOT / "docs" / "quick-start.md")

    def test_public_install_and_update_docs_pin_operator_commands(self):
        for path in self.DOC_PATHS:
            with self.subTest(path=path.relative_to(ROOT)):
                body = path.read_text()
                self.assertIn(self.INSTALL_COMMIT, body)
                self.assertIn(self.UPDATE_COMMIT, body)

    def test_public_install_docs_pin_runtime_floor_and_override(self):
        for path in self.DOC_PATHS:
            with self.subTest(path=path.relative_to(ROOT)):
                body = path.read_text()
                self.assertIn("Python 3.9+", body)
                self.assertIn("SC_PYTHON", body)

    def test_launched_shell_boot_keeps_branch_only_guidance(self):
        body = (ROOT / ".super-coder" / "templates" / "boot.md").read_text()
        self.assertIn("vibe included", body)
        self.assertIn("Create a branch and retry", body)
        self.assertNotIn("git commit --no-verify", body)


if __name__ == "__main__":
    unittest.main()
