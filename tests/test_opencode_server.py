#!/usr/bin/env python3
"""Managed OpenCode server lifecycle and connected-provider projection."""
from __future__ import annotations

import copy
import json
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
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
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
        self.state_path = Path(self.tmp.name) / "opencode-server.json"
        self.state_patch = mock.patch.object(
            opencode, "SERVER_STATE", self.state_path
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        opencode._SERVER_PROCESS = None
        opencode._SERVER_ENDPOINT = opencode.SERVER_ENDPOINT
        opencode._SERVER_PASSWORD = None
        opencode._SERVER_LOG_HANDLE = None
        self.probe_patch = mock.patch.object(
            opencode.harness_versions, "probe", return_value=None
        )
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.addCleanup(opencode.stop_server)

    def test_healthy_existing_server_is_reused_without_spawning(self):
        with mock.patch.object(
            opencode, "_server_healthy", return_value=True
        ), mock.patch.object(opencode.subprocess, "Popen") as spawn:
            endpoint, password = opencode.ensure_server()
        self.assertEqual(endpoint, opencode.SERVER_ENDPOINT)
        self.assertIsNone(password)
        spawn.assert_not_called()

    def test_managed_server_uses_private_dynamic_loopback_port(self):
        process = FakeProcess()
        checks = iter([False, True])
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(
                    opencode, "_server_healthy",
                    side_effect=lambda _endpoint, _password: next(checks),
                ), mock.patch.object(
                    opencode.shutil, "which", return_value="/bin/opencode"
                ), mock.patch.object(
                    opencode.secrets, "token_urlsafe", return_value="server-secret"
                ), mock.patch.object(
                    opencode, "_available_loopback_port", return_value=43210
                ), mock.patch.object(
                    opencode.subprocess, "Popen", return_value=process
                ) as spawn:
            os.environ.pop("OPENCODE_SERVER_PASSWORD", None)
            endpoint, password = opencode.ensure_server(timeout=1)

        self.assertEqual(endpoint, "http://127.0.0.1:43210")
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
                "43210",
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

    def test_orphaned_managed_server_is_readopted_via_recorded_password(self):
        self.state_path.write_text(
            json.dumps({"pid": os.getpid(), "password": "orphan-secret"})
        )
        with mock.patch.object(
            opencode,
            "_server_healthy",
            side_effect=lambda endpoint, password: (
                endpoint == opencode.SERVER_ENDPOINT
                and password == "orphan-secret"
            ),
        ), mock.patch.object(opencode.subprocess, "Popen") as spawn, \
                mock.patch.object(opencode, "_reap_orphan_server") as reap:
            endpoint, password = opencode.ensure_server()

        self.assertEqual(endpoint, opencode.SERVER_ENDPOINT)
        self.assertEqual(password, "orphan-secret")
        spawn.assert_not_called()
        reap.assert_not_called()
        self.assertTrue(self.state_path.exists())

    def test_version_mismatched_managed_orphan_is_rotated_before_adoption(self):
        self.state_path.write_text(json.dumps({
            "pid": 4322,
            "password": "stale-secret",
            "port": 43212,
        }))
        process = FakeProcess(pid=9999)
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(
                    opencode,
                    "_server_healthy",
                    side_effect=lambda endpoint, password: (
                        (endpoint, password)
                        in {
                            ("http://127.0.0.1:43212", "stale-secret"),
                            ("http://127.0.0.1:43213", "fresh-secret"),
                        }
                    ),
                ), mock.patch.object(
                    opencode, "_server_version", return_value="1.18.18"
                ), mock.patch.object(
                    opencode.harness_versions, "probe", return_value="1.18.22"
                ), mock.patch.object(
                    opencode, "_pid_is_opencode_serve", return_value=True
                ), mock.patch.object(
                    opencode, "_reap_orphan_server"
                ) as reap, mock.patch.object(
                    opencode.shutil, "which", return_value="/bin/opencode"
                ), mock.patch.object(
                    opencode.secrets, "token_urlsafe", return_value="fresh-secret"
                ), mock.patch.object(
                    opencode, "_available_loopback_port", return_value=43213
                ), mock.patch.object(
                    opencode.subprocess, "Popen", return_value=process
                ):
            os.environ.pop("OPENCODE_SERVER_PASSWORD", None)
            endpoint, password = opencode.ensure_server(timeout=1)

        self.assertEqual(endpoint, "http://127.0.0.1:43213")
        self.assertEqual(password, "fresh-secret")
        reap.assert_called_once_with(4322)
        self.assertEqual(
            json.loads(self.state_path.read_text()),
            {"pid": 9999, "password": "fresh-secret", "port": 43213},
        )

    def test_version_mismatch_never_reaps_unverified_recorded_process(self):
        self.state_path.write_text(json.dumps({
            "pid": 4322,
            "password": "recorded-secret",
            "port": 43212,
        }))
        with mock.patch.object(
            opencode,
            "_server_healthy",
            side_effect=lambda endpoint, password: (
                (endpoint, password)
                == ("http://127.0.0.1:43212", "recorded-secret")
            ),
        ), mock.patch.object(
            opencode, "_server_version", return_value="1.18.18"
        ), mock.patch.object(
            opencode.harness_versions, "probe", return_value="1.18.22"
        ), mock.patch.object(
            opencode, "_pid_is_opencode_serve", return_value=False
        ), mock.patch.object(
            opencode, "_reap_orphan_server"
        ) as reap, mock.patch.object(opencode.subprocess, "Popen") as spawn:
            endpoint, password = opencode.ensure_server()

        self.assertEqual(endpoint, "http://127.0.0.1:43212")
        self.assertEqual(password, "recorded-secret")
        reap.assert_not_called()
        spawn.assert_not_called()

    def test_unreachable_orphan_is_reaped_then_respawned(self):
        self.state_path.write_text(
            json.dumps({"pid": 4322, "password": "stale-secret"})
        )
        process = FakeProcess(pid=9999)
        checks = iter([False, False, False, True])
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(
                    opencode, "_server_healthy",
                    side_effect=lambda _endpoint, _password: next(checks),
                ), mock.patch.object(
                    opencode, "_pid_is_opencode_serve", return_value=True
                ) as identify, mock.patch.object(
                    opencode, "_reap_orphan_server"
                ) as reap, mock.patch.object(
                    opencode.shutil, "which", return_value="/bin/opencode"
                ), mock.patch.object(
                    opencode.secrets, "token_urlsafe", return_value="fresh-secret"
                ), mock.patch.object(
                    opencode, "_available_loopback_port", return_value=43211
                ), mock.patch.object(
                    opencode.subprocess, "Popen", return_value=process
                ):
            os.environ.pop("OPENCODE_SERVER_PASSWORD", None)
            endpoint, password = opencode.ensure_server(timeout=1)

        self.assertEqual(endpoint, "http://127.0.0.1:43211")
        self.assertEqual(password, "fresh-secret")
        identify.assert_called_once_with(4322)
        reap.assert_called_once_with(4322)
        self.assertEqual(
            json.loads(self.state_path.read_text()),
            {"pid": 9999, "password": "fresh-secret", "port": 43211},
        )

    def test_stop_server_preserves_state_of_adopted_orphan(self):
        self.state_path.write_text(
            json.dumps({"pid": 4322, "password": "orphan-secret"})
        )
        opencode.stop_server()
        self.assertTrue(self.state_path.exists())

        opencode._SERVER_PROCESS = FakeProcess()
        opencode.stop_server()
        self.assertFalse(self.state_path.exists())

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

    def test_connected_models_preserves_exact_native_variants_in_source_order(self):
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
                            "max": {"reasoningEffort": "max"},
                            "Extreme.Mode": ["payload", "shape", "ignored"],
                            "Case/Sensitive": "opaque-provider-value",
                        },
                    }
                },
            }],
        })

        self.assertEqual(len(got), 1)
        model = got[0]
        self.assertEqual(
            model["supported_efforts"],
            ["low", "high", "max", "Extreme.Mode", "Case/Sensitive"],
        )
        self.assertEqual(model["native_option_ids"], model["supported_efforts"])
        self.assertIsNone(model["default_effort"])
        self.assertIsNone(model["native_default_option_id"])
        self.assertEqual(model["native_variant_ids"], {
            "low": "low", "high": "high", "max": "max",
            "Extreme.Mode": "Extreme.Mode",
            "Case/Sensitive": "Case/Sensitive",
        })
        self.assertEqual(model["adapter_metadata"], {})
        self.assertNotIn("variants", model)
        self.assertEqual(model["cli_version"], "1.18.9")

    def test_connected_models_rejects_malformed_or_oversized_native_options(self):
        base = {
            "connected": ["openai"],
            "all": [{
                "id": "openai",
                "models": {"gpt-live": {"variants": {}}},
            }],
        }
        malformed = copy.deepcopy(base)
        malformed["all"][0]["models"]["gpt-live"]["variants"] = ["high"]
        with self.assertRaisesRegex(
            opencode.AdapterError, "variants must be an object"
        ):
            opencode.connected_models(malformed)

        oversized = copy.deepcopy(base)
        oversized["all"][0]["models"]["gpt-live"]["variants"] = {
            f"option-{index}": {"future": index}
            for index in range(opencode.MAX_NATIVE_OPTIONS + 1)
        }
        with self.assertRaisesRegex(opencode.AdapterError, "too many native options"):
            opencode.connected_models(oversized)

    def test_default_adapter_probe_is_process_free(self):
        with mock.patch.object(
            opencode, "ensure_server"
        ) as ensure, mock.patch.object(
            opencode, "UrlHttpTransport"
        ) as transport, mock.patch.object(
            opencode,
            "command_version",
            return_value="1.18.9",
        ) as version:
            adapter = opencode.OpenCodeAdapter()
            ensure.assert_not_called()
            transport.assert_not_called()
            result = adapter.probe()
        ensure.assert_not_called()
        transport.assert_not_called()
        version.assert_called_once_with(["opencode", "--version"])
        self.assertEqual(result.version, "1.18.9")


if __name__ == "__main__":
    unittest.main()


class OpenCodeServeIdentityTest(unittest.TestCase):
    """`_pid_is_opencode_serve` reads /proc cmdline as bytes; it must not crash.

    The catalogue swallows any exception from the provider probe as "no
    routes", so a TypeError here silently emptied the GUI's OpenCode model
    picker on every host where an orphan serve reached the identity check.
    """

    def _with_cmdline(self, raw: bytes) -> bool:
        with mock.patch.object(opencode.Path, "read_bytes", return_value=raw):
            return opencode._pid_is_opencode_serve(17195)

    def test_recognizes_an_opencode_serve_from_proc_cmdline(self) -> None:
        raw = (
            b"/home/op/.opencode/bin/opencode\x00serve\x00--hostname\x00"
            b"127.0.0.1\x00--port\x0055353\x00"
        )
        self.assertTrue(self._with_cmdline(raw))

    def test_rejects_a_process_that_is_not_an_opencode_serve(self) -> None:
        self.assertFalse(self._with_cmdline(b"python3\x00server.py\x00--port\x008837\x00"))
        self.assertFalse(self._with_cmdline(b"/usr/bin/opencode\x00models\x00"))

    def test_falls_back_to_ps_when_proc_is_unreadable(self) -> None:
        completed = mock.Mock(returncode=0, stdout="/usr/bin/opencode serve --port 1\n")
        with mock.patch.object(
            opencode.Path, "read_bytes", side_effect=OSError("no /proc")
        ), mock.patch.object(opencode.subprocess, "run", return_value=completed):
            self.assertTrue(opencode._pid_is_opencode_serve(17195))
