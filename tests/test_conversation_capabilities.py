#!/usr/bin/env python3
"""Contract tests for Feature #24's live-probed harness capabilities."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / ".super-coder" / "adapters"
PROBES = ROOT / "tests" / "fixtures" / "conversations" / "capability_probes.json"
REQUIRED = ("opencode", "claude", "codex", "kimi")
CAPABILITIES = {
    "exact_session_resume",
    "structured_streaming",
    "interruption",
    "interactive_permission_response",
    "server_backed",
    "session_inspection",
}
NORMALIZED_EVENTS = {
    "session.started",
    "run.started",
    "assistant.delta",
    "tool.started",
    "tool.completed",
    "permission.requested",
    "input.requested",
    "usage",
    "run.completed",
    "run.failed",
    "run.interrupted",
}
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ConversationCapabilityContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probes = json.loads(PROBES.read_text())

    def adapter(self, harness: str) -> dict:
        return json.loads(
            (ADAPTERS / harness / "adapter.json").read_text()
        )

    def test_required_harnesses_have_verified_conversation_contracts(self) -> None:
        self.assertEqual(
            set(self.probes["required_harnesses"]),
            set(REQUIRED),
        )
        for harness in REQUIRED:
            with self.subTest(harness=harness):
                conversation = self.adapter(harness)["conversation"]
                probe = self.probes["required_harnesses"][harness]
                self.assertEqual(conversation["contract_version"], 1)
                self.assertEqual(
                    set(conversation["normalized_events"]),
                    NORMALIZED_EVENTS,
                )
                execution = conversation["execution_permissions"]
                self.assertEqual(
                    execution["requirement"],
                    "worktree-command-access",
                )
                self.assertFalse(execution["sandbox_flag_required"])
                self.assertRegex(conversation["minimum_cli_version"], VERSION)
                self.assertRegex(
                    conversation["maximum_cli_version_exclusive"], VERSION
                )
                self.assertEqual(
                    conversation["verified_cli_version"],
                    probe["verified_cli_version"],
                )
                self.assertEqual(
                    conversation["maximum_cli_version_exclusive"],
                    probe["maximum_cli_version_exclusive"],
                )
                self.assertLessEqual(
                    tuple(map(int, conversation["minimum_cli_version"].split("."))),
                    tuple(map(int, conversation["verified_cli_version"].split("."))),
                )
                self.assertLess(
                    tuple(map(int, conversation["verified_cli_version"].split("."))),
                    tuple(
                        map(
                            int,
                            conversation["maximum_cli_version_exclusive"].split("."),
                        )
                    ),
                )
                self.assertEqual(conversation["driver"], probe["driver"])
                self.assertEqual(
                    set(conversation["capabilities"]),
                    CAPABILITIES,
                )
                for proof in (
                    "two_turn_exact_resume",
                    "same_worktree_isolation",
                    "structured_streaming",
                    "interruption",
                    "resume_after_compute_exit",
                    "session_inspection",
                ):
                    self.assertIs(
                        probe["proof"][proof],
                        True,
                        f"{harness} lacks live proof for {proof}",
                    )

    def test_opencode_uses_exact_server_resources(self) -> None:
        conversation = self.adapter("opencode")["conversation"]
        self.assertEqual(conversation["session_ref"], {
            "source": "session.create",
            "field": "id",
        })
        self.assertEqual(conversation["stream"]["path"], "/event")
        self.assertIn("{session_ref}", conversation["interrupt"]["path"])
        self.assertIn("{session_ref}", conversation["inspect"]["path"])

    def test_claude_streaming_and_resume_flags_are_explicit(self) -> None:
        conversation = self.adapter("claude")["conversation"]
        flags = conversation["start"]["stream_flags"]
        self.assertIn("--verbose", flags)
        self.assertIn("stream-json", flags)
        self.assertEqual(conversation["start"]["session_flag"], "--session-id")
        self.assertEqual(conversation["resume"]["session_flag"], "--resume")
        self.assertEqual(conversation["interrupt"]["signal"], "SIGINT")
        self.assertFalse(
            conversation["capabilities"]["interactive_permission_response"]
        )

    def test_codex_uses_app_server_and_permission_policy_not_resume_sandbox_flag(
            self) -> None:
        conversation = self.adapter("codex")["conversation"]
        self.assertEqual(conversation["start"]["method"], "thread/start")
        self.assertEqual(conversation["resume"], {
            "method": "thread/resume",
            "session_field": "threadId",
        })
        self.assertEqual(conversation["interrupt"]["method"], "turn/interrupt")
        self.assertEqual(
            conversation["permission_values"]["unrestricted"],
            "danger-full-access",
        )
        self.assertTrue(
            self.probes["required_harnesses"]["codex"]["proof"]
            ["resume_after_server_restart"]
        )

    def test_kimi_uses_prompt_mode_and_native_store_identity(self) -> None:
        conversation = self.adapter("kimi")["conversation"]
        probe = self.probes["required_harnesses"]["kimi"]
        self.assertEqual(
            {
                key: conversation[key]
                for key in (
                    "minimum_cli_version",
                    "verified_cli_version",
                    "maximum_cli_version_exclusive",
                )
            },
            {
                "minimum_cli_version": "0.30.0",
                "verified_cli_version": "0.33.0",
                "maximum_cli_version_exclusive": "0.34.0",
            },
        )
        self.assertEqual(
            {
                key: probe[key]
                for key in (
                    "minimum_cli_version",
                    "verified_cli_version",
                    "maximum_cli_version_exclusive",
                )
            },
            {
                "minimum_cli_version": "0.30.0",
                "verified_cli_version": "0.33.0",
                "maximum_cli_version_exclusive": "0.34.0",
            },
        )
        self.assertEqual(conversation["driver"], "kimi-print")
        self.assertEqual(conversation["session_ref"], {
            "source": "native-session-store",
            "field": "session-directory-name",
        })
        self.assertEqual(
            conversation["start"]["identity_source"],
            "new-main-wire-turn.prompt",
        )
        self.assertEqual(
            conversation["resume"],
            {
                "session_flag": "-S",
                "identity_source": "appended-main-wire-turn.prompt",
            },
        )
        self.assertEqual(
            conversation["stream"]["transport"],
            "stdout-jsonl-with-raw-interleave",
        )
        self.assertEqual(
            conversation["permission_policy"],
            "kimi-prompt-auto-mode",
        )
        self.assertFalse(
            conversation["capabilities"]["interactive_permission_response"]
        )
        self.assertFalse(conversation["capabilities"]["server_backed"])
        self.assertIn("session.resume_hint", probe["observed_events"])
        self.assertIn("turn.cancel", probe["observed_events"])


if __name__ == "__main__":
    unittest.main()
