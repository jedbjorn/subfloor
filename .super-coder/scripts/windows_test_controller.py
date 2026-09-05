#!/usr/bin/env python3
"""Fixed-target controller for the disposable W10C-Testing guest.

The controller runs on Halo.  A caller either starts ``serve`` locally or
reaches the same command through an SSH ForceCommand.  Requests are one JSON
line followed by an optional exact-length byte stream; responses use the same
framing.  No request can select a libvirt connection, domain, host command, or
host path.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, cast

SCHEMA_VERSION = 1
LIBVIRT_URI = "qemu:///system"
DOMAIN = "W10C-Testing"
DOMAIN_UUID = "0b314d1a-bd03-47b9-8155-01a6d470f7a9"
CONFIG_PATH = Path.home() / ".config" / "subfloor" / "windows-test-controller.json"
CLIENT_PATH = Path(".sc-state/local/windows-test-client.json")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_HEADER_BYTES = 64 * 1024
MAX_TIMEOUT = 4 * 60 * 60
DEFAULT_STOP_TIMEOUT = 120
CHUNK_SIZE = 1024 * 1024


class ControllerError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _error(operation: str, exc: ControllerError) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "operation": operation,
        "result": None,
        "error": {
            "code": exc.code,
            "message": str(exc)[:500],
            "details": exc.details,
        },
    }


def _success(operation: str, result: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "operation": operation,
        "result": result,
        "error": None,
    }


def _result_error(operation: str, code: str, message: str, result: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "operation": operation,
        "result": result,
        "error": {"code": code, "message": message[:500], "details": {}},
    }


def _require_owner_file(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ControllerError(
            "configuration_unavailable", f"{label} is unavailable"
        ) from exc
    if mode & 0o077:
        raise ControllerError(
            "configuration_permissions",
            f"{label} must not be readable or writable by group or other users",
        )


def _require_owner_directory(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ControllerError(
            "configuration_unavailable", f"{label} is unavailable"
        ) from exc
    if not path.is_dir() or mode & 0o077:
        raise ControllerError(
            "configuration_permissions",
            f"{label} must be a private directory",
        )


def _read_json(path: Path, label: str, *, required: bool = True) -> dict:
    if not path.exists() and not required:
        return {}
    _require_owner_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControllerError(
            "configuration_invalid", f"{label} is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ControllerError(
            "configuration_invalid", f"{label} must contain a JSON object"
        )
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _validated_timeout(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ControllerError(
            "timeout_invalid", "timeout must be an integer number of seconds"
        )
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ControllerError(
            "timeout_invalid", "timeout must be an integer number of seconds"
        ) from exc
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise ControllerError(
            "timeout_invalid", f"timeout must be between 1 and {MAX_TIMEOUT} seconds"
        )
    return timeout


def _snapshot_name(value: object, *, allow_working: bool = False) -> str:
    name = str(value or "")
    if allow_working and name == "working":
        return name
    if not NAME_RE.fullmatch(name):
        raise ControllerError(
            "snapshot_name_invalid",
            "snapshot name must use 1-64 letters, digits, dot, underscore, or dash",
        )
    return name


def _guest_relative(value: object) -> str:
    raw = str(value or "")
    path = PureWindowsPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.drive
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ControllerError(
            "guest_path_invalid",
            "guest path must be a relative path inside the configured workspace",
        )
    return str(path)


def _ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class PullPlan:
    operation: str
    command: list[str]
    size: int
    timeout: int
    lock_fd: int


class Controller:
    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config_path = config_path
        self.state_path = config_path.with_name("windows-test-controller-state.json")
        self.lock_path = config_path.with_name("windows-test-controller.lock")
        self.run = run
        self.popen = popen
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.config = self._load_config()

    def _load_config(self) -> dict:
        _require_owner_directory(
            self.config_path.parent, "controller configuration directory"
        )
        cfg = _read_json(self.config_path, "controller configuration")
        guest = cfg.get("guest")
        required = ("host", "user", "key_path", "workspace")
        if not isinstance(guest, dict) or any(
            not str(guest.get(k, "")).strip() for k in required
        ):
            raise ControllerError(
                "configuration_invalid",
                "controller configuration requires guest host, user, key_path, and workspace",
            )
        recovery = _snapshot_name(cfg.get("recovery_snapshot"))
        initial = _snapshot_name(cfg.get("initial_working_baseline"))
        if recovery == initial:
            raise ControllerError(
                "configuration_invalid",
                "recovery and initial working baseline snapshots must differ",
            )
        key = Path(os.path.expanduser(str(guest["key_path"])))
        if not key.is_absolute():
            raise ControllerError(
                "configuration_invalid", "guest key_path must be absolute"
            )
        _require_owner_file(key, "guest SSH key")
        port = guest.get("port", 22)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ControllerError(
                "configuration_invalid", "guest port must be an integer from 1 to 65535"
            )
        lease_seconds = cfg.get("lease_seconds", 4 * 60 * 60)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 60 <= lease_seconds <= MAX_TIMEOUT
        ):
            raise ControllerError(
                "configuration_invalid",
                f"lease_seconds must be an integer from 60 to {MAX_TIMEOUT}",
            )
        cfg["recovery_snapshot"] = recovery
        cfg["initial_working_baseline"] = initial
        guest["key_path"] = str(key)
        guest["port"] = port
        return cfg

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = self._acquire_lock()
        try:
            yield
        finally:
            self._release_lock(fd)

    def _acquire_lock(self) -> int:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ControllerError(
                "controller_busy", "another controller operation is still running"
            ) from exc
        return fd

    @staticmethod
    def _release_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _state(self) -> dict:
        state = _read_json(self.state_path, "controller state", required=False)
        if "working_baseline" not in state:
            state["working_baseline"] = self.config["initial_working_baseline"]
        state.setdefault("lease", None)
        state.setdefault("uncertain_exec", False)
        return state

    def _save_state(self, state: dict) -> None:
        _atomic_json(self.state_path, state)

    def _run(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            return self.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ControllerError(
                "host_tool_unavailable", f"required host tool is unavailable: {argv[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ControllerError(
                "host_command_timeout", f"host operation exceeded {timeout} seconds"
            ) from exc

    def _virsh(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return self._run(["virsh", "--connect", LIBVIRT_URI, *args], timeout)

    def _verify_target(self) -> None:
        proc = self._virsh("domuuid", DOMAIN)
        observed = proc.stdout.strip().lower()
        if proc.returncode != 0 or observed != DOMAIN_UUID:
            raise ControllerError(
                "target_identity_mismatch",
                "configured W10C-Testing domain identity could not be verified",
                {"domain": DOMAIN, "expected_uuid": DOMAIN_UUID},
            )

    def _domain_state(self) -> str:
        proc = self._virsh("domstate", DOMAIN)
        if proc.returncode != 0:
            raise ControllerError(
                "domain_state_failed", "could not read W10C-Testing state"
            )
        raw = proc.stdout.strip().lower()
        return {
            "shut off": "powered_off",
            "running": "running",
            "paused": "paused",
            "in shutdown": "shutting_down",
            "crashed": "crashed",
        }.get(raw, "unknown")

    def _ssh_argv(self, script: str) -> list[str]:
        guest = self.config["guest"]
        remote = f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {_ps_encoded(script)}"
        return [
            "ssh",
            "-T",
            "-i",
            guest["key_path"],
            "-p",
            str(guest["port"]),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
            f"{guest['user']}@{guest['host']}",
            remote,
        ]

    def _run_ps(self, script: str, timeout: int) -> dict:
        proc = self._run(self._ssh_argv(script), timeout)
        if proc.returncode != 0:
            raise ControllerError(
                "guest_command_failed",
                "guest PowerShell transport failed",
                {"exit_code": proc.returncode, "stderr": proc.stderr[-500:]},
            )
        try:
            value = json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ControllerError(
                "guest_response_invalid",
                "guest PowerShell returned an invalid response",
            ) from exc
        if not isinstance(value, dict):
            raise ControllerError(
                "guest_response_invalid", "guest PowerShell response was not an object"
            )
        return value

    def _guest_probe(self) -> dict:
        result = self._run_ps(
            """
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
@{ administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); powershell = $PSVersionTable.PSVersion.ToString() } | ConvertTo-Json -Compress
""",
            15,
        )
        if result.get("administrator") is not True:
            raise ControllerError(
                "guest_not_administrator",
                "configured guest SSH account is not an administrator",
            )
        return {
            "ready": True,
            "administrator": True,
            "powershell": str(result.get("powershell", "unknown")),
        }

    def _wait_guest(self, timeout: int) -> tuple[dict | None, str | None]:
        deadline = self.monotonic() + timeout
        last_error = "guest readiness was not attempted"
        while self.monotonic() < deadline:
            try:
                return self._guest_probe(), None
            except ControllerError as exc:
                last_error = str(exc)
            self.sleep(min(2, max(0, deadline - self.monotonic())))
        return None, last_error

    def _workspace_script(self, relative: str, body: str, *, must_exist: bool) -> str:
        root = _ps_literal(str(self.config["guest"]["workspace"]))
        rel = _ps_literal(relative)
        existence = (
            "if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw 'file not found' }; "
            "if (((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'reparse point refused' }"
            if must_exist
            else "if ((Test-Path -LiteralPath $target) -and (((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'reparse point refused' }; [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null"
        )
        return f"""
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath({root}).TrimEnd('\\')
$target = [IO.Path]::GetFullPath((Join-Path $root {rel}))
if (-not $target.StartsWith($root + '\\', [StringComparison]::OrdinalIgnoreCase)) {{ throw 'path escapes workspace' }}
$cursor = [IO.Path]::GetDirectoryName($target)
while ($cursor -and $cursor.Length -ge $root.Length) {{
  if (Test-Path -LiteralPath $cursor) {{
    $item = Get-Item -LiteralPath $cursor -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{ throw 'reparse point refused' }}
  }}
  if ($cursor -eq $root) {{ break }}
  $cursor = [IO.Path]::GetDirectoryName($cursor)
}}
{existence}
{body}
"""

    def _lease(self, state: dict, token: object) -> dict:
        lease = state.get("lease")
        if (
            not isinstance(lease, dict)
            or float(lease.get("expires_at", 0)) <= self.now()
        ):
            raise ControllerError(
                "lease_required", "an active controller lease is required"
            )
        if not secrets.compare_digest(str(lease.get("token", "")), str(token or "")):
            raise ControllerError(
                "lease_conflict", "the controller is leased by another session"
            )
        return lease

    def _wait_state(self, expected: str, timeout: int) -> str:
        deadline = self.monotonic() + timeout
        observed = self._domain_state()
        while observed != expected and self.monotonic() < deadline:
            self.sleep(min(1, max(0, deadline - self.monotonic())))
            observed = self._domain_state()
        return observed

    def _stop(self, *, force: bool, timeout: int, state: dict) -> dict:
        self._verify_target()
        observed = self._domain_state()
        forced = False
        if observed == "powered_off":
            state["uncertain_exec"] = False
            self._save_state(state)
            return {
                "domain": DOMAIN,
                "state": observed,
                "forced": False,
                "changed": False,
            }
        proc = self._virsh("shutdown", DOMAIN)
        if proc.returncode != 0:
            raise ControllerError(
                "stop_failed", "graceful shutdown request was rejected"
            )
        observed = self._wait_state("powered_off", timeout)
        if observed != "powered_off" and force:
            proc = self._virsh("destroy", DOMAIN)
            if proc.returncode != 0:
                raise ControllerError(
                    "forced_stop_failed", "explicit forced stop was rejected"
                )
            forced = True
            observed = self._wait_state("powered_off", 30)
        if observed != "powered_off":
            raise ControllerError(
                "stop_timeout",
                "guest did not stop before the bounded timeout; no forced stop was performed",
                {"state": observed, "force_requested": force},
            )
        state["uncertain_exec"] = False
        self._save_state(state)
        return {"domain": DOMAIN, "state": observed, "forced": forced, "changed": True}

    def status(self) -> dict:
        self._verify_target()
        state = self._state()
        lease = state.get("lease")
        active_lease = (
            lease
            if (
                isinstance(lease, dict)
                and float(lease.get("expires_at", 0)) > self.now()
            )
            else None
        )
        snapshots = self._snapshot_list()
        domain_state = self._domain_state()
        guest: dict[str, object] = {
            "ready": False,
            "administrator": None,
            "powershell": None,
        }
        guest_error: dict[str, str] | None = None
        if domain_state == "running":
            try:
                guest = self._guest_probe()
            except ControllerError as exc:
                guest_error = {"code": exc.code, "message": str(exc)[:500]}
        guest["error"] = guest_error
        return {
            "domain": DOMAIN,
            "uuid": DOMAIN_UUID,
            "state": domain_state,
            "guest": guest,
            "working_baseline": state["working_baseline"],
            "recovery_snapshot": self.config["recovery_snapshot"],
            "uncertain_exec": bool(state.get("uncertain_exec")),
            "lease": {
                "active": active_lease is not None,
                "owner": active_lease.get("owner") if active_lease else None,
                "expires_at": active_lease.get("expires_at") if active_lease else None,
            },
            "snapshots": snapshots,
        }

    def acquire(self, owner: object) -> dict:
        name = str(owner or "shell").strip()
        if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
            raise ControllerError(
                "lease_owner_invalid", "lease owner must be 1-80 printable characters"
            )
        with self._locked():
            state = self._state()
            current = state.get("lease")
            if (
                isinstance(current, dict)
                and float(current.get("expires_at", 0)) > self.now()
            ):
                raise ControllerError(
                    "lease_conflict",
                    "the controller is already leased",
                    {
                        "owner": current.get("owner"),
                        "expires_at": current.get("expires_at"),
                    },
                )
            ttl = int(self.config.get("lease_seconds", 4 * 60 * 60))
            ttl = min(MAX_TIMEOUT, max(60, ttl))
            lease = {
                "token": secrets.token_urlsafe(32),
                "owner": name,
                "acquired_at": self.now(),
                "expires_at": self.now() + ttl,
            }
            state["lease"] = lease
            self._save_state(state)
            return dict(lease)

    def release(self, token: object) -> dict:
        with self._locked():
            state = self._state()
            self._lease(state, token)
            state["lease"] = None
            self._save_state(state)
            return {"released": True}

    def start(self, token: object, timeout: object) -> dict:
        wait = _validated_timeout(timeout, 120)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            observed = self._domain_state()
            changed = False
            if observed == "powered_off":
                proc = self._virsh("start", DOMAIN)
                if proc.returncode != 0:
                    raise ControllerError(
                        "start_failed", "W10C-Testing could not be started"
                    )
                changed = True
                observed = self._wait_state("running", wait)
            if observed != "running":
                raise ControllerError(
                    "start_state_invalid",
                    "W10C-Testing did not reach running state",
                    {"state": observed},
                )
            guest, last_error = self._wait_guest(wait)
            if guest is None:
                raise ControllerError(
                    "guest_readiness_timeout",
                    f"guest did not become ready within {wait} seconds",
                    {"last_error": last_error},
                )
            return {
                "domain": DOMAIN,
                "state": observed,
                "changed": changed,
                "guest": guest,
            }

    def stop(self, token: object, timeout: object, force: object) -> dict:
        wait = _validated_timeout(timeout, DEFAULT_STOP_TIMEOUT)
        if force not in (None, False, True):
            raise ControllerError("force_invalid", "force must be boolean")
        with self._locked():
            state = self._state()
            self._lease(state, token)
            return self._stop(force=bool(force), timeout=wait, state=state)

    def exec(
        self, token: object, command: object, cwd: object, timeout: object
    ) -> dict:
        command_text = str(command or "")
        if not command_text.strip():
            raise ControllerError(
                "exec_command_invalid", "PowerShell command must not be empty"
            )
        relative = _guest_relative(cwd)
        wait = _validated_timeout(timeout, 120)
        command_b64 = base64.b64encode(command_text.encode("utf-8")).decode("ascii")
        body = f"""
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{ throw 'guest account is not an administrator' }}
$id = [Guid]::NewGuid().ToString('N')
$scriptPath = Join-Path $env:TEMP ("sc-test-" + $id + ".ps1")
$stdoutPath = Join-Path $env:TEMP ("sc-test-" + $id + ".out")
$stderrPath = Join-Path $env:TEMP ("sc-test-" + $id + ".err")
try {{
  [IO.File]::WriteAllText($scriptPath, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{command_b64}')), [Text.UTF8Encoding]::new($false))
  $p = Start-Process powershell.exe -WorkingDirectory $target -ArgumentList @('-NoProfile','-NonInteractive','-File',('"' + $scriptPath + '"')) -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
  $finished = $p.WaitForExit({wait * 1000})
  if (-not $finished) {{ $null = & taskkill.exe /PID $p.Id /T /F; $p.WaitForExit() }}
  $result = @{{ exit_code = $(if ($finished) {{ $p.ExitCode }} else {{ 124 }}); stdout = $(if (Test-Path $stdoutPath) {{ [IO.File]::ReadAllText($stdoutPath) }} else {{ '' }}); stderr = $(if (Test-Path $stderrPath) {{ [IO.File]::ReadAllText($stderrPath) }} else {{ '' }}); timed_out = (-not $finished) }}
  $result | ConvertTo-Json -Compress
}} finally {{ Remove-Item -LiteralPath $scriptPath,$stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue }}
"""
        script = self._workspace_script(relative, body, must_exist=False)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            try:
                result = self._run_ps(script, wait + 30)
            except ControllerError as exc:
                if exc.code == "host_command_timeout":
                    state["uncertain_exec"] = True
                    self._save_state(state)
                raise
            return {
                "exit_code": int(result.get("exit_code", -1)),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr", "")),
                "timed_out": bool(result.get("timed_out")),
            }

    def _snapshot_list(self) -> list[str]:
        proc = self._virsh("snapshot-list", DOMAIN, "--name")
        if proc.returncode != 0:
            raise ControllerError(
                "snapshot_list_failed", "could not list W10C-Testing snapshots"
            )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def snapshot_list(self) -> dict:
        self._verify_target()
        state = self._state()
        return {
            "snapshots": self._snapshot_list(),
            "working_baseline": state["working_baseline"],
            "recovery_snapshot": self.config["recovery_snapshot"],
        }

    def _ensure_off(self, state: dict) -> None:
        if self._domain_state() != "powered_off":
            self._stop(force=False, timeout=DEFAULT_STOP_TIMEOUT, state=state)

    def _create_snapshot(self, name: str) -> None:
        proc = self._virsh(
            "snapshot-create-as",
            DOMAIN,
            name,
            "--description",
            "Subfloor disposable Windows testing snapshot",
            timeout=300,
        )
        if proc.returncode != 0:
            raise ControllerError(
                "snapshot_create_failed", f"snapshot '{name}' could not be created"
            )
        verify = self._virsh("snapshot-info", DOMAIN, "--snapshotname", name)
        if verify.returncode != 0:
            raise ControllerError(
                "snapshot_verify_failed", f"snapshot '{name}' could not be verified"
            )

    def snapshot_create(self, token: object, name: object) -> dict:
        snapshot = _snapshot_name(name)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            if snapshot in (
                self.config["recovery_snapshot"],
                state["working_baseline"],
            ):
                raise ControllerError(
                    "snapshot_protected",
                    "recovery and working baseline snapshots cannot be replaced",
                )
            self._ensure_off(state)
            self._create_snapshot(snapshot)
            return {"snapshot": snapshot, "state": "powered_off"}

    def snapshot_delete(self, token: object, name: object) -> dict:
        snapshot = _snapshot_name(name)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            if snapshot in (
                self.config["recovery_snapshot"],
                state["working_baseline"],
            ):
                raise ControllerError(
                    "snapshot_protected",
                    "recovery and working baseline snapshots cannot be deleted",
                )
            proc = self._virsh(
                "snapshot-delete", DOMAIN, "--snapshotname", snapshot, timeout=120
            )
            if proc.returncode != 0:
                raise ControllerError(
                    "snapshot_delete_failed",
                    f"snapshot '{snapshot}' could not be deleted",
                )
            return {"snapshot": snapshot, "deleted": True}

    def reset(self, token: object, name: object) -> dict:
        requested = _snapshot_name(name, allow_working=True)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            if state.get("uncertain_exec"):
                raise ControllerError(
                    "exec_outcome_uncertain",
                    "reset is blocked until an explicit stop confirms the guest is powered off",
                )
            self._verify_target()
            snapshot = (
                state["working_baseline"] if requested == "working" else requested
            )
            self._ensure_off(state)
            proc = self._virsh(
                "snapshot-revert", DOMAIN, "--snapshotname", snapshot, timeout=180
            )
            if proc.returncode != 0:
                raise ControllerError(
                    "snapshot_reset_failed",
                    f"snapshot '{snapshot}' could not be restored",
                )
            observed = self._domain_state()
            if observed != "powered_off":
                raise ControllerError(
                    "snapshot_reset_state",
                    "snapshot reset did not leave the guest powered off",
                    {"state": observed},
                )
            return {"snapshot": snapshot, "state": observed}

    def promote(self, token: object, name: object) -> dict:
        candidate = _snapshot_name(
            name or f"working-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(self.now()))}"
        )
        with self._locked():
            state = self._state()
            self._lease(state, token)
            if state.get("uncertain_exec"):
                raise ControllerError(
                    "exec_outcome_uncertain",
                    "promotion is blocked until an explicit stop confirms power-off",
                )
            self._verify_target()
            previous = state["working_baseline"]
            if candidate in (self.config["recovery_snapshot"], previous):
                raise ControllerError(
                    "snapshot_protected", "promotion requires a fresh snapshot name"
                )
            self._ensure_off(state)
            self._create_snapshot(candidate)
            state["working_baseline"] = candidate
            self._save_state(state)
            return {
                "working_baseline": candidate,
                "previous_baseline": previous,
                "previous_preserved": True,
                "state": "powered_off",
            }

    def push(
        self,
        token: object,
        relative: object,
        payload: BinaryIO,
        size: object,
        timeout: object,
    ) -> dict:
        dest = _guest_relative(relative)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ControllerError(
                "payload_size_invalid",
                "push payload_bytes must be a non-negative integer",
            )
        wait = _validated_timeout(timeout, 300)
        body = """
$input = [Console]::OpenStandardInput()
$file = [IO.File]::Open($target, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $input.CopyTo($file) } finally { $file.Dispose() }
"""
        script = self._workspace_script(dest, body, must_exist=False)
        with self._locked():
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            process = self.popen(
                self._ssh_argv(script),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            remaining = size
            try:
                while remaining:
                    chunk = payload.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ControllerError(
                            "payload_incomplete",
                            "push payload ended before payload_bytes",
                        )
                    process.stdin.write(chunk)
                    remaining -= len(chunk)
                process.stdin.close()
                rc = process.wait(timeout=wait)
            except ControllerError:
                process.kill()
                process.wait()
                raise
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise ControllerError(
                    "push_timeout", f"push exceeded {wait} seconds"
                ) from exc
            if rc != 0:
                stderr = (
                    process.stderr.read(500).decode("utf-8", "replace")
                    if process.stderr
                    else ""
                )
                raise ControllerError(
                    "push_failed",
                    "guest artifact upload failed",
                    {"exit_code": rc, "stderr": stderr},
                )
            return {"path": dest, "bytes": size}

    def pull_plan(self, token: object, relative: object, timeout: object) -> PullPlan:
        source = _guest_relative(relative)
        wait = _validated_timeout(timeout, 300)
        size_script = self._workspace_script(
            source,
            "@{ size = (Get-Item -LiteralPath $target).Length } | ConvertTo-Json -Compress",
            must_exist=True,
        )
        lock_fd = self._acquire_lock()
        try:
            state = self._state()
            self._lease(state, token)
            self._verify_target()
            meta = self._run_ps(size_script, 30)
            size = int(meta.get("size", -1))
            if size < 0:
                raise ControllerError(
                    "guest_response_invalid", "guest returned an invalid file size"
                )
            stream_script = self._workspace_script(
                source,
                "$output = [Console]::OpenStandardOutput(); $file = [IO.File]::OpenRead($target); try { $file.CopyTo($output) } finally { $file.Dispose() }",
                must_exist=True,
            )
            return PullPlan("pull", self._ssh_argv(stream_script), size, wait, lock_fd)
        except Exception:
            self._release_lock(lock_fd)
            raise

    def stream_pull(self, plan: PullPlan, output: BinaryIO) -> None:
        try:
            process = self.popen(
                plan.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            remaining = plan.size
            try:
                while remaining:
                    chunk = process.stdout.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    output.flush()
                    remaining -= len(chunk)
                rc = process.wait(timeout=plan.timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise ControllerError(
                    "pull_timeout", f"pull exceeded {plan.timeout} seconds"
                ) from exc
            if rc != 0 or remaining:
                raise ControllerError(
                    "pull_failed",
                    "guest artifact download did not complete",
                    {"exit_code": rc, "missing_bytes": remaining},
                )
        finally:
            self._release_lock(plan.lock_fd)

    def dispatch(self, request: dict, payload: BinaryIO) -> dict | PullPlan:
        operation = str(request.get("operation", ""))
        token = request.get("lease")
        if operation == "status":
            return _success(operation, self.status())
        if operation == "acquire":
            return _success(operation, self.acquire(request.get("owner")))
        if operation == "release":
            return _success(operation, self.release(token))
        if operation == "start":
            return _success(operation, self.start(token, request.get("timeout")))
        if operation == "stop":
            return _success(
                operation,
                self.stop(token, request.get("timeout"), request.get("force")),
            )
        if operation == "exec":
            result = self.exec(
                token,
                request.get("command"),
                request.get("cwd"),
                request.get("timeout"),
            )
            if result["timed_out"]:
                return _result_error(
                    operation,
                    "exec_timeout",
                    "guest PowerShell exceeded its timeout and its process tree was stopped",
                    result,
                )
            if result["exit_code"] != 0:
                return _result_error(
                    operation,
                    "exec_failed",
                    f"guest PowerShell exited {result['exit_code']}",
                    result,
                )
            return _success(operation, result)
        if operation == "push":
            return _success(
                operation,
                self.push(
                    token,
                    request.get("path"),
                    payload,
                    request.get("payload_bytes"),
                    request.get("timeout"),
                ),
            )
        if operation == "pull":
            return self.pull_plan(token, request.get("path"), request.get("timeout"))
        if operation == "snapshot_list":
            return _success(operation, self.snapshot_list())
        if operation == "snapshot_create":
            return _success(operation, self.snapshot_create(token, request.get("name")))
        if operation == "snapshot_delete":
            return _success(operation, self.snapshot_delete(token, request.get("name")))
        if operation == "reset":
            return _success(operation, self.reset(token, request.get("name")))
        if operation == "baseline_promote":
            return _success(operation, self.promote(token, request.get("name")))
        raise ControllerError("operation_unknown", "unknown controller operation")


def _read_header(stream: BinaryIO) -> dict:
    raw = stream.readline(MAX_HEADER_BYTES + 1)
    if not raw or len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\n"):
        raise ControllerError(
            "protocol_header_invalid", "request header must be one bounded JSON line"
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError(
            "protocol_header_invalid", "request header is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ControllerError(
            "protocol_header_invalid", "request header must be a JSON object"
        )
    return value


def _write_header(stream: BinaryIO, value: dict) -> None:
    stream.write(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")
    stream.flush()


def serve_once(
    stdin: BinaryIO, stdout: BinaryIO, controller: Controller | None = None
) -> int:
    operation = "unknown"
    try:
        active_controller = controller or Controller()
        request = _read_header(stdin)
        operation = str(request.get("operation", "unknown"))
        result = active_controller.dispatch(request, stdin)
    except ControllerError as exc:
        _write_header(stdout, _error(operation, exc))
        return 1
    if isinstance(result, PullPlan):
        header = _success(
            result.operation, {"bytes": result.size, "payload_bytes": result.size}
        )
        _write_header(stdout, header)
        try:
            active_controller.stream_pull(result, stdout)
        except ControllerError:
            return 1
    else:
        _write_header(stdout, result)
    return 0


def _client_config_path() -> Path:
    return Path.cwd() / CLIENT_PATH


def _load_client() -> dict:
    return _read_json(_client_config_path(), "Windows test client configuration")


def _client_process(cfg: dict) -> subprocess.Popen:
    transport = cfg.get("transport")
    if transport == "local":
        argv = [sys.executable, str(Path(__file__).resolve()), "serve"]
    elif transport == "ssh":
        host = str(cfg.get("host", ""))
        if not SSH_ALIAS_RE.fullmatch(host):
            raise ControllerError(
                "client_configuration_invalid", "SSH controller alias is invalid"
            )
        argv = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            host,
        ]
    else:
        raise ControllerError(
            "client_configuration_invalid", "transport must be local or ssh"
        )
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def client_request(
    request: dict, *, input_path: Path | None = None, output_path: Path | None = None
) -> dict:
    cfg = _load_client()
    token = cfg.get("lease")
    if token and "lease" not in request:
        request["lease"] = token
    if input_path is not None:
        request["payload_bytes"] = input_path.stat().st_size
    process = _client_process(cfg)
    assert process.stdin is not None and process.stdout is not None
    process_stdin = cast(BinaryIO, process.stdin)
    process_stdout = cast(BinaryIO, process.stdout)
    _write_header(process_stdin, request)
    if input_path is not None:
        with input_path.open("rb") as source:
            shutil.copyfileobj(source, process_stdin, CHUNK_SIZE)
    process_stdin.close()
    response = _read_header(process_stdout)
    expected = int((response.get("result") or {}).get("payload_bytes", 0))
    tmp: Path | None = None
    try:
        if output_path is not None and response.get("ok"):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_tmp = tempfile.mkstemp(
                prefix=f".{output_path.name}.", dir=output_path.parent
            )
            tmp = Path(raw_tmp)
            with os.fdopen(fd, "wb") as target:
                remaining = expected
                while remaining:
                    chunk = process_stdout.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ControllerError(
                            "response_incomplete",
                            "controller response payload was incomplete",
                        )
                    target.write(chunk)
                    remaining -= len(chunk)
                target.flush()
                os.fsync(target.fileno())
        rc = process.wait()
        if rc != 0 and response.get("ok"):
            raise ControllerError(
                "transport_failed",
                "controller transport exited before completing a successful response",
            )
        if tmp is not None and output_path is not None:
            os.replace(tmp, output_path)
            tmp = None
            response["result"]["path"] = str(output_path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if response.get("ok") and request["operation"] == "acquire":
        cfg["lease"] = response["result"]["token"]
        _atomic_json(_client_config_path(), cfg)
        response["result"].pop("token", None)
        response["result"]["lease_saved"] = True
    if response.get("ok") and request["operation"] == "release":
        cfg.pop("lease", None)
        _atomic_json(_client_config_path(), cfg)
    return response


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./sc vm test", description="Control the fixed W10C-Testing guest."
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser(
        "init", help="select Halo-local or constrained SSH transport"
    )
    init_sub = init.add_subparsers(dest="transport", required=True)
    init_sub.add_parser("local")
    ssh = init_sub.add_parser("ssh")
    ssh.add_argument(
        "host", help="SSH config alias whose server key has the controller ForceCommand"
    )
    commands.add_parser("status")
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--owner", default="shell")
    commands.add_parser("release")
    for verb in ("start", "stop"):
        item = commands.add_parser(verb)
        item.add_argument("--timeout", type=int)
        if verb == "stop":
            item.add_argument("--force", action="store_true")
    execute = commands.add_parser("exec")
    execute.add_argument("--cwd", required=True)
    execute.add_argument("--timeout", type=int)
    execute.add_argument("--command-file")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    push = commands.add_parser("push")
    push.add_argument("source")
    push.add_argument("destination")
    push.add_argument("--timeout", type=int)
    pull = commands.add_parser("pull")
    pull.add_argument("source")
    pull.add_argument("destination")
    pull.add_argument("--timeout", type=int)
    snapshot = commands.add_parser("snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_action", required=True)
    snapshot_sub.add_parser("list")
    for verb in ("create", "delete"):
        item = snapshot_sub.add_parser(verb)
        item.add_argument("name")
    reset = commands.add_parser("reset")
    reset.add_argument("name", help="snapshot name or 'working'")
    baseline = commands.add_parser("baseline")
    baseline_sub = baseline.add_subparsers(dest="baseline_action", required=True)
    promote = baseline_sub.add_parser("promote")
    promote.add_argument("name", nargs="?")
    return parser


def client_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.operation == "init":
        cfg = {"transport": args.transport}
        if args.transport == "ssh":
            if not SSH_ALIAS_RE.fullmatch(args.host):
                raise ControllerError(
                    "client_configuration_invalid", "SSH controller alias is invalid"
                )
            cfg["host"] = args.host
        _atomic_json(_client_config_path(), cfg)
        response = _success(
            "init", {"transport": args.transport, "host": cfg.get("host")}
        )
    else:
        request: dict = {"operation": args.operation}
        input_path = output_path = None
        if args.operation == "acquire":
            request["owner"] = args.owner
        elif args.operation in ("start", "stop"):
            request["timeout"] = args.timeout
            if args.operation == "stop":
                request["force"] = args.force
        elif args.operation == "exec":
            parts = args.command[1:] if args.command[:1] == ["--"] else args.command
            if args.command_file and parts:
                raise ControllerError(
                    "exec_arguments_invalid",
                    "use --command-file or command arguments, not both",
                )
            if args.command_file:
                command = Path(args.command_file).read_text(encoding="utf-8")
            else:
                command = " ".join(parts)
            request.update(command=command, cwd=args.cwd, timeout=args.timeout)
        elif args.operation == "push":
            input_path = Path(args.source)
            request.update(path=args.destination, timeout=args.timeout)
        elif args.operation == "pull":
            output_path = Path(args.destination)
            request.update(path=args.source, timeout=args.timeout)
        elif args.operation == "snapshot":
            request["operation"] = f"snapshot_{args.snapshot_action}"
            if args.snapshot_action != "list":
                request["name"] = args.name
        elif args.operation == "reset":
            request["name"] = args.name
        elif args.operation == "baseline":
            request["operation"] = "baseline_promote"
            request["name"] = args.name
        response = client_request(
            request, input_path=input_path, output_path=output_path
        )
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response["ok"] else 1


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["serve"]:
            return serve_once(sys.stdin.buffer, sys.stdout.buffer)
        return client_main(argv)
    except BrokenPipeError:
        raise
    except (ControllerError, OSError, UnicodeError) as exc:
        error = (
            exc
            if isinstance(exc, ControllerError)
            else ControllerError("client_io_failed", "client file operation failed")
        )
        print(
            json.dumps(_error("client", error), separators=(",", ":")), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
