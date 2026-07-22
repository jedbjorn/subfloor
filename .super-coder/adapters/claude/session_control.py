#!/usr/bin/env python3
"""Claude inbox-watcher and dormant-resume session-control transport."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
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
from claude_cli import probe_claude  # noqa: E402


CHANNEL_HEARTBEAT_MAX_AGE = 90
ACK_TIMEOUT = 4 * 60 * 60
ACK_POLL_INTERVAL = 1.5
BINDING_RECHECK_INTERVAL = 5.0


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


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def active_channel_ready(binding: dict, *, now: datetime | None = None) -> bool:
    if binding.get("active_channel_pid") is None:
        return False
    if binding.get("active_channel_start_ticks") is None:
        return False
    heartbeat = _timestamp(binding.get("active_channel_heartbeat_at"))
    if heartbeat is None:
        return False
    now = now or datetime.now(timezone.utc)
    age = (now - heartbeat).total_seconds()
    return -5 <= age <= CHANNEL_HEARTBEAT_MAX_AGE


def resume_command(binding: dict, prompt: str) -> list[str]:
    native_id = binding.get("native_session_id")
    if not native_id:
        raise RuntimeError("Claude native session ID is unavailable")
    settings = _settings(binding)
    command = ["claude", "--resume", str(native_id)]
    model = settings.get("model") or binding.get("archive_model")
    if model:
        command.extend(["--model", str(model)])
    if settings.get("effort"):
        command.extend(["--effort", str(settings["effort"])])
    permission = settings.get("permission_mode")
    if permission == "bypassPermissions":
        command.append("--dangerously-skip-permissions")
    elif permission:
        command.extend(["--permission-mode", str(permission)])
    command.extend(["-p", prompt])
    return command


def resume_environment(binding: dict) -> dict[str, str]:
    env = dict(os.environ)
    if _settings(binding).get("permission_mode") == "bypassPermissions":
        env["IS_SANDBOX"] = "1"
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
            raise RuntimeError("Claude resume vanished while recording its lease")
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
                error=f"Claude resume exited {returncode}" if returncode else None,
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
        raise RuntimeError(f"Claude resume exited {rc}")


def _running_unread(binding: dict) -> int:
    con = db_driver.connect(DB_PATH)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM session_wake_jobs j "
                "JOIN shell_messages m ON m.message_id=j.trigger_message_id "
                "WHERE j.binding_id=? AND j.state='running' AND m.read_at IS NULL",
                (binding["binding_id"],),
            ).fetchone()[0]
        )
    finally:
        con.close()


def _current_delivery_binding(binding: dict) -> dict:
    con = db_driver.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT lease_pid, lease_start_ticks, active_channel_pid, "
            "active_channel_start_ticks, active_channel_heartbeat_at "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("Claude session binding disappeared during delivery")
        return dict(row)
    finally:
        con.close()


def _delivery_owner_lost(binding: dict, *, now: datetime) -> bool:
    lease_vacant = (
        binding.get("lease_pid") is None
        or binding.get("lease_start_ticks") is None
    )
    return lease_vacant and not active_channel_ready(binding, now=now)


def _wait_for_ack(
    binding: dict,
    *,
    unread: Callable[[dict], int] = _running_unread,
    binding_reader: Callable[[dict], dict] = _current_delivery_binding,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    started = clock()
    deadline = started + ACK_TIMEOUT
    next_binding_check = started + BINDING_RECHECK_INTERVAL
    while unread(binding):
        current = clock()
        if current >= deadline:
            raise TimeoutError("Claude inbox wake was not acknowledged before timeout")
        if current >= next_binding_check:
            current_binding = binding_reader(binding)
            if _delivery_owner_lost(current_binding, now=now()):
                raise RuntimeError(
                    "Claude delivery owner and inbox watcher disappeared before "
                    "acknowledgement"
                )
            next_binding_check = current + BINDING_RECHECK_INTERVAL
        sleeper(ACK_POLL_INTERVAL)


class ClaudeAdapter:
    def __init__(
        self,
        *,
        ack_waiter: Callable[[dict], None] = _wait_for_ack,
        unread: Callable[[dict], int] = _running_unread,
        resume_runner: Callable[[dict, list[str], Path], None] = _run_fenced_resume,
        resume_probe: Callable[[], dict] = probe_claude,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.ack_waiter = ack_waiter
        self.unread = unread
        self.resume_runner = resume_runner
        self.resume_probe = resume_probe
        self.now = now

    def status(self, binding: dict) -> str:
        if not binding.get("native_session_id"):
            return "starting"
        if binding.get("lease_pid") is None or binding.get("lease_start_ticks") is None:
            return "dormant"
        if not _capabilities(binding).get("active_delivery"):
            return "error"
        if active_channel_ready(binding, now=self.now()):
            return "idle"
        return "active"

    def deliver(self, binding: dict, _prompt: str) -> None:
        capabilities = _capabilities(binding)
        common_session_control.validate_managed_wake_posture(  # type: ignore[attr-defined]
            capabilities
        )
        if not capabilities.get("active_delivery"):
            raise RuntimeError("Claude inbox-watcher delivery is unsupported")
        if not self.unread(binding):
            return
        if not active_channel_ready(binding, now=self.now()):
            raise common_session_control.ProviderBusy(  # type: ignore[attr-defined]
                "Claude inbox watcher fired before dispatcher delivery"
            )
        self.ack_waiter(binding)

    def resume(self, binding: dict, prompt: str) -> None:
        capabilities = _capabilities(binding)
        common_session_control.validate_managed_wake_posture(  # type: ignore[attr-defined]
            capabilities
        )
        if not capabilities.get("resume"):
            raise RuntimeError("Claude CLI resume capability is unavailable")
        if not self.resume_probe().get("resume"):
            raise RuntimeError(
                "installed Claude CLI failed the resume capability probe"
            )
        settings = _settings(binding)
        cwd = Path(
            settings.get("cwd")
            or session_supervisor.expected_worktree(
                REPO_ROOT, binding.get("shortname"), binding.get("flavor")
            )
        )
        self.resume_runner(binding, resume_command(binding, prompt), cwd)


def create_adapter() -> ClaudeAdapter:
    return ClaudeAdapter()
