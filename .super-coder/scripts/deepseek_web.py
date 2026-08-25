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
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
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

import harness_versions
import ports
from deepseek_candidate_authority import DeepSeekCandidateAuthority
from deepseek_identity_registry import (
    DeepSeekIdentityError,
    DeepSeekIdentityRegistry,
)

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
CANDIDATE_FENCE_CODES = frozenset({
    "HARNESS_PROOF_BINDING_MISMATCH",
    "HARNESS_PROOF_CAPABILITY_EXPIRED",
    "HARNESS_PROOF_CAPABILITY_MISMATCH",
    "HARNESS_PROOF_CAPABILITY_REVOKED",
    "HARNESS_PROOF_CAPABILITY_STALE",
    "HARNESS_PROOF_RESTART_BINDING_MISMATCH",
    "HARNESS_PROOF_ROOT_REFUSED",
})
PROMOTION_RUNNER_CONTRACT = "sc-dsh-promotion-runner-v1"
PROOF_CONTEXT_CONTRACT = "sc-dsh-proof-context-v1"
SESSION_MUTATION_FIELDS = {
    "/api/session.create": ("sessionId",),
    "/api/session.selectModel": ("sessionId",),
    "/api/session.rename": ("sessionId",),
    "/api/session.fork": ("sessionId",),
    "/api/session.prompt": ("sessionId",),
    "/api/session.updateQueue": ("sessionId",),
    "/api/session.cancel": ("sessionId",),
    "/api/subagent.prompt": ("parentSessionId",),
    "/api/subagent.interrupt": ("parentSessionId",),
    "/api/workspace.insertSessionBefore": ("sessionId", "beforeSessionId"),
    "/api/workspace.archiveSession": ("sessionId",),
}
SESSION_MUTATION_PATHS = frozenset(SESSION_MUTATION_FIELDS)
WORKSPACE_DELETE_PATH = "/api/workspace.delete"


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


def _exact_engine_ref() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ENGINE), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = subprocess.run(
            [
                "git", "-C", str(ENGINE), "status", "--porcelain",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeepSeekWebError(
            "HARNESS_PROOF_REF_UNAVAILABLE",
            "cannot resolve the exact engine ref for proof admission",
        ) from exc
    value = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise DeepSeekWebError(
            "HARNESS_PROOF_REF_UNAVAILABLE",
            "engine ref is not one exact commit",
        )
    if dirty.stdout.strip():
        raise DeepSeekWebError(
            "HARNESS_PROOF_REF_DIRTY",
            "proof admission requires the exact clean engine ref",
        )
    return value


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


def _unproven_path() -> Path:
    return _state_path().with_name("deepseek-shell-identity-unproven.json")


def _read_unproven() -> dict[str, Any] | None:
    path = _unproven_path()
    try:
        metadata = path.lstat()
        if path.is_symlink() or metadata.st_mode & 0o777 != 0o600:
            raise OSError("unsafe unproven-work artifact")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
                raise OSError("proof artifact changed before reading")
            value = json.load(handle)
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("proof artifact changed while reading")
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "DeepSeek has unreadable unproven work; credential rotation is refused",
        ) from exc
    if not isinstance(value, Mapping):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "DeepSeek has invalid unproven work; credential rotation is refused",
        )
    session_id = value.get("session_id")
    shell_id = value.get("shell_id")
    shortname = value.get("shortname")
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"sc-[0-9a-f]{32}", session_id) is None
        or not isinstance(shell_id, int)
        or shell_id <= 0
        or not isinstance(shortname, str)
        or not shortname
    ):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "DeepSeek has invalid unproven work; credential rotation is refused",
        )
    return {"session_id": session_id, "shell_id": shell_id, "shortname": shortname}


def mark_unproven_execution(env: Mapping[str, str], session_id: str) -> None:
    shell_id = env.get("SC_SHELL_ID", "")
    shortname = env.get("SC_SHELL_SHORTNAME", "")
    if not shell_id.isdecimal() or int(shell_id) <= 0 or not shortname:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "cannot preserve unproven DeepSeek work without canonical shell identity",
        )
    path = _unproven_path()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(
                {
                    "session_id": session_id,
                    "shell_id": int(shell_id),
                    "shortname": shortname,
                },
                handle,
            )
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _clear_terminal_unproven(env: Mapping[str, str]) -> None:
    marker = _read_unproven()
    if marker is None:
        return
    if (
        env.get("SC_SHELL_ID") != str(marker["shell_id"])
        or env.get("SC_SHELL_SHORTNAME") != marker["shortname"]
    ):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "another shell owns DeepSeek work without terminal proof",
        )
    state = _read_state()
    service_port = state.get("service_port")
    if not isinstance(service_port, int) or not _history_is_terminal(
        service_port, marker["session_id"], 0
    ):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_BUSY",
            "DeepSeek work has no terminal proof; credential rotation is refused",
        )
    try:
        _unproven_path().unlink()
    except FileNotFoundError:
        pass


def acquire_shell_identity(
    *,
    env: Mapping[str, str],
    wait_seconds: float = 0.0,
) -> ShellIdentityLease:
    """Acquire the full-lifetime Host identity lease before any mutation.

    Native Web and one-shot callers retain the fail-fast default. Managed
    Managed conversation turns may opt into a bounded wait so simultaneous wake
    turns serialize at the shared stock Host identity boundary.
    """
    if wait_seconds < 0:
        raise ValueError("DeepSeek identity wait must be non-negative")
    path = _identity_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    os.chmod(path, 0o600)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handle.close()
                raise DeepSeekWebError(
                    "HARNESS_SHELL_IDENTITY_BUSY",
                    "another DeepSeek execution owns the shared Host credential",
                ) from exc
            time.sleep(min(0.05, remaining))
    try:
        _clear_terminal_unproven(env)
    except Exception:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        raise
    return ShellIdentityLease(handle)


def _verify_shell_identity(env: Mapping[str, str]) -> None:
    token = env.get("SC_API_TOKEN", "")
    api_base = env.get("SC_API_BASE", "")
    shell_id = env.get("SC_SHELL_ID", "")
    shortname = env.get("SC_SHELL_SHORTNAME", "")
    present = tuple(bool(value) for value in (token, api_base, shell_id, shortname))
    if not all(present):
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek requires complete canonical shell API wiring",
        )
    try:
        expected_shell_id = int(shell_id)
    except ValueError as exc:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek shell identity has an invalid shell ID",
        ) from exc
    if expected_shell_id <= 0 or str(expected_shell_id) != shell_id:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek shell identity has an invalid shell ID",
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
    resolved_id = payload.get("shell_id") if isinstance(payload, Mapping) else None
    resolved_name = payload.get("shortname") if isinstance(payload, Mapping) else None
    if resolved_id != expected_shell_id or resolved_name != shortname:
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
        return (
            "dsh" in joined
            and (
                "web" in cmdline
                or ("--host" in cmdline and "--no-open" in cmdline)
            )
        )
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
    value = _host_rpc(port, "workspace.create", {"path": str(worktree)})
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


def _host_rpc(port: int, method: str, rpc_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    rpc_id = str(uuid.uuid4())
    body = {
        "type": "client-request",
        "rpcId": rpc_id,
        "method": method,
        "payload": dict(rpc_payload),
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{method}",
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
            "HARNESS_WEB_GATEWAY_BUSY",
            f"official Host did not prove browser work terminal: {exc}",
        ) from exc
    if payload.get("rpcId") != rpc_id:
        raise DeepSeekWebError(
            "HARNESS_PROTOCOL_ERROR", f"{method} returned a mismatched rpcId"
        )
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else None
        raise DeepSeekWebError(
            "HARNESS_WEB_GATEWAY_BUSY",
            f"{method} did not prove browser work terminal: {error or 'invalid response'}",
        )
    value = result.get("value")
    if not isinstance(value, Mapping):
        raise DeepSeekWebError(
            "HARNESS_PROTOCOL_ERROR", f"{method} returned no value"
        )
    return value


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
    # A crashed service can leave its state record behind without ever having
    # initialized gateway activity.  When neither recorded process survives
    # its start-ticks proof, there is no credentialed execution to drain; clear
    # that dead record so the next owner can start.  A partially live service
    # remains fail-closed below because it might still own accepted work.
    if state and not _activity_path().exists() and not any(
        _verified_process(
            state.get(f"{prefix}_pid"), state.get(f"{prefix}_start_ticks"),
            "web" if prefix == "web" else "relay",
        )
        for prefix in ("web", "relay")
    ):
        state = {}
    relay_live = _verified_process(
        state.get("relay_pid"), state.get("relay_start_ticks"), "relay"
    )
    if state and not _activity_path().exists() and not relay_live:
        # Startup has not published the gateway, so no browser request can have
        # crossed it.  A live private Host is safe to terminate without an
        # activity ledger; a live relay without that ledger still fails closed.
        activity = {"admission": "closed", "requests": {}}
    else:
        activity = _close_gateway_admission(state)
    if not _drain_gateway_work(state, activity):
        raise DeepSeekWebError(
            "HARNESS_WEB_GATEWAY_BUSY",
            "DeepSeek Web has accepted browser work without terminal proof",
        )
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
        _legacy_credential_path().unlink()
    except FileNotFoundError:
        pass
    try:
        _generation_path().unlink()
    except FileNotFoundError:
        pass
    try:
        _reservation_path().unlink()
    except FileNotFoundError:
        pass
    try:
        _activity_path().unlink()
    except FileNotFoundError:
        pass
    return {"stopped": bool(state), "web": web_stopped, "relay": relay_stopped}


def _legacy_credential_path() -> Path:
    """Locate the pre-neutral-Host artifact so stop removes upgrade residue."""
    return _state_path().with_name("deepseek-shell-api.json")


def _generation_path() -> Path:
    return _state_path().with_name("deepseek-web-generation.json")


def _reservation_path() -> Path:
    return _state_path().with_name("deepseek-managed-session.json")


def _activity_path() -> Path:
    return _state_path().with_name("deepseek-web-activity.json")


def _gateway_lock_path() -> Path:
    return _state_path().with_name("deepseek-web-gateway.lock")


@contextmanager
def _gateway_lock():
    path = _gateway_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


async def _acquire_gateway_forward_lock():
    """Acquire the cross-process gate without blocking the relay event loop."""
    path = _gateway_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        os.chmod(path, 0o600)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                await asyncio.sleep(0.01)
    except BaseException:
        handle.close()
        raise


def _release_gateway_forward_lock(handle) -> None:
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()


def _activity_error(detail: str) -> DeepSeekWebError:
    return DeepSeekWebError("HARNESS_WEB_GATEWAY_BUSY", detail)


def _browser_session_id(value: object) -> str | None:
    """Accept only a bounded opaque stock-Web session reference.

    Native DSH creates browser chats as ``session-<UUID>`` while managed
    Subfloor conversations use ``sc-<hex>``.  Activity is an observation of
    stock browser work, not a managed-session identity artifact, so it must
    preserve either safe upstream form.  The reservation artifact remains
    intentionally stricter below.
    """
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value)
    ):
        return value
    return None


def _read_activity(*, required: bool) -> dict[str, Any]:
    path = _activity_path()
    try:
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise OSError("unsafe activity artifact")
        value = json.loads(path.read_text())
    except FileNotFoundError:
        if not required:
            return {"admission": "open", "requests": {}}
        raise _activity_error("DeepSeek Web activity evidence is unavailable") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise _activity_error("DeepSeek Web activity evidence is unreadable") from exc
    if not isinstance(value, Mapping) or value.get("admission") not in {"open", "closed"}:
        raise _activity_error("DeepSeek Web activity evidence is invalid")
    requests = value.get("requests")
    if not isinstance(requests, Mapping):
        raise _activity_error("DeepSeek Web activity requests are invalid")
    checked: dict[str, dict[str, Any]] = {}
    for request_id, record in requests.items():
        if not isinstance(request_id, str) or not isinstance(record, Mapping):
            raise _activity_error("DeepSeek Web activity requests are invalid")
        session_id = record.get("session_id")
        boundary = record.get("boundary")
        if (
            _browser_session_id(session_id) is None
            or not isinstance(boundary, int)
            or isinstance(boundary, bool)
            or boundary < 0
        ):
            raise _activity_error("DeepSeek Web activity requests are invalid")
        status = record.get("status")
        if status not in {"pending", "accepted"}:
            raise _activity_error("DeepSeek Web activity requests are invalid")
        checked[request_id] = {
            "session_id": session_id,
            "boundary": boundary,
            "status": status,
        }
    return {"admission": value["admission"], "requests": checked}


def _write_activity(value: Mapping[str, Any]) -> None:
    path = _activity_path()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(dict(value), handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _initialize_activity() -> None:
    with _gateway_lock():
        _write_activity({"admission": "open", "requests": {}})
        _write_reservation(None)


def _history_boundary(service_port: int, session_id: str) -> int:
    result = _host_rpc(service_port, "session.history", {"sessionId": session_id})
    events = result.get("events") if isinstance(result, Mapping) else None
    if not isinstance(events, list):
        raise _activity_error("DeepSeek Web could not read browser session history")
    sequence = [
        event.get("event", {}).get("seq")
        for event in events
        if isinstance(event, Mapping)
        and isinstance(event.get("event"), Mapping)
        and isinstance(event["event"].get("seq"), int)
        and not isinstance(event["event"].get("seq"), bool)
    ]
    return max(sequence, default=-1) + 1


def _history_is_terminal(service_port: int, session_id: str, boundary: int) -> bool:
    try:
        result = _host_rpc(service_port, "session.history", {"sessionId": session_id})
    except DeepSeekWebError:
        return False
    events = result.get("events") if isinstance(result, Mapping) else None
    if not isinstance(events, list):
        return False
    for envelope in reversed(events):
        event = envelope.get("event") if isinstance(envelope, Mapping) else None
        if (
            not isinstance(event, Mapping)
            or event.get("type") != "turn/end"
            or not isinstance(event.get("seq"), int)
            or event["seq"] < boundary
        ):
            continue
        reason = event.get("data")
        return isinstance(reason, Mapping) and isinstance(reason.get("reason"), Mapping)
    return False


def _close_gateway_admission(state: Mapping[str, Any]) -> dict[str, Any]:
    if not state:
        return {"admission": "closed", "requests": {}}
    with _gateway_lock():
        activity = _read_activity(required=True)
        activity["admission"] = "closed"
        _write_activity(activity)
        return activity


def _drain_gateway_work(state: Mapping[str, Any], activity: Mapping[str, Any]) -> bool:
    requests = activity.get("requests")
    if not isinstance(requests, Mapping):
        return False
    if not requests:
        return True
    service_port = state.get("service_port")
    if not isinstance(service_port, int):
        return False
    pending = {
        request_id: record
        for request_id, record in requests.items()
        if not isinstance(record, Mapping)
        or record.get("status") != "accepted"
        or not _history_is_terminal(
            service_port,
            str(record.get("session_id")),
            int(record.get("boundary", -1)),
        )
    }
    with _gateway_lock():
        current = _read_activity(required=True)
        if current.get("admission") != "closed":
            return False
        current["requests"] = pending
        _write_activity(current)
    return not pending


def _active_browser_requests(
    service_port: int, requests: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """Retain only browser work whose matching terminal proof is absent."""
    return {
        request_id: record
        for request_id, record in requests.items()
        if record.get("status") != "accepted"
        or not _history_is_terminal(
            service_port, str(record["session_id"]), int(record["boundary"])
        )
    }


def _workspace_contains_session(
    service_port: int, workspace_id: object, session_id: str
) -> bool:
    if not isinstance(workspace_id, str) or not workspace_id:
        raise _activity_error("DeepSeek Web workspace identity is invalid")
    value = _host_rpc(service_port, "workspace.list", {})
    rows = value.get("items") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise _activity_error("DeepSeek Web could not confirm workspace membership")
    workspace = next(
        (
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("workspaceId") == workspace_id
        ),
        None,
    )
    if workspace is None:
        return False
    session_ids = workspace.get("sessionIds")
    if not isinstance(session_ids, list):
        raise _activity_error("DeepSeek Web workspace membership is invalid")
    return session_id in session_ids


def _record_browser_prompt_locked(service_port: int, session_id: str) -> str:
    if _browser_session_id(session_id) is None:
        raise _activity_error("DeepSeek Web browser session identity is invalid")
    activity = _read_activity(required=True)
    if activity["admission"] != "open":
        raise _activity_error("DeepSeek Web is closing browser admission")
    if session_id == _reserved_session():
        raise DeepSeekWebError(
            "HARNESS_WEB_SESSION_BUSY",
            "a managed DeepSeek turn owns this native session",
        )
    activity["requests"] = _active_browser_requests(
        service_port, activity["requests"]
    )
    if any(record["session_id"] == session_id for record in activity["requests"].values()):
        raise DeepSeekWebError(
            "HARNESS_WEB_SESSION_BUSY",
            "native Web already owns an accepted or pending prompt for this session",
        )
    request_id = uuid.uuid4().hex
    activity["requests"][request_id] = {
        "session_id": session_id,
        "boundary": _history_boundary(service_port, session_id),
        "status": "pending",
    }
    _write_activity(activity)
    return request_id


def _record_browser_prompt(service_port: int, session_id: str) -> str:
    with _gateway_lock():
        return _record_browser_prompt_locked(service_port, session_id)


def _settle_browser_prompt(request_id: str, *, accepted: bool) -> None:
    with _gateway_lock():
        activity = _read_activity(required=True)
        record = activity["requests"].get(request_id)
        if record is None:
            raise _activity_error("DeepSeek Web prompt evidence disappeared")
        if accepted:
            record["status"] = "accepted"
        else:
            del activity["requests"][request_id]
        _write_activity(activity)


def reserve_managed_session(session_id: str) -> None:
    """Publish the sole managed prompt owner for the native-Web gateway."""
    if re.fullmatch(r"sc-[0-9a-f]{32}", session_id) is None:
        raise DeepSeekWebError(
            "HARNESS_SESSION_INVALID", "managed DeepSeek session identity is invalid"
        )
    with _gateway_lock():
        activity = _read_activity(required=True)
        state = _read_state()
        service_port = state.get("service_port")
        if not isinstance(service_port, int):
            raise _activity_error("DeepSeek Web service state is unavailable")
        active = _active_browser_requests(service_port, activity["requests"])
        if any(record["session_id"] == session_id for record in active.values()):
            raise DeepSeekWebError(
                "HARNESS_WEB_SESSION_BUSY",
                "native Web has accepted browser work for this managed session",
            )
        activity["requests"] = active
        _write_activity(activity)
        _write_reservation(session_id)


def release_managed_session(session_id: str) -> None:
    with _gateway_lock():
        if _reserved_session() == session_id:
            _write_reservation(None)


def _write_reservation(session_id: str | None) -> None:
    path = _reservation_path()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"session_id": session_id}, handle)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _reserved_session() -> str | None:
    try:
        path = _reservation_path()
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise OSError("unsafe managed-session reservation")
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _activity_error("DeepSeek Web managed-session reservation is unavailable") from exc
    session_id = value.get("session_id") if isinstance(value, Mapping) else None
    if session_id is None:
        return None
    if re.fullmatch(r"sc-[0-9a-f]{32}", session_id) is None:
        raise _activity_error("DeepSeek Web managed-session reservation is invalid")
    return session_id


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


def _identity_registry(env: Mapping[str, str]) -> DeepSeekIdentityRegistry:
    override = env.get("SC_DEEPSEEK_IDENTITY_ROOT")
    if override:
        runtime_root = Path(override)
    elif env.get("SC_DEEPSEEK_WEB_STATE"):
        runtime_root = _state_path().with_name("deepseek-identity")
    else:
        runtime_root = ENGINE / "run" / "deepseek-identity"
    return DeepSeekIdentityRegistry(repo_root=REPO_ROOT, runtime_root=runtime_root)


def _candidate_authority(
    registry: DeepSeekIdentityRegistry,
) -> DeepSeekCandidateAuthority:
    return DeepSeekCandidateAuthority(registry.layout.root / "proof-authority")


def _current_dsh_version() -> str:
    version = harness_versions.probe("deepseek")
    if not isinstance(version, str) or not version:
        raise DeepSeekWebError(
            "HARNESS_PROOF_VERSION_UNAVAILABLE",
            "cannot resolve the live DeepSeek version for proof admission",
        )
    return version


def _owner_proof_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise OSError("unsafe owner-only proof artifact")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r") as handle:
            before = os.fstat(handle.fileno())
            if (
                before.st_dev != metadata.st_dev
                or before.st_ino != metadata.st_ino
                or before.st_uid != metadata.st_uid
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise OSError("owner-only proof artifact changed before open")
            value = json.load(handle)
            after = os.fstat(handle.fileno())
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise OSError("owner-only proof artifact changed during read")
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise DeepSeekIdentityError(
            code, "owner-only proof artifact is unavailable"
        ) from exc
    if not isinstance(value, dict):
        raise DeepSeekIdentityError(code, "owner-only proof artifact is malformed")
    return value


def _canonical_proof_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _validate_promotion_runner(
    *,
    registry: DeepSeekIdentityRegistry,
    runner_authorization: Mapping[str, str] | None,
    mode: str,
    proof_run_id: str,
    roots: Mapping[str, Mapping[str, Any]],
) -> str:
    expected_path = registry.layout.root / "proof-authority" / "runner.json"
    raw_path = (
        runner_authorization.get("state_path")
        if isinstance(runner_authorization, Mapping)
        else None
    )
    token = (
        runner_authorization.get("token")
        if isinstance(runner_authorization, Mapping)
        else None
    )
    if not isinstance(raw_path, str) or not isinstance(token, str) or not token:
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_RUNNER_REQUIRED",
            "proof capability mint requires dedicated runner authorization",
        )
    presented_path = Path(raw_path)
    try:
        path = presented_path.resolve(strict=True)
    except OSError as exc:
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_RUNNER_REQUIRED",
            "dedicated runner authorization is unavailable",
        ) from exc
    if (
        not presented_path.is_absolute()
        or presented_path.is_symlink()
        or presented_path != path
        or path != expected_path.resolve()
    ):
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_RUNNER_REQUIRED",
            "proof capability mint rejected foreign runner authority",
        )
    state = _owner_proof_json(path, code="HARNESS_PROOF_RUNNER_REQUIRED")
    expected_roots = {
        root_session_id: dict(root) for root_session_id, root in roots.items()
    }
    exact_ref = _exact_engine_ref()
    if (
        state.get("contract") != PROMOTION_RUNNER_CONTRACT
        or state.get("state") != "authorizing"
        or state.get("runner_pid") != os.getpid()
        or state.get("runner_start_ticks") != process_start_ticks(os.getpid())
        or state.get("runner_token_sha256")
        != hashlib.sha256(token.encode()).hexdigest()
        or state.get("mode") != mode
        or state.get("proof_run_id") != proof_run_id
        or state.get("exact_ref") != exact_ref
        or state.get("roots_sha256") != _canonical_proof_digest(expected_roots)
    ):
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_RUNNER_REQUIRED",
            "dedicated runner identity or engine-derived proof facts changed",
        )
    acceptance = state.get("acceptance")
    if (mode == "candidate" and acceptance is not None) or (
        mode == "promoted"
        and not isinstance(acceptance, Mapping)
    ):
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_ACCEPTANCE_REQUIRED",
            "proof mode is not authorized by the candidate acceptance transition",
        )
    return exact_ref


def _candidate_artifact(
    *,
    env: Mapping[str, str],
    root_session_id: str | None = None,
    conversation_id: str | None = None,
    lifecycle_epoch: int | None = None,
    surface: str | None = None,
) -> Path | None:
    raw_context = env.get("SC_DSH_PROOF_CONTEXT_FILE")
    if raw_context is None:
        if env.get("SC_DSH_PROOF_CAPABILITY_FILE") is not None:
            raise DeepSeekWebError(
                "HARNESS_PROOF_RUNNER_REQUIRED",
                "ambient proof capability selection is refused",
            )
        return None
    registry = _identity_registry(env)
    authority_root = registry.layout.root / "proof-authority"
    contexts_root = authority_root / "contexts"
    presented_context = Path(raw_context)
    try:
        context_path = presented_context.resolve(strict=True)
        if (
            not presented_context.is_absolute()
            or presented_context.is_symlink()
            or presented_context != context_path
        ):
            raise OSError("aliased proof context")
        if context_path.parent != contexts_root.resolve():
            raise OSError("foreign proof context")
        context = _owner_proof_json(
            context_path, code="HARNESS_PROOF_RUNNER_REQUIRED"
        )
        runner = _owner_proof_json(
            authority_root / "runner.json",
            code="HARNESS_PROOF_RUNNER_REQUIRED",
        )
    except (OSError, DeepSeekIdentityError) as exc:
        code = getattr(exc, "code", "HARNESS_PROOF_RUNNER_REQUIRED")
        detail = getattr(exc, "detail", "dedicated proof context is unavailable")
        raise DeepSeekWebError(code, detail) from exc
    expected_context_path = runner.get("contexts", {}).get(
        context.get("root_session_id")
    )
    if (
        context.get("contract") != PROOF_CONTEXT_CONTRACT
        or runner.get("contract") != PROMOTION_RUNNER_CONTRACT
        or runner.get("state") != "active"
        or context.get("runner_id") != runner.get("runner_id")
        or context.get("proof_run_id") != runner.get("proof_run_id")
        or context.get("mode") != runner.get("mode")
        or expected_context_path != str(context_path)
        or (surface is not None and context.get("surface") != surface)
    ):
        raise DeepSeekWebError(
            "HARNESS_PROOF_RUNNER_REQUIRED",
            "proof context is stale or does not match this exact execution",
        )
    if (
        context.get("generation") != runner.get("generation")
        or context.get("artifact") != runner.get("artifact")
    ):
        raise DeepSeekWebError(
            "HARNESS_PROOF_CAPABILITY_STALE",
            "proof context presents a stale capability generation",
        )
    if (
        (root_session_id is not None and context.get("root_session_id") != root_session_id)
        or (conversation_id is not None and context.get("conversation_id") != conversation_id)
        or (lifecycle_epoch is not None and context.get("lifecycle_epoch") != lifecycle_epoch)
    ):
        raise DeepSeekWebError(
            "HARNESS_PROOF_ROOT_REFUSED",
            "proof context does not enumerate this exact root lifecycle",
        )
    artifact = context.get("artifact")
    if not isinstance(artifact, str):
        raise DeepSeekWebError(
            "HARNESS_PROOF_RUNNER_REQUIRED", "proof context lacks an artifact"
        )
    return Path(artifact)


def proof_root_from_environment(
    *, env: Mapping[str, str], surface: str
) -> str | None:
    """Resolve the runner-installed exact root for a non-conversation surface."""
    artifact = _candidate_artifact(env=env, surface=surface)
    if artifact is None:
        return None
    context = _owner_proof_json(
        Path(env["SC_DSH_PROOF_CONTEXT_FILE"]),
        code="HARNESS_PROOF_RUNNER_REQUIRED",
    )
    root_session_id = context.get("root_session_id")
    if not isinstance(root_session_id, str):
        raise DeepSeekWebError(
            "HARNESS_PROOF_ROOT_REFUSED", "proof context lacks an exact root"
        )
    return root_session_id


def _binding_inputs(
    env: Mapping[str, str], worktree: Path
) -> tuple[int, str, str, str, Path]:
    _verify_shell_identity(env)
    try:
        return (
            int(env["SC_SHELL_ID"]),
            env["SC_SHELL_SHORTNAME"],
            env["SC_API_BASE"],
            env["SC_API_TOKEN"],
            worktree.resolve(strict=True),
        )
    except (KeyError, ValueError, OSError) as exc:
        raise DeepSeekWebError(
            "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
            "DeepSeek session binding lacks canonical shell identity",
        ) from exc


def mint_candidate_capability(
    *,
    env: Mapping[str, str],
    mode: str,
    disposable_baseline: str,
    proof_run_id: str,
    roots: Mapping[str, Mapping[str, Any]],
    ttl_seconds: int,
    runner_authorization: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Server-side mint from live exact-ref, Host, and clean-seat evidence."""
    registry = _identity_registry(env)
    try:
        exact_ref = _validate_promotion_runner(
            registry=registry,
            runner_authorization=runner_authorization,
            mode=mode,
            proof_run_id=proof_run_id,
            roots=roots,
        )
        snapshot = registry.read_snapshot()
        health = registry.read_live_health()
        observed_live_roots = sorted(
            root_session_id
            for root_session_id, record in snapshot["records"].items()
            if isinstance(record, Mapping)
            and record.get("state") in {"active", "closing"}
        )
        if observed_live_roots:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_SEAT_NOT_CLEAN",
                "initial proof capability requires an empty live session set",
            )
        with registry.candidate_mint_guard(
            expected_plugin_contract_generation=health[
                "plugin_contract_generation"
            ]
        ) as locked_snapshot:
            grant = _candidate_authority(registry).mint(
                mode=mode,
                exact_ref=exact_ref,
                pinned_dsh_version=_current_dsh_version(),
                disposable_baseline=disposable_baseline,
                proof_run_id=proof_run_id,
                roots=roots,
                plugin_contract_generation=health[
                    "plugin_contract_generation"
                ],
                ttl_seconds=ttl_seconds,
                live_registry_roots=sorted(
                    root_session_id
                    for root_session_id, record in locked_snapshot["records"].items()
                    if isinstance(record, Mapping)
                    and record.get("state") in {"active", "closing"}
                ),
            )
        return {
            "mode": grant.mode,
            "generation": grant.generation,
            "artifact": str(grant.artifact),
            "proof_run_id": grant.proof_run_id,
            "exact_ref": grant.exact_ref,
            "plugin_contract_generation": grant.plugin_contract_generation,
        }
    except DeepSeekIdentityError as exc:
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def _candidate_root_terminal(root_session_id: str) -> bool:
    state = _read_state()
    service_port = state.get("service_port")
    if not isinstance(service_port, int):
        return False
    try:
        _host_rpc(service_port, "session.cancel", {"sessionId": root_session_id})
    except DeepSeekWebError:
        pass
    for attempt in range(3):
        if _history_is_terminal(service_port, root_session_id, 0):
            return True
        if attempt < 2:
            time.sleep(0.05)
    return False


def _fence_candidate_capability(
    *,
    env: Mapping[str, str],
    artifact: Path,
    strict_revoke: bool,
    require_live_root: bool,
    failure_code: str | None = None,
) -> dict[str, Any] | None:
    registry = _identity_registry(env)
    authority = _candidate_authority(registry)
    contract = authority.refusal_contract(artifact=artifact)
    roots = contract["roots"]
    fenced = registry.begin_close_roots(
        roots=roots,
        require_live_root=require_live_root,
        fence_mismatch=True,
    )
    if require_live_root and not fenced:
        return None
    revoked = (
        authority.revoke(artifact=artifact)
        if strict_revoke
        else authority.revoke_for_refusal(
            artifact=artifact, reason_code=failure_code
        )
    )
    outcomes: dict[str, dict[str, Any]] = {}
    for root_session_id, receipt in fenced.items():
        if receipt.state == "terminal":
            outcomes[root_session_id] = {
                "state": "terminal",
                "record_generation": receipt.record_generation,
            }
            continue
        try:
            terminal = _candidate_root_terminal(root_session_id)
            outcomes[root_session_id] = retire_session_identity(
                env=env,
                root_session_id=root_session_id,
                quiesced=terminal,
            )
        except DeepSeekWebError as exc:
            outcomes[root_session_id] = {
                "state": "closing",
                "record_generation": receipt.record_generation,
                "teardown_error": exc.code,
            }
    if (
        not strict_revoke
        and failure_code is not None
        and isinstance(revoked.get("failure"), dict)
    ):
        authority.record_refusal_outcomes(artifact=artifact, roots=outcomes)
        revoked["failure"]["roots"] = outcomes
    return {**revoked, "roots": outcomes}


def _fence_failed_candidate(
    *,
    env: Mapping[str, str],
    artifact: Path,
    failure_code: str | None = None,
    require_live_root: bool = True,
) -> dict[str, Any] | None:
    return _fence_candidate_capability(
        env=env,
        artifact=artifact,
        strict_revoke=False,
        require_live_root=require_live_root,
        failure_code=failure_code,
    )


def revoke_candidate_capability(
    *, env: Mapping[str, str], artifact: Path
) -> dict[str, Any]:
    """Revoke current proof authority and close every enumerated live root."""
    if not artifact.is_absolute() or len(artifact.parents) < 2:
        raise DeepSeekWebError(
            "HARNESS_PROOF_CAPABILITY_UNSAFE",
            "proof capability path must be absolute",
        )
    try:
        result = _fence_candidate_capability(
            env=env,
            artifact=artifact,
            strict_revoke=True,
            require_live_root=False,
        )
        if result is None:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_INVALID",
                "proof capability revocation produced no receipt",
            )
        return result
    except DeepSeekIdentityError as exc:
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def bind_session_identity(
    *,
    env: Mapping[str, str],
    root_session_id: str,
    conversation_id: str,
    lifecycle_epoch: int,
    worktree: Path,
    candidate_preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one exact managed/one-shot root before model-facing admission."""
    if lifecycle_epoch <= 0:
        raise DeepSeekWebError(
            "HARNESS_LIFECYCLE_INVALID",
            "DeepSeek lifecycle epoch must be positive",
        )
    shell_id, shell_shortname, api_base, token, selected = _binding_inputs(
        env, worktree
    )
    registry = _identity_registry(env)
    try:
        health = registry.read_live_health()
        contract_generation = health["plugin_contract_generation"]
        snapshot = registry.read_snapshot()
        record = snapshot["records"].get(root_session_id)
        if candidate_preflight is not None:
            expected = {
                "root_session_id": root_session_id,
                "plugin_contract_generation": contract_generation,
                "binding_snapshot_generation": snapshot[
                    "snapshot_generation"
                ],
                "binding_record_generation": (
                    record.get("record_generation")
                    if isinstance(record, Mapping)
                    else None
                ),
            }
            if any(
                candidate_preflight.get(key) != value
                for key, value in expected.items()
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "proof preflight no longer matches the binding snapshot",
                )
            if record is not None:
                if not isinstance(record, Mapping) or record.get("state") != "active":
                    raise DeepSeekIdentityError(
                        "HARNESS_PROOF_BINDING_MISMATCH",
                        "proof preflight cannot mutate a non-current binding",
                    )
                if not registry.binding_current(
                    root_session_id=root_session_id,
                    conversation_id=conversation_id,
                    lifecycle_epoch=lifecycle_epoch,
                    shell_id=shell_id,
                    shell_shortname=shell_shortname,
                    shell_worktree=selected,
                    api_base=api_base,
                    token=token,
                    plugin_contract_generation=contract_generation,
                ):
                    raise DeepSeekIdentityError(
                        "HARNESS_PROOF_BINDING_MISMATCH",
                        "proof preflight cannot rotate a stale binding",
                    )
        if record is None:
            registry.create_binding(
                expected_snapshot_generation=snapshot["snapshot_generation"],
                root_session_id=root_session_id,
                conversation_id=conversation_id,
                lifecycle_epoch=lifecycle_epoch,
                shell_id=shell_id,
                shell_shortname=shell_shortname,
                shell_worktree=selected,
                api_base=api_base,
                token=token,
                plugin_contract_generation=contract_generation,
            )
        elif candidate_preflight is not None:
            pass
        else:
            if not isinstance(record, dict):
                raise DeepSeekIdentityError(
                    "HARNESS_REGISTRY_INVALID", "binding record is malformed"
                )
            same_owner = (
                record.get("root_session_id") == root_session_id
                and record.get("conversation_id") == conversation_id
                and record.get("shell_id") == shell_id
                and record.get("shell_shortname") == shell_shortname
                and record.get("shell_worktree") == str(selected)
            )
            previous_epoch = record.get("lifecycle_epoch")
            if not same_owner:
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_REUSE_REFUSED",
                    "DSH root session belongs to another exact identity",
                )
            if not isinstance(previous_epoch, int) or lifecycle_epoch < previous_epoch:
                raise DeepSeekIdentityError(
                    "HARNESS_LIFECYCLE_STALE",
                    "DeepSeek lifecycle epoch is stale",
                )
            if lifecycle_epoch > previous_epoch:
                if record.get("state") == "active":
                    closing = registry.begin_close(
                        expected_snapshot_generation=snapshot["snapshot_generation"],
                        root_session_id=root_session_id,
                        expected_record_generation=record["record_generation"],
                    )
                    registry.retire_binding(
                        expected_snapshot_generation=closing.snapshot_generation,
                        root_session_id=root_session_id,
                        expected_record_generation=closing.record_generation,
                        quiesced=True,
                    )
                elif record.get("state") == "closing":
                    registry.retire_binding(
                        expected_snapshot_generation=snapshot["snapshot_generation"],
                        root_session_id=root_session_id,
                        expected_record_generation=record["record_generation"],
                        quiesced=True,
                    )
                elif record.get("state") == "terminal":
                    pass
                else:
                    raise DeepSeekIdentityError(
                        "HARNESS_BINDING_REOPEN_REFUSED",
                        "DeepSeek binding cannot advance from its durable state",
                    )
                reopened_snapshot = registry.read_snapshot()
                terminal_record = reopened_snapshot["records"][root_session_id]
                registry.reopen_binding(
                    expected_snapshot_generation=reopened_snapshot[
                        "snapshot_generation"
                    ],
                    root_session_id=root_session_id,
                    expected_record_generation=terminal_record["record_generation"],
                    conversation_id=conversation_id,
                    lifecycle_epoch=lifecycle_epoch,
                    shell_id=shell_id,
                    shell_shortname=shell_shortname,
                    shell_worktree=selected,
                    api_base=api_base,
                    token=token,
                    plugin_contract_generation=contract_generation,
                )
            elif record.get("state") != "active":
                raise DeepSeekIdentityError(
                    "HARNESS_BINDING_NOT_LIVE",
                    "DeepSeek root is not active in this lifecycle epoch",
                )
            elif not registry.binding_current(
                root_session_id=root_session_id,
                conversation_id=conversation_id,
                lifecycle_epoch=lifecycle_epoch,
                shell_id=shell_id,
                shell_shortname=shell_shortname,
                shell_worktree=selected,
                api_base=api_base,
                token=token,
                plugin_contract_generation=contract_generation,
            ):
                registry.rotate_binding(
                    expected_snapshot_generation=snapshot["snapshot_generation"],
                    root_session_id=root_session_id,
                    expected_record_generation=record["record_generation"],
                    token=token,
                    plugin_contract_generation=contract_generation,
                    recovery=(
                        record.get("plugin_contract_generation")
                        != contract_generation
                    ),
                )
        current = registry.resolve_record(root_session_id)
        if not registry.binding_current(
            root_session_id=root_session_id,
            conversation_id=conversation_id,
            lifecycle_epoch=lifecycle_epoch,
            shell_id=shell_id,
            shell_shortname=shell_shortname,
            shell_worktree=selected,
            api_base=api_base,
            token=token,
            plugin_contract_generation=contract_generation,
        ):
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_MISMATCH",
                "DeepSeek binding changed before admission",
            )
        return {
            "root_session_id": root_session_id,
            "conversation_id": conversation_id,
            "lifecycle_epoch": lifecycle_epoch,
            "record_generation": current["record_generation"],
            "plugin_contract_generation": contract_generation,
        }
    except DeepSeekIdentityError as exc:
        raw_artifact = (
            candidate_preflight.get("proof_artifact")
            if isinstance(candidate_preflight, Mapping)
            else None
        )
        if (
            candidate_preflight is not None
            and isinstance(raw_artifact, str)
            and exc.code in CANDIDATE_FENCE_CODES
        ):
            _fence_failed_candidate(
                env=env, artifact=Path(raw_artifact), failure_code=exc.code
            )
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def preflight_candidate_execution(
    *,
    env: Mapping[str, str],
    root_session_id: str,
    conversation_id: str,
    lifecycle_epoch: int,
    worktree: Path,
) -> dict[str, Any] | None:
    """Validate proof authority completely before any binding mutation."""
    artifact = _candidate_artifact(
        env=env,
        root_session_id=root_session_id,
        conversation_id=conversation_id,
        lifecycle_epoch=lifecycle_epoch,
    )
    if artifact is None:
        return None
    if not artifact.is_absolute() or len(artifact.parents) < 2:
        raise DeepSeekWebError(
            "HARNESS_PROOF_CAPABILITY_UNSAFE",
            "proof capability path must be absolute",
        )
    shell_id, shell_shortname, api_base, token, selected = _binding_inputs(
        env, worktree
    )
    registry = _identity_registry(env)
    authority = _candidate_authority(registry)
    try:
        contract = authority.describe(artifact=artifact)
        health = registry.read_live_health()
        snapshot = registry.read_snapshot()
        record = snapshot["records"].get(root_session_id)
        if record is not None and not isinstance(record, Mapping):
            raise DeepSeekIdentityError(
                "HARNESS_REGISTRY_INVALID", "binding record is malformed"
            )
        record_generation = (
            record.get("record_generation")
            if isinstance(record, Mapping)
            else None
        )
        lineage = sorted(
            session_id
            for session_id, item in snapshot["lineage"].items()
            if isinstance(item, Mapping)
            and item.get("root_session_id") == root_session_id
            and item.get("lifecycle_epoch") == lifecycle_epoch
            and (
                record_generation is None
                or item.get("record_generation") == record_generation
            )
        )
        admitted = authority.admit(
            artifact=artifact,
            mode=contract["mode"],
            exact_ref=_exact_engine_ref(),
            pinned_dsh_version=_current_dsh_version(),
            root_session_id=root_session_id,
            conversation_id=conversation_id,
            lifecycle_epoch=lifecycle_epoch,
            verified_lineage=lineage,
            plugin_contract_generation=health["plugin_contract_generation"],
        )
        if record is not None and (
            record.get("state") != "active"
            or not registry.binding_current(
                root_session_id=root_session_id,
                conversation_id=conversation_id,
                lifecycle_epoch=lifecycle_epoch,
                shell_id=shell_id,
                shell_shortname=shell_shortname,
                shell_worktree=selected,
                api_base=api_base,
                token=token,
                plugin_contract_generation=health[
                    "plugin_contract_generation"
                ],
            )
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_BINDING_MISMATCH",
                "proof root has a partially recovered or stale binding",
            )
        return {
            **admitted,
            "proof_artifact": str(artifact),
            "binding_snapshot_generation": snapshot[
                "snapshot_generation"
            ],
            "binding_record_generation": record_generation,
        }
    except DeepSeekIdentityError as exc:
        if exc.code in CANDIDATE_FENCE_CODES:
            _fence_failed_candidate(
                env=env, artifact=artifact, failure_code=exc.code
            )
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def retire_session_identity(
    *, env: Mapping[str, str], root_session_id: str, quiesced: bool
) -> dict[str, Any]:
    """Close a disposable root, retiring it only after quiescence proof."""
    registry = _identity_registry(env)
    try:
        snapshot = registry.read_snapshot()
        record = snapshot["records"].get(root_session_id)
        if not isinstance(record, dict):
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_NOT_LIVE", "DeepSeek root binding is missing"
            )
        if record.get("state") == "active":
            closing = registry.begin_close(
                expected_snapshot_generation=snapshot["snapshot_generation"],
                root_session_id=root_session_id,
                expected_record_generation=record["record_generation"],
            )
            closing_snapshot_generation = closing.snapshot_generation
            closing_record_generation = closing.record_generation
        elif record.get("state") == "closing":
            closing_snapshot_generation = snapshot["snapshot_generation"]
            closing_record_generation = record["record_generation"]
        elif record.get("state") == "terminal":
            return {
                "root_session_id": root_session_id,
                "state": "terminal",
                "lifecycle_epoch": record["lifecycle_epoch"],
                "record_generation": record["record_generation"],
            }
        else:
            raise DeepSeekIdentityError(
                "HARNESS_BINDING_NOT_LIVE",
                "DeepSeek root is not eligible for terminal retirement",
            )
        if not quiesced:
            return {
                "root_session_id": root_session_id,
                "state": "closing",
                "lifecycle_epoch": record["lifecycle_epoch"],
                "record_generation": closing_record_generation,
            }
        receipt = registry.retire_binding(
            expected_snapshot_generation=closing_snapshot_generation,
            root_session_id=root_session_id,
            expected_record_generation=closing_record_generation,
            quiesced=True,
        )
        return {
            "root_session_id": root_session_id,
            "state": receipt.state,
            "lifecycle_epoch": receipt.lifecycle_epoch,
            "record_generation": receipt.record_generation,
        }
    except DeepSeekIdentityError as exc:
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def admit_candidate_execution(
    *,
    env: Mapping[str, str],
    root_session_id: str,
    conversation_id: str,
    lifecycle_epoch: int,
) -> dict[str, Any] | None:
    """Admit one server-minted proof root; ordinary callers return ``None``."""
    artifact = _candidate_artifact(
        env=env,
        root_session_id=root_session_id,
        conversation_id=conversation_id,
        lifecycle_epoch=lifecycle_epoch,
    )
    if artifact is None:
        return None
    if not artifact.is_absolute() or len(artifact.parents) < 2:
        raise DeepSeekWebError(
            "HARNESS_PROOF_CAPABILITY_UNSAFE",
            "proof capability path must be absolute",
        )
    registry = _identity_registry(env)
    authority = _candidate_authority(registry)
    try:
        contract = authority.describe(artifact=artifact)
        exact_ref = _exact_engine_ref()
        pinned_version = _current_dsh_version()
        health = registry.read_live_health()
        record = registry.resolve_record(root_session_id)
        snapshot = registry.read_snapshot()
        lineage = sorted(
            session_id
            for session_id, item in snapshot["lineage"].items()
            if isinstance(item, Mapping)
            and item.get("root_session_id") == root_session_id
            and item.get("lifecycle_epoch") == lifecycle_epoch
            and item.get("record_generation") == record.get("record_generation")
        )
        return authority.admit(
            artifact=artifact,
            mode=contract["mode"],
            exact_ref=exact_ref,
            pinned_dsh_version=pinned_version,
            root_session_id=root_session_id,
            conversation_id=conversation_id,
            lifecycle_epoch=lifecycle_epoch,
            verified_lineage=lineage,
            plugin_contract_generation=health["plugin_contract_generation"],
        )
    except DeepSeekIdentityError as exc:
        if exc.code in CANDIDATE_FENCE_CODES:
            _fence_failed_candidate(
                env=env, artifact=artifact, failure_code=exc.code
            )
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def ratchet_candidate_after_host_restart(
    *, env: Mapping[str, str], artifact: Path, ttl_seconds: int
) -> dict[str, Any]:
    """Advance proof authority only after every exact binding recovered."""
    if not artifact.is_absolute() or len(artifact.parents) < 2:
        raise DeepSeekWebError(
            "HARNESS_PROOF_CAPABILITY_UNSAFE",
            "proof capability path must be absolute",
        )
    registry = _identity_registry(env)
    authority = _candidate_authority(registry)
    try:
        expected = authority.recovery_contract(artifact=artifact)
        if (
            expected["exact_ref"] != _exact_engine_ref()
            or expected["pinned_dsh_version"]
            != harness_versions.probe("deepseek")
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_MISMATCH",
                "proof restart no longer runs the exact candidate runtime",
            )
        health = registry.read_live_health()
        new_contract = health["plugin_contract_generation"]
        snapshot = registry.read_snapshot()
        actual_roots: dict[str, dict[str, Any]] = {}
        for root_session_id, expected_root in expected["roots"].items():
            record = snapshot["records"].get(root_session_id)
            if (
                not isinstance(record, Mapping)
                or record.get("state") != "active"
                or record.get("conversation_id")
                != expected_root["conversation_id"]
                or record.get("lifecycle_epoch")
                != expected_root["lifecycle_epoch"]
                or record.get("plugin_contract_generation") != new_contract
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_RESTART_BINDING_MISMATCH",
                    "proof restart did not recover every exact root binding",
                )
            lineage = sorted(
                session_id
                for session_id, item in snapshot["lineage"].items()
                if isinstance(item, Mapping)
                and item.get("root_session_id") == root_session_id
                and item.get("lifecycle_epoch") == record["lifecycle_epoch"]
                and item.get("record_generation") == record["record_generation"]
            )
            actual_roots[root_session_id] = {
                "conversation_id": record["conversation_id"],
                "lifecycle_epoch": record["lifecycle_epoch"],
                "verified_lineage": lineage,
            }
        grant = authority.ratchet_after_host_restart(
            artifact=artifact,
            old_plugin_contract_generation=expected[
                "plugin_contract_generation"
            ],
            new_plugin_contract_generation=new_contract,
            roots=actual_roots,
            ttl_seconds=ttl_seconds,
        )
        return {
            "mode": grant.mode,
            "generation": grant.generation,
            "artifact": str(grant.artifact),
            "proof_run_id": grant.proof_run_id,
            "exact_ref": grant.exact_ref,
            "plugin_contract_generation": grant.plugin_contract_generation,
        }
    except (DeepSeekIdentityError, DeepSeekWebError) as exc:
        try:
            _fence_failed_candidate(
                env=env,
                artifact=artifact,
                failure_code=f"ratchet:{exc.code}",
                require_live_root=False,
            )
        except (DeepSeekIdentityError, DeepSeekWebError) as fence_exc:
            code = getattr(fence_exc, "code", "HARNESS_PROOF_RATCHET_FENCE_FAILED")
            raise DeepSeekWebError(
                "HARNESS_PROOF_RATCHET_FENCE_FAILED",
                f"proof restart failed with {exc.code} and fencing failed with {code}",
            ) from fence_exc
        if isinstance(exc, DeepSeekWebError):
            raise
        raise DeepSeekWebError(exc.code, exc.detail) from exc


def stop(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Stop only while holding the same identity lease as every acting path."""
    env = os.environ if env is None else env
    lease = acquire_shell_identity(env=env)
    try:
        _verify_shell_identity(env)
        with _service_lock():
            return _stop_unlocked()
    finally:
        lease.close()


def _existing_healthy(
    state: Mapping[str, Any],
    service_port: int,
    relay_port: int,
    *,
    listen_host: str,
    allowed_peers: tuple[str, ...],
    identity_registry: DeepSeekIdentityRegistry,
) -> bool:
    if state.get("schema_version") != 5:
        return False
    if state.get("service_port") != service_port:
        return False
    if not _verified_process(state.get("web_pid"), state.get("web_start_ticks"), "web"):
        return False
    if not _http_ready(service_port):
        return False
    if (
        state.get("fork_id") != identity_registry.layout.fork_id
        or state.get("profile_id") != identity_registry.layout.profile_id
        or state.get("registry_path")
        != str(identity_registry.layout.registry.resolve())
    ):
        return False
    try:
        health = identity_registry.read_live_health(
            expected_host_boot_generation=state.get("host_boot_generation")
        )
    except DeepSeekIdentityError:
        return False
    if state.get("plugin_contract_generation") != health.get(
        "plugin_contract_generation"
    ):
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
    register_workspace: bool = True,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    if identity_lease is None:
        lease = acquire_shell_identity(env=env)
        try:
            return ensure(
                worktree,
                env=env,
                identity_lease=lease,
                register_workspace=register_workspace,
            )
        finally:
            lease.close()
    _verify_shell_identity(env)
    with _service_lock():
        if _disabled(env):
            _stop_unlocked()
            raise DeepSeekWebError("HARNESS_DISABLED", "DeepSeek is disabled")
        selected = _worktree(worktree)
        config = ports.resolve(persist=True)
        public_port = _service_port(config, env)
        # Docker maps the fixed host entry to the sandbox gateway port. On a
        # bare host the gateway itself owns that fixed entry and stock dsh
        # moves to the private offset instead. The browser URL never changes.
        sandbox = bool(env.get("SC_SANDBOX"))
        service_port = public_port if sandbox else public_port + ports.DEEPSEEK_RELAY_OFFSET
        relay_port = service_port + ports.DEEPSEEK_RELAY_OFFSET if sandbox else public_port
        state = _read_state()
        identity_registry = _identity_registry(env)
        listen_host, allowed_peers = _relay_configuration(sandbox=sandbox)
        reused = _existing_healthy(
            state,
            service_port,
            relay_port,
            listen_host=listen_host,
            allowed_peers=allowed_peers,
            identity_registry=identity_registry,
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
            web_env = dict(env)
            identity_env = identity_registry.host_environment()
            for name in tuple(web_env):
                if name in {
                    "SC_API_TOKEN", "SC_API_BASE", "SC_MEM_CREDENTIAL_FILE",
                    "SC_SHELL_ID", "SC_SHELL_SHORTNAME", "SC_SHELL_WORKTREE",
                    "DSH_SHELL",
                } or name.startswith(("DSH_SC_", "SC_DSH_")):
                    web_env.pop(name, None)
            web_env.update(identity_env)
            web_pid, web_ticks = _spawn(
                [
                    executable,
                    "--profile",
                    identity_registry.layout.profile_id,
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
                "schema_version": 5,
                "web_pid": web_pid,
                "web_start_ticks": web_ticks,
                "service_port": service_port,
                "relay_port": relay_port,
                "relay_policy": RELAY_POLICY,
                "relay_listen_host": listen_host,
                "relay_allowed_peers": list(allowed_peers),
                "url": f"http://127.0.0.1:{public_port}",
                "host_identity": "neutral",
                "fork_id": identity_registry.layout.fork_id,
                "profile_id": identity_registry.layout.profile_id,
                "registry_path": str(identity_registry.layout.registry.resolve()),
                "plugin_health_path": str(identity_registry.layout.health.resolve()),
                "host_boot_generation": identity_env[
                    "SC_DSH_HOST_BOOT_GENERATION"
                ],
            }
            _write_state(state)
            try:
                identity_registry.observe_host(
                    host_boot_generation=state["host_boot_generation"],
                    host_pid=web_pid,
                    host_start_ticks=web_ticks,
                )
            except DeepSeekIdentityError as exc:
                _stop_unlocked()
                raise DeepSeekWebError(exc.code, exc.detail) from exc
            if not _wait_ready(lambda: _http_ready(service_port)):
                _stop_unlocked()
                raise DeepSeekWebError(
                    "HARNESS_SERVICE_UNAVAILABLE",
                    f"official dsh Web did not become ready; inspect {_log_path()}",
                )
            try:
                health = identity_registry.read_live_health(
                    expected_host_boot_generation=state["host_boot_generation"]
                )
            except DeepSeekIdentityError as exc:
                _stop_unlocked()
                raise DeepSeekWebError(exc.code, exc.detail) from exc
            state["plugin_contract_generation"] = health[
                "plugin_contract_generation"
            ]
            _write_state(state)
            generation = _write_generation()
            # The relay never calls the engine API.  Give it no shell
            # credential: inheriting the launcher environment would expose the
            # API token through the relay's process environment even though the
            # stock Host correctly receives only the owner-only artifact.
            relay_env = dict(os.environ)
            for name in tuple(relay_env):
                if name in {
                    "SC_API_TOKEN", "SC_API_BASE", "SC_MEM_CREDENTIAL_FILE",
                    "SC_SHELL_ID", "SC_SHELL_SHORTNAME", "SC_SHELL_WORKTREE",
                    "DSH_SHELL",
                } or name.startswith(("DSH_SC_", "SC_DSH_PROOF_")):
                    relay_env.pop(name, None)
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
                env=relay_env,
            )
            state.update(
                {"relay_pid": relay_pid, "relay_start_ticks": relay_ticks}
            )
            _write_state(state)
            _initialize_activity()
            if not _wait_ready(lambda: _tcp_ready("127.0.0.1", relay_port)):
                _stop_unlocked()
                raise DeepSeekWebError(
                    "HARNESS_SERVICE_UNAVAILABLE",
                    "DeepSeek loopback publication relay did not become ready",
                )
        generation = generation or _read_generation(_generation_path())
        url = f"http://127.0.0.1:{relay_port}/?sc_generation={generation}"
        if not register_workspace:
            return {**state, "url": url, "reused": reused}
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
    plugin_health = "unavailable"
    plugin_contract_generation = None
    plugin_current = False
    if state.get("schema_version") == 5:
        try:
            identity = _identity_registry(os.environ)
            health = identity.read_live_health(
                expected_host_boot_generation=state.get("host_boot_generation")
            )
            plugin_contract_generation = health["plugin_contract_generation"]
            plugin_current = plugin_contract_generation == state.get(
                "plugin_contract_generation"
            )
            plugin_health = "loaded" if plugin_current else "mismatch"
        except DeepSeekIdentityError as exc:
            plugin_health = exc.code
    ready = (
        web
        and isinstance(service_port, int)
        and _http_ready(service_port)
        and relay
        and relay_safe
        and (relay_port is None or _tcp_ready("127.0.0.1", relay_port))
        and plugin_current
    )
    return {
        **state,
        "ready": ready,
        "web_process": web,
        "relay_process": relay,
        "relay_safe": relay_safe,
        "plugin_health": plugin_health,
        "plugin_contract_generation": plugin_contract_generation,
    }


async def _relay_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_port: int,
    allowed_peers: frozenset[str],
    generation: str | None = None,
    generation_file: Path | None = None,
) -> None:
    forward_lock = None

    def release_forward_lock() -> None:
        nonlocal forward_lock
        if forward_lock is None:
            return
        _release_gateway_forward_lock(forward_lock)
        forward_lock = None

    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer or peer[0] not in allowed_peers:
        writer.close()
        await writer.wait_closed()
        return
    if generation_file is not None:
        try:
            generation = _read_generation(generation_file)
        except DeepSeekWebError:
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
    clean_target = None
    if len(request_line) == 3:
        parsed_target = urllib.parse.urlsplit(request_line[1])
        query = urllib.parse.parse_qs(parsed_target.query)
        query_generation = (query.get("sc_generation") or [None])[0]
        if query_generation is not None:
            clean_query = [(key, value) for key, values in query.items() for value in values if key != "sc_generation"]
            request_line[1] = urllib.parse.urlunsplit(("", "", parsed_target.path or "/", urllib.parse.urlencode(clean_query), ""))
            clean_target = request_line[1]
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
    if query_generation is not None:
        # Consume the one-shot capability before the upstream app sees a
        # request.  The browser retains only this clean URL in history and its
        # subsequent Referer cannot disclose the generation to DSH.
        assert clean_target is not None
        writer.write(
            b"HTTP/1.1 302 Found\r\n"
            + f"Location: {clean_target}\r\n".encode("iso-8859-1")
            + f"Set-Cookie: {GENERATION_COOKIE}={generation}; HttpOnly; SameSite=Strict; Path=/\r\n".encode("iso-8859-1")
            + b"Cache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return
    target = request_line[1] if len(request_line) == 3 else ""
    websocket = any(
        line.lower() == "upgrade: websocket" for line in lines[1:]
    )
    if websocket and target.split("?", 1)[0] not in {
        "/api/events.mux", "/api/events.host"
    }:
        writer.close()
        await writer.wait_closed()
        return
    target_path = target.split("?", 1)[0]
    guarded_mutation = (
        target_path in SESSION_MUTATION_PATHS
        or target_path == WORKSPACE_DELETE_PATH
    )
    prompt_record_id = None
    if guarded_mutation:
        content_length = next(
            (
                line.split(":", 1)[1].strip()
                for line in lines[1:]
                if line.lower().startswith("content-length:")
            ),
            "0",
        )
        try:
            length = int(content_length)
            body = await asyncio.wait_for(
                reader.readexactly(length), timeout=HTTP_TIMEOUT_SECONDS
            )
            payload = json.loads(body)
        except (
            ValueError,
            asyncio.IncompleteReadError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            writer.close()
            await writer.wait_closed()
            return
        rpc_payload = payload.get("payload") if isinstance(payload, Mapping) else None
        session_id = (
            rpc_payload.get("sessionId") if isinstance(rpc_payload, Mapping) else None
        )
        if (
            target_path != "/api/session.create"
            and target_path != WORKSPACE_DELETE_PATH
            and (
                not isinstance(rpc_payload, Mapping)
                or not any(
                    isinstance(rpc_payload.get(field), str)
                    for field in SESSION_MUTATION_FIELDS.get(target_path, ())
                )
            )
        ):
            writer.close()
            await writer.wait_closed()
            return
        try:
            forward_lock = await _acquire_gateway_forward_lock()
            reserved = _reserved_session()
            targets_reserved = (
                reserved is not None
                and isinstance(rpc_payload, Mapping)
                and any(
                    rpc_payload.get(field) == reserved
                    for field in SESSION_MUTATION_FIELDS.get(target_path, ())
                )
            )
            if (
                not targets_reserved
                and reserved is not None
                and target_path == WORKSPACE_DELETE_PATH
            ):
                targets_reserved = _workspace_contains_session(
                    target_port,
                    rpc_payload.get("workspaceId")
                    if isinstance(rpc_payload, Mapping)
                    else None,
                    reserved,
                )
        except DeepSeekWebError as exc:
            release_forward_lock()
            body = json.dumps({"error": exc.code}, separators=(",", ":")).encode()
            writer.write(
                b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        except BaseException:
            release_forward_lock()
            raise
        if targets_reserved:
            release_forward_lock()
            body = b'{"error":"HARNESS_WEB_SESSION_BUSY"}'
            writer.write(
                b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        if target_path == "/api/session.prompt":
            try:
                prompt_record_id = _record_browser_prompt_locked(
                    target_port, session_id
                )
            except DeepSeekWebError as exc:
                release_forward_lock()
                body = json.dumps({"error": exc.code}, separators=(",", ":")).encode()
                writer.write(
                    b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            except BaseException:
                release_forward_lock()
                raise
        request += body
    headers = [
        line for line in lines[1:]
        if line
        and not (line.lower().startswith("referer:") and "sc_generation=" in line.lower())
    ]
    if not websocket:
        headers = [
            line for line in headers if not line.lower().startswith("connection:")
        ]
        request = "\r\n".join(
            [lines[0], *headers, "Connection: close", "", ""]
        ).encode("iso-8859-1")
        if guarded_mutation:
            request += body
    else:
        request = "\r\n".join([lines[0], *headers, "", ""]).encode("iso-8859-1")
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", target_port
        )
    except OSError:
        release_forward_lock()
        if "prompt_record_id" in locals() and prompt_record_id is not None:
            _settle_browser_prompt(prompt_record_id, accepted=False)
        writer.close()
        await writer.wait_closed()
        return
    except BaseException:
        release_forward_lock()
        writer.close()
        raise
    try:
        upstream_writer.write(request)
        await upstream_writer.drain()
    except OSError:
        release_forward_lock()
        # The TCP peer accepted a connection; whether it consumed the prompt is
        # unknowable, so preserve the pending record and fail handoff closed.
        upstream_writer.close()
        writer.close()
        await upstream_writer.wait_closed()
        await writer.wait_closed()
        return
    except BaseException:
        release_forward_lock()
        upstream_writer.close()
        writer.close()
        raise
    if target_path == "/api/session.prompt":
        # Prompt ownership moves to the terminal activity ledger after Host
        # admission so concurrent same-session requests can fail immediately.
        release_forward_lock()

    try:
        response = await asyncio.wait_for(upstream_reader.readuntil(b"\r\n\r\n"), timeout=HTTP_TIMEOUT_SECONDS)
    except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError):
        release_forward_lock()
        upstream_writer.close()
        writer.close()
        await upstream_writer.wait_closed()
        await writer.wait_closed()
        return
    if "prompt_record_id" in locals() and prompt_record_id is not None:
        response_lines = response.decode("iso-8859-1").split("\r\n")
        raw_length = next(
            (line.split(":", 1)[1].strip() for line in response_lines if line.lower().startswith("content-length:")),
            None,
        )
        chunked = any(
            line.lower().startswith("transfer-encoding:")
            and "chunked" in line.lower()
            for line in response_lines
        )
        try:
            if chunked:
                wire_chunks: list[bytes] = []
                decoded_chunks: list[bytes] = []
                while True:
                    size_line = await asyncio.wait_for(
                        upstream_reader.readuntil(b"\r\n"), timeout=HTTP_TIMEOUT_SECONDS
                    )
                    wire_chunks.append(size_line)
                    size = int(size_line[:-2].split(b";", 1)[0], 16)
                    if size == 0:
                        while True:
                            trailer = await asyncio.wait_for(
                                upstream_reader.readuntil(b"\r\n"), timeout=HTTP_TIMEOUT_SECONDS
                            )
                            wire_chunks.append(trailer)
                            if trailer == b"\r\n":
                                break
                        break
                    chunk = await asyncio.wait_for(
                        upstream_reader.readexactly(size + 2), timeout=HTTP_TIMEOUT_SECONDS
                    )
                    if chunk[-2:] != b"\r\n":
                        raise ValueError("malformed chunked response")
                    wire_chunks.append(chunk)
                    decoded_chunks.append(chunk[:-2])
                response_body = b"".join(wire_chunks)
                decoded_body = b"".join(decoded_chunks)
            else:
                response_body = await asyncio.wait_for(
                    upstream_reader.readexactly(int(raw_length or "-1")), timeout=HTTP_TIMEOUT_SECONDS
                )
                decoded_body = response_body
            result = json.loads(decoded_body).get("result")
            accepted = (
                isinstance(result, Mapping)
                and result.get("ok") is True
                and isinstance(result.get("value"), Mapping)
                and result["value"].get("accepted") is True
            )
        except (ValueError, asyncio.IncompleteReadError, TimeoutError, json.JSONDecodeError):
            release_forward_lock()
            upstream_writer.close()
            writer.close()
            await upstream_writer.wait_closed()
            await writer.wait_closed()
            return
        _settle_browser_prompt(prompt_record_id, accepted=accepted)
        release_forward_lock()
        writer.write(response + response_body)
        await writer.drain()
        upstream_writer.close()
        writer.close()
        await upstream_writer.wait_closed()
        await writer.wait_closed()
        return
    async def drain_upstream_response() -> None:
        try:
            while await upstream_reader.read(64 * 1024):
                pass
        except ConnectionError:
            pass

    try:
        writer.write(response)
        await writer.drain()
    except ConnectionError:
        writer.close()
        try:
            if forward_lock is not None:
                await drain_upstream_response()
        finally:
            release_forward_lock()
        upstream_writer.close()
        await upstream_writer.wait_closed()
        await writer.wait_closed()
        return
    except BaseException:
        release_forward_lock()
        upstream_writer.close()
        writer.close()
        raise

    async def copy(
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
        *,
        drain_source: bool = False,
    ) -> None:
        destination_open = True
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    break
                if not destination_open:
                    continue
                try:
                    destination.write(chunk)
                    await destination.drain()
                except ConnectionError:
                    destination.close()
                    if not drain_source:
                        break
                    destination_open = False
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            destination.close()

    if websocket:
        tasks = [
            asyncio.create_task(copy(reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, writer)),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        await copy(
            upstream_reader,
            writer,
            drain_source=forward_lock is not None,
        )
        release_forward_lock()
        upstream_writer.close()
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
            reader,
            writer,
            target_port,
            allowed_peers,
            generation,
            generation_file,
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
