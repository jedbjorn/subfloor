#!/usr/bin/env python3
"""Authenticated Kimi session-server client and capability probe."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO


# Validated version floor — newer CLIs are accepted on feature-probe evidence
# (the command-tree detection below), older ones fail closed. Support-latest
# policy: a version bump alone never disables session control.
MIN_TESTED_SERVER_VERSION = (0, 27)
DEFAULT_TURN_TIMEOUT = 4 * 60 * 60
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:token|authorization)\s*[=:]\s*)[^\s,;]+"),
)


def _sanitize(value: object) -> str:
    text = " ".join(str(value).replace("\x00", "").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:500] or "request failed"


class KimiProtocolError(RuntimeError):
    """The local server returned a response outside its documented contract."""


class KimiApiError(RuntimeError):
    """The local server returned a structured non-zero API result."""

    def __init__(self, code: int | None, message: str):
        self.code = code
        super().__init__(
            f"Kimi API error {code if code is not None else 'unknown'}: {_sanitize(message)}"
        )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=5, check=False
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def probe_kimi(
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
) -> dict:
    """Probe real CLI surfaces; unknown versions fail closed for live delivery."""
    try:
        version_result = runner(["kimi", "--version"])
        root_result = runner(["kimi", "--help"])
        server_result = runner(["kimi", "server", "run", "--help"])
        web_result = runner(["kimi", "web", "--help"])
        acp_result = runner(["kimi", "acp", "--help"])
    except (OSError, subprocess.SubprocessError):
        return {
            "cli_version": None,
            "create": False,
            "deliver": False,
            "resume": False,
            "active_delivery": False,
            "acp": False,
            "normal_steer": False,
            "server_command": None,
        }

    version_text = _output(version_result).strip()
    version = _version_tuple(version_text)
    known_server = bool(version and version[:2] >= MIN_TESTED_SERVER_VERSION)
    root_help = _output(root_result)
    server_help = _output(server_result)
    web_help = _output(web_result)
    acp_help = _output(acp_result)

    command: list[str] | None = None
    if known_server and "--foreground" in web_help and "--no-open" in web_help:
        command = ["kimi", "web", "--port", "0", "--foreground", "--no-open"]
    elif known_server and "--foreground" in server_help:
        command = ["kimi", "server", "run", "--port", "0", "--foreground"]

    resume = (
        version_result.returncode == 0
        and root_result.returncode == 0
        and "--session" in root_help
        and "--prompt" in root_help
        and "--model" in root_help
    )
    active = command is not None
    return {
        "cli_version": ".".join(map(str, version)) if version else None,
        "create": active,
        "deliver": active,
        "resume": resume,
        "active_delivery": active,
        "acp": acp_result.returncode == 0 and "ACP" in acp_help,
        "normal_steer": False,
        "server_command": command,
    }


class KimiClient:
    """Small blocking client for the stable ``/api/v1`` Kimi web contract."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        opener: Callable[..., IO[bytes]] = urllib.request.urlopen,
        timeout: float = 10,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.opener = opener
        self.timeout = timeout
        self.sleeper = sleeper
        self.clock = clock

    def request(self, method: str, path: str, body: dict | None = None) -> object:
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.endpoint + "/api/v1/" + path.lstrip("/"),
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                envelope = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise KimiApiError(exc.code, exc.reason or "HTTP request failed") from exc
        except json.JSONDecodeError as exc:
            raise KimiProtocolError("Kimi server returned invalid JSON") from exc
        if not isinstance(envelope, dict):
            raise KimiProtocolError("Kimi server returned a non-object envelope")
        code = envelope.get("code")
        if code != 0:
            message = str(envelope.get("msg") or "request failed")
            raise KimiApiError(code if isinstance(code, int) else None, message)
        if "data" not in envelope:
            raise KimiProtocolError("Kimi server response omitted data")
        return envelope["data"]

    @staticmethod
    def _object(value: object, operation: str) -> dict:
        if not isinstance(value, dict):
            raise KimiProtocolError(f"Kimi {operation} returned non-object data")
        return value

    def create_session(self, cwd: Path) -> dict:
        return self._object(
            self.request("POST", "sessions", {"metadata": {"cwd": str(cwd)}}),
            "session create",
        )

    def get_session(self, session_id: str) -> dict:
        return self._object(self.request("GET", f"sessions/{session_id}"), "session read")

    def update_profile(self, session_id: str, agent_config: dict) -> dict:
        return self._object(
            self.request(
                "POST",
                f"sessions/{session_id}/profile",
                {"agent_config": agent_config},
            ),
            "profile update",
        )

    def get_status(self, session_id: str) -> dict:
        return self._object(
            self.request("GET", f"sessions/{session_id}/status"), "status read"
        )

    def submit_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str,
        effort: str | None,
        permission_mode: str,
    ) -> str:
        body: dict[str, object] = {
            "content": [{"type": "text", "text": prompt}],
            "model": model,
            "permission_mode": permission_mode,
        }
        if effort:
            body["thinking"] = effort
        data = self._object(
            self.request(
                "POST",
                f"sessions/{session_id}/prompts",
                body,
            ),
            "prompt submit",
        )
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise KimiProtocolError("Kimi prompt response omitted prompt_id")
        return prompt_id

    def list_prompts(self, session_id: str) -> dict:
        return self._object(
            self.request("GET", f"sessions/{session_id}/prompts"), "prompt list"
        )

    def wait_prompt(
        self, session_id: str, prompt_id: str, *, timeout: float = DEFAULT_TURN_TIMEOUT
    ) -> None:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            prompts = self.list_prompts(session_id)
            active = prompts.get("active")
            queued = prompts.get("queued") or []
            active_id = active.get("prompt_id") if isinstance(active, dict) else None
            queued_ids = {
                item.get("prompt_id")
                for item in queued
                if isinstance(item, dict)
            }
            session = self.get_session(session_id)
            pending = session.get("pending_interaction", "none")
            if pending != "none":
                raise RuntimeError(
                    f"Kimi managed turn requires interactive {pending}; refusing to wedge"
                )
            if active_id != prompt_id and prompt_id not in queued_ids:
                if session.get("busy") or session.get("main_turn_active"):
                    self.sleeper(0.2)
                    continue
                if session.get("last_turn_reason") == "failed":
                    raise RuntimeError("Kimi managed turn failed")
                return
            self.sleeper(0.2)
        raise TimeoutError("Kimi managed turn did not become idle before timeout")

    def deliver(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str,
        effort: str | None,
        permission_mode: str,
    ) -> None:
        prompt_id = self.submit_prompt(
            session_id,
            prompt,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
        )
        self.wait_prompt(session_id, prompt_id)


def read_token(path: Path) -> str:
    try:
        mode = path.stat().st_mode & 0o777
        token = path.read_text().strip()
    except OSError as exc:
        raise RuntimeError("Kimi session-control token is unavailable") from exc
    if mode & 0o077:
        raise RuntimeError("Kimi session-control token file permissions are unsafe")
    if not token:
        raise RuntimeError("Kimi session-control token is empty")
    return token
