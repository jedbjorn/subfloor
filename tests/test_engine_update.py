"""The Engine drawer's update badge: is an engine update available?

Real git throughout: an upstream repository, a clone standing in for the
checkout, and `engine_update.check()` reading both without fetching.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
import engine_ops
import engine_update


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class EngineUpdateCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.upstream = base / "upstream"
        git(base, "init", "-q", "-b", "main", str(self.upstream))
        self.first = commit(self.upstream, "first")
        self.root = base / "checkout"
        git(base, "clone", "-q", str(self.upstream), str(self.root))
        (self.root / ".sc-state").mkdir()
        self.pin = None
        for patcher in (
            mock.patch.object(engine_update, "REPO_ROOT", self.root),
            mock.patch.object(engine_update, "ENGINE_SOURCE",
                              self.root / ".sc-state" / "engine.source"),
            mock.patch.object(engine_update, "EJECTED_MARKER",
                              self.root / ".sc-state" / "ejected"),
            mock.patch.object(engine_ops, "engine_ref", side_effect=lambda: self.pin),
            mock.patch.dict(engine_update._cache, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def pin_fork(self, sha: str, source: str | None = None) -> None:
        self.pin = sha
        (self.root / ".sc-state" / "engine.source").write_text(
            (source or self.upstream.as_uri()) + "\n")

    # ── the source repo: HEAD against its upstream branch ────────────────────

    def test_source_checkout_at_upstream_is_current(self):
        out = engine_update.check()
        self.assertEqual(out["state"], "current", out)
        self.assertEqual((out["mode"], out["installed"], out["upstream"], out["branch"]),
                         ("source", self.first, self.first, "main"))

    def test_source_checkout_behind_upstream_has_an_update(self):
        newer = commit(self.upstream, "second")
        out = engine_update.check()
        self.assertEqual((out["state"], out["upstream"]), ("available", newer), out)
        # ls-remote fetched nothing into the checkout.
        self.assertNotEqual(git(self.root, "rev-parse", "origin/main"), newer)

    def test_source_checkout_ahead_of_upstream_is_current(self):
        commit(self.root, "local only")
        self.assertEqual(engine_update.check()["state"], "current")

    def test_untracked_branch_is_unknown_with_its_reason(self):
        git(self.root, "checkout", "-q", "-b", "loose")
        out = engine_update.check()
        self.assertEqual(out["state"], "unknown")
        self.assertIn("tracks no upstream", out["reason"])

    # ── a fork: the engine pin against the recorded source's main ────────────

    def test_fork_pin_at_upstream_main_is_current(self):
        self.pin_fork(self.first)
        out = engine_update.check()
        self.assertEqual((out["state"], out["mode"]), ("current", "fork"), out)

    def test_fork_pin_behind_upstream_main_has_an_update(self):
        self.pin_fork(self.first)
        newer = commit(self.upstream, "second")
        out = engine_update.check()
        self.assertEqual((out["state"], out["upstream"]), ("available", newer), out)

    def test_fork_without_a_recorded_source_is_unknown(self):
        self.pin = self.first
        out = engine_update.check()
        self.assertEqual(out["state"], "unknown")
        self.assertIn("engine.source", out["reason"])

    def test_a_source_that_is_not_a_git_locator_is_never_run(self):
        self.pin_fork(self.first, source="ext::sh -c touch% /tmp/pwned")
        with mock.patch.object(engine_update, "_git") as run:
            out = engine_update.check()
        self.assertEqual(out["state"], "unknown")
        run.assert_not_called()

    def test_unreachable_upstream_is_unknown(self):
        self.pin_fork(self.first, source=(self.root.parent / "missing").as_uri())
        out = engine_update.check()
        self.assertEqual(out["state"], "unknown")
        self.assertTrue(out["reason"])

    def test_ejected_fork_has_no_upstream(self):
        self.pin_fork(self.first)
        (self.root / ".sc-state" / "ejected").write_text("")
        self.assertEqual(engine_update.check()["state"], "unknown")

    # ── caching ──────────────────────────────────────────────────────────────

    def test_answer_is_cached_until_the_installed_ref_moves(self):
        self.pin_fork(self.first)
        self.assertEqual(engine_update.check()["state"], "current")
        newer = commit(self.upstream, "second")
        self.assertEqual(engine_update.check()["state"], "current")   # cached
        self.pin = newer                                              # updated
        out = engine_update.check()
        self.assertEqual((out["state"], out["installed"]), ("current", newer))
        commit(self.upstream, "third")
        with mock.patch.object(engine_update, "TTL_SECONDS", 0):
            engine_update._cache.clear()
            self.assertEqual(engine_update.check()["state"], "available")


if __name__ == "__main__":
    unittest.main()
