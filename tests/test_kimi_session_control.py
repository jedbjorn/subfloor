#!/usr/bin/env python3
"""Hermetic Kimi authenticated-server and fallback-resume fixtures."""
from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ADAPTER = ENGINE / "adapters" / "kimi"
EXPECTED_ENDPOINT = "http://127.0.0.1:43223"
sys.path.insert(0, str(ENGINE / "scripts"))
import run  # noqa: E402
import session_control as common_session_control  # noqa: E402

sys.path.insert(0, str(ADAPTER))
import kimi_http  # noqa: E402


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = load_file("kimi_session_control_adapter", ADAPTER / "session_control.py")
launcher_module = load_file("kimi_session_launcher", ADAPTER / "kimi-session.py")


def completed(command: list[str], output: str, returncode: int = 0):
    return subprocess.CompletedProcess(command, returncode, output, "")


def binding(*, permission: str = "auto", endpoint: str = EXPECTED_ENDPOINT) -> dict:
    capabilities = {
        "active_delivery": True,
        "deliver": True,
        "resume": True,
        "normal_steer": False,
        "settings": {
            "model": "kimi-code/k3",
            "cwd": "/repo/.sc-worktrees/pln1",
            "effort": "high",
            "permission_mode": permission,
        },
    }
    return {
        "binding_id": 8,
        "native_session_id": "session-native-1",
        "control_endpoint": endpoint,
        "control_capabilities": json.dumps(capabilities),
        "archive_model": "kimi-code/k3",
        "shortname": "PLN1",
        "flavor": "planner",
        "state": "idle",
    }


class ProbeTest(unittest.TestCase):
    @staticmethod
    def runner(version: str, *, web: bool, server: bool = True):
        def run(command: list[str]):
            if command == ["kimi", "--version"]:
                return completed(command, version)
            if command == ["kimi", "--help"]:
                return completed(command, "--session --prompt --model")
            if command == ["kimi", "web", "--help"]:
                text = "--foreground --no-open" if web else "web unavailable"
                return completed(command, text, 0 if web else 1)
            if command == ["kimi", "server", "run", "--help"]:
                text = "--foreground" if server else "server unavailable"
                return completed(command, text, 0 if server else 1)
            if command == ["kimi", "acp", "--help"]:
                return completed(command, "Agent Client Protocol (ACP)")
            raise AssertionError(command)

        return run

    def test_installed_027_uses_legacy_authenticated_server_tree(self):
        probe = kimi_http.probe_kimi(self.runner("0.27.0", web=False))
        self.assertEqual(probe["cli_version"], "0.27.0")
        self.assertEqual(
            probe["server_command"],
            ["kimi", "server", "run", "--port", "0", "--foreground"],
        )
        self.assertEqual(
            (probe["create"], probe["deliver"], probe["resume"], probe["acp"]),
            (True, True, True, True),
        )
        self.assertFalse(probe["normal_steer"])

    def test_current_028_prefers_web_and_keeps_auth_enabled(self):
        probe = kimi_http.probe_kimi(self.runner("0.28.1", web=True))
        self.assertEqual(
            probe["server_command"],
            ["kimi", "web", "--port", "0", "--foreground", "--no-open"],
        )
        self.assertNotIn("--dangerous-bypass-auth", probe["server_command"])
        self.assertTrue(probe["active_delivery"])

    def test_newer_version_is_accepted_on_feature_probe_evidence(self):
        probe = kimi_http.probe_kimi(self.runner("0.29.0", web=True))
        self.assertEqual(
            probe["server_command"],
            ["kimi", "web", "--port", "0", "--foreground", "--no-open"],
        )
        self.assertEqual(
            (probe["create"], probe["deliver"], probe["active_delivery"]),
            (True, True, True),
        )
        self.assertTrue(probe["resume"])

    def test_older_version_fails_closed_for_server_but_retains_tested_resume(self):
        probe = kimi_http.probe_kimi(self.runner("0.26.3", web=True))
        self.assertEqual(
            (probe["create"], probe["deliver"], probe["active_delivery"]),
            (False, False, False),
        )
        self.assertIsNone(probe["server_command"])
        self.assertTrue(probe["resume"])


class JsonResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None

    def read(self) -> bytes:
        return self.payload


class HttpClientTest(unittest.TestCase):
    def test_delivery_uses_normal_prompt_endpoint_and_waits_for_idle(self):
        requests: list[tuple[str, str, dict | None, str | None]] = []
        responses = iter(
            [
                {"code": 0, "data": {"prompt_id": "msg-7"}},
                {"code": 0, "data": {"active": None, "queued": []}},
                {
                    "code": 0,
                    "data": {
                        "busy": False,
                        "main_turn_active": False,
                        "pending_interaction": "none",
                        "last_turn_reason": "completed",
                    },
                },
            ]
        )

        def opener(request, *, timeout):
            body = json.loads(request.data) if request.data else None
            requests.append(
                (
                    request.method,
                    request.full_url,
                    body,
                    request.get_header("Authorization"),
                )
            )
            self.assertEqual(timeout, 10)
            return JsonResponse(next(responses))

        client = kimi_http.KimiClient(
            EXPECTED_ENDPOINT, "secret-token", opener=opener, sleeper=lambda _n: None
        )
        client.deliver(
            "session-native-1",
            "check inbox",
            model="kimi-code/k3",
            effort="high",
            permission_mode="auto",
        )

        self.assertEqual(
            [(method, url) for method, url, _body, _auth in requests],
            [
                ("POST", EXPECTED_ENDPOINT + "/api/v1/sessions/session-native-1/prompts"),
                ("GET", EXPECTED_ENDPOINT + "/api/v1/sessions/session-native-1/prompts"),
                ("GET", EXPECTED_ENDPOINT + "/api/v1/sessions/session-native-1"),
            ],
        )
        self.assertEqual(
            requests[0][2], {
                "content": [{"type": "text", "text": "check inbox"}],
                "model": "kimi-code/k3",
                "thinking": "high",
                "permission_mode": "auto",
            }
        )
        self.assertEqual({request[3] for request in requests}, {"Bearer secret-token"})
        self.assertFalse(any(":steer" in request[1] for request in requests))

    def test_structured_error_never_echoes_bearer_token(self):
        token = "sensitive-bearer-value"

        def opener(_request, *, timeout):
            self.assertEqual(timeout, 10)
            return JsonResponse(
                {"code": 40101, "msg": f"Authorization: Bearer {token}", "data": None}
            )

        client = kimi_http.KimiClient(EXPECTED_ENDPOINT, token, opener=opener)
        with self.assertRaises(kimi_http.KimiApiError) as caught:
            client.get_session("session-native-1")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_pending_approval_fails_instead_of_marking_delivery_complete(self):
        responses = iter(
            [
                {"code": 0, "data": {"prompt_id": "msg-7"}},
                {
                    "code": 0,
                    "data": {"active": {"prompt_id": "msg-7"}, "queued": []},
                },
                {
                    "code": 0,
                    "data": {"busy": True, "pending_interaction": "approval"},
                },
            ]
        )
        client = kimi_http.KimiClient(
            EXPECTED_ENDPOINT,
            "token",
            opener=lambda _request, timeout: JsonResponse(next(responses)),
            sleeper=lambda _n: None,
        )
        with self.assertRaisesRegex(RuntimeError, "interactive approval"):
            client.deliver(
                "session-native-1",
                "check inbox",
                model="kimi-code/k3",
                effort="high",
                permission_mode="auto",
            )


class FakeClient:
    def __init__(self, session: dict | None = None):
        self.session = session or {
            "busy": False,
            "main_turn_active": False,
            "pending_interaction": "none",
        }
        self.deliveries: list[tuple[str, str, dict]] = []

    def get_session(self, _session_id: str) -> dict:
        return dict(self.session)

    def deliver(self, session_id: str, prompt: str, **settings) -> None:
        self.deliveries.append((session_id, prompt, settings))


class AdapterTest(unittest.TestCase):
    def adapter(self, client: FakeClient, **kwargs):
        return adapter_module.KimiAdapter(
            client_factory=lambda endpoint, token: (
                self.assertEqual((endpoint, token), (EXPECTED_ENDPOINT, "runtime-token"))
                or client
            ),
            **kwargs,
        )

    def test_status_maps_idle_busy_and_missing_server_without_guessing(self):
        with mock.patch.object(adapter_module, "read_token", return_value="runtime-token"):
            self.assertEqual(self.adapter(FakeClient()).status(binding()), "idle")
            busy = FakeClient({
                "busy": True,
                "main_turn_active": True,
                "pending_interaction": "none",
            })
            self.assertEqual(self.adapter(busy).status(binding()), "active")

            def unavailable(_endpoint, _token):
                raise urllib.error.URLError("connection refused")

            dormant = adapter_module.KimiAdapter(client_factory=unavailable)
            self.assertEqual(dormant.status(binding()), "dormant")

    def test_busy_race_queues_without_prompt_or_steer(self):
        client = FakeClient({
            "busy": True,
            "main_turn_active": True,
            "pending_interaction": "none",
        })
        adapter = self.adapter(client)
        with mock.patch.object(adapter_module, "read_token", return_value="runtime-token"):
            with self.assertRaises(common_session_control.ProviderBusy):
                adapter.deliver(binding(), "check inbox")
        self.assertEqual(client.deliveries, [])

    def test_manual_permission_posture_refuses_before_auth_or_transport(self):
        clients: list[bool] = []
        adapter = adapter_module.KimiAdapter(
            client_factory=lambda _endpoint, _token: clients.append(True)
        )
        with self.assertRaisesRegex(RuntimeError, "permission_mode='auto'"):
            adapter.deliver(binding(permission="manual"), "check inbox")
        self.assertEqual(clients, [])

    def test_idle_delivery_uses_bound_native_session(self):
        client = FakeClient()
        adapter = self.adapter(client)
        with mock.patch.object(adapter_module, "read_token", return_value="runtime-token"):
            adapter.deliver(binding(), "check inbox")
        self.assertEqual(client.deliveries, [(
            "session-native-1",
            "check inbox",
            {
                "model": "kimi-code/k3",
                "effort": "high",
                "permission_mode": "auto",
            },
        )])

    def test_resume_pins_native_id_k3_route_effort_and_worktree(self):
        calls: list[tuple[dict, list[str], Path]] = []
        adapter = adapter_module.KimiAdapter(
            resume_runner=lambda row, command, cwd: calls.append((row, command, cwd)),
            resume_probe=lambda: {"resume": True},
        )
        row = binding()
        adapter.resume(row, "check inbox")
        self.assertEqual(
            calls,
            [
                (
                    row,
                    [
                        "kimi",
                        "--session",
                        "session-native-1",
                        "--model",
                        "kimi-code/k3",
                        "--prompt",
                        "check inbox",
                    ],
                    Path("/repo/.sc-worktrees/pln1"),
                )
            ],
        )
        self.assertEqual(
            adapter_module.resume_environment(row, {"PATH": "/bin"}),
            {"PATH": "/bin", "KIMI_MODEL_THINKING_EFFORT": "high"},
        )

    def test_non_loopback_or_credentialed_endpoint_fails_before_auth_read(self):
        adapter = adapter_module.KimiAdapter()
        with mock.patch.object(adapter_module, "read_token") as token_read:
            with self.assertRaisesRegex(ValueError, "loopback"):
                adapter.deliver(
                    binding(endpoint="http://user:pw@example.test:43223"), "check inbox"
                )
        token_read.assert_not_called()


class LauncherTest(unittest.TestCase):
    def test_controlled_launcher_is_declared(self):
        adapter = run.load_adapter("kimi")
        self.assertEqual(
            run.session_control_launch(adapter),
            ["python3", str(ADAPTER / "kimi-session.py")],
        )

    def test_runtime_token_file_is_mode_0600_and_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kimi-8.token"
            launcher_module.write_private(path, "runtime-secret")
            self.assertEqual(path.read_text(), "runtime-secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_runtime_token_file_is_removed_when_server_exits(self):
        class Connection:
            def execute(self, _query, _params):
                return self

            def fetchone(self):
                return {
                    "binding_id": 8,
                    "native_session_id": None,
                    "control_capabilities": json.dumps({
                        "settings": {"model": "kimi-code/k3", "effort": "high"}
                    }),
                }

            def close(self):
                return None

        server = mock.Mock(stdout=None, returncode=0)
        server.wait.return_value = 0
        server.poll.return_value = 0
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            runtime_token = run_dir / "kimi-8.token"

            def client(endpoint, token):
                self.assertEqual((endpoint, token), (EXPECTED_ENDPOINT, "runtime-secret"))
                self.assertEqual(runtime_token.read_text(), "runtime-secret\n")
                return mock.Mock()

            with (
                mock.patch.dict(
                    "os.environ", {"SC_SESSION_BINDING_ID": "8"}, clear=False
                ),
                mock.patch.object(launcher_module, "RUN_DIR", run_dir),
                mock.patch.object(
                    launcher_module,
                    "probe_kimi",
                    return_value={
                        "create": True,
                        "server_command": ["kimi", "server", "run"],
                        "cli_version": "0.27.0",
                    },
                ),
                mock.patch.object(
                    launcher_module.db_driver,
                    "connect",
                    side_effect=[Connection(), Connection()],
                ),
                mock.patch.object(launcher_module.subprocess, "Popen", return_value=server),
                mock.patch.object(
                    launcher_module,
                    "wait_for_server",
                    return_value=(EXPECTED_ENDPOINT, "runtime-secret"),
                ),
                mock.patch.object(launcher_module, "KimiClient", side_effect=client),
                mock.patch.object(
                    launcher_module,
                    "configure_session",
                    return_value=(
                        "session-native-1",
                        {
                            "model": "kimi-code/k3",
                            "cwd": str(ROOT),
                            "effort": "high",
                            "permission_mode": "auto",
                        },
                    ),
                ),
                mock.patch.object(
                    launcher_module.session_supervisor, "register_native_session"
                ),
                mock.patch.object(launcher_module.webbrowser, "open"),
            ):
                self.assertEqual(launcher_module.main(), 0)

            self.assertFalse(runtime_token.exists())

    def test_new_session_pins_k3_effort_and_auto_permission(self):
        class Client:
            def __init__(self):
                self.created: list[Path] = []
                self.profile: list[tuple[str, dict]] = []

            def create_session(self, cwd):
                self.created.append(cwd)
                return {"id": "session-created"}

            def update_profile(self, session_id, config):
                self.profile.append((session_id, config))
                return {}

            def get_status(self, session_id):
                self.status_id = session_id
                return {
                    "model": "kimi-code/k3",
                    "thinking_level": "high",
                    "permission": "auto",
                }

        client = Client()
        session_id, settings = launcher_module.configure_session(
            client,
            {"native_session_id": None},
            cwd=Path("/repo/.sc-worktrees/pln1"),
            model="kimi-code/k3",
            effort="high",
        )
        self.assertEqual(session_id, "session-created")
        self.assertEqual(client.created, [Path("/repo/.sc-worktrees/pln1")])
        self.assertEqual(
            client.profile,
            [
                (
                    "session-created",
                    {
                        "model": "kimi-code/k3",
                        "thinking": "high",
                        "permission_mode": "auto",
                    },
                )
            ],
        )
        self.assertEqual(
            settings,
            {
                "model": "kimi-code/k3",
                "cwd": "/repo/.sc-worktrees/pln1",
                "effort": "high",
                "permission_mode": "auto",
            },
        )
        self.assertNotIn("approval_policy", settings)
        self.assertNotIn("sandbox", settings)

    def test_existing_binding_resumes_exact_session_without_creating_another(self):
        class Client:
            def __init__(self):
                self.reads: list[str] = []

            def get_session(self, session_id):
                self.reads.append(session_id)
                return {"id": session_id}

            def create_session(self, _cwd):
                raise AssertionError("must not replace a missing/known native session")

            def update_profile(self, _session_id, _config):
                return {}

            def get_status(self, _session_id):
                return {
                    "model": "kimi-code/k3",
                    "thinking_level": "high",
                    "permission": "auto",
                }

        client = Client()
        session_id, _settings = launcher_module.configure_session(
            client,
            {"native_session_id": "session-existing"},
            cwd=Path("/repo/.sc-worktrees/pln1"),
            model="kimi-code/k3",
            effort="high",
        )
        self.assertEqual(session_id, "session-existing")
        self.assertEqual(client.reads, ["session-existing"])

    def test_effective_route_drift_fails_before_binding_registration(self):
        class Client:
            def create_session(self, _cwd):
                return {"id": "session-created"}

            def update_profile(self, _session_id, _config):
                return {}

            def get_status(self, _session_id):
                return {
                    "model": "kimi-code/k2",
                    "thinking_level": "high",
                    "permission": "auto",
                }

        with self.assertRaisesRegex(RuntimeError, "route drifted"):
            launcher_module.configure_session(
                Client(),
                {"native_session_id": None},
                cwd=Path("/repo/.sc-worktrees/pln1"),
                model="kimi-code/k3",
                effort="high",
            )


if __name__ == "__main__":
    unittest.main()
