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
        spawned: list[tuple[list[str], Path]] = []

        def spawn(argv: list[str], *, cwd: Path, log: Path) -> tuple[int, int]:
            spawned.append((argv, cwd))
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
        ):
            first = deepseek_web.ensure(worktree, env=service_env(root))
            second = deepseek_web.ensure(worktree, env=service_env(root))

        assert first["reused"] is False
        assert second["reused"] is True
        assert first["url"] == "http://127.0.0.1:8942"
        assert spawned[0] == (
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
        assert spawned[1][0][-4:] == [
            "--listen-port",
            "18942",
            "--target-port",
            "8942",
        ]
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
            payload = await reader.readexactly(4)
            writer.write(payload)
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
                writer.write(b"ping")
                await writer.drain()
                try:
                    return await asyncio.wait_for(reader.read(4), timeout=1)
                except ConnectionResetError:
                    return b""
            finally:
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()

        try:
            assert await exchange("127.0.0.1") == b"ping"
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
