"""Focused gates for the shared read-only Git/PR review service."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(Path(__file__).resolve().parent)]

import git_review
from github_pull_requests import (
    GitHubPullRequestReader,
    GitHubReadError,
    GitHubResponseTooLarge,
    _check_state,
    normalize_pull_request,
)
from review_fixtures import MockGitHub, ReviewRepository


class FixtureGitHubReader:
    def __init__(self, *, available: bool = True) -> None:
        self.fixture = MockGitHub(available=available)

    def list(self):
        return [normalize_pull_request(item) for item in self.fixture.list_prs()]

    def get(self, number: int):
        return normalize_pull_request(self.fixture.pr(number))

    def patch(self, number: int):
        return self.fixture.patch(number)


class GitHubReaderTest(unittest.TestCase):
    def test_check_rollups_stay_pending_until_every_item_succeeds(self) -> None:
        fixture = MockGitHub()
        queued = fixture.pr(827)["statusCheckRollup"]
        failed = fixture.pr(822)["statusCheckRollup"]
        cases = (
            (queued, ("PENDING", False)),
            ([{"status": "QUEUED", "conclusion": None}], ("PENDING", False)),
            ([{"status": "IN_PROGRESS", "conclusion": None}], ("PENDING", False)),
            ([{"status": "WAITING", "conclusion": None}], ("PENDING", False)),
            ([{"status": "REQUESTED", "conclusion": None}], ("PENDING", False)),
            ([{"status": "COMPLETED", "conclusion": None}], ("PENDING", False)),
            ([{"state": "UNKNOWN"}], ("PENDING", False)),
            (failed + queued, ("FAILURE", True)),
            (
                [
                    {"status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"status": "COMPLETED", "conclusion": "NEUTRAL"},
                ],
                ("SUCCESS", False),
            ),
            (fixture.pr(821)["statusCheckRollup"], ("SUCCESS", False)),
            (fixture.pr(828)["statusCheckRollup"], ("PENDING", False)),
            ([{"state": "SUCCESS"}], ("SUCCESS", False)),
            ([{"state": "FAILURE"}], ("FAILURE", True)),
            (
                [{"conclusion": "SUCCESS", "state": "FAILURE", "status": "QUEUED"}],
                ("SUCCESS", False),
            ),
            ([], (None, False)),
        )

        for rollup, expected in cases:
            with self.subTest(rollup=rollup):
                self.assertEqual(expected, _check_state(rollup))

    def test_list_normalizes_full_projection_with_read_only_command(self) -> None:
        payload = json.dumps(MockGitHub().list_prs()).encode()
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

        reader = GitHubPullRequestReader("/tmp/repo", runner=runner)
        pull_requests = reader.list()

        self.assertEqual(pull_requests[0].number, 821)
        self.assertEqual(pull_requests[0].checks, "SUCCESS")
        self.assertFalse(pull_requests[0].checks_failed)
        failing = next(item for item in pull_requests if item.number == 822)
        self.assertTrue(failing.checks_failed)
        self.assertEqual(
            calls[0][0][:4],
            ["gh", "pr", "list", "--state"],
        )
        self.assertNotIn("edit", calls[0][0])
        self.assertNotIn("merge", calls[0][0])

    def test_exact_metadata_and_patch_reads_use_pr_number(self) -> None:
        fixture = MockGitHub()
        responses = [
            json.dumps(fixture.pr(823)).encode(),
            fixture.patch(823).encode(),
        ]
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout=responses.pop(0),
                stderr=b"",
            )

        reader = GitHubPullRequestReader("/tmp/repo", runner=runner)
        self.assertEqual(reader.get(823).state, "MERGED")
        self.assertIn("squash.txt", reader.patch(823))
        self.assertEqual(calls[0][:4], ["gh", "pr", "view", "823"])
        self.assertEqual(calls[1], ["gh", "pr", "diff", "823", "--patch"])

    def test_explicit_repository_scopes_every_read(self) -> None:
        payload = json.dumps(MockGitHub().pr(821)).encode()
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

        reader = GitHubPullRequestReader(
            "/tmp/repo", repository="acme/project", runner=runner
        )
        self.assertEqual(821, reader.get(821).number)
        self.assertEqual(
            ["gh", "pr", "view", "821", "--json"], calls[0][:5]
        )
        self.assertEqual(["--repo", "acme/project"], calls[0][-2:])

    def test_response_cap_fails_closed(self) -> None:
        def runner(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b"x" * 11, stderr=b"")

        reader = GitHubPullRequestReader(
            "/tmp/repo",
            runner=runner,
            max_response_bytes=10,
        )
        with self.assertRaises(GitHubResponseTooLarge):
            reader.patch(1)


class LocalReviewProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReviewRepository()
        self.addCleanup(self.fixture.cleanup)

    def test_three_dot_projection_excludes_unrelated_base_advancement(self) -> None:
        self.fixture.build_three_dot_case()

        projection = git_review.review_files(
            self.fixture.repo,
            "main",
            head_ref="feature/three-dot",
            include_worktree=False,
        )

        self.assertEqual({item.path for item in projection.files}, {"topic.txt"})
        self.assertNotIn(
            "unrelated-base.txt",
            {item.path for item in projection.files},
        )
        self.assertEqual(projection.etag, f'"{projection.fingerprint}"')

    def test_current_worktree_projection_separates_dirty_branch_and_commits(self) -> None:
        self.fixture.write("tracked-ignored.txt", "tracked base\n")
        self.fixture.write("tracked-visible.txt", "visible base\n")
        self.fixture.commit("tracked ignored base")
        self.fixture.git("push", "origin", "main")
        self.fixture.branch("feature/current-worktree")
        self.fixture.write(
            ".gitignore",
            "ignored-untracked.txt\ntracked-ignored.txt\nignored-commit.txt\n",
        )
        self.fixture.commit("add current ignore rules")
        self.fixture.write("ignored-commit.txt", "hidden commit\n")
        self.fixture.git("add", "-f", "ignored-commit.txt")
        hidden_sha = self.fixture.commit("ignored only commit")
        self.fixture.write("visible-commit.txt", "visible commit\n")
        visible_sha = self.fixture.commit("visible commit")
        self.fixture.write("tracked-ignored.txt", "hidden dirty\n")
        self.fixture.write("tracked-visible.txt", "visible unstaged\n")
        self.fixture.write("ignored-untracked.txt", "hidden untracked\n")
        self.fixture.write("dirty-staged.txt", "staged\n")
        self.fixture.git("add", "dirty-staged.txt")
        self.fixture.write("dirty-untracked.txt", "untracked\n")

        projection = git_review.project_current_worktree(self.fixture.repo)

        self.assertEqual(
            {item.path for item in projection.dirty},
            {"dirty-staged.txt", "dirty-untracked.txt", "tracked-visible.txt"},
        )
        self.assertEqual(
            {item.path for item in projection.branch_files},
            {".gitignore", "visible-commit.txt"},
        )
        self.assertEqual(
            [item.sha for item in projection.commits],
            [visible_sha, self.fixture.git("rev-parse", "HEAD~2")],
        )
        self.assertNotIn(hidden_sha, {item.sha for item in projection.commits})
        self.assertEqual(projection.visible_ahead, 2)
        self.assertEqual(projection.behind, 0)
        self.assertTrue(projection.base_available)

    def test_current_worktree_projection_keeps_conflicted_paths_dirty(self) -> None:
        self.fixture.build_conflict_case()

        projection = git_review.project_current_worktree(self.fixture.repo)
        conflict = next(
            item for item in projection.dirty if item.path == "conflict.txt"
        )

        self.assertEqual(conflict.status, "conflict")
        self.assertTrue(conflict.staged)
        self.assertTrue(conflict.unstaged)
        self.assertTrue(conflict.conflict)

    def test_ignored_only_commit_and_dirty_state_are_no_code_changes(self) -> None:
        self.fixture.write("tracked.txt", "base\n")
        self.fixture.commit("tracked base")
        self.fixture.write(".gitignore", "tracked.txt\nhidden.txt\n")
        self.fixture.commit("ignore policy")
        self.fixture.git("push", "origin", "main")
        self.fixture.branch("feature/ignored-only")
        self.fixture.write("hidden.txt", "committed but hidden\n")
        self.fixture.git("add", "-f", "hidden.txt")
        self.fixture.commit("hidden commit")
        self.fixture.write("tracked.txt", "dirty but hidden\n")

        projection = git_review.project_current_worktree(self.fixture.repo)

        self.assertEqual(projection.dirty, ())
        self.assertEqual(projection.branch_files, ())
        self.assertEqual(projection.commits, ())
        self.assertEqual(projection.visible_ahead, 0)
        self.assertEqual(projection.behind, 0)

    def test_projection_uses_merge_base_for_diverged_and_detached_heads(self) -> None:
        self.fixture.branch("feature/diverged")
        self.fixture.write("topic.txt", "topic\n")
        topic_sha = self.fixture.commit("topic")
        self.fixture.checkout("main")
        self.fixture.write("remote-main.txt", "remote\n")
        self.fixture.commit("advance remote main")
        self.fixture.git("push", "origin", "main")
        self.fixture.checkout("feature/diverged")
        self.fixture.git("fetch", "origin", "main")
        self.fixture.git("checkout", "--detach", topic_sha)

        projection = git_review.project_current_worktree(self.fixture.repo)

        self.assertIsNone(projection.branch)
        self.assertEqual(projection.behind, 1)
        self.assertEqual(projection.visible_ahead, 1)
        self.assertEqual(
            {item.path for item in projection.branch_files},
            {"topic.txt"},
        )
        self.assertNotIn(
            "remote-main.txt",
            {item.path for item in projection.branch_files},
        )

    def test_origin_main_fetch_is_fixed_bounded_and_failure_tolerant(self) -> None:
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 1, b"", b"offline\n")

        result = git_review.fetch_origin_main(self.fixture.repo, runner=runner)

        self.assertFalse(result.fresh)
        self.assertEqual(result.error, "offline")
        self.assertEqual(
            calls[0][0][-4:],
            [
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
        )
        self.assertEqual(calls[0][1]["timeout"], 20.0)
        self.assertEqual(calls[0][1]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(calls[0][1]["env"]["GIT_OPTIONAL_LOCKS"], "1")

    def test_workspace_projects_all_bounded_file_states(self) -> None:
        self.fixture.build_file_state_case()

        projection = git_review.collect_workspace(self.fixture.repo)
        by_path = {item.path: item for item in projection.files}

        self.assertEqual(by_path["modified.txt"].status, "modified")
        self.assertEqual(by_path["renamed-after.txt"].status, "renamed")
        self.assertEqual(
            by_path["renamed-after.txt"].old_path,
            "renamed-before.txt",
        )
        self.assertEqual(by_path["deleted.txt"].status, "deleted")
        self.assertEqual(by_path["added.txt"].status, "added")
        self.assertEqual(by_path["untracked.txt"].status, "untracked")
        self.assertTrue(by_path["binary.dat"].binary)
        self.assertTrue(by_path["oversized.txt"].oversized)

        self.fixture.write(".gitattributes", "*.gen linguist-generated\n")
        self.fixture.write("output.gen", "generated\n")
        generated = git_review.collect_workspace(self.fixture.repo)
        self.assertTrue(
            next(
                item for item in generated.files if item.path == "output.gen"
            ).generated
        )

        capped = git_review.collect_workspace(
            self.fixture.repo,
            limits=git_review.ReviewLimits(max_files=2),
        )
        self.assertEqual(len(capped.files), 2)
        self.assertTrue(capped.files_truncated)

    def test_local_and_pushed_branches_are_distinct(self) -> None:
        self.fixture.branch("feature/pushed")
        self.fixture.write("topic.txt", "topic\n")
        self.fixture.commit("topic")
        local = git_review.collect_workspace(self.fixture.repo)
        self.assertFalse(local.pushed)
        self.assertIsNone(local.remote_branch_sha)

        self.fixture.git("push", "-u", "origin", "feature/pushed")
        pushed = git_review.collect_workspace(self.fixture.repo)
        self.assertTrue(pushed.pushed)
        self.assertEqual(pushed.remote_branch_sha, pushed.head_sha)

    def test_public_commit_resolution_and_ancestry_are_exact(self) -> None:
        base_sha = self.fixture.git("rev-parse", "HEAD")
        self.fixture.branch("feature/identity")
        self.fixture.write("identity.txt", "identity\n")
        head_sha = self.fixture.commit("identity")

        self.assertEqual(
            git_review.resolve_commit(self.fixture.repo, "feature/identity"),
            head_sha,
        )
        self.assertTrue(
            git_review.commit_is_ancestor(
                self.fixture.repo,
                base_sha,
                head_sha,
            )
        )
        self.assertFalse(
            git_review.commit_is_ancestor(
                self.fixture.repo,
                head_sha,
                base_sha,
            )
        )
        with self.assertRaisesRegex(
            git_review.GitReadError,
            "Git ref is invalid",
        ):
            git_review.resolve_commit(self.fixture.repo, "--help")

    def test_conflict_and_local_only_overlay_are_explicit(self) -> None:
        self.fixture.build_conflict_case()
        conflict = git_review.collect_workspace(self.fixture.repo)
        self.assertTrue(conflict.files[0].conflict)
        self.assertEqual(conflict.files[0].status, "conflict")

        # Recreate a clean fixture for the selected-head overlay.
        second = ReviewRepository()
        self.addCleanup(second.cleanup)
        second.branch("feature/local")
        second.write("in-pr.txt", "remote head\n")
        selected_head = second.commit("selected PR head")
        second.write("later.txt", "later commit\n")
        second.commit("unpushed")
        second.write("dirty.txt", "dirty\n")
        second.write("untracked.txt", "untracked\n")

        local = git_review.local_only_files(second.repo, selected_head)
        self.assertEqual(
            {item.path for item in local.files},
            {"later.txt", "dirty.txt", "untracked.txt"},
        )

    def test_commit_projection_is_machine_bounded(self) -> None:
        self.fixture.branch("feature/commits")
        for index in range(3):
            self.fixture.write(f"{index}.txt", f"{index}\n")
            self.fixture.commit(f"topic {index}")

        projection = git_review.review_commits(
            self.fixture.repo,
            "main",
            limits=git_review.ReviewLimits(max_commits=2),
        )

        self.assertEqual(len(projection.commits), 2)
        self.assertTrue(projection.commits_truncated)
        self.assertTrue(
            all(item.subject.startswith("topic") for item in projection.commits)
        )
        self.assertEqual(projection.etag, f'"{projection.fingerprint}"')

    def test_patch_projection_handles_text_binary_oversize_and_paths(self) -> None:
        self.fixture.build_file_state_case()

        text = git_review.read_file_patch(
            self.fixture.repo,
            "HEAD",
            "modified.txt",
        )
        self.assertIn("modified.txt", text.text or "")
        self.assertFalse(text.binary)

        untracked = git_review.read_file_patch(
            self.fixture.repo,
            "HEAD",
            "untracked.txt",
        )
        self.assertIn("new file mode", untracked.text or "")

        binary = git_review.read_file_patch(
            self.fixture.repo,
            "HEAD",
            "binary.dat",
        )
        self.assertTrue(binary.binary)
        self.assertIsNone(binary.text)

        oversized = git_review.read_file_patch(
            self.fixture.repo,
            "HEAD",
            "oversized.txt",
        )
        self.assertTrue(oversized.truncated)
        self.assertEqual(oversized.unavailable_reason, "oversized")

        (self.fixture.repo / "outside-link").symlink_to("/tmp")
        with self.assertRaisesRegex(git_review.ReviewError, "regular file"):
            git_review.read_file_patch(
                self.fixture.repo,
                "HEAD",
                "outside-link",
            )
        for unsafe in ("../secret", "/etc/passwd", "dir/../../secret"):
            with self.assertRaises(git_review.ReviewError):
                git_review.validate_review_path(unsafe)

    def test_review_commands_are_read_only_and_disable_diff_helpers(self) -> None:
        self.fixture.branch("feature/commands")
        self.fixture.write("topic.txt", "topic\n")
        self.fixture.commit("topic")
        commands = []

        def recording_runner(args, **kwargs):
            commands.append(args)
            kwargs.pop("check", None)
            return subprocess.run(args, check=False, **kwargs)

        git_review.review_files(
            self.fixture.repo,
            "main",
            runner=recording_runner,
        )

        mutation_verbs = {
            "add",
            "branch",
            "checkout",
            "clean",
            "commit",
            "fetch",
            "merge",
            "push",
            "rebase",
            "reset",
            "restore",
            "rm",
            "switch",
        }
        for command in commands:
            self.assertTrue(mutation_verbs.isdisjoint(command))
        diff_commands = [command for command in commands if "diff" in command]
        self.assertTrue(diff_commands)
        self.assertTrue(
            all(
                "--no-ext-diff" in command and "--no-textconv" in command
                for command in diff_commands
            )
        )


class RemoteReviewProjectionTest(unittest.TestCase):
    def test_branch_reuse_keeps_distinct_pr_targets(self) -> None:
        candidates = git_review.discover_pull_requests(
            FixtureGitHubReader(),
            branch_name="feature/reused",
            head_sha="7777777777777777777777777777777777777777",
        )

        self.assertEqual(
            [item.pull_request.number for item in candidates],
            [825, 824],
        )
        self.assertEqual(candidates[0].lifecycle, "pr_open")
        self.assertEqual(candidates[1].lifecycle, "pr_merged")

    def test_exact_pr_and_check_failure_lifecycle_are_authoritative(self) -> None:
        merged = git_review.discover_pull_requests(
            FixtureGitHubReader(),
            branch_name="ignored",
            pr_number=823,
        )
        failing = git_review.discover_pull_requests(
            FixtureGitHubReader(),
            branch_name="feature/failing",
        )

        self.assertEqual(merged[0].lifecycle, "pr_merged")
        self.assertEqual(failing[0].lifecycle, "checks_failed")

    def test_canonical_patch_is_exact_and_bounded(self) -> None:
        reader = FixtureGitHubReader()
        patch = git_review.canonical_pr_patch(reader, 823)
        self.assertIn("squash.txt", patch.text or "")
        self.assertFalse(patch.truncated)

        capped = git_review.canonical_pr_patch(
            reader,
            823,
            limits=git_review.ReviewLimits(max_patch_bytes=20),
        )
        self.assertTrue(capped.truncated)
        self.assertLessEqual(len((capped.text or "").encode()), 20)

        parsed = git_review.parse_canonical_patch(patch)
        self.assertEqual([item.path for item in parsed.files], ["squash.txt"])
        self.assertEqual(parsed.files[0].status, "added")
        self.assertIn("squash.txt", parsed.file_patches)

        class MixedPatch:
            def patch(self, number: int):
                return (
                    "diff --git a/text.txt b/text.txt\n"
                    "--- a/text.txt\n"
                    "+++ b/text.txt\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new\n"
                    "diff --git a/image.bin b/image.bin\n"
                    "new file mode 100644\n"
                    "Binary files /dev/null and b/image.bin differ\n"
                )

        mixed = git_review.parse_canonical_patch(
            git_review.canonical_pr_patch(MixedPatch(), 1)
        )
        self.assertEqual(
            [item.path for item in mixed.files],
            ["text.txt", "image.bin"],
        )
        self.assertFalse(mixed.files[0].binary)
        self.assertTrue(mixed.files[1].binary)

    def test_remote_outage_is_explicit_and_uses_cached_evidence(self) -> None:
        cached = normalize_pull_request(MockGitHub().pr(821))

        class Offline:
            def list(self):
                raise GitHubReadError("offline")

        with_cache = git_review.collect_pull_requests(
            Offline(),
            branch_name="feature/open",
            cached=(cached,),
        )
        self.assertEqual(with_cache.freshness, "cached")
        self.assertEqual(with_cache.pull_requests[0].freshness, "cached")
        self.assertEqual(with_cache.pull_requests[0].pull_request.number, 821)

        unavailable = git_review.collect_pull_requests(
            Offline(),
            branch_name="feature/missing",
        )
        self.assertEqual(unavailable.freshness, "unavailable")
        self.assertEqual(unavailable.pull_requests, ())


class MergedPatchCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="review-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = git_review.MergedPatchCache(self.root)

    def test_cache_is_relative_owner_only_and_hash_validated(self) -> None:
        artifact = self.cache.store("owner/repo", 823, "canonical patch\n")
        path = self.root / artifact.relative_path

        self.assertFalse(Path(artifact.relative_path).is_absolute())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(path.parent.stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(
            self.cache.load(artifact.relative_path, artifact.sha256),
            "canonical patch\n",
        )

        path.write_text("tampered\n")
        self.assertIsNone(self.cache.load(artifact.relative_path, artifact.sha256))

    def test_cache_rejects_escape_and_oversize(self) -> None:
        with self.assertRaises(git_review.ReviewError):
            self.cache.load("../escape", "0" * 64)
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        linked = self.root / "linked-root"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(git_review.ReviewError):
            git_review.MergedPatchCache(linked).store("owner/repo", 1, "x")
        small = git_review.MergedPatchCache(
            self.root,
            limits=git_review.ReviewLimits(max_artifact_bytes=4),
        )
        with self.assertRaises(git_review.ReviewError):
            small.store("owner/repo", 1, "12345")

    def test_merged_patch_read_populates_then_reuses_cache(self) -> None:
        pull_request = normalize_pull_request(MockGitHub().pr(823))
        first = git_review.read_canonical_pr_patch(
            FixtureGitHubReader(),
            pull_request,
            repository="owner/repo",
            cache=self.cache,
        )
        self.assertEqual(first.freshness, "fresh")
        self.assertIsNotNone(first.artifact)

        class Offline:
            def patch(self, number: int):
                raise GitHubReadError("offline")

        second = git_review.read_canonical_pr_patch(
            Offline(),
            pull_request,
            repository="owner/repo",
            cache=self.cache,
            cached_artifact=first.artifact,
        )
        self.assertEqual(second.freshness, "cached")
        self.assertEqual(second.patch.sha256, first.patch.sha256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
