"""Binding-driven transport snapshots and negative uncontrolled routes."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import route_transport
import run


def controlled(harness: str) -> tuple[dict, str]:
    effort = "high"
    model = "openai/gpt-test" if harness == "opencode" else "model-test"
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": harness,
        "requested_model": model,
        "provider_model": "gpt-test" if harness == "opencode" else model,
        "requested_effort": effort,
        "effective_effort": effort,
        "native_variant_id": effort if harness == "opencode" else None,
        "transport": {
            "claude": "claude-effort-argument",
            "codex": "codex-reasoning-config",
            "kimi": "kimi-effort-environment",
            "opencode": "opencode-route-agent",
        }[harness],
        "catalogue_generation": "1" * 32,
        "evidence_digest": "2" * 64,
        "selector_binding": {"kind": "exact-test-route"},
        "adapter_metadata": (
            {
                "compatibility_manifest": "opencode-1.18.9-v1",
                "provider_family": "openai-ai-sdk",
                "variant_options": {"reasoningEffort": "high"},
            }
            if harness == "opencode" else {}
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return binding, digest


def model_default(harness: str) -> tuple[dict, str]:
    model = "openai/gpt-test" if harness == "opencode" else "model-test"
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": harness,
        "requested_model": model,
        "provider_model": "gpt-test" if harness == "opencode" else model,
        "requested_effort": "default",
        "effective_effort": "default",
        "native_variant_id": None,
        "transport": {
            "claude": "claude-effort-argument",
            "codex": "codex-reasoning-config",
            "kimi": "kimi-effort-environment",
            "opencode": "opencode-route-agent",
        }[harness],
        "catalogue_generation": "1" * 32,
        "evidence_digest": None,
        "selector_binding": {"kind": "exact-test-route"},
        "adapter_metadata": {},
    }
    digest = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return binding, digest


def uncontrolled(state: str, harness: str, model: str | None) -> tuple[dict, str]:
    binding = {
        "contract_version": 2,
        "control_state": state,
        "harness": harness,
        "requested_model": model,
        "provider_model": None,
        "requested_effort": None,
        "effective_effort": None,
        "native_variant_id": None,
        "transport": "native-default",
        "catalogue_generation": None,
        "evidence_digest": None,
        "selector_binding": None,
        "adapter_metadata": {},
    }
    digest = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return binding, digest


def live_native(
    harness: str,
    native_option_id: str | None,
) -> tuple[dict, str]:
    model = "ollama-cloud/glm-5.2"
    return route_transport.route_bindings.live_native_v3_binding(
        harness,
        model,
        "glm-5.2",
        native_option_id,
    )


class RouteTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_direct_command_and_environment_snapshots(self) -> None:
        expected = {
            "claude": [
                "claude", "--model", "model-test", "--effort", "high",
                "-p", "hello",
            ],
            "codex": [
                "codex", "exec", "-m", "model-test", "-c",
                'model_reasoning_effort="high"', "hello",
            ],
            "kimi": ["kimi", "-m", "model-test", "-p", "hello"],
        }
        for harness in ("claude", "codex", "kimi"):
            with self.subTest(harness=harness):
                binding, digest = controlled(harness)
                projection = route_transport.project(
                    binding, digest, expected_harness=harness,
                    worktree=self.root, interface="headless",
                )
                command = run.headless_command(
                    run.load_adapter(harness),
                    "hello",
                    transport=projection,
                )
                self.assertEqual(command, expected[harness])
                self.assertEqual(
                    projection.env(),
                    {"KIMI_MODEL_THINKING_EFFORT": "high"}
                    if harness == "kimi" else {},
                )

    def test_opencode_headless_uses_full_agent_and_exact_native_variant(self):
        binding, digest = controlled("opencode")
        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            worktree=self.root,
            interface="headless",
        )
        command = run.headless_command(
            run.load_adapter("opencode"), "hello", transport=projection
        )
        agent = f"sc-route-{digest}"

        self.assertEqual(command, [
            "opencode", "run", "--model", "openai/gpt-test",
            "--agent", agent, "--variant", "high", "hello",
        ])
        self.assertEqual(projection.native_variant_id, "high")
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(list(configured["agent"]), [agent])
        self.assertEqual(configured["agent"][agent]["reasoningEffort"], "high")

    def test_live_native_headless_uses_exact_option_without_route_agent(self):
        for harness, flag in (("opencode", "--variant"), ("deepseek", "--effort")):
            with self.subTest(harness=harness):
                binding, digest = live_native(harness, "MAX.Future")
                projection = route_transport.project(
                    binding,
                    digest,
                    expected_harness=harness,
                    worktree=self.root,
                    interface="headless",
                )
                command = run.headless_command(
                    run.load_adapter(harness), "hello", transport=projection
                )

                self.assertEqual(
                    command[-5:],
                    ["--selector" if harness == "deepseek" else "--model",
                     "ollama-cloud/glm-5.2", flag, "MAX.Future", "hello"],
                )
                self.assertEqual(projection.native_variant_id, "MAX.Future")
                self.assertFalse((self.root / "opencode.json").exists())

    def test_opencode_live_native_headless_sends_exact_model_and_variant(self):
        binding, digest = route_transport.route_bindings.live_native_v3_binding(
            "opencode",
            "Ollama-Cloud/GLM-5.2",
            "GLM-5.2",
            "MaX.Future",
        )
        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            worktree=self.root,
            interface="headless",
        )

        command = run.headless_command(
            run.load_adapter("opencode"),
            "dispatch exactly once",
            transport=projection,
        )

        self.assertEqual(command, [
            "opencode", "run", "--model", "Ollama-Cloud/GLM-5.2",
            "--variant", "MaX.Future", "dispatch exactly once",
        ])
        self.assertNotIn("--agent", command)
        self.assertIsNone(projection.route_agent)
        self.assertFalse((self.root / "opencode.json").exists())

    def test_opencode_live_native_headless_default_omits_variant(self):
        binding, digest = route_transport.route_bindings.live_native_v3_binding(
            "opencode",
            "Ollama-Cloud/GLM-5.2",
            "GLM-5.2",
            None,
        )
        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            worktree=self.root,
            interface="headless",
        )

        command = run.headless_command(
            run.load_adapter("opencode"),
            "dispatch exactly once",
            transport=projection,
        )

        self.assertEqual(command, [
            "opencode", "run", "--model", "Ollama-Cloud/GLM-5.2",
            "dispatch exactly once",
        ])
        self.assertNotIn("--variant", command)
        self.assertNotIn("--agent", command)
        self.assertIsNone(projection.route_agent)
        self.assertFalse((self.root / "opencode.json").exists())

    def test_opencode_live_native_interactive_uses_native_variant_without_agent(self):
        binding, digest = route_transport.route_bindings.live_native_v3_binding(
            "opencode",
            "Ollama-Cloud/GLM-5.2",
            "GLM-5.2",
            "Case/Sensitive.MAX",
        )

        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            interface="interactive",
        )

        self.assertEqual(projection.argument_tail, (
            "--model", "Ollama-Cloud/GLM-5.2",
            "--variant", "Case/Sensitive.MAX",
        ))
        self.assertIsNone(projection.route_agent)
        self.assertFalse((self.root / "opencode.json").exists())

    def test_live_native_headless_null_option_invokes_harness_default(self):
        for harness in ("opencode", "deepseek"):
            with self.subTest(harness=harness):
                binding, digest = live_native(harness, None)
                projection = route_transport.project(
                    binding,
                    digest,
                    expected_harness=harness,
                    worktree=self.root,
                    interface="headless",
                )
                command = run.headless_command(
                    run.load_adapter(harness), "hello", transport=projection
                )

                self.assertNotIn("--variant", command)
                self.assertNotIn("--effort", command)
                self.assertIsNone(projection.effort)
                self.assertIsNone(projection.native_variant_id)

    def test_opencode_interactive_uses_model_and_full_agent_without_variant(self):
        binding, digest = controlled("opencode")
        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            worktree=self.root,
            interface="interactive",
        )

        self.assertEqual(projection.argument_tail, (
            "--model", "openai/gpt-test", "--agent", f"sc-route-{digest}",
        ))
        self.assertNotIn("--variant", projection.argument_tail)

    def test_model_default_omits_every_effort_transport(self) -> None:
        expected = {
            "claude": ["claude", "--model", "model-test", "-p", "hello"],
            "codex": ["codex", "exec", "-m", "model-test", "hello"],
            "kimi": ["kimi", "-m", "model-test", "-p", "hello"],
        }
        for harness in ("claude", "codex", "kimi"):
            with self.subTest(harness=harness):
                binding, digest = model_default(harness)
                projection = route_transport.project(
                    binding, digest, expected_harness=harness,
                    worktree=self.root, interface="headless",
                )
                self.assertEqual(projection.effort, "default")
                self.assertEqual(projection.argument_tail, ())
                self.assertEqual(projection.environment, ())
                command = run.headless_command(
                    run.load_adapter(harness),
                    "hello",
                    transport=projection,
                )
                self.assertEqual(command, expected[harness])
                self.assertNotIn("KIMI_MODEL_THINKING_EFFORT", projection.env())

    def test_opencode_model_default_has_agent_without_variant_overlay(self):
        binding, digest = model_default("opencode")
        projection = route_transport.project(
            binding,
            digest,
            expected_harness="opencode",
            worktree=self.root,
            interface="headless",
        )
        command = run.headless_command(
            run.load_adapter("opencode"), "hello", transport=projection
        )
        agent = f"sc-route-{digest}"

        self.assertEqual(command, [
            "opencode", "run", "--model", "openai/gpt-test",
            "--agent", agent, "hello",
        ])
        self.assertNotIn("--variant", command)
        self.assertIsNone(projection.native_variant_id)
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(
            configured["agent"][agent],
            {"mode": "primary", "model": "openai/gpt-test"},
        )

    def test_vibe_and_harness_default_never_emit_effort_transport(self):
        routes = [
            uncontrolled("native-uncontrolled", "vibe", "vibe-model"),
            uncontrolled("harness-default", "claude", None),
        ]
        for binding, digest in routes:
            with self.subTest(state=binding["control_state"]):
                projection = route_transport.project(binding, digest)
                self.assertIsNone(projection.effort)
                self.assertEqual(projection.argument_tail, ())
                self.assertEqual(projection.environment, ())
                self.assertIsNone(projection.native_variant_id)
                self.assertIsNone(projection.route_agent)

    def test_invalid_digest_refuses_before_config_or_command(self):
        binding, _digest = controlled("opencode")

        with self.assertRaises(
            route_transport.route_bindings.RouteResolutionError
        ) as refused:
            route_transport.project(
                binding,
                "f" * 64,
                expected_harness="opencode",
                worktree=self.root,
                interface="headless",
            )

        self.assertEqual(
            getattr(refused.exception, "code", None), "thinking_evidence_missing"
        )
        self.assertFalse((self.root / "opencode.json").exists())


if __name__ == "__main__":
    unittest.main()
