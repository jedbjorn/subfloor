"""Contract and fixture gates for Feature #26 browser Diff review."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from review_fixtures import (
    FIXTURES,
    MockGitHub,
    RemoteUnavailable,
    ReviewRepository,
)


class ProjectionContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            (FIXTURES / "projection_contract.json").read_text()
        )

    def test_target_lifecycle_freshness_and_error_vocabularies_are_frozen(
        self,
    ) -> None:
        self.assertEqual(self.contract["contract_version"], 1)
        self.assertEqual(
            set(self.contract["target_kinds"]),
            {"workspace", "local_branch", "pull_request"},
        )
        self.assertEqual(
            set(self.contract["lifecycle_statuses"]),
            {
                "local_branch",
                "pushed",
                "pr_open",
                "checks_failed",
                "pr_merged",
                "pr_closed",
                "remote_unknown",
            },
        )
        self.assertEqual(
            set(self.contract["freshness_states"]),
            {"fresh", "cached", "unavailable"},
        )
        self.assertEqual(
            set(self.contract["error_codes"]),
            {
                "REVIEW_TARGET_NOT_FOUND",
                "REVIEW_TARGET_UNAVAILABLE",
                "REVIEW_WORKTREE_MISSING",
                "REVIEW_NOT_A_GIT_REPOSITORY",
                "REVIEW_REF_MISSING",
                "REVIEW_REMOTE_UNAVAILABLE",
                "REVIEW_PATH_INVALID",
                "REVIEW_DIFF_TOO_LARGE",
                "REVIEW_TARGET_CHANGED",
            },
        )

    def test_every_required_lifecycle_scenario_has_an_expectation(self) -> None:
        self.assertEqual(
            set(self.contract["scenario_expectations"]),
            {
                "clean",
                "dirty",
                "local",
                "pushed",
                "open",
                "checks_failed",
                "closed",
                "squash_merged",
                "retained_behind",
                "pruned",
                "branch_reuse",
                "offline_cached",
                "offline_uncached",
            },
        )
        self.assertTrue(
            self.contract["scenario_expectations"]["squash_merged"]
            ["remote_overrides_ancestry"]
        )
        self.assertEqual(
            self.contract["scenario_expectations"]["branch_reuse"]
            ["identity_field"],
            "pr_number",
        )

    def test_comparison_and_file_state_contracts_are_complete(self) -> None:
        self.assertEqual(
            set(self.contract["comparison_scopes"]),
            {"review", "local", "commits"},
        )
        self.assertEqual(
            set(self.contract["file_states"]),
            {
                "added",
                "modified",
                "deleted",
                "renamed",
                "untracked",
                "conflict",
                "binary",
                "oversized",
            },
        )


class GitComparisonFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReviewRepository()
        self.addCleanup(self.fixture.cleanup)

    def test_three_dot_review_excludes_unrelated_base_advancement(self) -> None:
        self.fixture.build_three_dot_case()

        three_dot = self.fixture.changed_paths(
            "main...feature/three-dot"
        )
        direct_tips = self.fixture.changed_paths(
            "main..feature/three-dot"
        )

        self.assertEqual(three_dot, {"topic.txt"})
        self.assertEqual(
            direct_tips,
            {"topic.txt", "unrelated-base.txt"},
            "two-tip comparison demonstrates the unrelated-base failure mode",
        )

    def test_clean_local_and_pushed_branch_states_are_distinct(self) -> None:
        self.assertEqual(self.fixture.git("status", "--porcelain=v2"), "")
        self.fixture.branch("feature/pushed")
        self.fixture.write("pushed.txt", "pushed\n")
        self.fixture.commit("local topic")

        absent = self.fixture.git_result(
            "show-ref",
            "--verify",
            "refs/remotes/origin/feature/pushed",
            check=False,
        )
        self.assertNotEqual(absent.returncode, 0)

        self.fixture.git("push", "-u", "origin", "feature/pushed")
        self.assertEqual(
            self.fixture.git(
                "rev-parse",
                "feature/pushed",
            ),
            self.fixture.git(
                "rev-parse",
                "refs/remotes/origin/feature/pushed",
            ),
        )

    def test_squash_merge_remote_verdict_overrides_misleading_ancestry(
        self,
    ) -> None:
        topology = self.fixture.build_squash_merge_case()
        ancestry = self.fixture.git_result(
            "merge-base",
            "--is-ancestor",
            topology["topic_sha"],
            "main",
            check=False,
        )
        behind, ahead = self.fixture.git(
            "rev-list",
            "--left-right",
            "--count",
            "main...feature/squash",
        ).split()
        remote = MockGitHub().pr(823)

        self.assertNotEqual(
            ancestry.returncode,
            0,
            "squash merge must not manufacture topic ancestry",
        )
        self.assertGreater(int(behind), 0)
        self.assertGreater(int(ahead), 0)
        self.assertEqual(remote["state"], "MERGED")
        self.assertEqual(
            remote["headRefName"],
            "feature/squash",
            "PR identity survives the misleading retained branch",
        )

    def test_merged_branch_can_be_checked_out_behind_a_stale_local_base(
        self,
    ) -> None:
        topology = self.fixture.build_squash_merge_case()
        local_base_has_merge = self.fixture.git_result(
            "merge-base",
            "--is-ancestor",
            topology["merge_sha"],
            "origin/main",
            check=False,
        )
        self.fixture.checkout("feature/squash")
        behind, ahead = self.fixture.git(
            "rev-list",
            "--left-right",
            "--count",
            "main...HEAD",
        ).split()

        self.assertNotEqual(local_base_has_merge.returncode, 0)
        self.assertEqual(
            self.fixture.git("branch", "--show-current"),
            "feature/squash",
        )
        self.assertGreater(int(behind), 0)
        self.assertGreater(int(ahead), 0)
        self.assertEqual(MockGitHub().pr(823)["state"], "MERGED")

    def test_pr_target_survives_local_branch_pruning(self) -> None:
        self.fixture.build_squash_merge_case()
        self.fixture.git("branch", "-D", "feature/squash")
        local_branch = self.fixture.git_result(
            "show-ref",
            "--verify",
            "refs/heads/feature/squash",
            check=False,
        )

        self.assertNotEqual(local_branch.returncode, 0)
        self.assertEqual(MockGitHub().pr(823)["number"], 823)

    def test_dirty_rename_binary_and_oversized_states_are_real_git_fixtures(
        self,
    ) -> None:
        self.fixture.build_file_state_case()
        status = self.fixture.git_result(
            "status",
            "--porcelain=v2",
            "-z",
            "--branch",
            text=False,
        ).stdout
        numstat = self.fixture.git("diff", "HEAD", "--numstat")

        self.assertIn(b"modified.txt", status)
        self.assertIn(b"renamed-before.txt", status)
        self.assertIn(b"renamed-after.txt", status)
        self.assertIn(b"deleted.txt", status)
        self.assertIn(b"added.txt", status)
        self.assertIn(b"untracked.txt", status)
        self.assertIn(b"binary.dat", status)
        self.assertIn(b"oversized.txt", status)
        self.assertIn("-\t-\tbinary.dat", numstat)
        self.assertGreater(
            (self.fixture.repo / "oversized.txt").stat().st_size,
            1024 * 1024,
        )

    def test_conflict_state_is_a_real_unmerged_index(self) -> None:
        self.fixture.build_conflict_case()
        status = self.fixture.git_result(
            "status",
            "--porcelain=v2",
            "-z",
            text=False,
        ).stdout

        self.assertIn(b"u UU ", status)
        self.assertIn(b"conflict.txt", status)


class MockGitHubFixtureTest(unittest.TestCase):
    def test_checkrun_and_status_context_rollups_are_explicit(self) -> None:
        github = MockGitHub()
        open_pr = github.pr(821)
        failing_pr = github.pr(822)
        closed_pr = github.pr(826)
        queued_pr = github.pr(827)
        pending_pr = github.pr(828)

        self.assertEqual(open_pr["state"], "OPEN")
        self.assertEqual(
            open_pr["statusCheckRollup"][0]["state"],
            "SUCCESS",
        )
        self.assertNotIn("conclusion", open_pr["statusCheckRollup"][0])
        self.assertEqual(
            open_pr["statusCheckRollup"][1]["conclusion"],
            "SUCCESS",
        )
        self.assertEqual(failing_pr["state"], "OPEN")
        self.assertEqual(
            failing_pr["statusCheckRollup"][0]["state"],
            "FAILURE",
        )
        self.assertNotIn("conclusion", failing_pr["statusCheckRollup"][0])
        self.assertEqual(
            queued_pr["statusCheckRollup"][1],
            {"name": "pytest", "status": "QUEUED", "conclusion": None},
        )
        self.assertEqual(
            pending_pr["statusCheckRollup"][0],
            {"context": "legacy/tests", "state": "PENDING"},
        )
        self.assertEqual(closed_pr["state"], "CLOSED")
        self.assertIsNone(closed_pr["mergedAt"])

    def test_reused_branch_keeps_distinct_pr_identities(self) -> None:
        prs = [
            item
            for item in MockGitHub().list_prs()
            if item["headRefName"] == "feature/reused"
        ]

        self.assertEqual([item["number"] for item in prs], [824, 825])
        self.assertEqual(
            {item["headRefOid"] for item in prs},
            {
                "5555555555555555555555555555555555555555",
                "7777777777777777777777777777777777777777",
            },
        )
        self.assertEqual(
            {item["state"] for item in prs},
            {"MERGED", "OPEN"},
        )

    def test_offline_fixture_fails_explicitly_without_losing_cached_contract(
        self,
    ) -> None:
        cached = MockGitHub().pr(821)
        offline = MockGitHub(available=False)

        with self.assertRaises(RemoteUnavailable):
            offline.list_prs()
        self.assertEqual(cached["number"], 821)
        self.assertEqual(cached["state"], "OPEN")

    def test_canonical_patch_reads_are_by_exact_pr_number(self) -> None:
        github = MockGitHub()

        self.assertIn("open.txt", github.patch(821))
        self.assertIn("squash.txt", github.patch(823))
        with self.assertRaises(KeyError):
            github.patch(825)


if __name__ == "__main__":
    unittest.main()
