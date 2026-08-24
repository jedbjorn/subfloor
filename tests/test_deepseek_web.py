"""Official dsh Web/Host lifecycle regressions."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

deepseek_web = importlib.import_module("deepseek_web")


def service_env(root: Path) -> dict[str, str]:
    return {
        "SC_SANDBOX": "1",
        "SC_DEEPSEEK_HOST_PORT": "8942",
        "SC_DEEPSEEK_WEB_STATE": str(root / "state.json"),
        "SC_DEEPSEEK_WEB_LOG": str(root / "service.log"),
        "SC_DEEPSEEK_WEB_LOCK": str(root / "service.lock"),
        "SC_API_TOKEN": "test-shell-token",
        "SC_API_BASE": "http://127.0.0.1:8837",
        "SC_SHELL_ID": "4",
        "SC_SHELL_SHORTNAME": "DEV4",
    }


def test_ensure_starts_exact_stock_web_relay_registers_and_reuses() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / ".sc-worktrees" / "dev4"
        worktree.mkdir(parents=True)
        spawned: list[tuple[list[str], Path, dict[str, str] | None]] = []

        def spawn(
            argv: list[str],
            *,
            cwd: Path,
            log: Path,
            env: dict[str, str] | None = None,
        ) -> tuple[int, int]:
            spawned.append((argv, cwd, env))
            return (101 + len(spawned), 201 + len(spawned))

        with (
            mock.patch.dict(deepseek_web.os.environ, service_env(root), clear=False),
            mock.patch.object(deepseek_web, "REPO_ROOT", root),
            mock.patch.object(
                deepseek_web.ports,
                "resolve",
                return_value={"deepseek_host_port": 8942},
            ),
            mock.patch.object(deepseek_web.shutil, "which", return_value="/bin/dsh"),
            mock.patch.object(deepseek_web, "_spawn", side_effect=spawn),
            mock.patch.object(deepseek_web, "_http_ready", return_value=True),
            mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
            mock.patch.object(
                deepseek_web,
                "_relay_allowed_peers",
                return_value=("127.0.0.1", "172.18.0.1"),
            ),
            mock.patch.object(
                deepseek_web,
                "_post_workspace",
                return_value={"workspace_id": "ws-4", "workspace_created": True},
            ) as register,
                mock.patch.object(
                    deepseek_web,
                    "_verified_process",
                    side_effect=lambda pid, *_args, **_kwargs: isinstance(pid, int),
                ),
                mock.patch.object(deepseek_web, "_verify_shell_identity"),
            ):
            first = deepseek_web.ensure(worktree, env=service_env(root))
            second = deepseek_web.ensure(worktree, env=service_env(root))

        assert first["reused"] is False
        assert second["reused"] is True
        assert first["url"].startswith("http://127.0.0.1:18942/?sc_generation=")
        assert spawned[0][:2] == (
            [
                "/bin/dsh",
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                "8942",
                "--no-open",
            ],
            worktree,
        )
        assert spawned[1][0][3:5] == [
            "--listen-host",
            "0.0.0.0",
        ]
        assert spawned[1][0][9:13] == [
            "--listen-port",
            "18942",
            "--target-port",
            "8942",
        ]
        assert spawned[1][0][-2:] == ["--generation-file", str(root / "deepseek-web-generation.json")]
        assert spawned[1][0][5:9] == [
            "--allowed-peer",
            "127.0.0.1",
            "--allowed-peer",
            "172.18.0.1",
        ]
        assert register.call_count == 2
        state = json.loads((root / "state.json").read_text())
        assert state["last_worktree"] == str(worktree)
        assert state["last_workspace_id"] == "ws-4"
        assert "sc_generation=" not in state["url"]


def test_shell_identity_reaches_stock_host_only_through_owner_only_artifact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / ".sc-worktrees" / "dev4"
        worktree.mkdir(parents=True)
        env = {
            **service_env(root),
            "SC_API_TOKEN": "shell-token-never-in-host-env",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_SHORTNAME": "DEV4",
        }
        spawned: list[dict[str, str] | None] = []

        def spawn(
            argv: list[str],
            *,
            cwd: Path,
            log: Path,
            env: dict[str, str] | None = None,
        ) -> tuple[int, int]:
            spawned.append(env)
            return (101 + len(spawned), 201 + len(spawned))

        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "REPO_ROOT", root),
            mock.patch.object(
                deepseek_web.ports,
                "resolve",
                return_value={"deepseek_host_port": 8942},
            ),
            mock.patch.object(deepseek_web.shutil, "which", return_value="/bin/dsh"),
            mock.patch.object(deepseek_web, "_spawn", side_effect=spawn),
            mock.patch.object(deepseek_web, "_http_ready", return_value=True),
            mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
            mock.patch.object(
                deepseek_web,
                "_relay_allowed_peers",
                return_value=("127.0.0.1", "172.18.0.1"),
            ),
            mock.patch.object(
                deepseek_web,
                "_post_workspace",
                return_value={"workspace_id": "ws-4", "workspace_created": True},
            ),
            mock.patch.object(
                deepseek_web,
                "_verified_process",
                side_effect=lambda pid, *_args, **_kwargs: isinstance(pid, int),
            ),
            mock.patch.object(deepseek_web, "_verify_shell_identity"),
        ):
            result = deepseek_web.ensure(worktree, env=env)

        assert result["credential_shell"] == "DEV4"
        artifact = root / "deepseek-shell-api.json"
        assert artifact.stat().st_mode & 0o777 == 0o600
        assert json.loads(artifact.read_text()) == {
            "shell_id": 4,
            "shortname": "DEV4",
            "api_base": "http://127.0.0.1:8837",
            "token": "shell-token-never-in-host-env",
        }
        assert spawned[0] is not None
        assert "SC_API_TOKEN" not in spawned[0]
        assert "SC_API_BASE" not in spawned[0]
        assert spawned[0]["SC_MEM_CREDENTIAL_FILE"] == str(artifact)
        assert spawned[1] is not None
        assert "SC_API_TOKEN" not in spawned[1]
        assert "SC_API_BASE" not in spawned[1]


def test_two_shell_handoff_rotates_only_after_empty_gateway_quiescence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        first_worktree = root / "pln1"
        second_worktree = root / "pln2"
        first_worktree.mkdir()
        second_worktree.mkdir()
        pln1 = {**service_env(root), "SC_API_TOKEN": "pln1-token"}
        pln2 = {
            **pln1,
            "SC_API_TOKEN": "pln2-token",
            "SC_SHELL_ID": "5",
            "SC_SHELL_SHORTNAME": "DEV5",
        }
        spawned: list[tuple[list[str], dict[str, str] | None]] = []
        terminated: list[str] = []
        verified: list[tuple[str, str]] = []

        def spawn(argv, *, env=None, **_kwargs):
            spawned.append((argv, env))
            return (100 + len(spawned), 200 + len(spawned))

        def verify(env):
            verified.append((env["SC_SHELL_ID"], env["SC_SHELL_SHORTNAME"]))

        with (
            mock.patch.dict(deepseek_web.os.environ, pln1, clear=False),
            mock.patch.object(deepseek_web, "REPO_ROOT", root),
            mock.patch.object(
                deepseek_web.ports,
                "resolve",
                return_value={"deepseek_host_port": 8942},
            ),
            mock.patch.object(deepseek_web.shutil, "which", return_value="/bin/dsh"),
            mock.patch.object(deepseek_web, "_spawn", side_effect=spawn),
            mock.patch.object(deepseek_web, "_http_ready", return_value=True),
            mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
            mock.patch.object(
                deepseek_web,
                "_relay_allowed_peers",
                return_value=("127.0.0.1", "172.18.0.1"),
            ),
            mock.patch.object(
                deepseek_web,
                "_post_workspace",
                side_effect=[
                    {"workspace_id": "ws-pln1", "workspace_created": True},
                    {"workspace_id": "ws-pln2", "workspace_created": True},
                ],
            ),
            mock.patch.object(
                deepseek_web,
                "_verified_process",
                side_effect=lambda pid, *_args, **_kwargs: isinstance(pid, int),
            ),
            mock.patch.object(
                deepseek_web,
                "_terminate_verified",
                side_effect=lambda _state, prefix: terminated.append(prefix) or True,
            ),
            mock.patch.object(deepseek_web, "_verify_shell_identity", side_effect=verify),
        ):
            first = deepseek_web.ensure(first_worktree, env=pln1)
            old_generation = first["url"].split("sc_generation=", 1)[1]
            second = deepseek_web.ensure(second_worktree, env=pln2)

        state = json.loads((root / "state.json").read_text())
        credential = json.loads((root / "deepseek-shell-api.json").read_text())
        assert verified == [("4", "DEV4"), ("5", "DEV5")]
        assert terminated == ["relay", "web", "relay", "web"]
        assert first["credential_shell"] == "DEV4"
        assert second["credential_shell"] == "DEV5"
        assert state["credential_shell_id"] == 5
        assert credential == {
            "shell_id": 5,
            "shortname": "DEV5",
            "api_base": "http://127.0.0.1:8837",
            "token": "pln2-token",
        }
        assert old_generation not in (root / "state.json").read_text()
        assert "pln1-token" not in (root / "state.json").read_text()
        assert spawned[2][1] is not None
        assert spawned[2][1]["SC_SHELL_ID"] == "5"
        assert "SC_API_TOKEN" not in spawned[2][1]


def test_non_sandbox_entry_still_uses_the_generation_gateway() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        env = service_env(root)
        env.pop("SC_SANDBOX")
        spawned: list[list[str]] = []

        def spawn(argv, **_kwargs):
            spawned.append(argv)
            return (101 + len(spawned), 201 + len(spawned))

        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "REPO_ROOT", root),
            mock.patch.object(
                deepseek_web.ports,
                "resolve",
                return_value={"deepseek_host_port": 8942},
            ),
            mock.patch.object(deepseek_web.shutil, "which", return_value="/bin/dsh"),
            mock.patch.object(deepseek_web, "_spawn", side_effect=spawn),
            mock.patch.object(deepseek_web, "_http_ready", return_value=True),
            mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
            mock.patch.object(
                deepseek_web,
                "_post_workspace",
                return_value={"workspace_id": "ws-4", "workspace_created": True},
            ),
            mock.patch.object(
                deepseek_web,
                "_verified_process",
                side_effect=lambda pid, *_args, **_kwargs: isinstance(pid, int),
            ),
            mock.patch.object(deepseek_web, "_verify_shell_identity"),
        ):
            result = deepseek_web.ensure(worktree, env=env)

        assert result["url"].startswith("http://127.0.0.1:8942/?sc_generation=")
        assert spawned[1][3:9] == [
            "--listen-host",
            "127.0.0.1",
            "--allowed-peer",
            "127.0.0.1",
            "--listen-port",
            "8942",
        ]
        state = json.loads((root / "state.json").read_text())
        assert state["relay_listen_host"] == "127.0.0.1"
        assert state["relay_allowed_peers"] == ["127.0.0.1"]


def test_gateway_quiesces_before_host_and_credential_rotation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        (root / "state.json").write_text(json.dumps({"relay_pid": 12, "web_pid": 13}))
        (root / "deepseek-shell-api.json").write_text("credential")
        (root / "deepseek-web-generation.json").write_text("generation")
        calls: list[str] = []

        def terminate(_state, prefix):
            calls.append(prefix)
            return True

        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "_terminate_verified", side_effect=terminate),
        ):
            deepseek_web._initialize_activity()
            result = deepseek_web._stop_unlocked()

        assert result == {"stopped": True, "web": True, "relay": True}
        assert calls == ["relay", "web"]
        assert not (root / "state.json").exists()
        assert not (root / "deepseek-shell-api.json").exists()
        assert not (root / "deepseek-web-generation.json").exists()


def test_dead_state_without_activity_is_replaced_before_new_owner_starts() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        state = root / "state.json"
        state.write_text(json.dumps({"relay_pid": 12, "web_pid": 13}))
        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "_verified_process", return_value=False),
        ):
            result = deepseek_web._stop_unlocked()

        assert result == {"stopped": False, "web": True, "relay": True}
        assert not state.exists()


def test_gateway_quiescence_failure_preserves_old_host_credential() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        (root / "state.json").write_text(json.dumps({"relay_pid": 12, "web_pid": 13}))
        credential = root / "deepseek-shell-api.json"
        credential.write_text("credential")
        calls: list[str] = []

        def terminate(_state, prefix):
            calls.append(prefix)
            return prefix != "relay"

        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "_terminate_verified", side_effect=terminate),
            pytest.raises(deepseek_web.DeepSeekWebError) as refused,
        ):
            deepseek_web._initialize_activity()
            deepseek_web._stop_unlocked()

        assert refused.value.code == "HARNESS_WEB_GATEWAY_BUSY"
        assert calls == ["relay"]
        assert credential.read_text() == "credential"


def test_unproven_browser_work_refuses_handoff_before_gateway_termination() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        (root / "state.json").write_text(
            json.dumps({"relay_pid": 12, "web_pid": 13, "service_port": 8942})
        )
        credential = root / "deepseek-shell-api.json"
        credential.write_text("credential")
        session_id = "sc-" + "2" * 32
        calls: list[str] = []

        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "_host_rpc", return_value={"events": []}),
            mock.patch.object(
                deepseek_web,
                "_terminate_verified",
                side_effect=lambda _state, prefix: calls.append(prefix) or True,
            ),
        ):
            deepseek_web._write_activity({
                "admission": "open",
                "requests": {
                    "browser-rpc": {
                        "session_id": session_id,
                        "boundary": 0,
                        "status": "accepted",
                    },
                },
            })
            with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
                deepseek_web._stop_unlocked()
            active = deepseek_web._read_activity(required=True)["requests"]

        assert refused.value.code == "HARNESS_WEB_GATEWAY_BUSY"
        assert calls == []
        assert credential.read_text() == "credential"
        assert active == {
            "browser-rpc": {
                "session_id": session_id,
                "boundary": 0,
                "status": "accepted",
            }
        }


def test_disabled_deepseek_stops_owned_service_without_launching() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        with (
            mock.patch.dict(deepseek_web.os.environ, service_env(root), clear=False),
                mock.patch.object(deepseek_web, "REPO_ROOT", root),
                mock.patch.object(deepseek_web, "_stop_unlocked") as stopped,
                mock.patch.object(deepseek_web, "_verify_shell_identity"),
            mock.patch.object(
                deepseek_web.shutil,
                "which",
                side_effect=AssertionError("disabled harness must not launch"),
            ),
            pytest.raises(deepseek_web.DeepSeekWebError) as unavailable,
        ):
            deepseek_web.ensure(
                worktree,
                env={**service_env(root), "SC_DISABLED_HARNESSES": "codex,deepseek"},
            )

        assert unavailable.value.code == "HARNESS_DISABLED"
        stopped.assert_called_once_with()


def test_shell_identity_verifies_whoami_before_host_handoff() -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"shell_id":4,"shortname":"DEV4"}'

    captured = []

    def urlopen(request, *, timeout):
        captured.append((request, timeout))
        return Response()

    env = {
        "SC_API_TOKEN": "test-token",
        "SC_API_BASE": "http://127.0.0.1:8837",
        "SC_SHELL_ID": "4",
        "SC_SHELL_SHORTNAME": "DEV4",
    }
    with mock.patch.object(deepseek_web.urllib.request, "urlopen", side_effect=urlopen):
        deepseek_web._verify_shell_identity(env)

    request, timeout = captured[0]
    assert request.full_url == "http://127.0.0.1:8837/_sc/mem/whoami"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert timeout == deepseek_web.HTTP_TIMEOUT_SECONDS


def test_shell_identity_rejects_same_shortname_with_a_different_shell_id() -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"shell_id":9,"shortname":"DEV4"}'

    env = {
        "SC_API_TOKEN": "test-token",
        "SC_API_BASE": "http://127.0.0.1:8837",
        "SC_SHELL_ID": "4",
        "SC_SHELL_SHORTNAME": "DEV4",
    }
    with (
        mock.patch.object(deepseek_web.urllib.request, "urlopen", return_value=Response()),
        pytest.raises(deepseek_web.DeepSeekWebError) as refused,
    ):
        deepseek_web._verify_shell_identity(env)

    assert refused.value.code == "HARNESS_SHELL_IDENTITY_MISMATCH"


def test_shell_identity_lease_refuses_an_overlapping_owner() -> None:
    with tempfile.TemporaryDirectory() as raw:
        env = {**service_env(Path(raw))}
        first = deepseek_web.acquire_shell_identity(env=env)
        try:
            with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
                deepseek_web.acquire_shell_identity(env=env)
            assert refused.value.code == "HARNESS_SHELL_IDENTITY_BUSY"
        finally:
            first.close()

        second = deepseek_web.acquire_shell_identity(env=env)
        second.close()


def test_two_canonical_shells_refuse_before_host_or_workflow_side_effects() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        pln1 = service_env(root)
        pln2 = {
            **pln1,
            "SC_API_TOKEN": "pln2-token",
            "SC_SHELL_ID": "5",
            "SC_SHELL_SHORTNAME": "DEV5",
        }
        protected_artifacts = [
            root / "state.json",
            root / "deepseek-shell-api.json",
            root / "deepseek-web-generation.json",
            root / "deepseek-web-activity.json",
        ]
        first = deepseek_web.acquire_shell_identity(env=pln1)
        try:
            with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
                deepseek_web.acquire_shell_identity(env=pln2)
            assert refused.value.code == "HARNESS_SHELL_IDENTITY_BUSY"
        finally:
            first.close()

        # A refusal occurs before any Host credential/generation mutation; no
        # model prompt can consequently reach tool-backed memory, workflow,
        # message, or wake surfaces under the wrong shell.
        assert [path.exists() for path in protected_artifacts] == [False] * 4
        second = deepseek_web.acquire_shell_identity(env=pln2)
        second.close()


def test_sandbox_service_fails_closed_without_exact_injected_host_port() -> None:
    config = {"deepseek_host_port": 8942}

    with pytest.raises(deepseek_web.DeepSeekWebError) as missing:
        deepseek_web._service_port(config, {"SC_SANDBOX": "1"})
    assert missing.value.code == "HARNESS_ENDPOINT_UNAVAILABLE"
    assert "./sc launch --no-build" in missing.value.detail

    for env in (
        {"SC_SANDBOX": "1", "SC_DEEPSEEK_HOST_PORT": "invalid"},
        {"SC_SANDBOX": "1", "SC_DEEPSEEK_HOST_PORT": "8943"},
    ):
        with pytest.raises(deepseek_web.DeepSeekWebError) as unavailable:
            deepseek_web._service_port(config, env)
        assert unavailable.value.code == "HARNESS_ENDPOINT_UNAVAILABLE"


def test_default_gateway_is_derived_from_the_namespace_route_table() -> None:
    with tempfile.TemporaryDirectory() as raw:
        route = Path(raw) / "route"
        route.write_text(
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 00000000 010011AC 0003 0 0 0 00000000 0 0 0\n"
        )

        assert deepseek_web._default_gateway(route) == "172.17.0.1"


def test_relay_rejects_sibling_source_before_opening_stock_host() -> None:
    async def scenario() -> None:
        upstream_connections: list[tuple[str, int]] = []

        async def echo(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            upstream_connections.append(writer.get_extra_info("peername"))
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\npong")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        allowed = frozenset({"127.0.0.1"})
        relay = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, allowed
            ),
            "127.0.0.1",
            0,
        )
        relay_port = relay.sockets[0].getsockname()[1]

        async def exchange(source: str) -> bytes:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", relay_port, local_addr=(source, 0)
            )
            try:
                writer.write(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                await writer.drain()
                try:
                    return await asyncio.wait_for(reader.read(512), timeout=1)
                except ConnectionResetError:
                    return b""
            finally:
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()

        try:
            assert b"HTTP/1.1 200 OK" in await exchange("127.0.0.1")
            assert len(upstream_connections) == 1
            assert await exchange("127.0.0.2") == b""
            assert len(upstream_connections) == 1
        finally:
            relay.close()
            upstream.close()
            await relay.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_gateway_rejects_stale_generation_before_stock_host_forwarding() -> None:
    async def scenario() -> None:
        upstream_requests: list[bytes] = []

        async def upstream_handler(reader, writer) -> None:
            upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]

        async def request(target: str, headers: bytes = b"") -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(f"GET {target} HTTP/1.1\r\nHost: test\r\n".encode() + headers + b"\r\n")
                await writer.drain()
                return await asyncio.wait_for(reader.read(512), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            admitted = await request("/?sc_generation=" + "a" * 64)
            assert b"HTTP/1.1 302 Found" in admitted
            assert b"Location: /\r\n" in admitted
            assert b"Set-Cookie: sc_deepseek_generation=" + b"a" * 64 in admitted
            assert upstream_requests == []

            clean = await request(
                "/",
                b"Cookie: sc_deepseek_generation=" + b"a" * 64 + b"\r\n"
                + b"Referer: http://127.0.0.1/?sc_generation=" + b"a" * 64 + b"\r\n",
            )
            assert b"HTTP/1.1 200 OK" in clean
            assert len(upstream_requests) == 1
            assert b"sc_generation=" not in upstream_requests[0]

            websocket = await request(
                "/api/events.mux",
                b"Cookie: sc_deepseek_generation=" + b"a" * 64 + b"\r\n"
                + b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + b"Referer: http://127.0.0.1/?sc_generation=" + b"a" * 64 + b"\r\n",
            )
            assert b"HTTP/1.1 200 OK" in websocket
            assert len(upstream_requests) == 2
            assert b"sc_generation=" not in upstream_requests[1]

            stale = await request("/")
            assert stale == b'HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: 40\r\nConnection: close\r\n\r\n{"error":"HARNESS_WEB_GENERATION_STALE"}'
            assert len(upstream_requests) == 2
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "protected_effect",
    (
        "host_create",
        "host_prompt",
        "memory_write",
        "workflow_action",
        "message_send",
        "wake_enqueue",
    ),
)
def test_stale_prompt_has_zero_independently_instrumented_protected_effects(
    protected_effect: str,
) -> None:
    async def scenario() -> None:
        attempted = mock.Mock(name=protected_effect)

        async def upstream_handler(reader, writer) -> None:
            # Each parametrized case owns one distinct downstream boundary.
            # If stale admission regresses, only that selected Host/tool effect
            # is attempted, so another counter cannot mask it.
            await reader.readuntil(b"\r\n\r\n")
            attempted()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        body = json.dumps({
            "payload": {"sessionId": "session-550e8400-e29b-41d4-a716-446655440000"}
        }).encode()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(
                    b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"b" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()
            assert response.endswith(b'{"error":"HARNESS_WEB_GENERATION_STALE"}')
            attempted.assert_not_called()
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_gateway_refuses_mutations_to_the_reserved_managed_session() -> None:
    assert deepseek_web.SESSION_MUTATION_FIELDS == {
        "/api/session.create": ("sessionId",),
        "/api/session.selectModel": ("sessionId",),
        "/api/session.rename": ("sessionId",),
        "/api/session.fork": ("sessionId",),
        "/api/session.prompt": ("sessionId",),
        "/api/session.updateQueue": ("sessionId",),
        "/api/session.cancel": ("sessionId",),
        "/api/workspace.insertSessionBefore": ("sessionId", "beforeSessionId"),
        "/api/workspace.archiveSession": ("sessionId",),
    }

    async def scenario(root: Path) -> None:
        upstream_connections: list[tuple[str, int]] = []

        async def upstream_handler(_reader, writer) -> None:
            upstream_connections.append(writer.get_extra_info("peername"))
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1",
            0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        session_id = "sc-" + "1" * 32
        mutations = (
            ("/api/session.create", {"sessionId": session_id}),
            ("/api/session.selectModel", {"sessionId": session_id}),
            ("/api/session.rename", {"sessionId": session_id}),
            ("/api/session.fork", {"sessionId": session_id}),
            ("/api/session.prompt", {"sessionId": session_id}),
            ("/api/session.updateQueue", {"sessionId": session_id}),
            ("/api/session.cancel", {"sessionId": session_id}),
            ("/api/workspace.insertSessionBefore", {
                "workspaceId": "ws-managed", "sessionId": session_id,
            }),
            ("/api/workspace.insertSessionBefore", {
                "workspaceId": "ws-managed", "sessionId": "session-other",
                "beforeSessionId": session_id,
            }),
            ("/api/workspace.archiveSession", {"sessionId": session_id}),
        )
        try:
            deepseek_web.reserve_managed_session(session_id)
            for path, rpc_payload in mutations:
                payload = json.dumps({"payload": rpc_payload}).encode()
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", gateway_port
                )
                try:
                    writer.write(
                        f"POST {path} HTTP/1.1\r\nHost: test\r\n".encode()
                        + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                        + b"\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(512), timeout=1)
                finally:
                    writer.close()
                    await writer.wait_closed()
                assert response == (
                    b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 36\r\nConnection: close\r\n\r\n"
                    b'{"error":"HARNESS_WEB_SESSION_BUSY"}'
                )
            assert upstream_connections == []
        finally:
            deepseek_web.release_managed_session(session_id)
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(
            deepseek_web.os.environ, service_env(Path(raw)), clear=False
        ):
            asyncio.run(scenario(Path(raw)))


def test_gateway_refuses_deleting_workspace_that_owns_reserved_session() -> None:
    async def scenario(root: Path) -> None:
        upstream_connections: list[object] = []

        async def upstream_handler(_reader, writer) -> None:
            upstream_connections.append(writer.get_extra_info("peername"))
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1",
            0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        session_id = "sc-" + "2" * 32
        payload = json.dumps({"payload": {"workspaceId": "ws-managed"}}).encode()
        try:
            deepseek_web.reserve_managed_session(session_id)
            with mock.patch.object(
                deepseek_web,
                "_host_rpc",
                return_value={
                    "items": [{
                        "workspaceId": "ws-managed",
                        "sessionIds": [session_id, "session-native"],
                    }],
                    "archivedSessionIds": [],
                },
            ) as listed:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", gateway_port
                )
                try:
                    writer.write(
                        b"POST /api/workspace.delete HTTP/1.1\r\nHost: test\r\n"
                        + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                        + b"\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(512), timeout=1)
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert response.endswith(b'{"error":"HARNESS_WEB_SESSION_BUSY"}')
            assert listed.call_args_list == [mock.call(target_port, "workspace.list", {})]
            assert upstream_connections == []
        finally:
            deepseek_web.release_managed_session(session_id)
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(
            deepseek_web.os.environ, service_env(Path(raw)), clear=False
        ):
            asyncio.run(scenario(Path(raw)))


def test_gateway_forwards_mutation_for_a_distinct_native_session() -> None:
    async def scenario(root: Path) -> None:
        forwarded: list[dict[str, object]] = []

        async def upstream_handler(reader, writer) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1].strip()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            forwarded.append(json.loads(await reader.readexactly(length)))
            body = json.dumps({
                "result": {
                    "ok": True,
                    "value": {"selected": {"provider": "native", "model": "other"}},
                }
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1",
            0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        reserved = "sc-" + "3" * 32
        native = "session-550e8400-e29b-41d4-a716-446655440000"
        envelope = {
            "rpcId": "distinct-native-selection",
            "payload": {
                "sessionId": native,
                "provider": "native",
                "model": "other",
            },
        }
        payload = json.dumps(envelope).encode()
        try:
            deepseek_web.reserve_managed_session(reserved)
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(
                    b"POST /api/session.selectModel HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()
            assert b"HTTP/1.1 200 OK" in response
            assert forwarded == [envelope]
            assert deepseek_web._reserved_session() == reserved
        finally:
            deepseek_web.release_managed_session(reserved)
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(
            deepseek_web.os.environ, service_env(Path(raw)), clear=False
        ):
            asyncio.run(scenario(Path(raw)))


def test_managed_reservation_waits_for_admitted_mutation_forwarding() -> None:
    async def scenario(root: Path) -> None:
        forwarded: list[dict[str, object]] = []
        forward_entered = asyncio.Event()
        release_forward = asyncio.Event()
        original_open_connection = asyncio.open_connection

        async def upstream_handler(reader, writer) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1].strip()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            forwarded.append(json.loads(await reader.readexactly(length)))
            body = json.dumps({
                "result": {
                    "ok": True,
                    "value": {"selected": {"provider": "native", "model": "race"}},
                }
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]

        async def gated_open_connection(host, port, *args, **kwargs):
            if port == target_port:
                forward_entered.set()
                await release_forward.wait()
            return await original_open_connection(host, port, *args, **kwargs)

        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        session_id = "sc-" + "4" * 32
        envelope = {
            "rpcId": "reservation-race",
            "payload": {
                "sessionId": session_id,
                "provider": "native",
                "model": "race",
            },
        }
        payload = json.dumps(envelope).encode()

        async def request() -> bytes:
            reader, writer = await original_open_connection(
                "127.0.0.1", gateway_port
            )
            try:
                writer.write(
                    b"POST /api/session.selectModel HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=2)
            finally:
                writer.close()
                await writer.wait_closed()

        reservation_started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def reserve() -> None:
            loop.call_soon_threadsafe(reservation_started.set)
            deepseek_web.reserve_managed_session(session_id)

        try:
            with mock.patch.object(
                deepseek_web.asyncio,
                "open_connection",
                side_effect=gated_open_connection,
            ):
                first_request = asyncio.create_task(request())
                await asyncio.wait_for(forward_entered.wait(), timeout=1)
                reservation = asyncio.create_task(asyncio.to_thread(reserve))
                await asyncio.wait_for(reservation_started.wait(), timeout=1)
                assert reservation.done() is False
                assert deepseek_web._reserved_session() is None
                release_forward.set()
                first_response = await first_request
                await asyncio.wait_for(reservation, timeout=1)

            assert b"HTTP/1.1 200 OK" in first_response
            assert forwarded == [envelope]
            assert deepseek_web._reserved_session() == session_id

            second_response = await request()
            assert second_response.endswith(
                b'{"error":"HARNESS_WEB_SESSION_BUSY"}'
            )
            assert forwarded == [envelope]
        finally:
            release_forward.set()
            deepseek_web.release_managed_session(session_id)
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(
            deepseek_web.os.environ, service_env(Path(raw)), clear=False
        ):
            asyncio.run(scenario(Path(raw)))


def test_gateway_refuses_missing_or_unsafe_managed_reservation_before_forwarding() -> None:
    async def scenario(root: Path) -> None:
        upstream_connections: list[object] = []

        async def upstream_handler(_reader, writer) -> None:
            upstream_connections.append(writer.get_extra_info("peername"))
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        deepseek_web._reservation_path().unlink()
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        session_id = "sc-" + "9" * 32
        payload = json.dumps({"payload": {"sessionId": session_id}}).encode()

        async def request(method: str) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(
                    f"POST /api/session.{method} HTTP/1.1\r\nHost: test\r\n".encode()
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
                )
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            assert (await request("prompt")).endswith(b'{"error":"HARNESS_WEB_GATEWAY_BUSY"}')
            assert (await request("cancel")).endswith(b'{"error":"HARNESS_WEB_GATEWAY_BUSY"}')
            assert upstream_connections == []
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_browser_prompt_uses_engine_id_and_serializes_one_session() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        with mock.patch.dict(deepseek_web.os.environ, env, clear=False):
            (root / "state.json").write_text(json.dumps({"service_port": 8942}))
            deepseek_web._initialize_activity()
            # This is the stock DSH native-Web namespace, not Subfloor's
            # managed ``sc-<hex>`` conversation namespace.
            session_id = "session-550e8400-e29b-41d4-a716-446655440000"
            with mock.patch.object(deepseek_web, "_history_boundary", return_value=11):
                request_id = deepseek_web._record_browser_prompt(8942, session_id)
                assert request_id != "client-rpc-id"
                assert len(request_id) == 32
                with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
                    deepseek_web._record_browser_prompt(8942, session_id)
                assert refused.value.code == "HARNESS_WEB_SESSION_BUSY"
                assert deepseek_web._read_activity(required=True)["requests"] == {
                    request_id: {
                        "session_id": session_id,
                        "boundary": 11,
                        "status": "pending",
                    }
                }
                deepseek_web._settle_browser_prompt(request_id, accepted=True)
                with mock.patch.object(deepseek_web, "_history_is_terminal", return_value=False):
                    with pytest.raises(deepseek_web.DeepSeekWebError) as live_refused:
                        deepseek_web._record_browser_prompt(8942, session_id)
                assert live_refused.value.code == "HARNESS_WEB_SESSION_BUSY"
                with mock.patch.object(deepseek_web, "_history_is_terminal", return_value=True):
                    next_request_id = deepseek_web._record_browser_prompt(8942, session_id)
                assert next_request_id != request_id
                assert deepseek_web._read_activity(required=True)["requests"] == {
                    next_request_id: {
                        "session_id": session_id,
                        "boundary": 11,
                        "status": "pending",
                    }
                }
                deepseek_web._settle_browser_prompt(next_request_id, accepted=False)
            assert deepseek_web._read_activity(required=True)["requests"] == {}


def test_gateway_clears_pending_prompt_when_upstream_connection_is_refused() -> None:
    async def scenario(root: Path) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            target_port = int(listener.getsockname()[1])
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        payload = json.dumps({"payload": {"sessionId": "sc-" + "6" * 32}}).encode()
        try:
            with mock.patch.object(deepseek_web, "_history_boundary", return_value=0):
                reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
                writer.write(
                    b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
                )
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), timeout=1) == b""
                writer.close()
                await writer.wait_closed()
            assert deepseek_web._read_activity(required=True)["requests"] == {}
        finally:
            gateway.close()
            await gateway.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_gateway_clears_rejected_prompt_and_serializes_concurrent_same_session() -> None:
    async def scenario(root: Path) -> None:
        arrivals: list[str] = []
        release = asyncio.Event()

        async def upstream_handler(reader, writer) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1].strip()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            payload = json.loads(await reader.readexactly(length))
            arrivals.append(payload["rpcId"])
            await release.wait()
            body = json.dumps({
                "type": "server-response",
                "rpcId": payload["rpcId"],
                "result": {"ok": True, "value": {"accepted": False}},
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        session_id = "sc-" + "7" * 32

        async def prompt(rpc_id: str) -> bytes:
            body = json.dumps({"rpcId": rpc_id, "payload": {"sessionId": session_id}}).encode()
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(
                    b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                )
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            with mock.patch.object(deepseek_web, "_history_boundary", return_value=5):
                first = asyncio.create_task(prompt("same-client-rpc"))
                while not arrivals:
                    await asyncio.sleep(0)
                second = await prompt("same-client-rpc")
                assert second.endswith(b'{"error":"HARNESS_WEB_SESSION_BUSY"}')
                assert arrivals == ["same-client-rpc"]
                release.set()
                assert b"HTTP/1.1 200 OK" in await first
            assert deepseek_web._read_activity(required=True)["requests"] == {}
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_gateway_accepts_stock_native_session_and_prunes_its_terminal_turn() -> None:
    async def scenario(root: Path) -> None:
        forwarded: list[dict[str, object]] = []

        async def upstream_handler(reader, writer) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1].strip()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            payload = json.loads(await reader.readexactly(length))
            forwarded.append(payload)
            body = json.dumps({
                "type": "server-response",
                "rpcId": payload["rpcId"],
                "result": {"ok": True, "value": {"accepted": True}},
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1", 0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        session_id = "session-550e8400-e29b-41d4-a716-446655440000"

        async def prompt(rpc_id: str) -> bytes:
            body = json.dumps({"rpcId": rpc_id, "payload": {"sessionId": session_id}}).encode()
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            try:
                writer.write(
                    b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                    + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                    + b"\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                )
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=1)
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            with mock.patch.object(deepseek_web, "_history_boundary", return_value=23):
                first = await prompt("stock-first")
                assert b'"accepted": true' in first
                first_activity = deepseek_web._read_activity(required=True)["requests"]
                assert len(first_activity) == 1
                first_id, first_record = next(iter(first_activity.items()))
                assert first_record == {
                    "session_id": session_id,
                    "boundary": 23,
                    "status": "accepted",
                }
                with mock.patch.object(deepseek_web, "_history_is_terminal", return_value=True):
                    second = await prompt("stock-second")
                assert b'"accepted": true' in second
            second_activity = deepseek_web._read_activity(required=True)["requests"]
            assert len(second_activity) == 1
            second_id, second_record = next(iter(second_activity.items()))
            assert second_id != first_id
            assert second_record == {
                "session_id": session_id,
                "boundary": 23,
                "status": "accepted",
            }
            assert [item["payload"]["sessionId"] for item in forwarded] == [
                session_id, session_id,
            ]
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_gateway_closes_mutable_http_connection_after_one_guarded_request() -> None:
    async def scenario(root: Path) -> None:
        upstream_requests: list[bytes] = []

        async def upstream_handler(reader, writer) -> None:
            upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1",
            0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        session_id = "sc-" + "3" * 32
        prompt = json.dumps({"rpcId": "second", "payload": {"sessionId": session_id}}).encode()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            writer.write(
                b"GET /?sc_generation=" + b"a" * 64 + b" HTTP/1.1\r\nHost: test\r\n\r\n"
                + b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(prompt)}\r\n\r\n".encode() + prompt
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            writer.close()
            await writer.wait_closed()
            assert b"HTTP/1.1 302 Found" in response
            assert upstream_requests == []
            assert deepseek_web._read_activity(required=True)["requests"] == {}
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_closed_gateway_admission_refuses_new_prompt_before_host_forwarding() -> None:
    async def scenario(root: Path) -> None:
        upstream_connections: list[object] = []

        async def upstream_handler(_reader, writer) -> None:
            upstream_connections.append(writer.get_extra_info("peername"))
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target_port = upstream.sockets[0].getsockname()[1]
        (root / "state.json").write_text(json.dumps({"service_port": target_port}))
        deepseek_web._initialize_activity()
        deepseek_web._close_gateway_admission({"service_port": target_port})
        gateway = await asyncio.start_server(
            lambda reader, writer: deepseek_web._relay_connection(
                reader, writer, target_port, frozenset({"127.0.0.1"}), "a" * 64
            ),
            "127.0.0.1",
            0,
        )
        gateway_port = gateway.sockets[0].getsockname()[1]
        session_id = "sc-" + "4" * 32
        payload = json.dumps({"rpcId": "admission-race", "payload": {"sessionId": session_id}}).encode()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
            writer.write(
                b"POST /api/session.prompt HTTP/1.1\r\nHost: test\r\n"
                + b"Cookie: sc_deepseek_generation=" + b"a" * 64
                + b"\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1)
            writer.close()
            await writer.wait_closed()
            assert response.endswith(b'{"error":"HARNESS_WEB_GATEWAY_BUSY"}')
            assert upstream_connections == []
            assert deepseek_web._read_activity(required=True) == {
                "admission": "closed", "requests": {}
            }
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()

    with tempfile.TemporaryDirectory() as raw:
        with mock.patch.dict(deepseek_web.os.environ, service_env(Path(raw)), clear=False):
            asyncio.run(scenario(Path(raw)))


def test_gateway_terminal_proof_uses_the_prompt_boundary_not_an_old_turn_end() -> None:
    session_id = "sc-" + "5" * 32
    events = {
        "events": [
            {"event": {"seq": 3, "type": "turn/end", "data": {"reason": {"kind": "completed"}}}},
            {"event": {"seq": 4, "type": "turn/start", "data": {}}},
        ]
    }
    with mock.patch.object(deepseek_web, "_host_rpc", return_value=events):
        assert deepseek_web._history_is_terminal(8942, session_id, 4) is False


def test_malformed_activity_refuses_handoff_without_terminating_host() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        (root / "state.json").write_text(json.dumps({"relay_pid": 12, "web_pid": 13}))
        activity = root / "deepseek-web-activity.json"
        activity.write_text("not-json")
        activity.chmod(0o600)
        with (
            mock.patch.dict(deepseek_web.os.environ, env, clear=False),
            mock.patch.object(deepseek_web, "_terminate_verified") as terminated,
            pytest.raises(deepseek_web.DeepSeekWebError) as refused,
        ):
            deepseek_web._stop_unlocked()

    assert refused.value.code == "HARNESS_WEB_GATEWAY_BUSY"
    terminated.assert_not_called()


def test_existing_service_requires_both_shell_id_and_shortname() -> None:
    state = {
        "schema_version": 3,
        "service_port": 8942,
        "relay_port": 18942,
        "relay_policy": deepseek_web.RELAY_POLICY,
        "relay_listen_host": "0.0.0.0",
        "relay_allowed_peers": ["127.0.0.1"],
        "credential_shell": "DEV4",
        "credential_shell_id": 4,
        "web_pid": 11,
        "web_start_ticks": 12,
        "relay_pid": 13,
        "relay_start_ticks": 14,
    }
    with (
        mock.patch.object(deepseek_web, "_verified_process", return_value=True),
        mock.patch.object(deepseek_web, "_http_ready", return_value=True),
        mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
    ):
        assert deepseek_web._existing_healthy(
            state, 8942, 18942, listen_host="0.0.0.0",
            allowed_peers=("127.0.0.1",), credential_shell="DEV4", credential_shell_id=4,
        ) is True
        assert deepseek_web._existing_healthy(
            state, 8942, 18942, listen_host="0.0.0.0",
            allowed_peers=("127.0.0.1",), credential_shell="DEV4", credential_shell_id=5,
        ) is False


def test_unproven_one_shot_blocks_other_shell_until_matching_terminal_proof() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        env = service_env(root)
        session_id = "sc-" + "6" * 32
        with mock.patch.dict(deepseek_web.os.environ, env, clear=False):
            deepseek_web.mark_unproven_execution(env, session_id)
            with pytest.raises(deepseek_web.DeepSeekWebError) as blocked:
                deepseek_web.acquire_shell_identity(
                    env={**env, "SC_SHELL_ID": "5", "SC_SHELL_SHORTNAME": "DEV5"}
                )
            assert blocked.value.code == "HARNESS_SHELL_IDENTITY_BUSY"
            assert (root / "deepseek-shell-identity-unproven.json").exists()
            (root / "state.json").write_text(json.dumps({"service_port": 8942}))
            terminal = {
                "events": [{"event": {
                    "seq": 1,
                    "type": "turn/end",
                    "data": {"reason": {"kind": "cancelled"}},
                }}]
            }
            with mock.patch.object(deepseek_web, "_host_rpc", return_value=terminal):
                lease = deepseek_web.acquire_shell_identity(env=env)
            lease.close()

    assert not (root / "deepseek-shell-identity-unproven.json").exists()


def test_status_rejects_a_live_relay_without_the_host_gateway_policy() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state = {
            "web_pid": 101,
            "web_start_ticks": 201,
            "service_port": 8942,
            "relay_pid": 102,
            "relay_start_ticks": 202,
            "relay_port": 18942,
        }
        (root / "state.json").write_text(json.dumps(state))
        with (
            mock.patch.dict(deepseek_web.os.environ, service_env(root), clear=False),
            mock.patch.object(deepseek_web, "_verified_process", return_value=True),
            mock.patch.object(deepseek_web, "_http_ready", return_value=True),
            mock.patch.object(deepseek_web, "_tcp_ready", return_value=True),
        ):
            unsafe = deepseek_web.status()
            state.update(
                {
                    "relay_policy": deepseek_web.RELAY_POLICY,
                    "relay_allowed_peers": ["127.0.0.1", "172.18.0.1"],
                }
            )
            (root / "state.json").write_text(json.dumps(state))
            safe = deepseek_web.status()

    assert unsafe["ready"] is False
    assert unsafe["relay_safe"] is False
    assert safe["ready"] is True
    assert safe["relay_safe"] is True


def test_workspace_registration_uses_stock_rpc_envelope_and_verifies_path() -> None:
    class Response:
        status = 200

        def __init__(self, request) -> None:
            sent = json.loads(request.data)
            self.payload = {
                "type": "server-response",
                "rpcId": sent["rpcId"],
                "result": {
                    "ok": True,
                    "value": {
                        "created": True,
                        "workspace": {
                            "workspaceId": "official-workspace",
                            "path": sent["payload"]["path"],
                        },
                    },
                },
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    captured = []

    def urlopen(request, *, timeout):
        captured.append((request, timeout))
        return Response(request)

    worktree = ROOT.resolve()
    with mock.patch.object(deepseek_web.urllib.request, "urlopen", side_effect=urlopen):
        result = deepseek_web._post_workspace(8942, worktree)

    request = captured[0][0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/api/workspace.create")
    assert body["type"] == "client-request"
    assert body["method"] == "workspace.create"
    assert body["payload"] == {"path": str(worktree)}
    assert result == {"workspace_id": "official-workspace", "workspace_created": True}


def test_process_identity_rejects_pid_reuse_and_wrong_command() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        process = proc / "321"
        process.mkdir()
        process.joinpath("stat").write_text(
            "321 (dsh) S " + " ".join(["0"] * 18) + " 987654 0\n"
        )
        process.joinpath("cmdline").write_bytes(b"/home/sc/.local/bin/dsh\0web\0")

        assert deepseek_web._verified_process(321, 987654, "web", proc_root=proc)
        assert not deepseek_web._verified_process(321, 987655, "web", proc_root=proc)
        process.joinpath("cmdline").write_bytes(b"/usr/bin/python\0worker.py\0")
        assert not deepseek_web._verified_process(321, 987654, "web", proc_root=proc)


def test_worktree_registration_refuses_paths_outside_the_fork() -> None:
    with (
        tempfile.TemporaryDirectory() as fork_raw,
        tempfile.TemporaryDirectory() as other_raw,
        mock.patch.object(deepseek_web, "REPO_ROOT", Path(fork_raw).resolve()),
        pytest.raises(deepseek_web.DeepSeekWebError) as invalid,
    ):
        deepseek_web._worktree(Path(other_raw))
    assert invalid.value.code == "HARNESS_WORKTREE_INVALID"
