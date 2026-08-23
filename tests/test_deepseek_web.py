"""Official dsh Web/Host lifecycle regressions."""

from __future__ import annotations

import asyncio
import importlib
import json
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
        assert spawned[1][0][7:11] == [
            "--listen-port",
            "18942",
            "--target-port",
            "8942",
        ]
        assert spawned[1][0][-2:] == ["--generation-file", str(root / "deepseek-web-generation.json")]
        assert spawned[1][0][3:7] == [
            "--allowed-peer",
            "127.0.0.1",
            "--allowed-peer",
            "172.18.0.1",
        ]
        assert register.call_count == 2
        state = json.loads((root / "state.json").read_text())
        assert state["last_worktree"] == str(worktree)
        assert state["last_workspace_id"] == "ws-4"


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
            "shortname": "DEV4",
            "api_base": "http://127.0.0.1:8837",
            "token": "shell-token-never-in-host-env",
        }
        assert spawned[0] is not None
        assert "SC_API_TOKEN" not in spawned[0]
        assert "SC_API_BASE" not in spawned[0]
        assert spawned[0]["SC_MEM_CREDENTIAL_FILE"] == str(artifact)
        assert spawned[1] is None


def test_disabled_deepseek_stops_owned_service_without_launching() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        with (
            mock.patch.dict(deepseek_web.os.environ, service_env(root), clear=False),
            mock.patch.object(deepseek_web, "REPO_ROOT", root),
            mock.patch.object(deepseek_web, "_stop_unlocked") as stopped,
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
        "SC_SHELL_SHORTNAME": "DEV4",
    }
    with mock.patch.object(deepseek_web.urllib.request, "urlopen", side_effect=urlopen):
        deepseek_web._verify_shell_identity(env)

    request, timeout = captured[0]
    assert request.full_url == "http://127.0.0.1:8837/_sc/mem/whoami"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert timeout == deepseek_web.HTTP_TIMEOUT_SECONDS


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
