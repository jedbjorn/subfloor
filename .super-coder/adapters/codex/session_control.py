#!/usr/bin/env python3
"""Codex app-server transport for provider-neutral session control."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Callable

ADAPTER_DIR = Path(__file__).resolve().parent
ENGINE = ADAPTER_DIR.parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # type: ignore[import-not-found]  # noqa: E402
import session_control as common_session_control  # type: ignore[import-not-found]  # noqa: E402
import session_supervisor  # type: ignore[import-not-found]  # noqa: E402
from codex_rpc import (  # noqa: E402
    AppServerClient, CodexProtocolError, CodexRpcError, probe_codex,
    unix_socket_path,
)

CONTROL_DIR = ENGINE / "run" / "session-control"


def _capabilities(binding: dict) -> dict:
    value = binding.get("control_capabilities") or "{}"
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _settings(binding: dict) -> dict:
    value = _capabilities(binding).get("settings") or {}
    return value if isinstance(value, dict) else {}


def _thread_status(result: dict) -> str:
    thread = result.get("thread") or {}
    status = thread.get("status") or {}
    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        return str(status.get("type") or "")
    return ""


def resume_command(binding: dict, prompt: str) -> list[str]:
    settings = _settings(binding)
    native_id = binding.get("native_session_id")
    if not native_id:
        raise RuntimeError("Codex native thread ID is unavailable")
    command = ["codex", "exec", "resume"]
    model = settings.get("model") or binding.get("archive_model")
    if model:
        command.extend(["-m", str(model)])
    if settings.get("sandbox") == "danger-full-access":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = settings.get("sandbox")
        approval = settings.get("approval_policy")
        effort = settings.get("effort")
        if sandbox:
            command.extend(["-c", f"sandbox_mode={json.dumps(str(sandbox))}"])
        if approval:
            if not isinstance(approval, str):
                raise RuntimeError("Codex granular approval posture cannot be resumed safely")
            command.extend(["-c", f"approval_policy={json.dumps(approval)}"])
        if effort:
            command.extend(["-c", f"model_reasoning_effort={json.dumps(str(effort))}"])
    command.extend(["--dangerously-bypass-hook-trust", str(native_id), prompt])
    return command


def _run_fenced_resume(binding: dict, command: list[str], cwd: Path) -> None:
    lease: dict[str, int] = {}

    def preflight() -> None:
        con = db_driver.connect(DB_PATH)
        try:
            session_supervisor.preflight_lease(
                con, binding["binding_id"], repo_root=REPO_ROOT
            )
        finally:
            con.close()

    def started(pid: int) -> None:
        con = db_driver.connect(DB_PATH)
        try:
            generation = session_supervisor.claim_lease(
                con, binding["binding_id"], pid, repo_root=REPO_ROOT,
                state="dispatching",
            )
        finally:
            con.close()
        identity = session_supervisor.read_process(pid)
        if not identity:
            raise RuntimeError("Codex resume vanished while recording its lease")
        lease.update(pid=pid, start_ticks=identity.start_ticks, generation=generation)

    def exited(_pid: int, returncode: int) -> None:
        if not lease:
            return
        con = db_driver.connect(DB_PATH)
        try:
            session_supervisor.release_lease(
                con, binding["binding_id"], lease["pid"], lease["start_ticks"],
                lease["generation"],
                error=f"Codex resume exited {returncode}" if returncode else None,
            )
        finally:
            con.close()

    rc = session_supervisor.supervise(
        command, cwd=cwd, env=dict(os.environ),
        on_pre_spawn=preflight, on_started=started, on_exited=exited,
    )
    if rc:
        raise RuntimeError(f"Codex resume exited {rc}")


class CodexAdapter:
    def __init__(self, *, client_factory: Callable[..., AppServerClient] = AppServerClient,
                 endpoint_available: Callable[[Path], bool] | None = None,
                 resume_runner: Callable[[dict, list[str], Path], None] = _run_fenced_resume,
                 resume_probe: Callable[[], dict] = probe_codex):
        self.client_factory = client_factory
        self.endpoint_available = endpoint_available or self._is_socket
        self.resume_runner = resume_runner
        self.resume_probe = resume_probe

    @staticmethod
    def _is_socket(path: Path) -> bool:
        try:
            return stat.S_ISSOCK(path.lstat().st_mode)
        except OSError:
            return False

    def _endpoint(self, binding: dict) -> tuple[str, Path]:
        endpoint = str(binding.get("control_endpoint") or "")
        path = unix_socket_path(endpoint)
        expected = CONTROL_DIR / f"codex-{int(binding['binding_id'])}.sock"
        if path != expected:
            raise ValueError("Codex control endpoint does not match its binding socket")
        return endpoint, path

    def status(self, binding: dict) -> str:
        if not binding.get("native_session_id"):
            return "starting"
        try:
            endpoint, path = self._endpoint(binding)
        except ValueError:
            return "error"
        if not self.endpoint_available(path):
            return "dormant"
        if not _capabilities(binding).get("active_delivery"):
            return "error"
        try:
            with self.client_factory(endpoint) as client:
                state = _thread_status(client.request(
                    "thread/read", {"threadId": binding["native_session_id"],
                                    "includeTurns": False}
                ))
        except (FileNotFoundError, ConnectionRefusedError):
            return "dormant"
        except (OSError, CodexProtocolError, CodexRpcError, TimeoutError):
            return "error"
        if state in ("idle", "notLoaded"):
            return "idle"
        if state == "active":
            return "active"
        return "error"

    def deliver(self, binding: dict, prompt: str) -> None:
        if not _capabilities(binding).get("active_delivery"):
            raise RuntimeError("Codex active delivery is unsupported by this CLI version")
        endpoint, _path = self._endpoint(binding)
        thread_id = str(binding["native_session_id"])
        settings = _settings(binding)
        with self.client_factory(endpoint) as client:
            current = client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            state = _thread_status(current)
            if state == "active":
                raise common_session_control.ProviderBusy(  # type: ignore[attr-defined]
                    "Codex thread became active before wake delivery"
                )
            if state == "notLoaded":
                params = {"threadId": thread_id, "cwd": settings.get("cwd")}
                if settings.get("model"):
                    params["model"] = settings["model"]
                if settings.get("sandbox"):
                    params["sandbox"] = settings["sandbox"]
                if settings.get("approval_policy"):
                    params["approvalPolicy"] = settings["approval_policy"]
                client.request("thread/resume", params)
            elif state != "idle":
                raise RuntimeError(f"Codex thread is not deliverable ({state or 'unknown'})")
            result = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            })
            turn = result.get("turn") or {}
            turn_id = turn.get("id")
            if not turn_id:
                raise CodexProtocolError("Codex turn/start returned no turn ID")
            completed = client.wait_notification(
                "turn/completed",
                lambda message: (message.get("params") or {}).get("turn", {}).get("id") == turn_id,
            )
            final = (completed.get("params") or {}).get("turn") or {}
            if final.get("status") != "completed":
                raise RuntimeError(f"Codex wake turn ended {final.get('status') or 'unknown'}")

    def resume(self, binding: dict, prompt: str) -> None:
        if not _capabilities(binding).get("resume"):
            raise RuntimeError("Codex CLI resume capability is unavailable")
        if not self.resume_probe().get("resume"):
            raise RuntimeError("installed Codex CLI failed the resume capability probe")
        settings = _settings(binding)
        cwd = Path(settings.get("cwd") or session_supervisor.expected_worktree(
            REPO_ROOT, binding.get("shortname"), binding.get("flavor")
        ))
        self.resume_runner(binding, resume_command(binding, prompt), cwd)


def create_adapter() -> CodexAdapter:
    return CodexAdapter()
