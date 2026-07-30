"""Reusable Git and mocked-GitHub fixtures for browser Diff review tests."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Review Fixture",
    "GIT_AUTHOR_EMAIL": "review-fixture@example.test",
    "GIT_COMMITTER_NAME": "Review Fixture",
    "GIT_COMMITTER_EMAIL": "review-fixture@example.test",
}


class GitFixtureError(AssertionError):
    """A fixture-building Git command failed unexpectedly."""


class RemoteUnavailable(RuntimeError):
    """The mocked GitHub reader is deliberately offline."""


class ReviewRepository:
    """A disposable repository with a bare origin and deterministic identity."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sc-review-")
        self.root = Path(self._temporary.name)
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Review Fixture")
        self.git("config", "user.email", "review-fixture@example.test")
        self.git("init", "--bare", str(self.origin))
        self.git("remote", "add", "origin", str(self.origin))
        self.write("base.txt", "base\n")
        self.commit("base")
        self.git("push", "-u", "origin", "main")

    def cleanup(self) -> None:
        self._temporary.cleanup()

    def git_result(
        self,
        *args: str,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=text,
            env=GIT_ENV,
            timeout=10,
            check=False,
        )
        if check and result.returncode != 0:
            stderr = result.stderr if text else result.stderr.decode()
            raise GitFixtureError(
                f"git {' '.join(args)} failed ({result.returncode}): {stderr}"
            )
        return result

    def git(self, *args: str) -> str:
        return self.git_result(*args).stdout.strip()

    def write(self, relative: str, body: str | bytes) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
        return path

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str, start: str = "main") -> None:
        self.git("checkout", "-b", name, start)

    def checkout(self, name: str) -> None:
        self.git("checkout", name)

    def changed_paths(self, *diff_args: str) -> set[str]:
        return set(
            filter(
                None,
                self.git("diff", "--name-only", *diff_args).splitlines(),
            )
        )

    def build_three_dot_case(self) -> dict[str, str]:
        base_sha = self.git("rev-parse", "main")
        self.branch("feature/three-dot")
        self.write("topic.txt", "topic\n")
        topic_sha = self.commit("topic change")
        self.checkout("main")
        self.write("unrelated-base.txt", "base advanced\n")
        advanced_base_sha = self.commit("unrelated base advancement")
        return {
            "base_sha": base_sha,
            "topic_sha": topic_sha,
            "advanced_base_sha": advanced_base_sha,
        }

    def build_squash_merge_case(self) -> dict[str, str]:
        self.branch("feature/squash")
        self.write("squash.txt", "squash\n")
        topic_sha = self.commit("squash topic")
        self.checkout("main")
        self.git("merge", "--squash", "feature/squash")
        squash_sha = self.commit("squash merged topic")
        self.write("post-merge-base.txt", "later\n")
        latest_base_sha = self.commit("advance main after squash")
        return {
            "topic_sha": topic_sha,
            "merge_sha": squash_sha,
            "latest_base_sha": latest_base_sha,
        }

    def build_file_state_case(self) -> None:
        self.write("modified.txt", "before\n")
        self.write("renamed-before.txt", "rename me\n")
        self.write("deleted.txt", "delete me\n")
        self.write("conflict.txt", "base\n")
        self.commit("file-state base")
        self.branch("feature/files")
        self.write("modified.txt", "after\n")
        self.git("mv", "renamed-before.txt", "renamed-after.txt")
        (self.repo / "deleted.txt").unlink()
        self.write("added.txt", "added\n")
        self.git("add", "added.txt")
        self.write("untracked.txt", "untracked\n")
        self.write("binary.dat", b"\x00\x01\x02review\xff")
        self.git("add", "binary.dat")
        self.write("oversized.txt", "x" * (1024 * 1024 + 1))

    def build_conflict_case(self) -> None:
        self.write("conflict.txt", "base\n")
        self.commit("conflict base")
        self.branch("feature/conflict")
        self.write("conflict.txt", "topic\n")
        self.commit("topic conflict")
        self.checkout("main")
        self.write("conflict.txt", "main\n")
        self.commit("main conflict")
        result = self.git_result("merge", "feature/conflict", check=False)
        if result.returncode == 0:
            raise GitFixtureError("fixture merge unexpectedly had no conflict")


class MockGitHub:
    """Deterministic PR metadata/patch reader with an explicit offline mode."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self._data = json.loads((FIXTURES / "github_prs.json").read_text())

    def _require_available(self) -> None:
        if not self.available:
            raise RemoteUnavailable("mock GitHub is unavailable")

    def list_prs(self) -> list[dict]:
        self._require_available()
        return copy.deepcopy(self._data["pull_requests"])

    def pr(self, number: int) -> dict:
        self._require_available()
        for item in self._data["pull_requests"]:
            if item["number"] == number:
                return copy.deepcopy(item)
        raise KeyError(number)

    def patch(self, number: int) -> str:
        self._require_available()
        try:
            return self._data["patches"][str(number)]
        except KeyError as exc:
            raise KeyError(number) from exc
