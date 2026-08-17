#!/usr/bin/env python3
"""Managed OpenCode server lifecycle and connected-provider projection."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from conversation_adapters import opencode  # noqa: E402


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class OpenCodeServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_patch = mock.patch.object(
            opencode,
            "SERVER_LOG",
            Path(self.tmp.name) / "opencode-server.log",
        )
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        opencode._SERVER_PROCESS = None
        opencode._SERVER_PASSWORD = None
        opencode._SERVER_LOG_HANDLE = None
        self.addCleanup(opencode.stop_server)

    def test_healthy_existing_server_is_reused_without_spawning(self):
        with mock.patch.object(
            opencode, "_server_healthy", return_value=True
        ), mock.patch.object(opencode.subprocess, "Popen") as spawn:
            password = opencode.ensure_server()
        self.assertIsNone(password)
        spawn.assert_not_called()

    def test_managed_server_is_loopback_authenticated_and_stoppable(self):
        process = FakeProcess()
        checks = iter([False, True])
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(
                    opencode, "_server_healthy",
                    side_effect=lambda _password: next(checks),
                ), mock.patch.object(
                    opencode.shutil, "which", return_value="/bin/opencode"
                ), mock.patch.object(
                    opencode.secrets, "token_urlsafe", return_value="server-secret"
                ), mock.patch.object(
                    opencode.subprocess, "Popen", return_value=process
                ) as spawn:
            os.environ.pop("OPENCODE_SERVER_PASSWORD", None)
            password = opencode.ensure_server(timeout=1)

        self.assertEqual(password, "server-secret")
        argv = spawn.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "/bin/opencode",
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                "4096",
                "--log-level",
                "WARN",
            ],
        )
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], "server-secret")
        self.assertEqual(env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])

        opencode.stop_server()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_connected_models_excludes_disconnected_and_inactive_routes(self):
        got = opencode.connected_models(
            {
                "connected": ["openai"],
                "all": [
                    {
                        "id": "openai",
                        "models": {
                            "gpt-live": {
                                "name": "GPT Live",
                                "family": "gpt",
                                "status": "active",
                            },
                            "gpt-retired": {
                                "name": "GPT Retired",
                                "status": "deprecated",
                            },
                        },
                    },
                    {
                        "id": "not-connected",
                        "models": {
                            "model": {"name": "Must not appear"},
                        },
                    },
                ],
            }
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], "openai/gpt-live")
        self.assertEqual(got[0]["provider_model"], "gpt-live")
        self.assertEqual(got[0]["status"], "active")
        self.assertEqual(got[0]["supported_efforts"], [])
        self.assertEqual(got[0]["native_variant_ids"], {})
        self.assertNotEqual(got[0]["id"], "openai/gpt-retired")
        self.assertNotEqual(got[0]["provider"], "not-connected")

    def test_connected_models_admits_only_canonical_safe_openai_variants(self):
        got = opencode.connected_models({
            "_sc_cli_version": "1.18.9",
            "connected": ["openai"],
            "all": [{
                "id": "openai",
                "npm": "@ai-sdk/openai",
                "models": {
                    "gpt-live": {
                        "name": "GPT Live",
                        "variants": {
                            "low": {"reasoningEffort": "low"},
                            "high": {
                                "disabled": False,
                                "reasoningEffort": "high",
                                "reasoningSummary": "detailed",
                            },
                        },
                    }
                },
            }],
        })

        self.assertEqual(len(got), 1)
        model = got[0]
        self.assertEqual(model["supported_efforts"], ["low", "high"])
        self.assertEqual(model["default_effort"], "high")
        self.assertEqual(model["native_variant_ids"], {
            "low": "low", "high": "high",
        })
        self.assertEqual(
            model["adapter_metadata"]["variant_options_by_effort"],
            {
                "low": {"reasoningEffort": "low"},
                "high": {
                    "reasoningEffort": "high",
                    "reasoningSummary": "detailed",
                },
            },
        )
        self.assertEqual(model["cli_version"], "1.18.9")

    def test_variant_id_collision_rejects_every_member_before_value_admission(self):
        admitted = opencode.admitted_variants(
            {
                "high": {"reasoningEffort": "high"},
                "HIGH": {"reasoningEffort": "high"},
                " low ": {"reasoningEffort": "low"},
                "ｍｅｄｉｕｍ": {"reasoningEffort": "medium"},
                "safe": {"reasoningEffort": "medium"},
            },
            provider_family="openai-ai-sdk",
            model={},
        )

        self.assertEqual(admitted, {"safe": {"reasoningEffort": "medium"}})
        self.assertNotIn("high", admitted)
        self.assertNotIn("HIGH", admitted)
        self.assertNotIn(" low ", admitted)
        self.assertNotIn("ｍｅｄｉｕｍ", admitted)

    def test_variant_id_admission_rejects_every_noncanonical_shape(self):
        overlay = {"reasoningEffort": "high"}
        admitted = opencode.admitted_variants(
            {
                "": overlay,
                ".leading": overlay,
                "slash/value": overlay,
                "a" * 33: overlay,
                "café": overlay,
                "valid.id_1-low": overlay,
            },
            provider_family="openai-ai-sdk",
            model={},
        )

        self.assertEqual(admitted, {
            "valid.id_1-low": {"reasoningEffort": "high"},
        })

    def test_variant_value_manifest_rejects_disabled_unknown_and_sensitive_fields(self):
        rejected = {
            "disabled": {"disabled": True, "reasoningEffort": "high"},
            "bad-disabled": {"disabled": "false", "reasoningEffort": "high"},
            "unknown": {"reasoningEffort": "high", "temperature": 1},
            "credential": {"reasoningEffort": "high", "ApiKey": "secret"},
            "substitution": {"reasoningSummary": "{env:SECRET}"},
            "wrong-type": {"reasoningEffort": ["high"]},
            "non-object": "high",
        }

        admitted = opencode.admitted_variants(
            rejected,
            provider_family="openai-ai-sdk",
            model={},
        )

        self.assertEqual(admitted, {})

    def test_anthropic_variant_requires_exact_bounded_thinking_shape(self):
        admitted = opencode.admitted_variants(
            {
                "valid": {
                    "thinking": {"type": "enabled", "budgetTokens": 4096}
                },
                "over-limit": {
                    "thinking": {"type": "enabled", "budgetTokens": 8193}
                },
                "extra": {
                    "thinking": {
                        "type": "enabled", "budgetTokens": 10, "extra": True
                    }
                },
            },
            provider_family="anthropic-ai-sdk",
            model={"limit": {"output": 8192}},
        )

        self.assertEqual(admitted, {
            "valid": {
                "thinking": {"type": "enabled", "budgetTokens": 4096}
            }
        })

    def test_unknown_provider_family_admits_no_variant(self):
        admitted = opencode.admitted_variants(
            {"high": {"reasoningEffort": "high"}},
            provider_family=None,
            model={},
        )

        self.assertEqual(admitted, {})

    def test_default_adapter_uses_managed_server_password(self):
        with mock.patch.object(
            opencode, "ensure_server", return_value="managed-secret"
        ) as ensure, mock.patch.object(
            opencode, "UrlHttpTransport"
        ) as transport:
            opencode.OpenCodeAdapter()
        ensure.assert_called_once_with()
        transport.assert_called_once_with(
            opencode.SERVER_ENDPOINT,
            password="managed-secret",
            timeout=opencode.TURN_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
