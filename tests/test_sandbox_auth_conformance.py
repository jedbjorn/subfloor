"""Hermetic coverage for the live sandbox-auth conformance controller."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "maintainer" / "sandbox_auth_conformance.py"
SPEC = importlib.util.spec_from_file_location("sandbox_auth_conformance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
canary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = canary
SPEC.loader.exec_module(canary)


def complete_matrix() -> dict[str, dict[str, str]]:
    return {
        name: {"status": "passed", "proof": name} for name in canary.REQUIRED_MATRIX
    }


class FakeBackend:
    def __init__(
        self,
        *,
        matrix: dict[str, dict[str, str]] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.matrix = matrix or complete_matrix()
        self.failure = failure
        self.execute_calls = 0
        self.cleanup_calls = 0

    def execute(self, receipt, ledger):
        self.execute_calls += 1
        ledger.image = "disposable-image"
        ledger.agent_pid = 123
        ledger.agent_socket = "/tmp/disposable-agent.sock"
        ledger.dind_container = "disposable-dind"
        ledger.branches.append("subfloor-auth-canary/test-branch")
        ledger.denied_branches.append(
            {"repository": "cli/cli", "branch": "subfloor-auth-denied-test"}
        )
        ledger.pull_requests.append(991)
        receipt.data["candidate_sha"] = "a" * 40
        receipt.checkpoint(ledger)
        if self.failure is not None:
            raise self.failure
        return self.matrix

    def cleanup(self, ledger):
        self.cleanup_calls += 1
        actions = [
            {"resource": "pr:991", "removed": True},
            {"resource": "branch:test", "removed": True},
            {"resource": "branch:cli/cli:test", "removed": True},
            {"resource": "rootful_dind", "removed": True},
            {"resource": "canary_image", "removed": True},
            {"resource": "ssh_agent", "removed": True},
        ]
        ledger.pull_requests.clear()
        ledger.branches.clear()
        ledger.denied_branches.clear()
        ledger.dind_container = None
        ledger.image = None
        ledger.agent_pid = None
        ledger.agent_socket = None
        return actions


class SandboxAuthCanaryControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receipt = self.root / "receipt.json"
        self.config = canary.Config(
            source_repo=ROOT,
            repository="example/project",
            ssh_key=self.root / "id_ed25519",
            receipt=self.receipt,
            run_id="auth-test-001",
        )

    def read_receipt(self) -> dict:
        return json.loads(self.receipt.read_text())

    def test_complete_matrix_passes_and_cleanup_is_durable(self) -> None:
        backend = FakeBackend()

        result = canary.run(self.config, backend=backend)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["matrix"]), canary.REQUIRED_MATRIX)
        self.assertEqual(result["candidate_sha"], "a" * 40)
        self.assertTrue(result["cleanup"]["complete"])
        self.assertEqual(len(result["cleanup"]["actions"]), 6)
        self.assertEqual(backend.execute_calls, 1)
        self.assertEqual(backend.cleanup_calls, 1)
        durable = self.read_receipt()
        self.assertEqual(durable, result)
        self.assertEqual(durable["resources"]["branches"], [])
        self.assertEqual(durable["resources"]["denied_branches"], [])
        self.assertEqual(durable["resources"]["pull_requests"], [])

    def test_missing_case_fails_closed_after_cleanup(self) -> None:
        matrix = complete_matrix()
        matrix.pop("strict_host_trust")
        backend = FakeBackend(matrix=matrix)

        with self.assertRaisesRegex(canary.CanaryError, "matrix mismatch"):
            canary.run(self.config, backend=backend)

        durable = self.read_receipt()
        self.assertEqual(durable["status"], "failed")
        self.assertEqual(durable["failure"]["code"], "CANARY_MATRIX_INCOMPLETE")
        self.assertTrue(durable["cleanup"]["complete"])
        self.assertEqual(backend.cleanup_calls, 1)

    def test_failed_case_cannot_be_labeled_passed(self) -> None:
        matrix = complete_matrix()
        matrix["offline"] = {"status": "failed", "proof": "network was reachable"}
        backend = FakeBackend(matrix=matrix)

        with self.assertRaisesRegex(canary.CanaryError, "offline"):
            canary.run(self.config, backend=backend)

        durable = self.read_receipt()
        self.assertEqual(durable["failure"]["code"], "CANARY_MATRIX_FAILED")
        self.assertNotEqual(durable["status"], "passed")
        self.assertTrue(durable["cleanup"]["complete"])

    def test_failure_receipt_redacts_secret_and_preserves_stable_code(self) -> None:
        secret = "github_pat_this-value-must-never-survive"
        backend = FakeBackend(
            failure=canary.CanaryError(
                "CANARY_EXPECTED_FAILURE",
                f"token={secret}",
                stage="probe",
            )
        )

        with self.assertRaises(canary.CanaryError):
            canary.run(self.config, backend=backend)

        raw = self.receipt.read_text()
        durable = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertEqual(durable["failure"]["code"], "CANARY_EXPECTED_FAILURE")
        self.assertEqual(durable["failure"]["message"], "token=[REDACTED]")
        self.assertTrue(durable["cleanup"]["complete"])


class SandboxAuthCanaryContractTest(unittest.TestCase):
    def make_backend(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = canary.Config(
            source_repo=ROOT,
            repository="example/project",
            ssh_key=root / "id_ed25519",
            receipt=root / "receipt.json",
            run_id="auth-test-001",
        )
        return canary.HostBackend(config, root / "workspace")

    def test_required_matrix_pins_every_spec_case(self) -> None:
        self.assertEqual(
            canary.REQUIRED_MATRIX,
            {
                "ssh_oauth",
                "https_oauth",
                "explicit_scoped_token",
                "stale_explicit_fallback",
                "empty_candidate_fallback",
                "insufficient_push_access",
                "offline",
                "strict_host_trust",
                "selected_image_trust",
                "rootless_agent_access",
                "rootful_agent_access",
                "relaunch_refresh",
                "restart_refresh",
                "ssh_push_and_pr",
                "https_push_and_pr",
            },
        )

    def test_container_auth_argv_contains_names_and_socket_only(self) -> None:
        secret = "github_pat_not-in-argv"

        arguments = canary._container_auth_args(
            "/run/user/1000/agent.sock", user="1000:1000", token=True
        )

        self.assertEqual(arguments[:2], ("--user", "1000:1000"))
        self.assertIn("type=bind,src=/run/user/1000/agent.sock", " ".join(arguments))
        self.assertIn(("-e", "GH_TOKEN"), tuple(pairwise(arguments)))
        self.assertNotIn(secret, " ".join(arguments))
        self.assertNotIn(".ssh", " ".join(arguments))
        self.assertNotRegex(" ".join(arguments), r"id_(?:rsa|ed25519)")

    def test_no_token_or_socket_has_no_auth_forwarding(self) -> None:
        arguments = canary._container_auth_args(None, user="0:0", token=False)

        self.assertEqual(arguments, ("--user", "0:0"))
        self.assertNotIn("GH_TOKEN", arguments)
        self.assertNotIn("SSH_AUTH_SOCK", arguments)

    def test_ssh_failure_category_is_bounded_and_secret_free(self) -> None:
        secret = "github_pat_not-retained"
        result = canary.CommandResult(
            255,
            "",
            f"ssh: error connecting to agent; token={secret}",
        )

        category = canary._ssh_failure_category(result)

        self.assertEqual(category, "agent_unreachable")
        self.assertNotIn(secret, category)

    def test_redaction_covers_token_shapes_and_sensitive_mapping_keys(self) -> None:
        token = "gho_abcdefghijklmnopqrstuvwxyz"
        value = canary._safe_result(
            {
                "line": f"Authorization: bearer {token}",
                "nested": [{"token": token, "safe": "repository_unreachable"}],
            }
        )

        serialized = json.dumps(value)
        self.assertNotIn(token, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("repository_unreachable", serialized)

    def test_live_script_never_places_private_key_or_token_value_in_docker_argv(
        self,
    ) -> None:
        source = SCRIPT.read_text()

        self.assertNotIn("-e GH_TOKEN=", source)
        self.assertNotIn("-e SC_GH_TOKEN=", source)
        self.assertNotRegex(source, r"--mount[^\n]+(?:id_rsa|id_ed25519|/\.ssh)")
        self.assertIn('"-e", "GH_TOKEN"', source)
        self.assertIn('"SC_GH_TOKEN",', source)

    def test_rootful_dind_listens_on_its_unix_socket_only(self) -> None:
        source = inspect.getsource(canary.HostBackend._start_rootful_dind)

        self.assertIn(
            '"dockerd",\n            "--host=unix:///var/run/docker.sock"', source
        )
        self.assertNotIn("tcp://", source)
        self.assertNotIn("--tls=false", source)

    def test_rootful_agent_runs_as_uid_matched_user_and_records_socket_owner(
        self,
    ) -> None:
        backend = self.make_backend()
        ledger = canary.Ledger(
            image="disposable-image", dind_container="disposable-dind"
        )
        ownership = f"{os.getuid()}:{os.getgid()}:600"
        command_result = canary.CommandResult(
            1,
            ownership + "\n",
            "Hi canary! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )

        with patch.object(backend, "_run", return_value=command_result) as run:
            result = backend._rootful_agent(ledger)

        argv = run.call_args.args[0]
        self.assertIn(("--user", f"{os.getuid()}:{os.getgid()}"), tuple(pairwise(argv)))
        self.assertNotIn(("--user", "0:0"), tuple(pairwise(argv)))
        self.assertIn(
            "type=bind,src=/rootful-agent.sock,dst=/run/super-coder/ssh-agent,readonly",
            argv,
        )
        self.assertEqual(result["container_user"], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(result["socket_ownership"], ownership)
        self.assertEqual(
            result["ownership_model"],
            "uid_matched_container_via_operator_owned_unix_relay",
        )

    def test_selected_image_canary_verifies_current_and_rejects_stale(self) -> None:
        backend = self.make_backend()
        backend.workspace.mkdir()
        ledger = canary.Ledger(image="disposable-image")
        receipt = Mock()
        verified = SimpleNamespace(
            state="ready", reason="ssh_selected_image_trust_verified"
        )
        stale = SimpleNamespace(
            state="unavailable", reason="ssh_selected_image_trust_missing"
        )

        with (
            patch.object(
                backend.sandbox_auth,
                "verify_selected_image_trust",
                side_effect=(verified, stale),
            ) as verify,
            patch.object(
                backend,
                "_docker",
                return_value=canary.CommandResult(0, "", ""),
            ) as docker,
        ):
            result = backend._selected_image_trust(ledger, receipt)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["selected_image"], "verified")
        self.assertEqual(result["stale_image"], "rejected")
        self.assertEqual(result["stale_reason"], "ssh_selected_image_trust_missing")
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(ledger.stale_image, "subfloor-auth-canary-stale:auth-test-001")
        receipt.checkpoint.assert_called_once_with(ledger)
        self.assertIn("build", docker.call_args.args)

    def test_denied_target_branch_is_cleaned_even_after_rejected_delete(self) -> None:
        backend = self.make_backend()
        resource = {
            "repository": "cli/cli",
            "branch": "subfloor-auth-denied-auth-test-001",
        }
        ledger = canary.Ledger(denied_branches=[resource])

        with (
            patch.object(
                backend,
                "_run",
                return_value=canary.CommandResult(1, "", "not found"),
            ),
            patch.object(
                backend, "_remote_branch_exists", return_value=False
            ) as branch_exists,
        ):
            actions = backend.cleanup(ledger)

        self.assertEqual(
            actions,
            [
                {
                    "resource": ("branch:cli/cli:subfloor-auth-denied-auth-test-001"),
                    "removed": True,
                }
            ],
        )
        self.assertEqual(ledger.denied_branches, [])
        branch_exists.assert_called_once_with(
            "subfloor-auth-denied-auth-test-001",
            token=None,
            repository="cli/cli",
        )


if __name__ == "__main__":
    unittest.main()
