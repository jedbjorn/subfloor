#!/usr/bin/env python3
"""Own the fork-scoped official DeepSeek Web/Host service.

The stock service stays on container/host loopback. An engine-owned
HTTP/WebSocket gateway publishes its deterministic host-loopback entry,
generation-checking each browser connection before it reaches dsh. State
records PID start ticks before either child is trusted across calls.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ports

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent.resolve()
STATE = ENGINE / "run" / "deepseek-web.json"
LOG = ENGINE / "logs" / "deepseek-web.log"
LOCK = ENGINE / "run" / "deepseek-web.lock"
START_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 2.0
RELAY_POLICY = "host-gateway-only-v1"
GENERATION_COOKIE = "sc_deepseek_generation"


class DeepSeekWebError(RuntimeError):
    """Stable failure returned by native-Web entry."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ShellIdentityLease:
    """Exclusive process-credential ownership for one DeepSeek execution."""

    def __init__(self, handle) -> None:
        self._handle = handle

    def close(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _state_path() -> Path:
    override = os.environ.get("SC_DEEPSEEK_WEB_STATE")
    return Path(override) if override else STATE


def _log_path() -> Path:
    override = os.environ.get("SC_DEEPSEEK_WEB_LOG")
    return Path(override) if override else LOG


def _lock_path() -> Path:
    override = os.environ.get("SC_DEEPSEEK_WEB_LOCK")
    return Path(override) if override else LOCK


def _identity_lock_path() -> Path:
    return _state_path().with_name("deepseek-shell-identity.lock")


def acquire_shell_identity(*, env: Mapping[str, str]) -> ShellIdentityLease:
    """Acquire the full-lifetime Host identity lease or refuse before mutation."""
    path = _identity_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "another DeepSeek execution owns the shared Host credential",
        ) from exc
    return ShellIdentityLease(handle)


def _verify_shell_identity(env: Mapping[str, str]) -> None:
    token = env.get("SC_API_TOKEN", "")
    api_base = env.get("SC_API_BASE", "")
    shortname = env.get("SC_SHELL_SHORTNAME", "")
    present = tuple(bool(value) for value in (token, api_base, shortname))
    if not any(present):
        return
    if not all(present):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek requires complete Subfloor shell API wiring",
        )
    parsed = urllib.parse.urlsplit(api_base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek shell API must use a loopback endpoint",
        )
    request = urllib.request.Request(
        api_base.rstrip("/") + "/_sc/mem/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek shell API identity could not be authenticated",
        ) from exc
    resolved = payload.get("shortname") if isinstance(payload, Mapping) else None
    if resolved != shortname:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_MISMATCH",
            "DeepSeek shell API identity disagrees with the prepared shell",
        )


@contextmanager
def _service_lock():
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _write_state(payload: Mapping[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    try:
        fields = (proc_root / str(pid) / "stat").read_text().split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _process_cmdline(pid: int, *, proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(part.decode(errors="replace") for part in raw.split(b"\0") if part)


def _verified_process(
    pid: Any,
    start_ticks: Any,
    identity: str,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    if not isinstance(pid, int) or not isinstance(start_ticks, int) or pid <= 1:
        return False
    if process_start_ticks(pid, proc_root=proc_root) != start_ticks:
        return False
    cmdline = _process_cmdline(pid, proc_root=proc_root)
    joined = "\0".join(cmdline)
    if identity == "web":
        return "dsh" in joined and "web" in cmdline
    return Path(__file__).name in joined and "relay" in cmdline


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=HTTP_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _http_ready(port: int) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        headers={"Host": f"127.0.0.1:{port}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def _wait_ready(check, *, timeout: float = START_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def _disabled(env: Mapping[str, str]) -> bool:
    return "deepseek" in {
        item.strip().lower()
        for item in env.get("SC_DISABLED_HARNESSES", "").split(",")
        if item.strip()
    }


def _service_port(config: Mapping[str, Any], env: Mapping[str, str]) -> int:
    configured = int(config["deepseek_host_port"])
    if not env.get("SC_SANDBOX"):
        return configured
    raw = env.get("SC_DEEPSEEK_HOST_PORT")
    try:
        injected = int(raw or "")
    except ValueError as exc:
        raise DeepSeekWebError(
            "HARNESS_ENDPOINT_UNAVAILABLE",
            "SC_DEEPSEEK_HOST_PORT is missing or invalid; "
            "run ./sc launch --no-build from the host",
        ) from exc
    if injected != configured:
        raise DeepSeekWebError(
            "HARNESS_ENDPOINT_UNAVAILABLE",
            "SC_DEEPSEEK_HOST_PORT disagrees with this fork's persisted Host port",
        )
    return injected


def _default_gateway(route_path: Path = Path("/proc/net/route")) -> str | None:
    """Return the active namespace's IPv4 default gateway."""
    try:
        rows = route_path.read_text().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        except (OSError, ValueError, struct.error):
            continue
        if flags & 0x3 == 0x3:
            return gateway
    return None


def _relay_allowed_peers() -> tuple[str, ...]:
    gateway = _default_gateway()
    if gateway is None:
        raise DeepSeekWebError(
            "HARNESS_SERVICE_START_FAILED",
            "could not identify the sandbox host gateway for the loopback relay",
        )
    return ("127.0.0.1", gateway)


def _worktree(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DeepSeekWebError(
            "HARNESS_WORKTREE_INVALID", f"selected worktree is unavailable: {path}"
        ) from exc
    if not resolved.is_dir():
        raise DeepSeekWebError(
            "HARNESS_WORKTREE_INVALID", f"selected worktree is not a directory: {path}"
        )
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise DeepSeekWebError(
            "HARNESS_WORKTREE_INVALID",
            f"selected worktree is outside this fork: {resolved}",
        ) from exc
    return resolved


def _post_workspace(port: int, worktree: Path) -> dict[str, Any]:
    rpc_id = str(uuid.uuid4())
    body = {
        "type": "client-request",
        "rpcId": rpc_id,
        "method": "workspace.create",
        "payload": {"path": str(worktree)},
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/workspace.create",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Host": f"127.0.0.1:{port}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise DeepSeekWebError(
            "HARNESS_WORKSPACE_REGISTRATION_FAILED",
            f"official Host rejected the selected worktree: {exc}",
        ) from exc
    if payload.get("rpcId") != rpc_id:
        raise DeepSeekWebError(
            "HARNESS_PROTOCOL_ERROR", "workspace.create returned a mismatched rpcId"
        )
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else None
        raise DeepSeekWebError(
            "HARNESS_WORKSPACE_REGISTRATION_FAILED",
            f"workspace.create failed: {error or 'invalid response'}",
        )
    value = result.get("value")
    workspace = value.get("workspace") if isinstance(value, dict) else None
    returned_path = workspace.get("path") if isinstance(workspace, dict) else None
    if returned_path is None or Path(returned_path).resolve() != worktree:
        raise DeepSeekWebError(
            "HARNESS_WORKTREE_MISMATCH",
            "workspace.create did not return the selected worktree",
        )
    return {
        "workspace_id": workspace.get("workspaceId"),
        "workspace_created": bool(value.get("created")),
    }


def _spawn(
    argv: list[str],
    *,
    cwd: Path,
    log: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[int, int]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=None if env is None else dict(env),
        )
    ticks = process_start_ticks(process.pid)
    if ticks is None:
        raise DeepSeekWebError(
            "HARNESS_SERVICE_START_FAILED",
            f"could not record process identity for pid {process.pid}",
        )
    return process.pid, ticks


def _terminate_verified(state: Mapping[str, Any], prefix: str) -> bool:
    pid = state.get(f"{prefix}_pid")
    ticks = state.get(f"{prefix}_start_ticks")
    identity = "web" if prefix == "web" else "relay"
    if not _verified_process(pid, ticks, identity):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _verified_process(pid, ticks, identity):
            return True
        time.sleep(0.05)
    if not _verified_process(pid, ticks, identity):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return not _verified_process(pid, ticks, identity)


def _stop_unlocked() -> dict[str, Any]:
    state = _read_state()
    # The gateway owns browser admission. It must be gone before the upstream
    # Host or its owner-only credential can be replaced. SIGTERM cancels every
    # accepted relay task; an unproven cancellation blocks rotation.
    relay_stopped = _terminate_verified(state, "relay")
    if not relay_stopped:
        raise DeepSeekWebError(
            "HARNESS_WEB_GATEWAY_BUSY",
            "DeepSeek Web gateway could not quiesce accepted browser work",
        )
    web_stopped = _terminate_verified(state, "web")
    if not web_stopped:
        raise DeepSeekWebError(
            "HARNESS_SERVICE_STOP_FAILED",
            "official dsh Web could not stop after gateway quiescence",
        )
    try:
        _state_path().unlink()
    except FileNotFoundError:
        pass
    try:
        _credential_path().unlink()
    except FileNotFoundError:
        pass
    try:
        _generation_path().unlink()
    except FileNotFoundError:
        pass
    return {"stopped": bool(state), "web": web_stopped, "relay": relay_stopped}


def _credential_path() -> Path:
    return _state_path().with_name("deepseek-shell-api.json")


def _generation_path() -> Path:
    return _state_path().with_name("deepseek-web-generation.json")


def _write_generation() -> str:
    """Mint the relay-only capability without serializing it into service state."""
    token = uuid.uuid4().hex + uuid.uuid4().hex
    path = _generation_path()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"generation": token}, handle)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return token


def _read_generation(path: Path) -> str:
    try:
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise OSError("unsafe generation artifact")
        value = json.loads(path.read_text())
        token = value.get("generation") if isinstance(value, Mapping) else None
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekWebError(
            "HARNESS_WEB_GENERATION_STALE",
            "DeepSeek Web generation is unavailable",
        ) from exc
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise DeepSeekWebError(
            "HARNESS_WEB_GENERATION_STALE",
            "DeepSeek Web generation is invalid",
        )
    return token


def _write_shell_credential(env: Mapping[str, str]) -> tuple[Path, str] | None:
    token = env.get("SC_API_TOKEN", "")
    api_base = env.get("SC_API_BASE", "")
    shortname = env.get("SC_SHELL_SHORTNAME", "")
    present = tuple(bool(value) for value in (token, api_base, shortname))
    if not any(present):
        return None
    if not all(present):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek Web requires complete Subfloor shell API wiring",
        )
    path = _credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"shortname": shortname, "api_base": api_base, "token": token}, handle)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return path, shortname


def stop() -> dict[str, Any]:
    with _service_lock():
        return _stop_unlocked()


def _existing_healthy(
    state: Mapping[str, Any],
    service_port: int,
    relay_port: int,
    *,
    listen_host: str,
    allowed_peers: tuple[str, ...],
    credential_shell: str | None,
) -> bool:
    if state.get("service_port") != service_port:
        return False
    if not _verified_process(state.get("web_pid"), state.get("web_start_ticks"), "web"):
        return False
    if not _http_ready(service_port):
        return False
    if state.get("credential_shell") != credential_shell:
        return False
    return (
        state.get("relay_port") == relay_port
        and state.get("relay_policy") == RELAY_POLICY
        and state.get("relay_listen_host") == listen_host
        and state.get("relay_allowed_peers") == list(allowed_peers)
        and _verified_process(
            state.get("relay_pid"), state.get("relay_start_ticks"), "relay"
        )
        and _tcp_ready("127.0.0.1", relay_port)
    )


def _relay_configuration(*, sandbox: bool) -> tuple[str, tuple[str, ...]]:
    """Keep the engine gateway at the browser-facing boundary in every mode."""
    if sandbox:
        return "0.0.0.0", _relay_allowed_peers()
    return "127.0.0.1", ("127.0.0.1",)


def ensure(
    worktree: Path,
    *,
    env: Mapping[str, str] | None = None,
    identity_lease: ShellIdentityLease | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    if identity_lease is None:
        lease = acquire_shell_identity(env=env)
        try:
            return ensure(worktree, env=env, identity_lease=lease)
        finally:
            lease.close()
    _verify_shell_identity(env)
    with _service_lock():
        if _disabled(env):
            _stop_unlocked()
            raise DeepSeekWebError("HARNESS_DISABLED", "DeepSeek is disabled")
        selected = _worktree(worktree)
        config = ports.resolve(persist=True)
        service_port = _service_port(config, env)
        relay_port = service_port + ports.DEEPSEEK_RELAY_OFFSET
        state = _read_state()
        sandbox = bool(env.get("SC_SANDBOX"))
        listen_host, allowed_peers = _relay_configuration(sandbox=sandbox)
        credential_shell = env.get("SC_SHELL_SHORTNAME") or None
        reused = _existing_healthy(
            state,
            service_port,
            relay_port,
            listen_host=listen_host,
            allowed_peers=allowed_peers,
            credential_shell=credential_shell,
        )
        generation = None
        if not reused:
            _stop_unlocked()
            executable = shutil.which("dsh")
            if executable is None:
                raise DeepSeekWebError(
                    "HARNESS_UNAVAILABLE",
                    "official dsh is not installed; run ./sc ensure-harness",
                )
            credential = _write_shell_credential(env)
            web_env = dict(env)
            if credential is not None:
                credential_file, credential_shell = credential
                web_env.pop("SC_API_TOKEN", None)
                web_env.pop("SC_API_BASE", None)
                web_env["SC_MEM_CREDENTIAL_FILE"] = str(credential_file)
            web_pid, web_ticks = _spawn(
                [
                    executable,
                    "web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(service_port),
                    "--no-open",
                ],
                cwd=selected,
                log=_log_path(),
                env=web_env,
            )
            state = {
                "schema_version": 1,
                "web_pid": web_pid,
                "web_start_ticks": web_ticks,
                "service_port": service_port,
                "relay_port": relay_port,
                "relay_policy": RELAY_POLICY,
                "relay_listen_host": listen_host,
                "relay_allowed_peers": list(allowed_peers),
                "url": f"http://127.0.0.1:{relay_port}",
                "credential_shell": credential_shell,
            }
            _write_state(state)
            if not _wait_ready(lambda: _http_ready(service_port)):
                _stop_unlocked()
                raise DeepSeekWebError(
                    "HARNESS_SERVICE_UNAVAILABLE",
                    f"official dsh Web did not become ready; inspect {_log_path()}",
                )
            generation = _write_generation()
            relay_pid, relay_ticks = _spawn(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "relay",
                    "--listen-host",
                    listen_host,
                    *[
                        item
                        for peer in allowed_peers
                        for item in ("--allowed-peer", peer)
                    ],
                    "--listen-port",
                    str(relay_port),
                    "--target-port",
                    str(service_port),
                    "--generation-file",
                    str(_generation_path()),
                ],
                cwd=REPO_ROOT,
                log=_log_path(),
            )
            state.update(
                {"relay_pid": relay_pid, "relay_start_ticks": relay_ticks}
            )
            _write_state(state)
            if not _wait_ready(lambda: _tcp_ready("127.0.0.1", relay_port)):
                _stop_unlocked()
                raise DeepSeekWebError(
                    "HARNESS_SERVICE_UNAVAILABLE",
                    "DeepSeek loopback publication relay did not become ready",
                )
        elif credential_shell is not None:
            # Repair a missing/stale artifact (for example after shell-key
            # rotation) without restarting an otherwise healthy same-shell Host.
            _write_shell_credential(env)
        generation = generation or _read_generation(_generation_path())
        url = f"http://127.0.0.1:{relay_port}/?sc_generation={generation}"
        registration = _post_workspace(service_port, selected)
        state.update(
            {
                "last_worktree": str(selected),
                "last_workspace_id": registration["workspace_id"],
                "ready": True,
            }
        )
        _write_state(state)
        return {**state, **registration, "url": url, "reused": reused}


def browser_generation() -> str:
    """Return the one-shot generation capability after proving gateway health."""
    state = _read_state()
    relay_port = state.get("relay_port")
    if (
        not isinstance(relay_port, int)
        or not _verified_process(state.get("relay_pid"), state.get("relay_start_ticks"), "relay")
        or not _tcp_ready("127.0.0.1", relay_port)
    ):
        raise DeepSeekWebError(
            "HARNESS_SERVICE_UNAVAILABLE",
            "DeepSeek Web gateway is not ready for browser handoff",
        )
    return _read_generation(_generation_path())


def status() -> dict[str, Any]:
    state = _read_state()
    service_port = state.get("service_port")
    relay_port = state.get("relay_port")
    web = _verified_process(state.get("web_pid"), state.get("web_start_ticks"), "web")
    relay = relay_port is None or _verified_process(
        state.get("relay_pid"), state.get("relay_start_ticks"), "relay"
    )
    relay_safe = relay_port is None or (
        state.get("relay_policy") == RELAY_POLICY
        and isinstance(state.get("relay_allowed_peers"), list)
        and bool(state["relay_allowed_peers"])
    )
    ready = (
        web
        and isinstance(service_port, int)
        and _http_ready(service_port)
        and relay
        and relay_safe
        and (relay_port is None or _tcp_ready("127.0.0.1", relay_port))
    )
    return {
        "ready": ready,
        "web_process": web,
        "relay_process": relay,
        "relay_safe": relay_safe,
        **state,
    }


async def _relay_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_port: int,
    allowed_peers: frozenset[str],
    generation: str | None = None,
) -> None:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer or peer[0] not in allowed_peers:
        writer.close()
        await writer.wait_closed()
        return
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HTTP_TIMEOUT_SECONDS)
    except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError):
        writer.close()
        await writer.wait_closed()
        return
    lines = request.decode("iso-8859-1").split("\r\n")
    request_line = lines[0].split(" ", 2) if lines else []
    cookie = next((line[7:].strip() for line in lines[1:] if line.lower().startswith("cookie:")), "")
    parsed_cookie = {
        item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
        for item in cookie.split(";") if "=" in item
    }
    query_generation = None
    if len(request_line) == 3:
        parsed_target = urllib.parse.urlsplit(request_line[1])
        query = urllib.parse.parse_qs(parsed_target.query)
        query_generation = (query.get("sc_generation") or [None])[0]
        if query_generation is not None:
            clean_query = [(key, value) for key, values in query.items() for value in values if key != "sc_generation"]
            request_line[1] = urllib.parse.urlunsplit(("", "", parsed_target.path or "/", urllib.parse.urlencode(clean_query), ""))
            lines[0] = " ".join(request_line)
            request = ("\r\n".join(lines)).encode("iso-8859-1")
    if generation is not None and (
        parsed_cookie.get(GENERATION_COOKIE) != generation
        and query_generation != generation
    ):
        body = b'{"error":"HARNESS_WEB_GENERATION_STALE"}'
        writer.write(
            b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", target_port
        )
    except OSError:
        writer.close()
        await writer.wait_closed()
        return
    upstream_writer.write(request)
    await upstream_writer.drain()

    try:
        response = await asyncio.wait_for(upstream_reader.readuntil(b"\r\n\r\n"), timeout=HTTP_TIMEOUT_SECONDS)
    except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError):
        upstream_writer.close()
        writer.close()
        await upstream_writer.wait_closed()
        await writer.wait_closed()
        return
    response_lines = response.decode("iso-8859-1").split("\r\n")
    if generation is not None:
        response_lines.insert(-2, f"Set-Cookie: {GENERATION_COOKIE}={generation}; HttpOnly; SameSite=Strict; Path=/")
    writer.write("\r\n".join(response_lines).encode("iso-8859-1"))
    await writer.drain()

    async def copy(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                await destination.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            destination.close()

    tasks = [
        asyncio.create_task(copy(reader, upstream_writer)),
        asyncio.create_task(copy(upstream_reader, writer)),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    await upstream_writer.wait_closed()
    await writer.wait_closed()


async def _relay(
    listen_host: str,
    listen_port: int,
    target_port: int,
    allowed_peers: frozenset[str],
    generation_file: Path,
) -> None:
    generation = _read_generation(generation_file)
    server = await asyncio.start_server(
        lambda reader, writer: _relay_connection(
            reader, writer, target_port, allowed_peers, generation
        ),
        listen_host,
        listen_port,
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="deepseek_web.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure_parser = subparsers.add_parser("ensure")
    ensure_parser.add_argument("--worktree", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    subparsers.add_parser("generation")
    relay_parser = subparsers.add_parser("relay")
    relay_parser.add_argument("--allowed-peer", action="append", required=True)
    relay_parser.add_argument("--listen-host", required=True)
    relay_parser.add_argument("--listen-port", type=int, required=True)
    relay_parser.add_argument("--target-port", type=int, required=True)
    relay_parser.add_argument("--generation-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "ensure":
            result = ensure(Path(args.worktree))
        elif args.command == "status":
            result = status()
        elif args.command == "stop":
            result = stop()
        elif args.command == "generation":
            print(browser_generation())
            return 0
        else:
            asyncio.run(
                _relay(
                    args.listen_host,
                    args.listen_port,
                    args.target_port,
                    frozenset(args.allowed_peer),
                    args.generation_file,
                )
            )
            return 0
    except DeepSeekWebError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
