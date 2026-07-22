#!/usr/bin/env python3
"""Launch one Claude planner with an engine-supplied native session UUID."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Callable

ADAPTER_DIR = Path(__file__).resolve().parent
ENGINE = ADAPTER_DIR.parents[1]
DB_PATH = ENGINE / "shell_db.db"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # type: ignore[import-not-found]  # noqa: E402
import session_control as common_session_control  # type: ignore[import-not-found]  # noqa: E402
import session_supervisor  # type: ignore[import-not-found]  # noqa: E402
from claude_cli import probe_claude  # noqa: E402


def _capabilities(value: object) -> dict:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def launch_plan(
    binding: dict,
    probe: dict,
    *,
    cwd: Path,
    env: dict[str, str],
    native_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> tuple[str, list[str], dict]:
    """Return the supplied/resumed ID, exact argv, and recorded capabilities."""
    stored = _capabilities(binding.get("control_capabilities"))
    value = stored.get("settings") or {}
    settings = dict(value) if isinstance(value, dict) else {}
    model = settings.get("model") or env.get("SC_SESSION_MODEL") or None
    effort = settings.get("effort") or env.get("SC_SESSION_EFFORT") or None
    permission = settings.get("permission_mode")
    if not permission:
        permission = "bypassPermissions" if env.get("SC_SANDBOX") else "auto"
    settings = {
        "model": model,
        "cwd": str(cwd),
        "effort": effort,
        "permission_mode": permission,
    }
    common_session_control.validate_managed_wake_posture(  # type: ignore[attr-defined]
        {"settings": settings}
    )

    existing = binding.get("native_session_id")
    native_id = str(existing or native_id_factory())
    command = ["claude"]
    command.extend(["--resume", native_id] if existing else ["--session-id", native_id])
    if binding.get("display_name"):
        command.extend(["--name", str(binding["display_name"])])
    if model:
        command.extend(["--model", str(model)])
    if effort:
        command.extend(["--effort", str(effort)])
    if permission == "bypassPermissions":
        command.append("--dangerously-skip-permissions")
    else:
        command.extend(["--permission-mode", str(permission)])

    capabilities = {
        **stored,
        **probe,
        "provider": "claude",
        "transport": "background-task-inbox-v1",
        "normal_steer": False,
        "settings": settings,
    }
    return native_id, command, capabilities


def main() -> int:
    try:
        binding_id = int(os.environ["SC_SESSION_BINDING_ID"])
    except (KeyError, ValueError):
        raise SystemExit("Claude controlled launch requires SC_SESSION_BINDING_ID")

    probe = probe_claude()
    if not probe.get("create"):
        raise SystemExit(
            f"Claude {probe.get('cli_version') or 'unknown'} is not validated for "
            "managed inbox-watcher session control"
        )

    con = db_driver.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT b.*, s.display_name FROM shell_session_bindings b "
            "JOIN shells s ON s.shell_id=b.shell_id WHERE b.binding_id=?",
            (binding_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"unknown Claude session binding {binding_id}")

    native_id, command, capabilities = launch_plan(
        dict(row), probe, cwd=Path.cwd().resolve(), env=dict(os.environ)
    )
    con = db_driver.connect(DB_PATH)
    try:
        session_supervisor.register_native_session(
            con,
            binding_id,
            native_id,
            capabilities=json.dumps(capabilities, sort_keys=True),
            cli_version=probe.get("cli_version"),
        )
    finally:
        con.close()

    env = dict(os.environ)
    env["SC_SESSION_ACTIVE_CHANNEL"] = "claude-inbox"
    os.execvpe(command[0], command, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
