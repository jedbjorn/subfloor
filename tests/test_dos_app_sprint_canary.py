"""Hermetic coverage for the source-only dos-app Sprint canary."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [
    str(ROOT),
    str(ROOT / "tests"),
    str(ENGINE / "api"),
    str(ENGINE / "scripts"),
]

import mem
import server
import sprint_cli
from conversation_adapters import NativeTurn
from conversation_adapters.deepseek import DeepSeekAdapter
from test_sprint_v2_domain import apply_schema

from maintainer import dos_app_sprint_canary as canary

SHA = "a" * 40
BASE_SHA = "b" * 40


def route_admission_payload(
    *, category: str | None = None, **updates
) -> dict[str, object]:
    admitted = category is None
    payload: dict[str, object] = {
        "contract_version": 1,
        "requested_provider": "ollama-cloud",
        "requested_model": "deepseek-v4-pro:0813",
        "admitted": admitted,
        "error_code": (
            None
            if admitted
            else canary.ROUTE_ADMISSION_ERROR_CODES[category]
        ),
        "category": category,
        "required_surface": "sprint",
        "required_capability": "reviewer-shell-tool-execution",
        "freshness": "fresh",
        "authentication": "verified",
        "tool_capability": "supported" if admitted else "unknown",
        "exit_class": "success" if admitted else "route-rejected",
    }
    payload.update(updates)
    return payload


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
    ) -> dict[str, object]:
        self.calls.append("launch")
        if self.fail_at == "launch":
            raise canary.CanaryError("LAUNCH_TEST", "launch failed")
        if self.fail_at == "route_admission":
            raise canary.CanaryError(
                "CANARY_ROUTE_NOT_ADMITTED",
                "exact route rejected",
                details={
                    "route_admission": canary._route_admission_fallback(
                        "route-rejected"
                    )
                },
            )
        return {
            "versions": {"codex": "0.146.1", "kimi": "0.33.0"},
            "route_admission": None,
        }

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
            "force_new_barrier",
            "declare_and_arm",
            "force_new_pre_delivery",
            "force_new_delivery",
            "pickup_failure",
            "pickup_recovery",
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
                "force_new": {
                    "message_id": 81,
                    "inbox_absent": True,
                    "accept_rejected": True,
                    "accept_http_status": 409,
                    "decline_rejected": True,
                    "decline_http_status": 409,
                    "prior_conversation_id": "cv-review-qaqc",
                    "delivery_conversation_id": "cv-review-interrupted",
                    "fresh_chat": True,
                },
                "pickup_recovery": {
                    "induced": True,
                    "interrupted_run_id": 91,
                    "interrupted_conversation_id": "cv-review-interrupted",
                    "pause_reason": "wake_pickup_evidence_invalid",
                    "pause_event_id": 101,
                    "error_code": "WAKE_PICKUP_EVIDENCE_INVALID",
                    "failure_class": "evidence_invalid",
                    "attempt_count": 1,
                    "resume_changed": True,
                    "replacement_wake_id": 111,
                    "recovery_conversation_id": "cv-review-recovered",
                    "message_id": 81,
                    "final_disposition": "accepted",
                },
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


def gate_board(
    *,
    lifecycle: str = "armed",
    conversation_id: str | None = "cv-review-qaqc",
    review: str = "pending",
    column: str = "review",
    pickup: dict | None = None,
) -> dict:
    messages = []
    if review != "absent":
        messages.append(
            {
                "message_id": 680,
                "kind": "review_request",
                "disposition": review,
                "read_at": "2026-08-12 12:00:00" if review == "accepted" else None,
            }
        )
    return {
        "sprint": {"sprint_id": 12, "lifecycle": lifecycle},
        "participants": [
            {
                "role": "reviewer",
                "current_conversation_id": conversation_id,
            }
        ],
        "work_units": [
            {
                "work_unit_id": 55,
                "column": column,
                "messages": messages,
            }
        ],
        "pickup": pickup or {},
    }


class GateApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.barrier_active = True
        self.delivery_attempts: list[tuple[str, bool]] = []
        self.boards = iter(
            [
                gate_board(review="absent", column="dev"),
                gate_board(),
                gate_board(),
                gate_board(conversation_id=None),
                gate_board(conversation_id="cv-review-first"),
                gate_board(conversation_id="cv-review-first"),
                gate_board(conversation_id="cv-review-first"),
                gate_board(
                    lifecycle="paused",
                    conversation_id="cv-review-first",
                    pickup={
                        "action": "paused",
                        "pause_reason": "wake_pickup_evidence_invalid",
                    },
                ),
                gate_board(conversation_id="cv-review-first"),
                gate_board(
                    conversation_id="cv-review-recovered", review="accepted"
                ),
            ]
        )
        exhausted = {
            "event_id": 11,
            "type": "wake.pickup_exhausted",
            "details": {
                "message_id": 680,
                "conversation_id": "cv-review-first",
                "run_state": "cancelled",
                "error_code": "WAKE_PICKUP_EVIDENCE_INVALID",
                "failure_class": "evidence_invalid",
                "attempt_count": 1,
            },
        }
        self.events = iter(
            [
                {"items": [{"event_id": 10, "type": "review.requested", "details": {}}]},
                {"items": [exhausted]},
                {"items": [exhausted]},
                {
                    "items": [
                        exhausted,
                        {
                            "event_id": 12,
                            "type": "wake.requeued",
                            "details": {"replacement_wake_id": 44},
                        },
                    ]
                },
            ]
        )
        self.conversations = iter([{"state": "queued"}, {"state": "running"}])

    def attempt_delivery(self, boundary: str) -> None:
        self.delivery_attempts.append((boundary, self.barrier_active))

    def request(self, method, path, *, body=None, key=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/sprints/12":
            self.attempt_delivery("board_observation")
            return next(self.boards)
        if method == "GET" and path == "/api/conversations/cv-review-qaqc":
            return {"version": 7, "state": "idle"}
        if method == "PATCH" and path == "/api/conversations/cv-review-qaqc":
            self.closed_body = body
            return {"state": "closed"}
        if method == "GET" and path == "/api/conversations/cv-review-first":
            return next(self.conversations)
        if method == "POST" and path.endswith("/interruptions"):
            self.interruption_body = body
            self.interruption_key = key
            return {"run_id": 91}
        if method == "GET" and path == "/api/sprints/12/events?limit=100":
            return next(self.events)
        if method == "PATCH" and path == "/api/sprints/12":
            self.resume_body = body
            return {"changed": True, "sprint": {"lifecycle": "armed"}}
        raise AssertionError(f"unexpected API call: {method} {path}")


class GateBackend(canary.HostBackend):
    def _target_force_new_probe(
        self,
        facts,
        probe,
        *,
        sprint_id,
        message_id,
    ) -> None:
        self.probe = (probe.reviewer_id, sprint_id, message_id)
        self.gate_api.attempt_delivery("target_write")

    def _collect_force_new_probe(
        self,
        facts,
        probe,
        *,
        sprint_id,
        message_id,
    ):
        self.gate_api.attempt_delivery("proof_collection")
        return {
            "message_id": message_id,
            "inbox_absent": True,
            "accept_rejected": True,
            "accept_http_status": 409,
            "decline_rejected": True,
            "decline_http_status": 409,
        }

    def _close_force_new_barrier(self, api, probe) -> None:
        self.gate_api.attempt_delivery("before_close")
        self.gate_api.barrier_active = False
        self.closed_probe = probe.reviewer_id


class SameChatGateApi(GateApi):
    def __init__(self) -> None:
        super().__init__()
        self.boards = iter(
            [
                gate_board(review="absent", column="dev"),
                gate_board(),
                gate_board(),
                gate_board(conversation_id="cv-review-qaqc"),
            ]
        )


class MissingRecoveryGateApi(GateApi):
    def __init__(self) -> None:
        super().__init__()
        exhausted = {
            "event_id": 11,
            "type": "wake.pickup_exhausted",
            "details": {
                "message_id": 680,
                "conversation_id": "cv-review-first",
                "run_state": "cancelled",
                "error_code": "WAKE_PICKUP_EVIDENCE_INVALID",
                "failure_class": "evidence_invalid",
                "attempt_count": 1,
            },
        }
        self.events = iter(
            [
                {"items": [{"event_id": 10, "type": "review.requested", "details": {}}]},
                {"items": [exhausted]},
                {"items": [exhausted]},
                {"items": [exhausted]},
            ]
        )


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

    def explicit_parent(self) -> tuple[Path, canary.CanaryConfig]:
        home_root = self.root / "home"
        parent = home_root / "canary-anchor"
        parent.mkdir(parents=True, mode=0o700)
        parent.chmod(0o700)
        return home_root, dataclasses.replace(
            self.config,
            temp_parent=parent,
            temp_parent_explicit=True,
        )

    def test_explicit_temp_parent_resolves_one_exact_owned_child(self) -> None:
        home_root, config = self.explicit_parent()
        high_capacity = canary.MIN_FREE_BYTES + 1

        with (
            mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root),
            mock.patch.object(
                canary.shutil,
                "disk_usage",
                return_value=mock.Mock(free=high_capacity),
            ),
        ):
            parent, workspace = canary._validated_explicit_workspace(config)
            available = canary._require_disposable_capacity(parent)

        self.assertEqual(config.temp_parent, parent)
        self.assertEqual(high_capacity, available)
        self.assertEqual(parent, workspace.parent)
        self.assertEqual(
            f"{canary.WORKSPACE_PREFIX}{config.run_id}", workspace.name
        )
        self.assertFalse(workspace.exists())

    def test_explicit_temp_parent_rejects_low_capacity_before_commands(self) -> None:
        home_root, config = self.explicit_parent()
        backend = canary.HostBackend(canary.Deadline(100, 50))
        backend._run = mock.Mock(side_effect=AssertionError("command ran"))
        backend._git = mock.Mock(side_effect=AssertionError("git ran"))

        with (
            mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root),
            mock.patch.object(
                canary.shutil,
                "disk_usage",
                return_value=mock.Mock(free=canary.MIN_FREE_BYTES - 1),
            ),
            self.assertRaises(canary.CanaryError) as raised,
        ):
            backend.preflight(config)

        self.assertEqual("CANARY_CAPACITY_FAILED", raised.exception.code)
        backend._run.assert_not_called()
        backend._git.assert_not_called()

    def test_explicit_temp_parent_rejects_relative_traversal_and_symlinks(self) -> None:
        home_root, config = self.explicit_parent()
        real_parent = home_root / "real-anchor"
        real_parent.mkdir(mode=0o700)
        linked_parent = home_root / "linked-anchor"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        cases = {
            "relative": Path("relative-anchor"),
            "traversal": config.temp_parent / "..",
            "symlink": linked_parent,
        }

        with mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root):
            for label, parent in cases.items():
                with self.subTest(label=label), self.assertRaises(
                    canary.CanaryError
                ) as raised:
                    canary._validated_explicit_workspace(
                        dataclasses.replace(config, temp_parent=parent)
                    )
                self.assertEqual("CANARY_INPUT_INVALID", raised.exception.code)

    def test_explicit_temp_parent_rejects_unsafe_owner_and_mode(self) -> None:
        home_root, config = self.explicit_parent()
        with (
            mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root),
            mock.patch.object(canary.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(canary.CanaryError) as wrong_owner,
        ):
            canary._validated_explicit_workspace(config)
        self.assertEqual("CANARY_INPUT_INVALID", wrong_owner.exception.code)

        config.temp_parent.chmod(0o750)
        with (
            mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root),
            self.assertRaises(canary.CanaryError) as unsafe_mode,
        ):
            canary._validated_explicit_workspace(config)
        self.assertEqual("CANARY_INPUT_INVALID", unsafe_mode.exception.code)

    def test_explicit_temp_parent_rejects_protected_path_overlap(self) -> None:
        home_root, config = self.explicit_parent()
        workspace = config.temp_parent / (
            f"{canary.WORKSPACE_PREFIX}{config.run_id}"
        )
        cases = {
            "source": dataclasses.replace(config, source_repo=workspace / "source"),
            "foreign": dataclasses.replace(
                config, dos_app_repo=workspace / "dos-app"
            ),
            "receipt": dataclasses.replace(
                config, receipt_path=workspace / "receipt.json"
            ),
            "credential": dataclasses.replace(
                config, credential_file=workspace / "secret.key"
            ),
        }

        with mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root):
            for label, candidate in cases.items():
                with self.subTest(label=label), self.assertRaises(
                    canary.CanaryError
                ) as raised:
                    canary._validated_explicit_workspace(candidate)
                self.assertEqual("CANARY_INPUT_INVALID", raised.exception.code)

            with self.assertRaises(canary.CanaryError) as worktree_overlap:
                canary._validated_explicit_workspace(
                    config, protected_paths=[workspace / "nested-worktree"]
                )
        self.assertEqual("CANARY_INPUT_INVALID", worktree_overlap.exception.code)

    def test_explicit_temp_parent_rejects_exact_child_collision(self) -> None:
        home_root, config = self.explicit_parent()
        workspace = config.temp_parent / (
            f"{canary.WORKSPACE_PREFIX}{config.run_id}"
        )
        workspace.mkdir()

        with (
            mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root),
            self.assertRaises(canary.CanaryError) as raised,
        ):
            canary._validated_explicit_workspace(config)

        self.assertEqual("CANARY_COLLISION", raised.exception.code)

    def test_explicit_temp_cleanup_removes_only_identity_checked_child(self) -> None:
        home_root, config = self.explicit_parent()
        workspace = config.temp_parent / (
            f"{canary.WORKSPACE_PREFIX}{config.run_id}"
        )
        marker = workspace / ".git" / "subfloor-canary-marker.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"run_id": config.run_id, "candidate_sha": SHA}) + "\n"
        )
        sibling = config.temp_parent / "keep-sibling"
        sibling.mkdir()
        ledger = canary.ResourceLedger(workspace=str(workspace), candidate_sha=SHA)
        backend = canary.HostBackend(canary.Deadline(100, 50))

        with mock.patch.object(canary, "EXPLICIT_TEMP_ROOT", home_root):
            first = backend.cleanup(config, None, ledger)
            second = backend.cleanup(config, None, ledger)

        self.assertFalse(workspace.exists())
        self.assertTrue(config.temp_parent.is_dir())
        self.assertTrue(sibling.is_dir())
        self.assertIn(
            {"action": "remove_workspace", "ok": True, "returncode": 0}, first
        )
        self.assertIn(
            {"action": "remove_workspace_absent", "ok": True, "returncode": 0},
            second,
        )

    def test_temp_parent_cli_preserves_default_and_marks_explicit_selection(
        self,
    ) -> None:
        arguments = [
            "run",
            "--engine-ref",
            SHA,
            "--dos-app-repo",
            str(self.root / "dos-app"),
        ]
        default_config = canary.build_config(canary.parser().parse_args(arguments))
        explicit_config = canary.build_config(
            canary.parser().parse_args(
                [*arguments, "--temp-parent", str(self.root / "explicit")]
            )
        )

        self.assertFalse(default_config.temp_parent_explicit)
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), default_config.temp_parent)
        self.assertTrue(explicit_config.temp_parent_explicit)
        self.assertEqual(self.root / "explicit", explicit_config.temp_parent)

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
        self.assertEqual(
            {
                "message_id": 81,
                "inbox_absent": True,
                "accept_rejected": True,
                "accept_http_status": 409,
                "decline_rejected": True,
                "decline_http_status": 409,
                "prior_conversation_id": "cv-review-qaqc",
                "delivery_conversation_id": "cv-review-interrupted",
                "fresh_chat": True,
            },
            result["sprint"]["force_new"],
        )
        self.assertEqual(
            "accepted",
            result["sprint"]["pickup_recovery"]["final_disposition"],
        )
        self.assertEqual(
            "WAKE_PICKUP_EVIDENCE_INVALID",
            result["sprint"]["pickup_recovery"]["error_code"],
        )
        self.assertTrue(result["cleanup"]["complete"])
        self.assertEqual("cleanup", backend.calls[-1])
        self.assertIn(f"materialize:{SHA}", backend.calls)
        self.assertEqual(
            [
                "stage:planner_prepare",
                "stage:kimi_qaqc",
                "stage:force_new_barrier",
                "stage:declare_and_arm",
                "stage:force_new_pre_delivery",
                "stage:force_new_delivery",
                "stage:pickup_failure",
                "stage:pickup_recovery",
                "stage:sprint_execution",
            ],
            [item for item in backend.calls if item.startswith("stage:")],
        )

    def test_route_rejection_precedes_sprint_conversation_and_generation(self) -> None:
        backend = FakeBackend(self.facts, fail_at="route_admission")
        controller, _ = self.controller(backend)

        with self.assertRaises(canary.CanaryError) as raised:
            controller.run()

        self.assertEqual("CANARY_ROUTE_NOT_ADMITTED", raised.exception.code)
        self.assertEqual(
            ["preflight", f"materialize:{SHA}", "launch", "cleanup"],
            backend.calls,
        )
        payload = self.receipt()
        self.assertEqual("route_admission", payload["failure"]["primary"]["stage"])
        self.assertEqual({}, payload["sprint"])
        self.assertEqual({}, payload["pull_request"])
        self.assertTrue(payload["cleanup"]["complete"])

    def test_route_admission_validator_accepts_every_bounded_rejection(self) -> None:
        for category in sorted(canary.ROUTE_ADMISSION_CATEGORIES):
            with self.subTest(category=category):
                payload = route_admission_payload(category=category)
                result = canary._validated_route_admission(
                    canary.CommandResult(json.dumps(payload), "ignored raw", 2)
                )
                self.assertEqual(category, result["category"])
                self.assertFalse(result["admitted"])

    def test_route_admission_validator_distinguishes_tool_support_states(self) -> None:
        unsupported = route_admission_payload(
            category="tool-capability-unsupported",
            tool_capability="unsupported",
        )
        unproven = route_admission_payload(
            category="tool-capability-unproven",
            authentication="unproven",
            tool_capability="unproven",
        )

        self.assertEqual(
            "unsupported",
            canary._validated_route_admission(
                canary.CommandResult(json.dumps(unsupported), "", 2)
            )["tool_capability"],
        )
        self.assertEqual(
            "unproven",
            canary._validated_route_admission(
                canary.CommandResult(json.dumps(unproven), "", 2)
            )["tool_capability"],
        )

    def test_route_admission_validator_fails_closed_without_raw_output(self) -> None:
        cases = {
            "malformed": canary.CommandResult("not-json-private-body", "raw", 2),
            "unknown-key": canary.CommandResult(
                json.dumps({**route_admission_payload(), "raw": "private"}), "", 0
            ),
            "identity": canary.CommandResult(
                json.dumps(route_admission_payload(requested_model="nearby")), "", 0
            ),
            "stale-success": canary.CommandResult(
                json.dumps(route_admission_payload(freshness="stale")), "", 0
            ),
            "unknown-category": canary.CommandResult(
                json.dumps(
                    {
                        **route_admission_payload(category="unknown"),
                        "error_code": "ROUTE_ADMISSION_NOT_ALLOWLISTED",
                        "category": "not-allowlisted",
                    }
                ),
                "",
                2,
            ),
            "exit-mismatch": canary.CommandResult(
                json.dumps(route_admission_payload()), "", 7
            ),
        }
        for label, result in cases.items():
            with self.subTest(label=label), self.assertRaises(
                canary.CanaryError
            ) as raised:
                canary._validated_route_admission(result)
            serialized = json.dumps(raised.exception.details)
            self.assertNotIn(result.stdout, serialized)
            self.assertEqual(
                set(canary.ROUTE_ADMISSION_KEYS),
                set(raised.exception.details["route_admission"]),
            )

    def test_force_new_probe_requires_absence_and_both_direct_rejections(self) -> None:
        proof = {
            "sprint_id": 12,
            "message_id": 680,
            "inbox": {"returncode": 0, "parsed": True},
            "inbox_message_ids": [],
            "accept": {
                "returncode": 1,
                "http_status": 409,
                "not_delivered": True,
            },
            "decline": {
                "returncode": 1,
                "http_status": 409,
                "not_delivered": True,
            },
        }

        bounded = canary.HostBackend._validate_force_new_probe(
            proof, sprint_id=12, message_id=680
        )

        self.assertEqual(
            {
                "message_id": 680,
                "inbox_absent": True,
                "accept_rejected": True,
                "accept_http_status": 409,
                "decline_rejected": True,
                "decline_http_status": 409,
            },
            bounded,
        )

        invalid = {
            "message visible": {**proof, "inbox_message_ids": [680]},
            "inbox failed": {
                **proof,
                "inbox": {"returncode": 1, "parsed": False},
            },
            "accept succeeded": {
                **proof,
                "accept": {
                    "returncode": 0,
                    "http_status": None,
                    "not_delivered": False,
                },
            },
            "decline wrong failure": {
                **proof,
                "decline": {
                    "returncode": 1,
                    "http_status": 503,
                    "not_delivered": False,
                },
            },
            "identity mismatch": {**proof, "message_id": 681},
        }
        for label, payload in invalid.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                canary.CanaryError, "did not reject inbox acceptance"
            ) as raised:
                canary.HostBackend._validate_force_new_probe(
                    payload, sprint_id=12, message_id=680
                )
            self.assertEqual("CANARY_FORCE_NEW_GATE_FAILED", raised.exception.code)

    def test_pickup_event_retry_matches_only_the_exact_failed_conversation(self) -> None:
        first = {
            "items": [
                {
                    "event_id": 40,
                    "type": "wake.pickup_exhausted",
                    "details": {
                        "message_id": 680,
                        "conversation_id": "cv-old",
                    },
                }
            ]
        }
        self.assertIsNone(
            canary.HostBackend._event_after(
                first,
                event_type="wake.pickup_exhausted",
                after_event_id=40,
                message_id=680,
                conversation_id="cv-failed",
            )
        )

        retried = {
            "items": [
                *first["items"],
                {
                    "event_id": 41,
                    "type": "wake.pickup_exhausted",
                    "details": {
                        "message_id": 680,
                        "conversation_id": "cv-other",
                    },
                },
                {
                    "event_id": 42,
                    "type": "wake.pickup_exhausted",
                    "details": {
                        "message_id": 680,
                        "conversation_id": "cv-failed",
                        "run_state": "cancelled",
                        "error_code": "WAKE_PICKUP_EVIDENCE_INVALID",
                    },
                },
            ]
        }
        matched = canary.HostBackend._event_after(
            retried,
            event_type="wake.pickup_exhausted",
            after_event_id=40,
            message_id=680,
            conversation_id="cv-failed",
        )

        self.assertEqual(42, matched["event_id"])
        self.assertEqual(
            "WAKE_PICKUP_EVIDENCE_INVALID", matched["details"]["error_code"]
        )

    def test_review_delivery_gate_retries_then_resumes_through_public_surfaces(
        self,
    ) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = GateBackend(deadline, sleep=clock.advance)
        api = GateApi()
        backend.gate_api = api
        stages: list[str] = []
        probe = canary.ForceNewProbe(
            reviewer_id="cv-review-qaqc",
            target_path="/tmp/target",
            proof_path="/tmp/proof",
        )

        evidence, observed = backend._exercise_review_delivery_gates(
            cast(canary.JsonHttp, api),
            self.config,
            self.facts,
            sprint_id=12,
            probe=probe,
            stage=lambda name: (deadline.enter(name), stages.append(name)),
        )

        self.assertEqual(
            [
                "force_new_pre_delivery",
                "force_new_delivery",
                "pickup_failure",
                "pickup_recovery",
            ],
            stages,
        )
        self.assertEqual(["dev", "review"], observed)
        self.assertEqual(("cv-review-qaqc", 12, 680), backend.probe)
        self.assertEqual("cv-review-qaqc", backend.closed_probe)
        self.assertEqual(
            [
                ("board_observation", True),
                ("board_observation", True),
                ("target_write", True),
                ("proof_collection", True),
                ("board_observation", True),
                ("before_close", True),
            ],
            api.delivery_attempts[:6],
        )
        self.assertEqual({}, api.interruption_body)
        self.assertEqual(
            "unit-test-0001:reviewer:pickup-interrupt", api.interruption_key
        )
        self.assertEqual(
            {
                "lifecycle": "armed",
                "reason": "exact-ref canary pickup interruption repaired",
            },
            api.resume_body,
        )
        self.assertEqual(
            1,
            sum(
                method == "POST" and path.endswith("/interruptions")
                for method, path in api.calls
            ),
        )
        self.assertEqual(
            1,
            sum(
                method == "PATCH" and path == "/api/sprints/12"
                for method, path in api.calls
            ),
        )
        self.assertTrue(evidence["force_new"]["fresh_chat"])
        self.assertEqual(
            "cv-review-recovered",
            evidence["pickup_recovery"]["recovery_conversation_id"],
        )
        self.assertEqual(
            44, evidence["pickup_recovery"]["replacement_wake_id"]
        )

    def test_force_new_barrier_is_running_before_orchestration_continues(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = canary.HostBackend(deadline, sleep=clock.advance)

        class StartApi:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.states = iter(["queued", "running"])

            def request(self, method, path, *, body=None, key=None):
                self.calls.append((method, path))
                if method == "POST" and path.endswith("/messages"):
                    self.body = body
                    self.key = key
                    return {"message": {"state": "queued"}}
                if method == "GET" and path == "/api/conversations/cv-review-qaqc":
                    return {"state": next(self.states)}
                raise AssertionError(f"unexpected API call: {method} {path}")

        api = StartApi()
        probe = backend._start_force_new_barrier(
            cast(canary.JsonHttp, api),
            self.config,
            reviewer_id="cv-review-qaqc",
        )

        self.assertEqual("cv-review-qaqc", probe.reviewer_id)
        self.assertEqual(
            [
                ("POST", "/api/conversations/cv-review-qaqc/messages"),
                ("GET", "/api/conversations/cv-review-qaqc"),
                ("GET", "/api/conversations/cv-review-qaqc"),
            ],
            api.calls,
        )
        self.assertIn("wait_for(TARGET)", api.body["text"])
        self.assertIn("controller did not close", api.body["text"])

    def test_force_new_barrier_closes_while_running_and_fails_closed(self) -> None:
        deadline = canary.Deadline(100, 50, clock=FakeClock())
        backend = canary.HostBackend(deadline, sleep=lambda _: None)
        probe = canary.ForceNewProbe(
            "cv-review-qaqc", "/tmp/target", "/tmp/proof"
        )

        class CloseApi:
            def __init__(self, state="running", closed_state="closed") -> None:
                self.state = state
                self.closed_state = closed_state
                self.calls = []

            def request(self, method, path, *, body=None, key=None):
                self.calls.append((method, path, body))
                if method == "GET":
                    return {"state": self.state, "version": 9}
                if method == "PATCH":
                    return {"state": self.closed_state}
                raise AssertionError(f"unexpected API call: {method} {path}")

        api = CloseApi()
        backend._close_force_new_barrier(cast(canary.JsonHttp, api), probe)
        self.assertEqual(
            {
                "version": 9,
                "state": "closed",
            },
            api.calls[-1][2],
        )

        for label, failed_api in {
            "control turn stopped": CloseApi(state="idle"),
            "close not durable": CloseApi(closed_state="running"),
        }.items():
            with self.subTest(label=label), self.assertRaises(
                canary.CanaryError
            ) as raised:
                backend._close_force_new_barrier(
                    cast(canary.JsonHttp, failed_api), probe
                )
            self.assertEqual("CANARY_FORCE_NEW_BARRIER_FAILED", raised.exception.code)

    def test_force_new_gate_rejects_same_chat_delivery(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = GateBackend(deadline, sleep=clock.advance)
        api = SameChatGateApi()
        backend.gate_api = api
        probe = canary.ForceNewProbe(
            "cv-review-qaqc", "/tmp/target", "/tmp/proof"
        )

        with self.assertRaisesRegex(
            canary.CanaryError, "retained the pre-delivery Reviewer chat"
        ) as raised:
            backend._exercise_review_delivery_gates(
                cast(canary.JsonHttp, api),
                self.config,
                self.facts,
                sprint_id=12,
                probe=probe,
                stage=lambda name: deadline.enter(name),
            )

        self.assertEqual("CANARY_FORCE_NEW_DELIVERY_FAILED", raised.exception.code)
        self.assertTrue(all(active for _, active in api.delivery_attempts[:6]))

    def test_pickup_gate_rejects_acceptance_without_requeue_evidence(self) -> None:
        clock = FakeClock()
        deadline = canary.Deadline(100, 50, clock=clock)
        backend = GateBackend(deadline, sleep=clock.advance)
        api = MissingRecoveryGateApi()
        backend.gate_api = api
        probe = canary.ForceNewProbe(
            "cv-review-qaqc", "/tmp/target", "/tmp/proof"
        )

        with self.assertRaisesRegex(
            canary.CanaryError, "without durable requeue evidence"
        ) as raised:
            backend._exercise_review_delivery_gates(
                cast(canary.JsonHttp, api),
                self.config,
                self.facts,
                sprint_id=12,
                probe=probe,
                stage=lambda name: deadline.enter(name),
            )

        self.assertEqual("CANARY_PICKUP_RECOVERY_FAILED", raised.exception.code)

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
            def __init__(self) -> None:
                self.body = None

            def request(self, method, path, *, body=None, key=None):
                self.body = body
                return {
                    "conversation_id": "cv-nested",
                    "route": {
                        "harness": "codex",
                        "provider": "openai",
                        "model": "gpt-test",
                        "effort": "high",
                    },
                }

        api = FakeApi()
        projected = backend._create_conversation(
            cast(canary.JsonHttp, api),
            shell_id=1,
            harness="codex",
            key="nested-route",
        )

        self.assertNotIn("model", projected)
        self.assertEqual("gpt-test", projected["route"]["model"])
        self.assertEqual({"shell_id": 1, "harness": "codex"}, api.body)

    def test_deepseek_conversation_binds_the_admitted_exact_route(self) -> None:
        backend = canary.HostBackend(canary.Deadline(100, 50))

        class FakeApi:
            def request(self, method, path, *, body=None, key=None):
                self.body = body
                return {
                    "conversation_id": "cv-deepseek",
                    "route": {
                        "harness": "deepseek",
                        "provider": "ollama-cloud",
                        "model": canary.DEEPSEEK_MODEL,
                        "effort": "default",
                    },
                }

        api = FakeApi()
        projected = backend._create_conversation(
            cast(canary.JsonHttp, api),
            shell_id=2,
            harness="deepseek",
            model=canary.DEEPSEEK_MODEL,
            effort="default",
            key="deepseek-route",
        )

        self.assertEqual(
            {
                "shell_id": 2,
                "harness": "deepseek",
                "model": canary.DEEPSEEK_MODEL,
                "effort": "default",
            },
            api.body,
        )
        self.assertEqual(canary.DEEPSEEK_MODEL, projected["route"]["model"])

    def test_launch_refreshes_routes_before_reading_provider_credential(self) -> None:
        backend = canary.HostBackend(
            canary.Deadline(100, 50), sleep=lambda _: None
        )
        config = dataclasses.replace(
            self.config,
            profile=canary.DEEPSEEK_SPRINT_PROFILE,
            credential_file=self.root / "authorized-provider.key",
        )
        events: list[str] = []
        run_envs: dict[str, dict[str, str]] = {}
        run_argv: dict[str, tuple[str, ...]] = {}
        run_checks: dict[str, bool] = {}

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            events.append(label)
            run_envs[label] = dict(env or {})
            run_argv[label] = tuple(argv)
            run_checks[label] = check
            if label.startswith("resolve exact-image"):
                return canary.CommandResult('{"ok": true}', "", 0)
            if label.startswith("resolve admitted"):
                return canary.CommandResult(
                    json.dumps(route_admission_payload()), "", 0
                )
            if label == "verify non-secret route probe stopped":
                return canary.CommandResult("", "not found", 1)
            if label == "inspect launched harness versions":
                return canary.CommandResult(
                    "codex codex-cli-test\ndeepseek deepseek-test\n", "", 0
                )
            return canary.CommandResult("", "", 0)

        def fake_read(_path):
            events.append("read provider credential")
            return "provider-canary-secret-value"

        backend._run = fake_run  # type: ignore[method-assign]
        backend._read_provider_key = fake_read  # type: ignore[method-assign]
        health = mock.Mock()
        health.request.return_value = {"status": "ok"}

        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "unrelated-secret",
                "ANTHROPIC_API_KEY": "unrelated-secret",
            },
        ), mock.patch.object(canary, "JsonHttp", return_value=health):
            launch_result = backend.launch(
                config, self.facts, canary.ResourceLedger()
            )

        self.assertEqual(
            [
                "launch non-secret route probe",
                "refresh exact-image model routes",
                "resolve exact-image codex route",
                "stop non-secret route probe",
                "verify non-secret route probe stopped",
                "read provider credential",
                "launch isolated runtime",
                "refresh admitted deepseek route",
                "resolve admitted deepseek route",
                "inspect launched harness versions",
            ],
            events,
        )
        refresh_env = run_envs["launch non-secret route probe"]
        self.assertTrue(
            canary.PROVIDER_CREDENTIAL_ENV.isdisjoint(refresh_env)
        )
        launch_env = run_envs["launch isolated runtime"]
        self.assertEqual(
            {"OLLAMA_API_KEY"},
            canary.PROVIDER_CREDENTIAL_ENV.intersection(launch_env),
        )
        self.assertNotIn(
            "provider-canary-secret-value",
            run_argv["refresh admitted deepseek route"],
        )
        self.assertIn(
            "--sprint-admission-json",
            run_argv["resolve admitted deepseek route"],
        )
        self.assertFalse(run_checks["refresh admitted deepseek route"])
        self.assertFalse(run_checks["resolve admitted deepseek route"])
        self.assertEqual(
            {"codex": "codex-cli-test", "deepseek": "deepseek-test"},
            launch_result["versions"],
        )

    def test_dispatch_forwards_ollama_value_without_putting_it_in_argv(self) -> None:
        dispatcher = (
            ROOT / ".super-coder" / "scripts" / "dispatch.sh"
        ).read_text()

        self.assertIn(
            'ollama_env="-e OLLAMA_API_KEY"', dispatcher
        )
        self.assertIn("$mistral_env $ollama_env $pg_env", dispatcher)
        self.assertNotIn(
            'ollama_env="-e OLLAMA_API_KEY=${OLLAMA_API_KEY}"', dispatcher
        )

    def test_route_probe_failure_prevents_provider_credential_read(self) -> None:
        backend = canary.HostBackend(
            canary.Deadline(100, 50), sleep=lambda _: None
        )
        config = dataclasses.replace(
            self.config,
            profile=canary.DEEPSEEK_SPRINT_PROFILE,
            credential_file=self.root / "authorized-provider.key",
        )

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            if label == "refresh exact-image model routes":
                raise canary.CanaryError(
                    "CANARY_COMMAND_FAILED", "route refresh failed"
                )
            return canary.CommandResult("", "", 0)

        backend._run = fake_run  # type: ignore[method-assign]
        backend._read_provider_key = mock.Mock()  # type: ignore[method-assign]
        health = mock.Mock()
        health.request.return_value = {"status": "ok"}

        with mock.patch.object(canary, "JsonHttp", return_value=health), \
                self.assertRaisesRegex(canary.CanaryError, "route refresh failed"):
            backend.launch(config, self.facts, canary.ResourceLedger())

        backend._read_provider_key.assert_not_called()

    def test_participant_failure_retains_only_bounded_error_evidence(self) -> None:
        backend = canary.HostBackend(
            canary.Deadline(100, 50), sleep=lambda _: None
        )
        conversation_id = "cv_" + "1" * 32

        class ErrorApi:
            def request(self, method, path, *, body=None, key=None):
                return {"state": "error"}

        raw_detail = json.dumps(
            {
                "schema_version": 1,
                "source": "provider",
                "phase": "provider-response",
                "category": "provider-unavailable",
                "upstream_code": "TIMEOUT",
                "http_status": None,
                "provider_request_observed": True,
                "provider_exact": True,
                "model_exact": True,
                "reserved_default_omitted": True,
                "shell_tool_declared": True,
                "purpose": "conversation",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        backend._run = mock.Mock(
            return_value=canary.CommandResult(
                json.dumps(
                    [
                        {
                            "state": "failed",
                            "error_code": "HARNESS_RUNTIME_FAILED",
                            "error_detail": raw_detail,
                        }
                    ]
                ),
                "",
                0,
            )
        )

        with self.assertRaisesRegex(
            canary.CanaryError, "terminalized before completing"
        ) as raised:
            backend._wait_idle(
                cast(canary.JsonHttp, ErrorApi()),
                conversation_id,
                self.config,
                self.facts,
            )

        failure = raised.exception.details["failure"]
        self.assertEqual("HARNESS_RUNTIME_FAILED", failure["error_code"])
        self.assertEqual("provider-unavailable", failure["category"])
        self.assertEqual("provider", failure["source"])
        self.assertTrue(failure["shell_tool_declared"])
        encoded = json.dumps(raised.exception.details)
        self.assertNotIn("error_detail", encoded)
        self.assertNotIn("detail_sha256", encoded)

    def test_participant_failure_classifies_a_stable_native_error_code(self) -> None:
        backend = canary.HostBackend(
            canary.Deadline(100, 50), sleep=lambda _: None
        )
        backend._run = mock.Mock(
            return_value=canary.CommandResult(
                json.dumps(
                    [
                        {
                            "state": "failed",
                            "error_code": "HARNESS_NATIVE_RUN_INVALID_CREDENTIAL",
                        "error_detail": json.dumps(
                            {
                                "schema_version": 1,
                                "source": "provider",
                                "phase": "provider-response",
                                "category": "authentication",
                                "upstream_code": "INVALID_CREDENTIAL",
                                "http_status": 401,
                                "provider_request_observed": True,
                                "provider_exact": True,
                                "model_exact": True,
                                "reserved_default_omitted": True,
                                "shell_tool_declared": True,
                                "purpose": "conversation",
                            }
                        ),
                        }
                    ]
                ),
                "",
                0,
            )
        )

        evidence = backend._conversation_failure_evidence(
            self.facts, "cv_" + "1" * 32
        )

        self.assertEqual(
            "HARNESS_NATIVE_RUN_INVALID_CREDENTIAL", evidence["error_code"]
        )
        self.assertEqual("authentication", evidence["category"])
        self.assertEqual(401, evidence["http_status"])

    def test_participant_failure_rejects_unstructured_or_extra_evidence(self) -> None:
        backend = canary.HostBackend(
            canary.Deadline(100, 50), sleep=lambda _: None
        )
        for detail, diagnostic in (
            ("provider raw body", "structured_evidence_invalid"),
            (
                json.dumps({
                    "schema_version": 1,
                    "source": "provider",
                    "phase": "provider-response",
                    "category": "unknown",
                    "secret": "must-not-survive",
                }),
                "structured_evidence_mismatch",
            ),
        ):
            with self.subTest(diagnostic=diagnostic):
                backend._run = mock.Mock(
                    return_value=canary.CommandResult(
                        json.dumps([{
                            "state": "failed",
                            "error_code": "HARNESS_NATIVE_RUN_FAILED",
                            "error_detail": detail,
                        }]),
                        "",
                        0,
                    )
                )
                evidence = backend._conversation_failure_evidence(
                    self.facts, "cv_" + "1" * 32
                )
                self.assertEqual(diagnostic, evidence["diagnostic"])
                self.assertNotIn("secret", json.dumps(evidence))

    def test_qaqc_prompt_loads_the_predeclaration_reviewer_skill(self) -> None:
        prompt = canary.HostBackend._qaqc_reviewer_prompt(117)

        self.assertIn("Load sprint_rev", prompt)
        self.assertIn("pre-declaration QA/QC path", prompt)
        self.assertIn("there is no Sprint id or Sprint inbox yet", prompt)
        self.assertIn(
            "./sc sprint record-qaqc --document 117 --verdict pass", prompt
        )
        self.assertIn("retry the exact command", prompt)
        self.assertIn("stop only after confirmation", prompt)
        self.assertNotIn("sc sprint inbox --sprint", prompt)

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
                canary.ENGINE_REMOTE,
                f"{SHA}:refs/remotes/{canary.ENGINE_REMOTE}/main",
            ),
            git_calls,
        )
        self.assertIn(
            (
                "checkout",
                f"refs/remotes/{canary.ENGINE_REMOTE}/main",
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

    def test_local_exact_source_install_replaces_a_stale_pin(self) -> None:
        source = self.root / "candidate-cache"
        dos_app = self.root / "host-project"
        home = self.root / "home"
        source.mkdir()
        dos_app.mkdir()
        home.mkdir()
        shutil.copytree(
            ROOT / ".super-coder",
            source / ".super-coder",
            ignore=shutil.ignore_patterns(
                "shell_db.db*",
                "instance.json",
                "run",
                "logs",
                "__pycache__",
            ),
        )
        shutil.copy2(ROOT / "sc", source / "sc")
        (source / "README.md").write_text("# isolated engine candidate\n")
        stale_sha = "c" * 40
        (dos_app / ".sc-state").mkdir()
        (dos_app / ".sc-state" / "engine.ref").write_text(stale_sha + "\n")
        (dos_app / ".github" / "workflows").mkdir(parents=True)
        (dos_app / ".github" / "workflows" / "existing.yml").write_text("name: existing\n")
        (dos_app / "shared" / "redlines").mkdir(parents=True)
        (dos_app / "shared" / ".gitkeep").write_text("")
        (dos_app / "shared" / "redlines" / ".gitkeep").write_text("")
        (dos_app / "README.md").write_text("# disposable host project\n")

        def git(repo: Path, *args: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return completed.stdout.strip()

        for repo in (source, dos_app):
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Canary Install Test")
            git(repo, "config", "user.email", "canary-install@noreply.local")
            git(repo, "config", "maintenance.auto", "false")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "fixture")

        candidate_sha = git(source, "rev-parse", "HEAD")
        base_sha = git(dos_app, "rev-parse", "HEAD")
        self.assertNotIn("subfloor", str(source))
        self.assertNotIn("super-coder", str(source))
        self.assertNotIn("subfloor", str(dos_app))
        self.assertNotIn("super-coder", str(dos_app))

        workspace = self.root / f"{canary.WORKSPACE_PREFIX}local-install"
        config = canary.CanaryConfig(
            source_repo=source,
            engine_ref=candidate_sha,
            dos_app_repo=dos_app,
            dos_app_ref=base_sha,
            repository="acme/host-project",
            receipt_path=self.receipt_path,
            temp_parent=self.root,
            run_id="local-install",
            stage_timeout_s=120,
            whole_timeout_s=180,
            poll_interval_s=0.01,
        )
        facts = canary.Preflight(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            repository="acme/host-project",
            remote_url=str(dos_app.resolve()),
            workspace=workspace,
            base_branch=f"{canary.REMOTE_PREFIX}/local-install/base",
            head_branch=f"{canary.REMOTE_PREFIX}/local-install/head",
            container="unused-local-install",
            network="unused-local-install",
            api_port=8883,
            dev_port=8884,
            github_remaining=4999,
        )
        deadline = canary.Deadline(180, 120)
        deadline.enter("materialize")
        backend = canary.HostBackend(deadline)
        ledger = canary.ResourceLedger()
        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            try:
                backend.create_disposable(config, facts, ledger, lambda: None)
            except canary.CanaryError as exc:
                self.fail(f"{exc.code}: {exc.message}; details={exc.details}")
            callable_ref = subprocess.run(
                [str(workspace / "sc"), "engine-ref"],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        installed_ref = (workspace / ".sc-state" / "engine.ref").read_text().strip()
        self.assertEqual(candidate_sha, installed_ref)
        self.assertEqual(candidate_sha, callable_ref)
        self.assertNotEqual(stale_sha, installed_ref)
        self.assertNotEqual(stale_sha, callable_ref)

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

    def test_deepseek_credential_is_scoped_without_mutating_or_persisting_source(
        self,
    ) -> None:
        credential = self.root / "authorized-provider.key"
        credential.write_text("provider-canary-secret-value\n")
        credential.chmod(0o600)
        before = credential.read_bytes()
        backend = canary.HostBackend(canary.Deadline(100, 50))

        backend._provider_key = backend._read_provider_key(credential)
        env = backend._runtime_env(self.facts)
        config = dataclasses.replace(
            self.config,
            profile=canary.DEEPSEEK_SPRINT_PROFILE,
            credential_file=credential,
        )
        receipt = canary.Receipt(self.receipt_path, config).data

        self.assertEqual(before, credential.read_bytes())
        self.assertEqual(0o600, credential.stat().st_mode & 0o777)
        self.assertEqual("provider-canary-secret-value", env["OLLAMA_API_KEY"])
        self.assertTrue(canary.PROVIDER_CREDENTIAL_ENV.isdisjoint(
            set(env) - {"OLLAMA_API_KEY"}
        ))
        encoded = json.dumps(receipt, sort_keys=True)
        self.assertNotIn(str(credential), encoded)
        self.assertNotIn("provider-canary-secret-value", encoded)
        self.assertNotIn("OLLAMA_API_KEY", encoded)
        self.assertTrue(credential.is_file())

    def test_deepseek_credential_rejects_unsafe_mode_without_mutation(self) -> None:
        credential = self.root / "unsafe-provider.key"
        credential.write_text("provider-canary-secret-value\n")
        credential.chmod(0o640)
        before = credential.read_bytes()
        backend = canary.HostBackend(canary.Deadline(100, 50))

        with self.assertRaisesRegex(canary.CanaryError, "failed ownership, mode"):
            backend._read_provider_key(credential)

        self.assertEqual(before, credential.read_bytes())
        self.assertEqual(0o640, credential.stat().st_mode & 0o777)
        self.assertTrue(credential.is_file())

    def test_deepseek_participant_evidence_requires_skills_tools_and_handoff(
        self,
    ) -> None:
        backend = canary.HostBackend(canary.Deadline(100, 50))
        board = {
            "participants": [
                {
                    "role": "developer",
                    "harness": "deepseek",
                    "model": canary.DEEPSEEK_MODEL,
                    "effort": "default",
                    "current_conversation_id": "cv_" + "1" * 32,
                },
                {
                    "role": "reviewer",
                    "harness": "deepseek",
                    "model": canary.DEEPSEEK_MODEL,
                    "effort": "default",
                    "current_conversation_id": "cv_" + "2" * 32,
                },
            ]
        }

        def conversation(_facts, conversation_id):
            developer = conversation_id.endswith("1" * 32)
            return {
                "conversation_id": conversation_id,
                "boot_sha256": "b" * 64,
                "boot_bytes": 2048,
                "has_sprint_dev": developer,
                "has_sprint_rev": not developer,
                "tool_started": 3,
                "tool_completed": 3,
            }

        backend._conversation_evidence = mock.Mock(side_effect=conversation)
        backend._run = mock.Mock(
            return_value=canary.CommandResult('[{"handoffs":1}]', "", 0)
        )

        evidence = backend._deepseek_participant_evidence(self.facts, 9, board)

        self.assertEqual(1, evidence["developer_handoffs"])
        self.assertTrue(evidence["developer"]["role_skill_loaded"])
        self.assertTrue(evidence["reviewer"]["role_skill_loaded"])
        self.assertEqual(3, evidence["developer"]["tool_completed"])
        self.assertEqual(
            canary.DEEPSEEK_MODEL, evidence["reviewer"]["route"]["model"]
        )

        missing_tools = conversation(self.facts, "cv_" + "1" * 32)
        missing_tools["tool_completed"] = 0
        backend._conversation_evidence = mock.Mock(
            side_effect=[missing_tools, conversation(self.facts, "cv_" + "2" * 32)]
        )
        with self.assertRaisesRegex(canary.CanaryError, "completed-tool evidence"):
            backend._deepseek_participant_evidence(self.facts, 9, board)

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


class DeepSeekQaqcActionRehearsalTest(unittest.TestCase):
    """Secret-free rehearsal of the production Reviewer action boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "disposable"
        self.workspace.mkdir()
        self.reviewer_worktree = self.workspace / ".sc-worktrees" / "rev1"
        self.reviewer_worktree.mkdir(parents=True)
        (self.workspace / ".sc-state").mkdir()
        skill_target = (
            self.workspace
            / ".super-coder"
            / "assets"
            / "skills"
            / "sprint_rev"
        )
        skill_target.mkdir(parents=True)
        shutil.copy2(
            ENGINE / "assets" / "skills" / "sprint_rev" / "SKILL.md",
            skill_target / "SKILL.md",
        )
        composition_target = (
            self.workspace / ".super-coder" / "assets" / "deepseek"
        )
        composition_target.mkdir(parents=True)
        shutil.copy2(
            ENGINE / "assets" / "deepseek" / "cordis-ollama-cloud.yml",
            composition_target / "cordis-ollama-cloud.yml",
        )
        self.candidate_sha = "c" * 40
        (self.workspace / ".sc-state" / "engine.ref").write_text(
            self.candidate_sha + "\n"
        )
        (self.workspace / "sc").write_text("#!/bin/sh\nexit 0\n")
        (self.workspace / "sc").chmod(0o755)

        self.db = self.root / "shell.db"
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            apply_schema(con)
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'owner')")
            con.executemany(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
                "VALUES (?,?,?,?,?,1,?)",
                (
                    (1, "Planner", "PLN1", "planner", "planner", "planner-token"),
                    (2, "Reviewer", "REV1", "reviewer", "reviewer", "review-token"),
                    (3, "Developer", "DEV1", "dev", "developer", "dev-token"),
                ),
            )
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Disposable QAQC rehearsal','in_progress')"
                ).lastrowid
            )
            self.spec_body = "# Disposable spec\n\nCreate one deterministic file.\n"
            self.document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Disposable spec',?)",
                    (feature_id, self.spec_body),
                ).lastrowid
            )
            revision = hashlib.sha256(self.spec_body.encode()).hexdigest()
            self.sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                    "VALUES (?,1,1)",
                    (feature_id,),
                ).lastrowid
            )
            con.execute(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,approval_id,"
                "bound_revision_body) VALUES (?,?,?,NULL,?)",
                (self.sprint_id, self.document_id, revision, self.spec_body),
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,model,effort) "
                "VALUES (?,?,?,?,?,?)",
                (
                    (self.sprint_id, 1, "planner", "codex", None, None),
                    (
                        self.sprint_id,
                        2,
                        "reviewer",
                        "deepseek",
                        canary.DEEPSEEK_MODEL,
                        "default",
                    ),
                    (
                        self.sprint_id,
                        3,
                        "developer",
                        "deepseek",
                        canary.DEEPSEEK_MODEL,
                        "default",
                    ),
                ),
            )
            self.reviewer_participant_id = int(
                con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=2",
                    (self.sprint_id,),
                ).fetchone()[0]
            )
            self.assignment_generation = str(
                con.execute(
                    "SELECT conversation_generation FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0]
            )
            self.conversation_id = "cv_" + "d" * 32
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
                "effort,worktree,state,creation_idempotency_key,creation_request_hash) "
                "VALUES (?,2,1,'deepseek','ollama-cloud',?,'default',?,'running',?,?)",
                (
                    self.conversation_id,
                    canary.DEEPSEEK_MODEL,
                    str(self.reviewer_worktree),
                    "qaqc-rehearsal",
                    "r" * 64,
                ),
            )
            boot = (
                "# Reviewer\n\nRole: Reviewer\n\n"
                + (ENGINE / "assets" / "skills" / "sprint_rev" / "SKILL.md").read_text()
            )
            con.execute(
                "INSERT INTO conversation_boot_snapshots "
                "(conversation_id,content,content_sha256,content_bytes,"
                "format_version,binding_origin) VALUES (?,?,?,?,1,'new_conversation')",
                (
                    self.conversation_id,
                    boot,
                    hashlib.sha256(boot.encode()).hexdigest(),
                    len(boot.encode()),
                ),
            )
            message_id = int(
                con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state) "
                    "VALUES (?,'user','owner','prompt','bounded rehearsal',"
                    "'prompt-1','m','running')",
                    (self.conversation_id,),
                ).lastrowid
            )
            self.archive_id = int(
                con.execute(
                    "INSERT INTO shell_memory_archives "
                    "(shell_id,session_id,date,full_narrative) "
                    "VALUES (2,'0042',date('now'),'bounded rehearsal')"
                ).lastrowid
            )
            con.execute(
                "UPDATE shells SET active_archive_id=? WHERE shell_id=2",
                (self.archive_id,),
            )
            self.run_id = int(
                con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                    "lease_expires_at,started_at,archive_id) "
                    "VALUES (?,2,?,'running','rehearsal',datetime('now','+5 minutes'),"
                    "datetime('now'),?)",
                    (self.conversation_id, message_id, self.archive_id),
                ).lastrowid
            )
            con.execute(
                "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (2,?)",
                (self.conversation_id,),
            )
            con.commit()

        self.original_server_db = server.DB_PATH
        server.DB_PATH = self.db
        self.addCleanup(setattr, server, "DB_PATH", self.original_server_db)
        self.original_server_repo = server.REPO_ROOT
        server.REPO_ROOT = self.workspace
        self.addCleanup(setattr, server, "REPO_ROOT", self.original_server_repo)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.original_api_base = mem.SC_API_BASE
        self.original_api_token = mem.SC_API_TOKEN
        mem.SC_API_BASE = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        mem.SC_API_TOKEN = "review-token"
        self.addCleanup(setattr, mem, "SC_API_BASE", self.original_api_base)
        self.addCleanup(setattr, mem, "SC_API_TOKEN", self.original_api_token)

    def _normalize_action(
        self, command: str, receipt: str, *, failed: bool = False
    ) -> tuple[dict, dict]:
        adapter = DeepSeekAdapter()
        turn = NativeTurn(
            harness="deepseek",
            session_ref="deepseek-" + "e" * 32,
            run_ref="rehearsal-run",
            worktree=self.reviewer_worktree,
            metadata={"from_event_seq": 0, "seen_event_seq": set()},
        )
        started = adapter._session_event(
            turn,
            {
                "type": "tool/call",
                "seq": 1,
                "data": {
                    "callId": "qaqc-call",
                    "name": "bash",
                    "arguments": json.dumps({"cmd": command}),
                },
            },
        )[0]
        completed = adapter._session_event(
            turn,
            {
                "type": "tool/result",
                "seq": 2,
                "data": {
                    "message": {
                        "toolCallId": "qaqc-call",
                        "content": [{"type": "text", "text": receipt}],
                        "isError": failed,
                    }
                },
            },
        )[0]
        self.assertEqual("tool.started", started.type)
        self.assertEqual("tool.completed", completed.type)
        return dict(started.payload), dict(completed.payload)

    def _insert_action(self, started: dict, completed: dict) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.executemany(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,payload,run_id) "
                "VALUES (?,?,?,?,?)",
                (
                    (
                        self.conversation_id,
                        1,
                        "tool.started",
                        json.dumps(started),
                        self.run_id,
                    ),
                    (
                        self.conversation_id,
                        2,
                        "tool.completed",
                        json.dumps(completed),
                        self.run_id,
                    ),
                ),
            )
            con.commit()

    def _classify(self, event_transform=None) -> tuple[int | None, dict]:
        backend = canary.HostBackend(canary.Deadline(100, 50))

        def sqlite_run(argv, *, cwd=None, env=None, check=True, label):
            self.assertEqual("-json", argv[-2])
            with contextlib.closing(sqlite3.connect(self.db)) as con:
                con.row_factory = sqlite3.Row
                rows = [dict(row) for row in con.execute(argv[-1]).fetchall()]
            return canary.CommandResult(json.dumps(rows), "", 0)

        backend._run = sqlite_run  # type: ignore[method-assign]
        facts = canary.Preflight(
            candidate_sha=self.candidate_sha,
            base_sha="b" * 40,
            repository="acme/dos-app",
            remote_url="https://github.com/acme/dos-app.git",
            workspace=self.workspace,
            base_branch="canary/base",
            head_branch="canary/head",
            container="rehearsal-container",
            network="rehearsal-network",
            api_port=1,
            dev_port=2,
            github_remaining=5000,
        )
        real_api = canary.JsonHttp(mem.SC_API_BASE, canary.Deadline(100, 50))
        if event_transform is None:
            api = real_api
        else:
            api = mock.Mock()

            def request(method, path, body=None):
                result = real_api.request(method, path, body=body)
                return event_transform(result) if "/events?" in path else result

            api.request.side_effect = request
        return backend._qaqc_action_evidence(
            api,
            facts,
            self.conversation_id,
            sprint_id=self.sprint_id,
            reviewer_shell_id=2,
            document_id=self.document_id,
        )

    def _record_approval(self) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                sprint_cli.main(
                    [
                        "record-qaqc",
                        "--document",
                        str(self.document_id),
                        "--verdict",
                        "pass",
                    ]
                ),
            )
        return json.loads(output.getvalue())

    def _finish_run(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute(
                "UPDATE conversation_runs SET state='succeeded',ended_at=datetime('now'),"
                "exit_code=0 WHERE run_id=?",
                (self.run_id,),
            )
            con.execute(
                "UPDATE conversation_messages SET state='completed',"
                "completed_at=datetime('now') WHERE conversation_id=?",
                (self.conversation_id,),
            )
            con.execute(
                "UPDATE conversations SET state='idle' WHERE conversation_id=?",
                (self.conversation_id,),
            )
            con.commit()

    def _seed_approval_without_action_receipt(self, *, reviewer_shell_id: int = 2) -> int:
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            approval_id = int(
                con.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (?,?,?,'pass')",
                    (
                        self.document_id,
                        hashlib.sha256(self.spec_body.encode()).hexdigest(),
                        reviewer_shell_id,
                    ),
                ).lastrowid
            )
            con.commit()
        return approval_id

    def test_real_cli_api_and_deepseek_action_events_produce_exact_evidence(self) -> None:
        command = (
            f"bash -lc './sc sprint record-qaqc --document {self.document_id} "
            "--verdict pass'"
        )
        self.assertIsNone(shutil.which("sc", path="/usr/local/bin:/usr/bin:/bin"))
        self.assertTrue((self.workspace / "sc").is_file())

        receipt = self._record_approval()
        self.assertTrue(receipt["created"])
        self.assertEqual("pass", receipt["verdict"])
        self.assertEqual(
            hashlib.sha256(self.spec_body.encode()).hexdigest(),
            receipt["revision_sha256"],
        )
        action_receipt = receipt["action_receipt"]
        self.assertEqual("record-qaqc", action_receipt["action_kind"])
        self.assertEqual(self.sprint_id, action_receipt["sprint_id"])
        self.assertEqual(
            self.reviewer_participant_id, action_receipt["participant_id"]
        )
        self.assertEqual(self.assignment_generation, action_receipt["assignment_generation"])
        self.assertEqual(self.conversation_id, action_receipt["conversation_id"])
        self.assertEqual("0042", action_receipt["session_id"])
        self.assertEqual(self.run_id, action_receipt["run_id"])
        self.assertEqual(self.candidate_sha, action_receipt["candidate_sha"])
        self.assertEqual("pre-arm-qaqc", action_receipt["review_phase"])
        self.assertEqual(receipt["approval_id"], action_receipt["approval_id"])
        self.assertTrue(action_receipt["approval_created"])
        replay = self._record_approval()
        self.assertFalse(replay["created"])
        self.assertEqual(
            action_receipt["action_receipt_id"],
            replay["action_receipt"]["action_receipt_id"],
        )

        started, completed = self._normalize_action(command, json.dumps(receipt))
        self._insert_action(started, completed)
        self._finish_run()
        approval_id, evidence = self._classify()

        self.assertEqual(receipt["approval_id"], approval_id)
        self.assertEqual(
            {
                "boot": {
                    "role": "resolved",
                    "skill": "resolved",
                    "shell_tool": "resolved",
                    "candidate": "resolved",
                    "predeclaration": "resolved",
                },
                "terminal": "succeeded",
                "record_qaqc": {
                    "observed": True,
                    "invocation_count": 1,
                    "exit_class": "success",
                    "receipt": True,
                    "identity": "matched",
                },
                "action_receipt": {"count": 1, "identity": "matched"},
                "postcondition": "approved",
            },
            evidence,
        )
        self.assertTrue(canary.HostBackend._qaqc_evidence_passed(evidence))
        encoded = json.dumps(evidence)
        self.assertNotIn(command, encoded)
        self.assertNotIn(receipt["revision_sha256"], encoded)

    def test_engine_action_receipt_mismatch_categories_are_bounded(self) -> None:
        self._record_approval()
        self._finish_run()

        def transformed(*, field=None, value=None, drop=False, duplicate=False):
            def apply(page):
                projected = json.loads(json.dumps(page))
                if drop:
                    projected["items"] = []
                    return projected
                receipt_event = next(
                    item
                    for item in projected["items"]
                    if item.get("type") == "qaqc.action_recorded"
                )
                if field == "actor_shell_id":
                    receipt_event["actor"]["shell_id"] = value
                elif field == "malformed":
                    receipt_event["details"].pop("run_id")
                elif field is not None:
                    receipt_event["details"][field] = value
                if duplicate:
                    projected["items"].append(json.loads(json.dumps(receipt_event)))
                return projected

            return apply

        cases = (
            ("absent", transformed(drop=True)),
            ("duplicate", transformed(duplicate=True)),
            ("sprint_mismatch", transformed(field="sprint_id", value=self.sprint_id + 1)),
            ("participant_mismatch", transformed(field="participant_id", value=999)),
            ("shell_mismatch", transformed(field="actor_shell_id", value=999)),
            ("role_mismatch", transformed(field="role", value="developer")),
            ("generation_mismatch", transformed(field="assignment_generation", value="f" * 32)),
            ("conversation_mismatch", transformed(field="conversation_id", value="cv_" + "e" * 32)),
            ("session_mismatch", transformed(field="session_id", value="9999")),
            ("run_mismatch", transformed(field="run_id", value=self.run_id + 1)),
            ("candidate_mismatch", transformed(field="candidate_sha", value="d" * 40)),
            ("spec_mismatch", transformed(field="document_id", value=self.document_id + 1)),
            ("phase_mismatch", transformed(field="review_phase", value="other")),
            ("approval_mismatch", transformed(field="approval_id", value=999)),
            ("row_mismatch", transformed(field="approval_created", value=False)),
            ("malformed", transformed(field="malformed")),
        )
        for expected, transform in cases:
            with self.subTest(expected=expected):
                _approval_id, evidence = self._classify(transform)
                self.assertEqual(expected, evidence["action_receipt"]["identity"])
                self.assertFalse(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_absent_active_run_creates_approval_without_provenance_receipt(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("DELETE FROM active_shell_chats WHERE shell_id=2")
            con.commit()

        receipt = self._record_approval()

        self.assertTrue(receipt["created"])
        self.assertIsNone(receipt["action_receipt"])

    def test_missing_invocation_and_absent_approval_are_bounded(self) -> None:
        approval_id, evidence = self._classify()

        self.assertIsNone(approval_id)
        self.assertEqual("not_invoked", evidence["record_qaqc"]["exit_class"])
        self.assertFalse(evidence["record_qaqc"]["observed"])
        self.assertEqual(0, evidence["record_qaqc"]["invocation_count"])
        self.assertEqual("absent", evidence["postcondition"])
        self.assertFalse(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_rejected_command_is_distinct_from_missing_invocation(self) -> None:
        command = (
            f"./sc sprint record-qaqc --document {self.document_id} "
            "--verdict pass"
        )
        started, completed = self._normalize_action(
            command, "bounded rejection", failed=True
        )
        self._insert_action(started, completed)

        approval_id, evidence = self._classify()

        self.assertIsNone(approval_id)
        self.assertTrue(evidence["record_qaqc"]["observed"])
        self.assertEqual("failure", evidence["record_qaqc"]["exit_class"])
        self.assertFalse(evidence["record_qaqc"]["receipt"])
        self.assertEqual("absent", evidence["postcondition"])
        self.assertFalse(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_approval_row_without_engine_action_receipt_fails_closed(self) -> None:
        approval_id = self._seed_approval_without_action_receipt()
        self._finish_run()

        observed_approval_id, evidence = self._classify()

        self.assertEqual(approval_id, observed_approval_id)
        self.assertEqual({"count": 0, "identity": "absent"}, evidence["action_receipt"])
        self.assertEqual("approved", evidence["postcondition"])
        self.assertFalse(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_approval_row_written_by_another_actor_fails_closed(self) -> None:
        self._seed_approval_without_action_receipt(reviewer_shell_id=1)
        self._finish_run()

        approval_id, evidence = self._classify()

        self.assertIsNone(approval_id)
        self.assertEqual("reviewer_mismatch", evidence["postcondition"])
        self.assertEqual({"count": 0, "identity": "absent"}, evidence["action_receipt"])
        self.assertFalse(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_native_receipt_omission_is_diagnostic_when_engine_receipt_matches(self) -> None:
        receipt = self._record_approval()
        command = (
            f"./sc sprint record-qaqc --document {self.document_id} "
            "--verdict pass"
        )
        started, completed = self._normalize_action(command, "receipt omitted")
        self._insert_action(started, completed)
        self._finish_run()

        approval_id, evidence = self._classify()

        self.assertEqual(receipt["approval_id"], approval_id)
        self.assertEqual("success", evidence["record_qaqc"]["exit_class"])
        self.assertFalse(evidence["record_qaqc"]["receipt"])
        self.assertEqual("absent", evidence["record_qaqc"]["identity"])
        self.assertEqual({"count": 1, "identity": "matched"}, evidence["action_receipt"])
        self.assertEqual("approved", evidence["postcondition"])
        self.assertTrue(canary.HostBackend._qaqc_evidence_passed(evidence))

    def test_wrong_native_receipt_identity_does_not_override_engine_receipt(self) -> None:
        receipt = self._record_approval()
        command = (
            f"./sc sprint record-qaqc --document {self.document_id} "
            "--verdict pass"
        )
        wrong = {**receipt, "approval_id": receipt["approval_id"] + 100}
        started, completed = self._normalize_action(command, json.dumps(wrong))
        self._insert_action(started, completed)
        self._finish_run()

        approval_id, evidence = self._classify()

        self.assertEqual(receipt["approval_id"], approval_id)
        self.assertTrue(evidence["record_qaqc"]["receipt"])
        self.assertEqual("mismatch", evidence["record_qaqc"]["identity"])
        self.assertEqual({"count": 1, "identity": "matched"}, evidence["action_receipt"])
        self.assertEqual("approved", evidence["postcondition"])
        self.assertTrue(canary.HostBackend._qaqc_evidence_passed(evidence))


if __name__ == "__main__":
    unittest.main(verbosity=2)
