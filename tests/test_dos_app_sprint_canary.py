"""Hermetic coverage for the source-only dos-app Sprint canary."""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

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

        def fake_run(argv, *, cwd=None, env=None, check=True, label):
            events.append(label)
            run_envs[label] = dict(env or {})
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
            versions = backend.launch(config, self.facts, canary.ResourceLedger())

        self.assertEqual(
            [
                "refresh isolated model routes",
                "read provider credential",
                "launch isolated runtime",
                "inspect launched harness versions",
            ],
            events,
        )
        refresh_env = run_envs["refresh isolated model routes"]
        self.assertTrue(
            canary.PROVIDER_CREDENTIAL_ENV.isdisjoint(refresh_env)
        )
        launch_env = run_envs["launch isolated runtime"]
        self.assertEqual(
            {"OLLAMA_API_KEY"},
            canary.PROVIDER_CREDENTIAL_ENV.intersection(launch_env),
        )
        self.assertEqual(
            {"codex": "codex-cli-test", "deepseek": "deepseek-test"},
            versions,
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
