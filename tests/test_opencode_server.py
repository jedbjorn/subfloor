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
        self.assertEqual(
            got,
            [
                {
                    "id": "openai/gpt-live",
                    "provider": "openai",
                    "provider_model": "gpt-live",
                    "name": "GPT Live",
                    "family": "gpt",
                    "release_date": "",
                    "status": "active",
                }
            ],
        )

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
            timeout=600.0,
        )


if __name__ == "__main__":
    unittest.main()
