#!/usr/bin/env python3
"""Hermetic Codex app-server adapter and protocol fixtures."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ADAPTER = ENGINE / "adapters" / "codex"
EXPECTED_ENDPOINT = f"unix://{ENGINE / 'run' / 'session-control' / 'codex-8.sock'}"
sys.path.insert(0, str(ADAPTER))
sys.path.insert(0, str(ENGINE / "scripts"))

import codex_rpc  # noqa: E402
import run  # noqa: E402


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = load_file("codex_session_control_adapter", ADAPTER / "session_control.py")
launcher_module = load_file("codex_session_launcher", ADAPTER / "codex-session.py")


class BytesSocket:
    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.payload[:size])
        del self.payload[:size]
        return chunk


class RpcFixture:
    def __init__(self, state: str = "idle"):
        self.state = state
        self.requests: list[tuple[str, dict]] = []
        self.waits: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None

    def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": params["threadId"],
                               "status": {"type": self.state}}}
        if method == "thread/resume":
            self.state = "idle"
            return {"thread": {"id": params["threadId"],
                               "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn-7", "status": "inProgress"}}
        raise AssertionError(f"unexpected RPC method {method}")

    def wait_notification(self, method: str, predicate, *, timeout: float = 3600):
        self.waits.append(method)
        message = {"method": "turn/completed", "params": {
            "turn": {"id": "turn-7", "status": "completed"}
        }}
        if not predicate(message):
            raise AssertionError("completion predicate rejected the target turn")
        return message


def binding(*, state: str = "idle", endpoint: str = EXPECTED_ENDPOINT) -> dict:
    capabilities = {
        "active_delivery": True,
        "resume": True,
        "normal_steer": False,
        "settings": {
            "model": "gpt-5.6-sol",
            "cwd": "/repo/.sc-worktrees/pln1",
            "sandbox": "danger-full-access",
            "approval_policy": "never",
        },
    }
    return {
        "binding_id": 8,
        "native_session_id": "019-native-thread",
        "control_endpoint": endpoint,
        "control_capabilities": json.dumps(capabilities),
        "archive_model": "gpt-5.6-sol",
        "shortname": "PLN1",
        "flavor": "planner",
        "state": state,
    }


class ProtocolFrameTest(unittest.TestCase):
    def test_client_frames_round_trip_masked_across_all_length_encodings(self):
        for payload in (b"hello", b"x" * 126, b"y" * 65536):
            with self.subTest(size=len(payload)):
                frame = codex_rpc.encode_frame(payload, mask=b"mask")
                final, opcode, decoded = codex_rpc.decode_frame(BytesSocket(frame))
                self.assertEqual((final, opcode, decoded), (True, 1, payload))
                self.assertTrue(frame[1] & 0x80, "client frame must be masked")

    def test_unix_endpoint_rejects_tcp_and_relative_paths(self):
        with self.assertRaisesRegex(ValueError, "unix"):
            codex_rpc.unix_socket_path("ws://127.0.0.1:4500")
        with self.assertRaisesRegex(ValueError, "absolute"):
            codex_rpc.unix_socket_path("unix://relative.sock")

    def test_unexpected_server_request_fails_closed_instead_of_hanging(self):
        client = codex_rpc.AppServerClient(EXPECTED_ENDPOINT)
        client.sock = object()
        client._send_json = lambda _payload: None
        client._recv_json = lambda: {
            "id": 91, "method": "item/commandExecution/requestApproval", "params": {}
        }
        with self.assertRaisesRegex(codex_rpc.CodexProtocolError,
                                    "unsupported Codex server request"):
            client.request("turn/start", {})


class VersionProbeTest(unittest.TestCase):
    @staticmethod
    def runner_for(version: str):
        def run(command, **_kwargs):
            if command == ["codex", "--version"]:
                return subprocess.CompletedProcess(command, 0, f"codex-cli {version}\n", "")
            if command == ["codex", "app-server", "--help"]:
                return subprocess.CompletedProcess(command, 0, "--listen unix://PATH", "")
            if command == ["codex", "exec", "resume", "--help"]:
                return subprocess.CompletedProcess(command, 0, "SESSION_ID PROMPT", "")
            raise AssertionError(command)
        return run

    def test_validated_version_enables_active_delivery_and_resume(self):
        result = codex_rpc.probe_codex(self.runner_for("0.144.6"))
        self.assertEqual(result["cli_version"], "0.144.6")
        self.assertEqual(result["transport"], "unix-websocket-v2")
        self.assertTrue(result["active_delivery"])
        self.assertTrue(result["resume"])
        self.assertFalse(result["normal_steer"])

    def test_unknown_version_fails_active_closed_but_keeps_smoke_tested_resume(self):
        result = codex_rpc.probe_codex(self.runner_for("0.145.0"))
        self.assertFalse(result["create"])
        self.assertFalse(result["active_delivery"])
        self.assertTrue(result["resume"])


class AdapterTest(unittest.TestCase):
    def test_idle_status_reads_exact_bound_thread(self):
        rpc = RpcFixture("idle")
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda _endpoint: rpc,
            endpoint_available=lambda _path: True,
        )
        self.assertEqual(adapter.status(binding()), "idle")
        self.assertEqual(rpc.requests, [(
            "thread/read", {"threadId": "019-native-thread", "includeTurns": False}
        )])

    def test_active_status_keeps_delivery_queued(self):
        rpc = RpcFixture("active")
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda _endpoint: rpc,
            endpoint_available=lambda _path: True,
        )
        self.assertEqual(adapter.status(binding()), "active")
        self.assertEqual([method for method, _params in rpc.requests], ["thread/read"])

    def test_missing_server_is_dormant_without_opening_a_client(self):
        opened: list[str] = []
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda endpoint: opened.append(endpoint),
            endpoint_available=lambda _path: False,
        )
        self.assertEqual(adapter.status(binding()), "dormant")
        self.assertEqual(opened, [])

    def test_binding_cannot_redirect_control_to_another_unix_socket(self):
        adapter = adapter_module.CodexAdapter(endpoint_available=lambda _path: True)
        self.assertEqual(
            adapter.status(binding(endpoint="unix:///tmp/unrelated.sock")), "error"
        )

    def test_idle_delivery_starts_one_turn_waits_and_never_steers(self):
        rpc = RpcFixture("idle")
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda _endpoint: rpc,
            endpoint_available=lambda _path: True,
        )
        adapter.deliver(binding(), "check inbox")
        self.assertEqual(rpc.requests, [
            ("thread/read", {"threadId": "019-native-thread", "includeTurns": False}),
            ("turn/start", {
                "threadId": "019-native-thread",
                "input": [{"type": "text", "text": "check inbox"}],
            }),
        ])
        self.assertEqual(rpc.waits, ["turn/completed"])
        self.assertNotIn("turn/steer", [method for method, _params in rpc.requests])

    def test_approval_prompting_delivery_refuses_before_opening_transport(self):
        row = binding()
        capabilities = json.loads(row["control_capabilities"])
        capabilities["settings"].update(
            sandbox="workspace-write", approval_policy="on-request"
        )
        row["control_capabilities"] = json.dumps(capabilities)
        opened: list[str] = []
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda endpoint: opened.append(endpoint),
            endpoint_available=lambda _path: True,
        )

        with self.assertRaisesRegex(RuntimeError, "managed wake requires"):
            adapter.deliver(row, "check inbox")

        self.assertEqual(opened, [])

    def test_active_race_refuses_turn_start_and_never_steers(self):
        rpc = RpcFixture("active")
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda _endpoint: rpc,
            endpoint_available=lambda _path: True,
        )
        with self.assertRaisesRegex(RuntimeError, "became active"):
            adapter.deliver(binding(), "check inbox")
        self.assertEqual([method for method, _params in rpc.requests], ["thread/read"])
        self.assertNotIn("turn/steer", [method for method, _params in rpc.requests])

    def test_unloaded_thread_resumes_with_pinned_settings_before_turn(self):
        rpc = RpcFixture("notLoaded")
        adapter = adapter_module.CodexAdapter(
            client_factory=lambda _endpoint: rpc,
            endpoint_available=lambda _path: True,
        )
        adapter.deliver(binding(), "check inbox")
        self.assertEqual(rpc.requests[1], (
            "thread/resume", {
                "threadId": "019-native-thread",
                "cwd": "/repo/.sc-worktrees/pln1",
                "model": "gpt-5.6-sol",
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
            }
        ))
        self.assertEqual(rpc.requests[2][0], "turn/start")

    def test_resume_uses_native_id_pinned_route_and_injected_fenced_runner(self):
        calls: list[tuple[dict, list[str], Path]] = []
        adapter = adapter_module.CodexAdapter(
            resume_runner=lambda row, command, cwd: calls.append((row, command, cwd)),
            resume_probe=lambda: {"resume": True},
        )
        row = binding()
        adapter.resume(row, "check inbox")
        self.assertEqual(calls, [(
            row,
            ["codex", "exec", "resume", "-m", "gpt-5.6-sol",
             "--dangerously-bypass-approvals-and-sandbox",
             "--dangerously-bypass-hook-trust", "019-native-thread", "check inbox"],
            Path("/repo/.sc-worktrees/pln1"),
        )])

    def test_resume_reprobes_current_cli_before_spawning(self):
        calls: list[list[str]] = []
        adapter = adapter_module.CodexAdapter(
            resume_runner=lambda _row, command, _cwd: calls.append(command),
            resume_probe=lambda: {"resume": False},
        )
        with self.assertRaisesRegex(RuntimeError, "failed the resume capability probe"):
            adapter.resume(binding(), "check inbox")
        self.assertEqual(calls, [])

    def test_approval_prompting_resume_refuses_before_probe_or_spawn(self):
        row = binding()
        capabilities = json.loads(row["control_capabilities"])
        capabilities["settings"].update(
            sandbox="workspace-write", approval_policy="on-request"
        )
        row["control_capabilities"] = json.dumps(capabilities)
        probes: list[bool] = []
        calls: list[list[str]] = []
        adapter = adapter_module.CodexAdapter(
            resume_runner=lambda _row, command, _cwd: calls.append(command),
            resume_probe=lambda: probes.append(True),
        )

        with self.assertRaisesRegex(RuntimeError, "managed wake requires"):
            adapter.resume(row, "check inbox")

        self.assertEqual(probes, [])
        self.assertEqual(calls, [])

    def test_host_resume_pins_effective_sandbox_approval_and_effort(self):
        row = binding()
        capabilities = json.loads(row["control_capabilities"])
        capabilities["settings"].update(
            sandbox="workspace-write", approval_policy="on-request", effort="high"
        )
        row["control_capabilities"] = json.dumps(capabilities)
        command = adapter_module.resume_command(row, "check inbox")
        self.assertEqual(command, [
            "codex", "exec", "resume", "-m", "gpt-5.6-sol",
            "-c", 'sandbox_mode="workspace-write"',
            "-c", 'approval_policy="on-request"',
            "-c", 'model_reasoning_effort="high"',
            "--dangerously-bypass-hook-trust", "019-native-thread", "check inbox",
        ])


class LaunchIntegrationTest(unittest.TestCase):
    def test_adapter_declares_controlled_launcher(self):
        adapter = run.load_adapter("codex")
        self.assertEqual(run.session_control_launch(adapter), [
            "python3", str(ADAPTER / "codex-session.py")
        ])

    def test_socket_cleanup_is_scoped_and_removes_stale_runtime_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir()
            stale = root / "codex-8.sock"
            stale.write_text("stale")
            launcher_module.cleanup_socket(stale, root=root)
            self.assertFalse(stale.exists())

            outside = Path(tmp) / "outside.sock"
            outside.write_text("preserve")
            with self.assertRaisesRegex(ValueError, "outside"):
                launcher_module.cleanup_socket(outside, root=root)
            self.assertEqual(outside.read_text(), "preserve")

    def test_controlled_launch_captures_thread_and_attaches_remote_tui(self):
        class FakeConnection:
            def execute(self, sql, params):
                self.assert_query = (sql, params)
                return self

            def fetchone(self):
                return {"native_session_id": None}

            def close(self):
                return None

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = -9

        class FakeClient:
            requests: list[tuple[str, dict]] = []

            def __init__(self, endpoint):
                self.endpoint = endpoint

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return None

            def request(self, method, params):
                self.requests.append((method, params))
                if method == "config/read":
                    return {"config": {
                        "model": "config-model",
                        "sandbox_mode": "workspace-write",
                        "approval_policy": "on-request",
                        "model_reasoning_effort": "high",
                    }}
                return {"thread": {"id": "019-captured"}}

        processes: list[FakeProcess] = []
        registrations: list[tuple] = []

        def popen(command, **_kwargs):
            process = FakeProcess(command)
            processes.append(process)
            return process

        def register(_con, binding_id, native_id, **kwargs):
            registrations.append((binding_id, native_id, kwargs))

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(launcher_module, "RUN_DIR", Path(tmp)), \
             mock.patch.object(launcher_module, "AppServerClient", FakeClient), \
             mock.patch.object(launcher_module, "probe_codex", return_value={
                 "cli_version": "0.144.6", "create": True,
                 "active_delivery": True, "resume": True,
             }), \
             mock.patch.object(launcher_module.db_driver, "connect",
                               return_value=FakeConnection()), \
             mock.patch.object(launcher_module.session_supervisor,
                               "register_native_session", side_effect=register), \
             mock.patch.object(launcher_module.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(launcher_module.signal, "signal"), \
             mock.patch.dict(os.environ, {
                 "SC_SESSION_BINDING_ID": "8",
                 "SC_SESSION_MODEL": "gpt-5.6-sol",
                 "SC_SANDBOX": "1",
             }):
            self.assertEqual(launcher_module.main(), 0)

        endpoint = f"unix://{Path(tmp) / 'codex-8.sock'}"
        self.assertEqual(processes[0].command, [
            "codex", "--dangerously-bypass-hook-trust", "app-server",
            "--listen", endpoint,
        ])
        self.assertEqual(FakeClient.requests, [(
            "config/read", {
                "cwd": str(Path.cwd().resolve()), "includeLayers": False,
            }
        ), (
            "thread/start", {
                "cwd": str(Path.cwd().resolve()),
                "model": "gpt-5.6-sol",
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
            }
        )])
        self.assertEqual(registrations[0][0:2], (8, "019-captured"))
        self.assertEqual(registrations[0][2]["control_endpoint"], endpoint)
        recorded = json.loads(registrations[0][2]["capabilities"])
        self.assertEqual(recorded["settings"]["model"], "gpt-5.6-sol")
        self.assertEqual(recorded["settings"]["sandbox"], "danger-full-access")
        self.assertEqual(recorded["settings"]["approval_policy"], "never")
        self.assertEqual(recorded["settings"]["effort"], "high")
        self.assertEqual(processes[1].command, [
            "codex", "--dangerously-bypass-hook-trust", "--remote", endpoint,
            "--dangerously-bypass-approvals-and-sandbox", "resume", "019-captured",
        ])
