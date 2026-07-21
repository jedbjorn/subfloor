#!/usr/bin/env python3
"""Own one Codex app-server and attach the interactive TUI to its thread."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
ENGINE = ADAPTER_DIR.parents[1]
DB_PATH = ENGINE / "shell_db.db"
RUN_DIR = ENGINE / "run" / "session-control"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # type: ignore[import-not-found]  # noqa: E402
import session_supervisor  # type: ignore[import-not-found]  # noqa: E402
from codex_rpc import AppServerClient, probe_codex  # noqa: E402


def socket_path(binding_id: int) -> Path:
    return RUN_DIR / f"codex-{binding_id}.sock"


def cleanup_socket(path: Path, *, root: Path | None = None) -> None:
    root = root or RUN_DIR
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("refusing to clean a Codex socket outside the runtime dir") from exc
    path.unlink(missing_ok=True)


def _thread_params(model: str | None, cwd: Path, sandboxed: bool) -> dict:
    params: dict = {"cwd": str(cwd)}
    if model:
        params["model"] = model
    if sandboxed:
        params.update(sandbox="danger-full-access", approvalPolicy="never")
    return params


def main() -> int:
    try:
        binding_id = int(os.environ["SC_SESSION_BINDING_ID"])
    except (KeyError, ValueError):
        raise SystemExit("Codex controlled launch requires SC_SESSION_BINDING_ID")
    model = os.environ.get("SC_SESSION_MODEL") or None
    sandboxed = bool(os.environ.get("SC_SANDBOX"))
    cwd = Path.cwd().resolve()
    probe = probe_codex()
    if not probe["create"]:
        raise SystemExit(
            f"Codex {probe.get('cli_version') or 'unknown'} is not validated for "
            f"app-server session control (expected 0.144.x)"
        )

    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUN_DIR.chmod(0o700)
    path = socket_path(binding_id)
    cleanup_socket(path)
    endpoint = f"unix://{path}"
    log_path = RUN_DIR / f"codex-{binding_id}.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    log = os.fdopen(log_fd, "a")
    server: subprocess.Popen | None = None
    tui: subprocess.Popen | None = None

    def stop(signum, _frame) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, stop)

    try:
        server = subprocess.Popen(
            ["codex", "--dangerously-bypass-hook-trust", "app-server",
             "--listen", endpoint],
            cwd=str(cwd), stdout=log, stderr=log,
        )
        deadline = time.monotonic() + 10
        client = None
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"Codex app-server exited {server.returncode}")
            candidate = AppServerClient(endpoint)
            try:
                candidate.__enter__()
                client = candidate
                break
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
                candidate.__exit__(None, None, None)
                time.sleep(0.05)
        if client is None:
            raise RuntimeError("Codex app-server socket did not become ready")
        try:
            con = db_driver.connect(DB_PATH)
            try:
                binding = con.execute(
                    "SELECT native_session_id FROM shell_session_bindings "
                    "WHERE binding_id=?", (binding_id,),
                ).fetchone()
            finally:
                con.close()
            if not binding:
                raise RuntimeError(f"unknown Codex session binding {binding_id}")
            config_result = client.request(
                "config/read", {"cwd": str(cwd), "includeLayers": False}
            )
            config = config_result.get("config") or {}
            effective_model = model or config.get("model")
            effective_sandbox = (
                "danger-full-access" if sandboxed else config.get("sandbox_mode")
            )
            effective_approval = "never" if sandboxed else config.get("approval_policy")
            effective_effort = config.get("model_reasoning_effort")
            params = _thread_params(effective_model, cwd, sandboxed)
            if not sandboxed and effective_sandbox:
                params["sandbox"] = effective_sandbox
            if not sandboxed and effective_approval:
                params["approvalPolicy"] = effective_approval
            native_id = binding["native_session_id"]
            if native_id:
                result = client.request(
                    "thread/resume", {"threadId": native_id, **params}
                )
            else:
                result = client.request("thread/start", params)
            thread_id = (result.get("thread") or {}).get("id")
            if not thread_id:
                raise RuntimeError("Codex app-server did not return a thread ID")
        finally:
            client.__exit__(None, None, None)

        capabilities = {
            **probe,
            "settings": {
                "model": effective_model,
                "cwd": str(cwd),
                "sandbox": effective_sandbox,
                "approval_policy": effective_approval,
                "effort": effective_effort,
            },
        }
        con = db_driver.connect(DB_PATH)
        try:
            session_supervisor.register_native_session(
                con, binding_id, str(thread_id), control_endpoint=endpoint,
                capabilities=json.dumps(capabilities, sort_keys=True),
                cli_version=probe.get("cli_version"),
            )
        finally:
            con.close()

        command = ["codex", "--dangerously-bypass-hook-trust", "--remote", endpoint]
        if sandboxed:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(["resume", str(thread_id)])
        tui = subprocess.Popen(command, cwd=str(cwd))
        return tui.wait()
    finally:
        for process in (tui, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        log.close()
        cleanup_socket(path)


if __name__ == "__main__":
    raise SystemExit(main())
