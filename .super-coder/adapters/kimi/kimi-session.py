#!/usr/bin/env python3
"""Own one authenticated Kimi web server and its managed planner session."""
from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

ADAPTER_DIR = Path(__file__).resolve().parent
ENGINE = ADAPTER_DIR.parents[1]
DB_PATH = ENGINE / "shell_db.db"
RUN_DIR = ENGINE / "run" / "session-control"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # type: ignore[import-not-found]  # noqa: E402
import session_supervisor  # type: ignore[import-not-found]  # noqa: E402
from kimi_http import KimiClient, probe_kimi  # noqa: E402


_URL_PATTERN = re.compile(r"(http://(?:127\.0\.0\.1|localhost):\d+)/#token=([^\s]+)")
_TOKEN_PATTERN = re.compile(r"(?i)(token(?:=|:\s*))[^\s]+")


def _capabilities(value: object) -> dict:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def token_path(binding_id: int) -> Path:
    return RUN_DIR / f"kimi-{binding_id}.token"


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
        handle.write("\n")
    path.chmod(0o600)


def wait_for_server(
    process: subprocess.Popen[str], log: TextIO, *, timeout: float = 15
) -> tuple[str, str]:
    """Read the provider banner without persisting or echoing its bearer token."""
    if process.stdout is None:
        raise RuntimeError("Kimi server output is unavailable")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Kimi session server exited {process.returncode}")
        ready, _, _ = select.select([process.stdout], [], [], 0.1)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            continue
        match = _URL_PATTERN.search(line)
        log.write(_TOKEN_PATTERN.sub(r"\1[REDACTED]", line))
        log.flush()
        if match:
            return match.group(1), match.group(2)
    raise RuntimeError("Kimi session server did not become ready")


def drain_server_output(stream: TextIO, log: TextIO) -> None:
    for line in stream:
        log.write(_TOKEN_PATTERN.sub(r"\1[REDACTED]", line))
        log.flush()


def configure_session(
    client: KimiClient,
    binding: dict,
    *,
    cwd: Path,
    model: str,
    effort: str | None,
) -> tuple[str, dict]:
    native_id = binding.get("native_session_id")
    if native_id:
        session_id = str(native_id)
        client.get_session(session_id)
    else:
        created = client.create_session(cwd)
        session_id = str(created.get("id") or "")
        if not session_id:
            raise RuntimeError("Kimi session create returned no native ID")

    agent_config = {"model": model, "permission_mode": "auto"}
    if effort:
        agent_config["thinking"] = effort
    client.update_profile(session_id, agent_config)
    status = client.get_status(session_id)
    effective_model = status.get("model")
    effective_effort = status.get("thinking_level")
    permission = status.get("permission")
    if effective_model != model:
        raise RuntimeError(
            f"Kimi route drifted at launch: requested {model}, got {effective_model or 'none'}"
        )
    if effort and effective_effort != effort:
        raise RuntimeError(
            f"Kimi effort drifted at launch: requested {effort}, got {effective_effort or 'none'}"
        )
    if permission != "auto":
        raise RuntimeError(
            f"Kimi managed session is not approval-safe (permission={permission or 'none'})"
        )
    return session_id, {
        "model": effective_model,
        "cwd": str(cwd),
        "effort": effective_effort,
        "permission_mode": permission,
    }


def main() -> int:
    try:
        binding_id = int(os.environ["SC_SESSION_BINDING_ID"])
    except (KeyError, ValueError):
        raise SystemExit("Kimi controlled launch requires SC_SESSION_BINDING_ID")

    probe = probe_kimi()
    command = probe.get("server_command")
    if not probe.get("create") or not isinstance(command, list):
        raise SystemExit(
            f"Kimi {probe.get('cli_version') or 'unknown'} is not validated for "
            "authenticated session-server control"
        )

    cwd = Path.cwd().resolve()
    con = db_driver.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT * FROM shell_session_bindings WHERE binding_id=?", (binding_id,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"unknown Kimi session binding {binding_id}")
    binding = dict(row)
    stored_settings = _capabilities(binding.get("control_capabilities")).get("settings")
    stored_settings = stored_settings if isinstance(stored_settings, dict) else {}
    model = stored_settings.get("model") or os.environ.get("SC_SESSION_MODEL")
    if model != "kimi-code/k3":
        raise SystemExit("Kimi managed planners require the pinned kimi-code/k3 route")
    effort = (
        stored_settings.get("effort")
        or os.environ.get("SC_SESSION_EFFORT")
        or os.environ.get("KIMI_MODEL_THINKING_EFFORT")
        or None
    )

    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = RUN_DIR / f"kimi-{binding_id}.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    log = os.fdopen(log_fd, "a")
    server: subprocess.Popen[str] | None = None
    drain: threading.Thread | None = None

    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, stop)

    try:
        server = subprocess.Popen(
            [str(value) for value in command],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        endpoint, token = wait_for_server(server, log)
        if server.stdout is not None:
            drain = threading.Thread(
                target=drain_server_output,
                args=(server.stdout, log),
                name=f"kimi-{binding_id}-log-drain",
                daemon=True,
            )
            drain.start()
        runtime_token = token_path(binding_id)
        write_private(runtime_token, token)
        client = KimiClient(endpoint, token)
        session_id, settings = configure_session(
            client, binding, cwd=cwd, model=str(model), effort=effort
        )
        capabilities = {
            **probe,
            "token_file": str(runtime_token),
            "settings": settings,
        }
        con = db_driver.connect(DB_PATH)
        try:
            session_supervisor.register_native_session(
                con,
                binding_id,
                session_id,
                control_endpoint=endpoint,
                capabilities=json.dumps(capabilities, sort_keys=True),
                cli_version=probe.get("cli_version"),
            )
        finally:
            con.close()

        url = f"{endpoint}/sessions/{quote(session_id, safe='')}#token={token}"
        print(f"→ Kimi managed web session: {url}", flush=True)
        webbrowser.open(url)
        return server.wait()
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if drain is not None:
            drain.join(timeout=1)
        token_path(binding_id).unlink(missing_ok=True)
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
