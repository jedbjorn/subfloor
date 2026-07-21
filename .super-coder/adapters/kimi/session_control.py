#!/usr/bin/env python3
"""Kimi K3 transport for provider-neutral managed session control."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

ADAPTER_DIR = Path(__file__).resolve().parent
ENGINE = ADAPTER_DIR.parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
CONTROL_DIR = ENGINE / "run" / "session-control"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # type: ignore[import-not-found]  # noqa: E402
import session_control as common_session_control  # type: ignore[import-not-found]  # noqa: E402
import session_supervisor  # type: ignore[import-not-found]  # noqa: E402
from kimi_http import KimiApiError, KimiClient, probe_kimi, read_token  # noqa: E402


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


def token_path(binding_id: int) -> Path:
    return CONTROL_DIR / f"kimi-{binding_id}.token"


def _validated_endpoint(binding: dict) -> tuple[str, Path]:
    endpoint = str(binding.get("control_endpoint") or "")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.port is None
    ):
        raise ValueError("Kimi control endpoint must be an authenticated loopback URL")
    return endpoint.rstrip("/"), token_path(int(binding["binding_id"]))


def resume_command(binding: dict, prompt: str) -> list[str]:
    native_id = binding.get("native_session_id")
    if not native_id:
        raise RuntimeError("Kimi native session ID is unavailable")
    settings = _settings(binding)
    model = settings.get("model") or binding.get("archive_model")
    if model != "kimi-code/k3":
        raise RuntimeError("Kimi managed resume requires pinned kimi-code/k3 route")
    command = ["kimi", "--session", str(native_id), "--model", str(model)]
    command.extend(["--prompt", prompt])
    return command


def resume_environment(binding: dict, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    effort = _settings(binding).get("effort")
    if effort:
        env["KIMI_MODEL_THINKING_EFFORT"] = str(effort)
    return env


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
                con,
                binding["binding_id"],
                pid,
                repo_root=REPO_ROOT,
                state="dispatching",
            )
        finally:
            con.close()
        identity = session_supervisor.read_process(pid)
        if not identity:
            raise RuntimeError("Kimi resume vanished while recording its lease")
        lease.update(pid=pid, start_ticks=identity.start_ticks, generation=generation)

    def exited(_pid: int, returncode: int) -> None:
        if not lease:
            return
        con = db_driver.connect(DB_PATH)
        try:
            session_supervisor.release_lease(
                con,
                binding["binding_id"],
                lease["pid"],
                lease["start_ticks"],
                lease["generation"],
                error=f"Kimi resume exited {returncode}" if returncode else None,
            )
        finally:
            con.close()

    rc = session_supervisor.supervise(
        command,
        cwd=cwd,
        env=resume_environment(binding),
        on_pre_spawn=preflight,
        on_started=started,
        on_exited=exited,
    )
    if rc:
        raise RuntimeError(f"Kimi resume exited {rc}")


class KimiAdapter:
    def __init__(
        self,
        *,
        client_factory: Callable[[str, str], KimiClient] = KimiClient,
        resume_runner: Callable[[dict, list[str], Path], None] = _run_fenced_resume,
        resume_probe: Callable[[], dict] = probe_kimi,
    ):
        self.client_factory = client_factory
        self.resume_runner = resume_runner
        self.resume_probe = resume_probe

    def _client(self, binding: dict) -> KimiClient:
        endpoint, path = _validated_endpoint(binding)
        return self.client_factory(endpoint, read_token(path))

    def status(self, binding: dict) -> str:
        if not binding.get("native_session_id"):
            return "starting"
        try:
            client = self._client(binding)
            session = client.get_session(str(binding["native_session_id"]))
        except (ConnectionRefusedError, FileNotFoundError, urllib.error.URLError):
            return "dormant"
        except (KimiApiError, OSError, RuntimeError, ValueError):
            return "error"
        if session.get("pending_interaction", "none") != "none":
            return "error"
        if session.get("busy") or session.get("main_turn_active"):
            return "active"
        return "idle"

    def deliver(self, binding: dict, prompt: str) -> None:
        capabilities = _capabilities(binding)
        common_session_control.validate_managed_wake_posture(  # type: ignore[attr-defined]
            capabilities
        )
        if not capabilities.get("active_delivery"):
            raise RuntimeError("Kimi active delivery is unsupported by this CLI version")
        client = self._client(binding)
        session_id = str(binding["native_session_id"])
        session = client.get_session(session_id)
        if session.get("busy") or session.get("main_turn_active"):
            raise common_session_control.ProviderBusy(  # type: ignore[attr-defined]
                "Kimi session became active before wake delivery"
            )
        pending = session.get("pending_interaction", "none")
        if pending != "none":
            raise RuntimeError(f"Kimi session is waiting for interactive {pending}")
        settings = _settings(binding)
        client.deliver(
            session_id,
            prompt,
            model=str(settings.get("model") or binding.get("archive_model") or ""),
            effort=str(settings["effort"]) if settings.get("effort") else None,
            permission_mode=str(settings.get("permission_mode") or ""),
        )

    def resume(self, binding: dict, prompt: str) -> None:
        common_session_control.validate_managed_wake_posture(  # type: ignore[attr-defined]
            _capabilities(binding)
        )
        if not _capabilities(binding).get("resume"):
            raise RuntimeError("Kimi CLI resume capability is unavailable")
        if not self.resume_probe().get("resume"):
            raise RuntimeError("installed Kimi CLI failed the resume capability probe")
        settings = _settings(binding)
        cwd = Path(
            settings.get("cwd")
            or session_supervisor.expected_worktree(
                REPO_ROOT, binding.get("shortname"), binding.get("flavor")
            )
        )
        self.resume_runner(binding, resume_command(binding, prompt), cwd)


def create_adapter() -> KimiAdapter:
    return KimiAdapter()
