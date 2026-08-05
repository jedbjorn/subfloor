"""Hermetic coverage for the source-only dos-app Sprint canary."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maintainer import dos_app_sprint_canary as canary

SHA = "a" * 40
BASE_SHA = "b" * 40


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBackend:
    def __init__(
        self,
        facts: canary.Preflight,
        *,
        fail_at: str | None = None,
        cleanup_fails: bool = False,
        assert_receipt_absent: Path | None = None,
    ) -> None:
        self.facts = facts
        self.fail_at = fail_at
        self.cleanup_fails = cleanup_fails
        self.assert_receipt_absent = assert_receipt_absent
        self.calls: list[str] = []

    def preflight(self, config: canary.CanaryConfig) -> canary.Preflight:
        self.calls.append("preflight")
        if (
            self.assert_receipt_absent is not None
            and self.assert_receipt_absent.exists()
        ):
            raise AssertionError("receipt was written before preflight completed")
        if self.fail_at == "preflight":
            raise canary.CanaryError("PREFLIGHT_TEST", "preflight refused")
        return self.facts

    def create_disposable(
        self,
        config: canary.CanaryConfig,
        facts: canary.Preflight,
        ledger: canary.ResourceLedger,
        checkpoint,
    ) -> None:
        self.calls.append(f"materialize:{facts.candidate_sha}")
        self.assert_exact_ref(facts)
        ledger.workspace = str(facts.workspace)
        ledger.marker_written = True
        ledger.candidate_sha = facts.candidate_sha
        ledger.repository = facts.repository
        ledger.base_branch = facts.base_branch
        ledger.head_branch = facts.head_branch
        ledger.container = facts.container
        ledger.network = facts.network
        checkpoint()
        if self.fail_at == "materialize":
            raise canary.CanaryError("MATERIALIZE_TEST", "materialization failed")

    @staticmethod
    def assert_exact_ref(facts: canary.Preflight) -> None:
        if facts.candidate_sha != SHA:
            raise AssertionError("controller changed the resolved candidate SHA")

    def launch(
        self,
        config: canary.CanaryConfig,
        facts: canary.Preflight,
        ledger: canary.ResourceLedger,
    ) -> dict[str, str]:
        self.calls.append("launch")
        if self.fail_at == "launch":
            raise canary.CanaryError("LAUNCH_TEST", "launch failed")
        return {"codex": "0.146.1", "kimi": "0.33.0"}

    def orchestrate(
        self,
        config: canary.CanaryConfig,
        facts: canary.Preflight,
        ledger: canary.ResourceLedger,
        stage,
        checkpoint,
    ) -> dict:
        self.calls.append("orchestrate")
        for name in (
            "planner_prepare",
            "kimi_qaqc",
            "declare_and_arm",
            "sprint_execution",
        ):
            stage(name)
            self.calls.append(f"stage:{name}")
        ledger.pull_request = 77
        checkpoint()
        if self.fail_at == "orchestrate":
            raise canary.CanaryError(
                "ORCHESTRATE_TEST",
                "native participant failed",
                details={
                    "token": "ghp_super_secret_value",
                    "prompt": "private agent prompt",
                },
            )
        return {
            "routes": {
                "planner_initial": {
                    "harness": "codex",
                    "provider": "openai",
                    "model": "gpt-test",
                    "effort": "high",
                },
                "planner_reentry": {
                    "harness": "codex",
                    "provider": "openai",
                    "model": "gpt-test",
                    "effort": "high",
                },
                "reviewer": {
                    "harness": "kimi",
                    "provider": "moonshot",
                    "model": "kimi-test",
                    "effort": "high",
                },
            },
            "sprint": {
                "sprint_id": 9,
                "lifecycle": "completed",
                "observed_columns": ["waiting", "dev", "review", "done"],
            },
            "pull_request": {
                "number": 77,
                "state": "merged",
                "base_branch": facts.base_branch,
                "head_branch": facts.head_branch,
                "head_sha": "c" * 40,
            },
        }

    def cleanup(
        self,
        config: canary.CanaryConfig,
        facts: canary.Preflight | None,
        ledger: canary.ResourceLedger,
    ) -> list[dict]:
        self.calls.append("cleanup")
        if self.cleanup_fails:
            raise canary.CanaryError(
                "CANARY_CLEANUP_FAILED",
                "cleanup failed",
                details={
                    "failed_actions": ["remove_container"],
                    "actions": [{"action": "remove_container", "ok": False}],
                },
            )
        return [
            {"action": "remove_container", "ok": True},
            {"action": "delete_base_branch", "ok": True},
            {"action": "remove_workspace", "ok": True},
        ]


class DosAppSprintCanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt_path = self.root / "receipts" / "run.json"
        self.config = canary.CanaryConfig(
            source_repo=self.root / "source",
            engine_ref="candidate",
            dos_app_repo=self.root / "dos-app",
            dos_app_ref="origin/main",
            repository="acme/dos-app",
            receipt_path=self.receipt_path,
            temp_parent=self.root,
            run_id="unit-test-0001",
            stage_timeout_s=10,
            whole_timeout_s=100,
            poll_interval_s=0.01,
        )
        self.facts = canary.Preflight(
            candidate_sha=SHA,
            base_sha=BASE_SHA,
            repository="acme/dos-app",
            remote_url="https://github.com/acme/dos-app.git",
            workspace=self.root / f"{canary.WORKSPACE_PREFIX}unit-test-0001",
            base_branch=f"{canary.REMOTE_PREFIX}/unit-test-0001/base",
            head_branch=f"{canary.REMOTE_PREFIX}/unit-test-0001/head",
            container=f"sc-{canary.WORKSPACE_PREFIX}unit-test-0001",
            network="sc-canary-unit-test-0001",
            api_port=8881,
            dev_port=8882,
            github_remaining=4999,
        )

    def controller(self, backend: FakeBackend, clock: FakeClock | None = None):
        clock = clock or FakeClock()
        deadline = canary.Deadline(
            self.config.whole_timeout_s,
            self.config.stage_timeout_s,
            clock=clock,
        )
        receipt = canary.Receipt(self.receipt_path, self.config)
        return canary.CanaryController(self.config, backend, deadline, receipt), clock

    def receipt(self) -> dict:
        return json.loads(self.receipt_path.read_text())

    def test_preflight_failure_happens_before_every_resource_write(self) -> None:
        backend = FakeBackend(
            self.facts,
            fail_at="preflight",
            assert_receipt_absent=self.receipt_path,
        )
        controller, _ = self.controller(backend)

        with self.assertRaisesRegex(canary.CanaryError, "preflight refused"):
            controller.run()

        self.assertEqual(["preflight", "cleanup"], backend.calls)
        payload = self.receipt()
        self.assertEqual("failed", payload["status"])
        self.assertTrue(payload["cleanup"]["complete"])
        self.assertIsNone(payload["resources"]["workspace"])
        self.assertEqual("PREFLIGHT_TEST", payload["failure"]["primary"]["code"])

    def test_success_records_exact_ref_routes_stages_and_cleanup(self) -> None:
        backend = FakeBackend(self.facts)
        controller, _ = self.controller(backend)

        result = controller.run()

        self.assertEqual("passed", result["status"])
        self.assertEqual(SHA, result["candidate_sha"])
        self.assertEqual("completed", result["sprint"]["lifecycle"])
        self.assertEqual("codex", result["routes"]["planner_initial"]["harness"])
        self.assertEqual("kimi", result["routes"]["reviewer"]["harness"])
        self.assertEqual(
            ["waiting", "dev", "review", "done"],
            result["sprint"]["observed_columns"],
        )
        self.assertTrue(result["cleanup"]["complete"])
        self.assertEqual("cleanup", backend.calls[-1])
        self.assertIn(f"materialize:{SHA}", backend.calls)
        self.assertEqual(
            [
                "stage:planner_prepare",
                "stage:kimi_qaqc",
                "stage:declare_and_arm",
                "stage:sprint_execution",
            ],
            [item for item in backend.calls if item.startswith("stage:")],
        )

    def test_partial_orchestration_failure_still_cleans_and_redacts_receipt(
        self,
    ) -> None:
        backend = FakeBackend(self.facts, fail_at="orchestrate")
        controller, _ = self.controller(backend)

        with self.assertRaisesRegex(canary.CanaryError, "native participant failed"):
            controller.run()

        payload = self.receipt()
        serialized = json.dumps(payload)
        self.assertEqual("failed", payload["status"])
        self.assertEqual("cleanup", backend.calls[-1])
        self.assertTrue(payload["cleanup"]["complete"])
        self.assertNotIn("ghp_super_secret_value", serialized)
        self.assertNotIn("private agent prompt", serialized)
        self.assertEqual(
            "[REDACTED]",
            payload["failure"]["primary"]["details"]["prompt"],
        )

    def test_partial_materialization_checkpoints_cleanup_identities(self) -> None:
        backend = FakeBackend(self.facts, fail_at="materialize")
        controller, _ = self.controller(backend)

        with self.assertRaisesRegex(canary.CanaryError, "materialization failed"):
            controller.run()

        payload = self.receipt()
        resources = payload["resources"]
        self.assertEqual(str(self.facts.workspace), resources["workspace"])
        self.assertEqual(self.facts.base_branch, resources["base_branch"])
        self.assertEqual(self.facts.head_branch, resources["head_branch"])
        self.assertEqual(self.facts.container, resources["container"])
        self.assertEqual(self.facts.network, resources["network"])
        self.assertEqual("cleanup", backend.calls[-1])

    def test_cleanup_failure_is_fatal_and_preserves_primary_failure(self) -> None:
        backend = FakeBackend(self.facts, fail_at="orchestrate", cleanup_fails=True)
        controller, _ = self.controller(backend)

        with self.assertRaisesRegex(canary.CanaryError, "cleanup failed") as raised:
            controller.run()

        self.assertEqual("CANARY_CLEANUP_FAILED", raised.exception.code)
        payload = self.receipt()
        self.assertEqual("cleanup_failed", payload["status"])
        self.assertEqual("ORCHESTRATE_TEST", payload["failure"]["primary"]["code"])
        self.assertEqual("CANARY_CLEANUP_FAILED", payload["failure"]["cleanup"]["code"])
        self.assertFalse(payload["cleanup"]["complete"])

    def test_deadlines_enforce_stage_whole_run_and_cleanup_grace(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(whole_timeout_s=20, stage_timeout_s=5, clock=clock)
        deadline.enter("materialize")
        clock.advance(5.1)
        with self.assertRaisesRegex(canary.CanaryError, "stage:materialize"):
            deadline.remaining()

        clock = FakeClock()
        deadline = canary.Deadline(whole_timeout_s=8, stage_timeout_s=20, clock=clock)
        deadline.enter("sprint_execution")
        clock.advance(8.1)
        with self.assertRaisesRegex(canary.CanaryError, "whole-run"):
            deadline.remaining()
        deadline.enter("cleanup")
        self.assertGreater(deadline.remaining(), 4.9)

    def test_recursive_redaction_drops_prompt_and_masks_tokens(self) -> None:
        payload = canary.sanitize(
            {
                "prompt": "do private work",
                "nested": {
                    "authorization": "Bearer ghp_abcdefghijklmnop",
                    "api_key": "opaque-value-without-prefix",
                    "note": "token=sk-abcdefghijklmnopqrstu",
                },
                "ids": [1, "safe"],
            }
        )

        serialized = json.dumps(payload)
        self.assertEqual("[REDACTED]", payload["prompt"])
        self.assertEqual("[REDACTED]", payload["nested"]["authorization"])
        self.assertEqual("[REDACTED]", payload["nested"]["api_key"])
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertEqual([1, "safe"], payload["ids"])

    def test_browser_route_is_read_from_the_real_nested_projection(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = canary.HostBackend(deadline)

        class FakeApi:
            def request(self, method, path, *, body=None, key=None):
                return {
                    "conversation_id": "cv-nested",
                    "route": {
                        "harness": "codex",
                        "provider": "openai",
                        "model": "gpt-test",
                        "effort": "high",
                    },
                }

        projected = backend._create_conversation(
            cast(canary.JsonHttp, FakeApi()),
            shell_id=1,
            harness="codex",
            key="nested-route",
        )

        self.assertNotIn("model", projected)
        self.assertEqual("gpt-test", projected["route"]["model"])

    def test_live_materialization_uses_exact_sha_refspec_and_verifies_pin(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = canary.HostBackend(deadline)
        ledger = canary.ResourceLedger()
        git_calls: list[tuple[str, ...]] = []
        run_labels: list[str] = []

        def fake_git(repo, *args, label, check=True):
            git_calls.append(tuple(args))
            if label == "inspect ephemeral base preparation":
                return canary.CommandResult(" M .sc-state/engine.ref\n M sc\n", "", 0)
            return canary.CommandResult("", "", 0)

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            run_labels.append(label)
            if label == "clone disposable dos-app":
                (self.facts.workspace / ".git").mkdir(parents=True)
            elif label == "install disposable fork":
                (self.facts.workspace / ".sc-state").mkdir()
                (self.facts.workspace / ".super-coder").mkdir(exist_ok=True)
                (self.facts.workspace / ".sc-state" / "engine.ref").write_text(
                    SHA + "\n"
                )
                (self.facts.workspace / ".super-coder" / "instance.json").write_text(
                    '{"installed_at":"2026-08-06"}\n'
                )
            elif label == "verify callable exact engine ref":
                return canary.CommandResult(SHA + "\n", "", 0)
            return canary.CommandResult("", "", 0)

        backend._git = fake_git  # type: ignore[method-assign]
        backend._run = fake_run  # type: ignore[method-assign]

        checkpoints: list[dict] = []
        backend.create_disposable(
            self.config,
            self.facts,
            ledger,
            lambda: checkpoints.append(canary.dataclasses.asdict(ledger)),
        )

        self.assertIn(
            (
                "fetch",
                "--no-tags",
                "subfloor-canary",
                f"{SHA}:refs/remotes/subfloor-canary/main",
            ),
            git_calls,
        )
        self.assertIn(
            (
                "checkout",
                "refs/remotes/subfloor-canary/main",
                "--",
                ".super-coder",
                "sc",
            ),
            git_calls,
        )
        self.assertIn(
            (
                "push",
                "origin",
                f"HEAD:refs/heads/{self.facts.base_branch}",
            ),
            git_calls,
        )
        self.assertIn(
            ("add", "--", ".sc-state/engine.ref", "sc"),
            git_calls,
        )
        self.assertTrue(
            any(
                "commit" in call and "prepare exact engine" in " ".join(call)
                for call in git_calls
            )
        )
        self.assertEqual(
            SHA, (self.facts.workspace / ".sc-state" / "engine.ref").read_text().strip()
        )
        self.assertIn("verify callable exact engine ref", run_labels)
        self.assertTrue(checkpoints)
        self.assertEqual(self.facts.workspace, Path(checkpoints[0]["workspace"]))

    def test_cleanup_is_idempotent_when_resources_are_already_absent(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        deadline.enter("cleanup")
        backend = canary.HostBackend(deadline)
        ledger = canary.ResourceLedger(
            workspace=str(self.facts.workspace),
            marker_written=True,
            base_branch=self.facts.base_branch,
            head_branch=self.facts.head_branch,
            repository=self.facts.repository,
            container=self.facts.container,
            network=self.facts.network,
        )

        def fake_git(repo, *args, label, check=True):
            return canary.CommandResult(
                "", "error: unable to delete: remote ref does not exist\n", 1
            )

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            if "container" in label:
                return canary.CommandResult("", "No such container\n", 1)
            return canary.CommandResult("", "network not found\n", 1)

        backend._git = fake_git  # type: ignore[method-assign]
        backend._run = fake_run  # type: ignore[method-assign]

        first = backend.cleanup(self.config, self.facts, ledger)
        second = backend.cleanup(self.config, self.facts, ledger)

        self.assertTrue(all(action["ok"] for action in first))
        self.assertEqual(first, second)
        self.assertEqual(
            [
                "delete_head_branch",
                "delete_base_branch",
                "remove_container",
                "remove_network",
                "remove_workspace_absent",
            ],
            [action["action"] for action in first],
        )

    def test_cleanup_refuses_a_pr_whose_remote_identity_does_not_match(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        deadline.enter("cleanup")
        backend = canary.HostBackend(deadline)
        ledger = canary.ResourceLedger(
            candidate_sha=SHA,
            base_branch=self.facts.base_branch,
            head_branch=self.facts.head_branch,
            repository=self.facts.repository,
            pull_request=77,
        )

        def fake_git(repo, *args, label, check=True):
            return canary.CommandResult(
                "", "error: unable to delete: remote ref does not exist\n", 1
            )

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            return canary.CommandResult(
                json.dumps(
                    {
                        "state": "OPEN",
                        "headRefName": "somebody-elses-branch",
                        "baseRefName": self.facts.base_branch,
                    }
                ),
                "",
                0,
            )

        backend._git = fake_git  # type: ignore[method-assign]
        backend._run = fake_run  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            canary.CanaryError, "cleanup did not complete"
        ) as raised:
            backend.cleanup(self.config, self.facts, ledger)

        self.assertIn("inspect_pr_identity", raised.exception.details["failed_actions"])

    def test_maintainer_command_is_not_distributed_or_added_as_sc_verb(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / ".super-coder" / "scripts" / "engine_manifest.py"
        manifest = manifest_path.read_text()
        dispatcher = (root / ".super-coder" / "scripts" / "dispatch.sh").read_text()
        tree = ast.parse(manifest, filename=str(manifest_path))
        engine_paths = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "ENGINE_PATHS"
                for target in node.targets
            )
        )
        relative = "maintainer/dos_app_sprint_canary.py"

        self.assertFalse(
            any(
                relative == entry or relative.startswith(entry.rstrip("/") + "/")
                for entry in engine_paths
            )
        )
        self.assertNotIn("dos-app-sprint-canary)", dispatcher)
        self.assertTrue((root / "maintainer" / "dos_app_sprint_canary.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
