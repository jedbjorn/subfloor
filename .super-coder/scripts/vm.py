#!/usr/bin/env python3
"""Windows Test VM — config read/write + live connection checks.

Link-only by design: the engine never creates the VM. The operator brings a
ready Windows VM (OpenSSH enabled, a clean snapshot, a transfer dir, and — via
the admin `configure_winbox` skill — a baked toolchain). These checks validate
that the operator-supplied `vm` block actually reaches a reachable, provisioned
box BEFORE it is saved to instance.json.

The config lives under the `vm` key of `.super-coder/instance.json` (so there is
no schema migration — the VM is a host resource, not shell state). It holds a
key PATH, never key material — secrets posture matches the rest of the engine.

Each check runs ONE real host-side command and returns {ok, output}, mirroring
api/server.py's run_script contract so the GUI can render it the same way.

    domain    virsh dominfo <domain>                 VM exists / visible to libvirt
    ssh       ssh ... echo ok                        auth + remote exec work
    transfer  write+read+rm a probe in transfer_dir  host side of the share works
    snapshot  virsh snapshot-info <domain> <snap>    the named clean snapshot exists
    toolchain ssh ... dotnet --version               box is provisioned (verify-only)

The `toolchain` check is verify-only — it confirms `configure_winbox` has run;
it never installs anything.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import ports

CHECKS = ("domain", "ssh", "transfer", "snapshot", "toolchain")

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where windows_devkit curls it). No network surface; fs-perm gated.
RUN_DIR = ports.ENGINE / "run"
SOCKET = RUN_DIR / "vm-broker.sock"

# The GUI seam (#263): a broker-owned `ssh -N -L` forwards this unix socket to
# the guest's localhost-bound Windows-MCP port. Same posture as the broker
# socket — lives in the bind mount, fs-perm gated (0600), no network surface.
MCP_SOCKET = RUN_DIR / "vm-mcp.sock"
MCP_PIDFILE = RUN_DIR / "vm-mcp-tunnel.pid"
MCP_LOCKFILE = RUN_DIR / "vm-mcp-tunnel.lock"
MCP_LOG = RUN_DIR / "vm-mcp-tunnel.log"
MCP_RELAY_PORT = 18000
MCP_ENDPOINT_PATH = "/mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_PROBE_LIMIT = 8192

PROCESS_STATE_VERSION = 1
PROCESS_STOP_TIMEOUT = 2.0
PROCESS_KILL_TIMEOUT = 1.0
PROCESS_POLL_INTERVAL = 0.05
PROCESS_VERIFY_TIMEOUT = 0.2
PROCESS_VERIFY_INTERVAL = 0.01


def _process_snapshot(pid: int) -> dict | None:
    """Return the current Linux process identity needed before signaling.

    PID values alone are unsafe across container restarts because a new PID
    namespace can recycle the number recorded on the shared bind mount.
    Field-22 start ticks make that identity stable; executable and command-line
    checks keep a valid-but-unrelated process from satisfying the record.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields_after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields_after_comm[19])
        executable = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
        cmdline = [
            os.fsdecode(part)
            for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if part
        ]
    except (IndexError, OSError, ValueError):
        return None
    if pid <= 0 or start_ticks < 0 or not executable or not cmdline:
        return None
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "executable": executable,
        "cmdline": cmdline,
    }


def _process_socket_inodes(pid: int) -> set[str]:
    """Return kernel socket inodes currently held by a Linux process."""
    inodes = set()
    try:
        descriptors = Path(f"/proc/{pid}/fd").iterdir()
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    except OSError:
        return set()
    return inodes


def _process_owns_tcp_listener(pid: int, port: int) -> bool:
    """Whether pid owns the loopback TCP listener for port."""
    inodes = _process_socket_inodes(pid)
    if not inodes:
        return False
    local = f"0100007F:{port:04X}"
    try:
        rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return False
    return any(
        len(fields) > 9
        and fields[1] == local
        and fields[3] == "0A"
        and fields[9] in inodes
        for fields in (row.split() for row in rows)
    )


def _process_owns_unix_listener(pid: int, path: Path) -> bool:
    """Whether pid owns the listening Unix socket at path."""
    inodes = _process_socket_inodes(pid)
    if not inodes:
        return False
    try:
        rows = Path("/proc/net/unix").read_text().splitlines()[1:]
    except OSError:
        return False
    expected_path = str(path)
    for row in rows:
        fields = row.split(maxsplit=7)
        if (
            len(fields) == 8
            and fields[3] == "00010000"
            and fields[4] == "0001"
            and fields[6] in inodes
            and fields[7] == expected_path
        ):
            return True
    return False


def _new_process_state(
    pid: int,
    *,
    kind: str,
    expected_executable: str,
    required_token: str,
    **details,
) -> dict | None:
    deadline = time.monotonic() + PROCESS_VERIFY_TIMEOUT
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = _process_snapshot(pid)
        if _snapshot_matches(
            snapshot,
            expected_executable=expected_executable,
            required_token=required_token,
        ):
            break
        time.sleep(PROCESS_VERIFY_INTERVAL)
    else:
        return None
    return {
        "schema_version": PROCESS_STATE_VERSION,
        "kind": kind,
        "pid": snapshot["pid"],
        "start_ticks": snapshot["start_ticks"],
        "executable": snapshot["executable"],
        **details,
    }


def _snapshot_matches(
    snapshot: dict | None,
    *,
    expected_executable: str,
    required_token: str,
) -> bool:
    if snapshot is None:
        return False
    expected = os.path.realpath(expected_executable)
    return (
        snapshot["executable"] == expected
        and any(required_token in arg for arg in snapshot["cmdline"])
    )


def _read_process_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(state, dict):
        return None
    return state


def _owned_process(
    path: Path,
    *,
    kind: str,
    expected_executable: str,
    required_token: str,
) -> dict | None:
    state = _read_process_state(path)
    if (
        state is None
        or state.get("schema_version") != PROCESS_STATE_VERSION
        or state.get("kind") != kind
        or not isinstance(state.get("pid"), int)
        or not isinstance(state.get("start_ticks"), int)
        or not isinstance(state.get("executable"), str)
    ):
        return None
    snapshot = _process_snapshot(state["pid"])
    if not _snapshot_matches(
        snapshot,
        expected_executable=expected_executable,
        required_token=required_token,
    ):
        return None
    if (
        snapshot["start_ticks"] != state["start_ticks"]
        or snapshot["executable"] != state["executable"]
    ):
        return None
    return state


def _atomic_write_process_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _process_state_lock(path: Path):
    """Serialize status-changing commands across host/container processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _process_record_matches(
    state: dict,
    *,
    expected_executable: str,
    required_token: str,
) -> bool:
    snapshot = _process_snapshot(state["pid"])
    return bool(
        _snapshot_matches(
            snapshot,
            expected_executable=expected_executable,
            required_token=required_token,
        )
        and snapshot["start_ticks"] == state["start_ticks"]
        and snapshot["executable"] == state["executable"]
    )


def _terminate_owned_process(
    state: dict,
    *,
    expected_executable: str,
    required_token: str,
) -> bool:
    """Stop only the exact recorded process, with a bounded TERM/KILL ladder."""
    if not _process_record_matches(
        state,
        expected_executable=expected_executable,
        required_token=required_token,
    ):
        return True
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + PROCESS_STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not _process_record_matches(
            state,
            expected_executable=expected_executable,
            required_token=required_token,
        ):
            return True
        time.sleep(PROCESS_POLL_INTERVAL)
    if not _process_record_matches(
        state,
        expected_executable=expected_executable,
        required_token=required_token,
    ):
        return True
    try:
        os.kill(state["pid"], signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + PROCESS_KILL_TIMEOUT
    while time.monotonic() < deadline:
        if not _process_record_matches(
            state,
            expected_executable=expected_executable,
            required_token=required_token,
        ):
            return True
        time.sleep(PROCESS_POLL_INTERVAL)
    return False


def _bounded_log_tail(path: Path, limit: int = 1000) -> str:
    try:
        return path.read_text(errors="replace").strip()[-limit:]
    except OSError:
        return ""


def _unverified_process_error(
    process: subprocess.Popen,
    *,
    label: str,
    expected_executable: str,
    log_path: Path,
) -> str:
    """Capture verification evidence and reap the exact child we just spawned."""
    snapshot = _process_snapshot(process.pid)
    observed = snapshot["executable"] if snapshot else "(unavailable)"
    returncode = process.poll()
    if returncode is None:
        process.terminate()
    try:
        waited = process.wait(timeout=PROCESS_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        waited = process.wait(timeout=PROCESS_KILL_TIMEOUT)
    returncode = waited if waited is not None else process.returncode
    tail = _bounded_log_tail(log_path) or "(no output)"
    return (
        f"could not verify the new {label} process "
        f"(expected executable {os.path.realpath(expected_executable)}, "
        f"observed {observed}, rc {returncode}): {tail}"
    )
DOMAIN_STATE_TIMEOUT = 15
DOMAIN_START_TIMEOUT = 30
RESET_COMMAND_TIMEOUT = 60
START_READINESS_TIMEOUT = 90
START_READINESS_INTERVAL = 2
MUTATION_LOCK_TIMEOUT = 5
RESET_BROKER_BUDGET = (
    MUTATION_LOCK_TIMEOUT + RESET_COMMAND_TIMEOUT + DOMAIN_STATE_TIMEOUT
)
RESET_CLIENT_TIMEOUT = 130
START_BROKER_BUDGET = (
    MUTATION_LOCK_TIMEOUT
    + DOMAIN_STATE_TIMEOUT
    + DOMAIN_START_TIMEOUT
    + START_READINESS_TIMEOUT
)
START_CLIENT_TIMEOUT = START_BROKER_BUDGET + 15
DEFAULT_CLIENT_TIMEOUT = 30
EXEC_CLIENT_TIMEOUT = 130
CAPTURE_CLIENT_TIMEOUT = 45
RESULT_SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
CAPTURE_ARTIFACT_ROOT = (
    ports.ENGINE.parent / ".sc-state" / "local" / "vm-captures"
)
CAPTURE_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "ppm": "image/x-portable-pixmap",
}

DOMAIN_STATES = {
    "blocked": "blocked",
    "crashed": "crashed",
    "in shutdown": "shutting_down",
    "no state": "unknown",
    "paused": "paused",
    "pmsuspended": "suspended",
    "running": "running",
    "shut off": "powered_off",
}


# -- config (instance.json `vm` block) ---------------------------------------

def read() -> dict | None:
    """The persisted vm block, or None if the fork has not configured one."""
    return ports.resolve(persist=False).get("vm")


def write(vm: dict | None) -> dict | None:
    """Persist (or clear) the vm block, preserving every other config key."""
    cfg = ports.resolve(persist=False)
    if vm:
        cfg["vm"] = vm
    else:
        cfg.pop("vm", None)
    ports.save(cfg)
    return cfg.get("vm")


# -- check primitives --------------------------------------------------------

def _run(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        # errors="replace": Windows guests emit non-UTF-8 constantly (UTF-16
        # files, OEM-codepage console output) — decode lossily, never raise.
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        return False, f"command not found: {e.filename} — is it installed on the host?"
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{timeout}s)"


def _missing(cfg: dict, *fields: str) -> str | None:
    absent = [f for f in fields if not str(cfg.get(f, "")).strip()]
    return ("missing required field(s): " + ", ".join(absent)) if absent else None


def _virsh(cfg: dict, *args: str) -> list[str]:
    """A virsh argv against the configured connection. `libvirt_uri` in the vm
    block selects the hypervisor — set it to `qemu:///system` for a system-scope
    domain, which the default `qemu:///session` cannot see. Absent, virsh uses
    its own default (the `LIBVIRT_DEFAULT_URI` env var, else `qemu:///session`)."""
    uri = str(cfg.get("libvirt_uri", "")).strip()
    return ["virsh", *(["--connect", uri] if uri else []), *args]


def _ssh_argv(cfg: dict, remote: str) -> list[str]:
    """An ssh invocation against the configured guest. BatchMode keeps it
    non-interactive (no password/passphrase prompt can hang the server)."""
    key = os.path.expanduser(str(cfg.get("ssh_key_path", "")))
    return [
        "ssh", "-i", key,
        "-p", str(cfg.get("ssh_port", 22)),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg.get('ssh_user')}@{cfg.get('ssh_host')}", remote,
    ]


# -- the five checks ---------------------------------------------------------

def _check_domain(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "domain"):
        return False, m
    return _run(_virsh(cfg, "dominfo", str(cfg["domain"])), timeout=15)


def _check_ssh(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return False, m
    return _run(_ssh_argv(cfg, "echo ok"), timeout=20)


def _check_transfer(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "transfer_dir"):
        return False, m
    d = Path(os.path.expanduser(str(cfg["transfer_dir"])))
    if not d.is_dir():
        return False, f"transfer_dir does not exist or is not a directory: {d}"
    probe = d / ".sc_vm_probe"
    try:
        probe.write_text("ok")
        back = probe.read_text()
        probe.unlink()
    except OSError as e:
        return False, f"transfer_dir not writable host-side: {e}"
    if back != "ok":
        return False, "wrote a probe file but read back unexpected content"
    return True, f"wrote + read back a probe in {d} (host side of the share OK)"


def _check_snapshot(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "domain", "snapshot"):
        return False, m
    ok, out = _run(
        _virsh(cfg, "snapshot-info", str(cfg["domain"]),
               "--snapshotname", str(cfg["snapshot"])), timeout=15)
    if not ok and "Domain snapshot not found" in out:
        return False, (f"snapshot '{cfg['snapshot']}' not found on domain "
                       f"'{cfg['domain']}' — create the clean snapshot first.\n{out}")
    return ok, out


def _check_toolchain(cfg: dict) -> tuple[bool, str]:
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return False, m
    ok, out = _run(_ssh_argv(cfg, "dotnet --version"), timeout=20)
    if ok:
        return True, (f".NET SDK present: {out or '(version printed)'} — "
                      "configure_winbox has run (verify-only; nothing installed).")
    return False, ("toolchain probe failed — run the admin `configure_winbox` "
                   f"skill to provision the box, then re-snapshot.\n{out}")


_CHECKS = {
    "domain": _check_domain,
    "ssh": _check_ssh,
    "transfer": _check_transfer,
    "snapshot": _check_snapshot,
    "toolchain": _check_toolchain,
}


def validate(check: str, cfg: dict) -> dict | None:
    """Run one live check against the CANDIDATE config in `cfg` (the in-progress
    wizard form, not necessarily what is saved). Returns {ok, output, check} or
    None for an unknown check name (→ 404 at the API layer)."""
    fn = _CHECKS.get(check)
    if fn is None:
        return None
    ok, out = fn(cfg or {})
    return {"ok": ok, "output": out or "(no output)", "check": check}


# -- the loop verbs (host-side; the broker exposes these over the socket) -----
#
# Verbs operate on the SAVED `vm` block — windows_devkit names a command, not a
# config. (validate() above is the exception: it tests a CANDIDATE block the
# wizard passes in, before it is saved.)

def do_exec(command: str, timeout: int = 120) -> dict:
    """Run one command in the guest over SSH. Returns {ok, ran, exit, stdout, stderr}."""
    cfg = read() or {}
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return {
            "ok": False,
            "error": "exec_validation_failed",
            "exit": -1,
            "stdout": "",
            "stderr": m,
        }
    if not str(command).strip():
        return {
            "ok": False,
            "error": "exec_validation_failed",
            "exit": -1,
            "stdout": "",
            "stderr": "exec: empty command",
        }
    try:
        # errors="replace": guest output is routinely non-UTF-8 (UTF-16 files,
        # OEM codepages). A strict decode turned the whole exec into a 500 with
        # no exit code or partial output (#261) — lossy beats fatal here; callers
        # needing byte-exact output base64 it guest-side.
        # The broker's exec verb deliberately accepts an arbitrary guest command;
        # _ssh_argv builds a fixed host-side ssh argv and never invokes a host shell.
        p = subprocess.run(_ssh_argv(cfg, command),  # codeql[py/command-line-injection]
                           capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return {"ok": p.returncode == 0, "ran": True, "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except FileNotFoundError as e:
        return {
            "ok": False,
            "error": "exec_unavailable",
            "exit": 127,
            "stdout": "",
            "stderr": (
                f"command not found: {e.filename} — is ssh installed on the host?"
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "exec_timeout",
            "exit": 124,
            "stdout": "",
            "stderr": f"timed out (>{timeout}s)",
        }


def _domain_state(cfg: dict) -> tuple[bool, str]:
    """Return a stable state from virsh stdout; stderr diagnostics are not state."""
    try:
        process = subprocess.run(
            _virsh(cfg, "domstate", str(cfg["domain"])),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOMAIN_STATE_TIMEOUT,
        )
    except FileNotFoundError as exc:
        return (
            False,
            f"command not found: {exc.filename} — is it installed on the host?",
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out (>{DOMAIN_STATE_TIMEOUT}s)"
    if process.returncode != 0:
        return False, (process.stdout + process.stderr).strip()
    lines = [
        line.strip().lower()
        for line in process.stdout.splitlines()
        if line.strip()
    ]
    return True, DOMAIN_STATES.get(lines[0], "unknown") if lines else "unknown"


def _ssh_ready(cfg: dict, timeout: int = 10) -> tuple[bool, str | None]:
    ok, output = _run(_ssh_argv(cfg, "echo ok"), timeout=timeout)
    return ok, None if ok else (output or "SSH readiness probe failed")


def _wait_for_ssh(cfg: dict, wait: int = START_READINESS_TIMEOUT,
                  interval: int = START_READINESS_INTERVAL) -> tuple[bool, int, str | None]:
    """Paced, bounded readiness loop owned by the non-resetting start verb."""
    deadline = time.monotonic() + max(1, wait)
    attempts = 0
    last_error: str | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, attempts, last_error
        attempts += 1
        ready, last_error = _ssh_ready(cfg, timeout=max(1, min(10, int(remaining))))
        if ready:
            return True, attempts, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, attempts, last_error
        time.sleep(min(interval, remaining))


def do_status() -> dict:
    """Observe broker-owned VM state without starting, restarting, or resetting."""
    cfg = read() or {}
    if m := _missing(cfg, "domain", "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "output": m}
    state_ok, state = _domain_state(cfg)
    if not state_ok:
        return {"ok": False, "output": state, "domain_state": "unknown"}
    ssh_ready, ssh_error = _ssh_ready(cfg)
    tunnel = mcp_status()
    return {
        "ok": True,
        "domain": str(cfg["domain"]),
        "domain_state": state,
        "ssh_ready": ssh_ready,
        "ssh_error": ssh_error,
        "mcp_tunnel_running": bool(tunnel["running"]),
        "mcp_tunnel_listening": bool(tunnel.get("listening")),
        "mcp_tunnel_unverified": bool(tunnel.get("unverified")),
    }


def do_start(wait: int = START_READINESS_TIMEOUT) -> dict:
    """Start only an off domain, then own the bounded SSH-readiness wait."""
    cfg = read() or {}
    if m := _missing(cfg, "domain", "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "output": m, "domain_state": "unknown", "attempts": 0}
    state_ok, state = _domain_state(cfg)
    if not state_ok:
        return {"ok": False, "output": state, "domain_state": "unknown", "attempts": 0}

    started = False
    if state == "powered_off":
        ok, output = _run(
            _virsh(cfg, "start", str(cfg["domain"])), timeout=DOMAIN_START_TIMEOUT
        )
        if not ok:
            return {
                "ok": False,
                "output": output or "failed to start the VM",
                "domain": str(cfg["domain"]),
                "domain_state": state,
                "started": False,
                "attempts": 0,
                "last_readiness_error": None,
            }
        started = True
        state = "running"
    elif state != "running":
        return {
            "ok": False,
            "output": f"domain is {state}; start only handles powered_off or running",
            "domain": str(cfg["domain"]),
            "domain_state": state,
            "started": False,
            "attempts": 0,
            "last_readiness_error": None,
        }

    ready, attempts, last_error = _wait_for_ssh(cfg, wait=wait)
    return {
        "ok": ready,
        "output": (
            f"SSH ready after {attempts} attempt(s)"
            if ready else f"SSH was not ready within {wait}s"
        ),
        "domain": str(cfg["domain"]),
        "domain_state": state,
        "started": started,
        "attempts": attempts,
        "last_readiness_error": last_error,
    }


def do_reset(running: bool = True) -> dict:
    """Revert to the clean snapshot. The clean snapshot is OFFLINE (this CPU's
    non-migratable invtsc flag refuses a live snapshot), so a bare revert lands
    powered-off. `running=True` adds `--running` to boot it — START a run from a
    clean booted box. `running=False` leaves it OFF — END a run clean *and*
    powered down in one op, so the 12 GB guest doesn't idle on the host."""
    cfg = read() or {}
    if m := _missing(cfg, "domain", "snapshot"):
        return {"ok": False, "output": m}
    argv = _virsh(cfg, "snapshot-revert", str(cfg["domain"]),
                  "--snapshotname", str(cfg["snapshot"]))
    if running:
        argv.append("--running")
    ok, out = _run(argv, timeout=RESET_COMMAND_TIMEOUT)
    state_ok, observed = _domain_state(cfg)
    expected = "running" if running else "powered_off"
    domain_state = observed if state_ok else "unknown"

    if not ok:
        timed_out = out == f"timed out (>{RESET_COMMAND_TIMEOUT}s)"
        return {
            "ok": False,
            "output": (
                f"the snapshot reset could not be confirmed before the "
                f"{RESET_COMMAND_TIMEOUT}s command timeout"
                if timed_out else out or "the snapshot revert was rejected"
            ),
            "domain": str(cfg["domain"]),
            "snapshot": str(cfg["snapshot"]),
            "domain_state": domain_state,
            "reset_outcome": "unknown" if timed_out else "rejected",
        }
    if not state_ok:
        return {
            "ok": False,
            "output": (
                "the snapshot reset result could not be confirmed because "
                "the final domain state could not be observed"
            ),
            "domain": str(cfg["domain"]),
            "snapshot": str(cfg["snapshot"]),
            "domain_state": "unknown",
            "reset_outcome": "unknown",
        }
    if observed != expected:
        return {
            "ok": False,
            "output": (
                f"snapshot reset did not reach the expected domain state; "
                f"observed {observed}, expected {expected}"
            ),
            "domain": str(cfg["domain"]),
            "snapshot": str(cfg["snapshot"]),
            "domain_state": observed,
            "reset_outcome": "state_mismatch",
        }
    state = "running" if running else "powered off"
    return {
        "ok": True,
        "output": out or f"reverted '{cfg['domain']}' to '{cfg['snapshot']}' ({state})",
        "domain": str(cfg["domain"]),
        "snapshot": str(cfg["snapshot"]),
        "domain_state": observed,
        "reset_outcome": "confirmed",
    }


def do_bake(shutdown_timeout: int = 180) -> dict:
    """(Re)bake the CLEAN snapshot: graceful shutdown → delete the old snapshot
    → snapshot-create-as OFFLINE. The one-command form of the deploy doc's
    'provision, then bake' step, run AFTER configure_winbox has provisioned +
    verified the toolchain.

    HOST-side only, and deliberately NOT a broker verb: the snapshot is the
    fork's trust anchor — every test run reverts to it. A sandboxed shell may
    exec/reset AGAINST the snapshot, but must never redefine it; if a
    compromised sandbox could re-bake, it could persist tampering across every
    future reset. So baking stays with the operator, where virsh lives."""
    if os.environ.get("SC_SANDBOX"):
        return {"ok": False, "output":
                "bake refuses to run in the sandbox — the clean snapshot is the "
                "trust anchor every test reverts to; only the HOST may redefine "
                "it. Ask the operator to run: ./sc vm-bake"}
    cfg = read() or {}
    if m := _missing(cfg, "domain", "snapshot"):
        return {"ok": False, "output": m}
    dom, snap = str(cfg["domain"]), str(cfg["snapshot"])
    steps = []

    ok, state = _run(_virsh(cfg, "domstate", dom), timeout=15)
    if not ok:
        return {"ok": False, "output": state}
    if "shut off" not in state:
        ok, out = _run(_virsh(cfg, "shutdown", dom), timeout=15)
        if not ok:
            return {"ok": False, "output": out}
        steps.append("graceful shutdown sent")
        deadline = time.monotonic() + shutdown_timeout
        while time.monotonic() < deadline:
            ok, state = _run(_virsh(cfg, "domstate", dom), timeout=15)
            if ok and "shut off" in state:
                break
            time.sleep(3)
        else:
            return {"ok": False, "output":
                    f"guest did not shut off within {shutdown_timeout}s (state: "
                    f"{state.strip()}) — the clean snapshot must be OFFLINE. "
                    f"Shut it down in the guest and re-run ./sc vm-bake"}

    ok, _out = _run(_virsh(cfg, "snapshot-info", dom, "--snapshotname", snap),
                    timeout=15)
    if ok:  # an old bake exists — replace, never stack
        ok, out = _run(_virsh(cfg, "snapshot-delete", dom,
                              "--snapshotname", snap), timeout=120)
        if not ok:
            return {"ok": False, "output": out}
        steps.append(f"deleted old '{snap}'")

    ok, out = _run(_virsh(cfg, "snapshot-create-as", dom, snap, "--description",
                          "pristine OS + toolchain (sc vm-bake)"), timeout=300)
    if not ok:
        return {"ok": False, "output": out}
    steps.append(f"baked '{snap}' (offline)")
    return {"ok": True,
            "output": "; ".join(steps) + " — guest left powered off"}


def do_push(src: str, dest: str | None = None) -> dict:
    """Stage a host-visible artifact into transfer_dir (the host side of the
    guest's virtio-fs share). `src` is a path in the bind-mounted repo; the guest
    sees the copy under its mapped share. The fast path — no scp, no guest auth.

    Contained by design: `src` must resolve inside the repo and `dest` must stay
    inside transfer_dir. The broker socket is reachable from the sandbox (same
    uid, socket in the bind-mount), so without these an in-sandbox caller could
    read host files (`src: ~/.ssh/...`) or write outside the share as the host
    user (`dest: ../../..`) — a sandbox→host escape. fs-perm (0600) gates other
    users, not the sandbox."""
    cfg = read() or {}
    if m := _missing(cfg, "transfer_dir"):
        return {"ok": False, "output": m}
    repo_root = ports.ENGINE.parent.resolve()
    src_p = Path(os.path.expanduser(str(src)))
    if not src_p.is_absolute():
        src_p = repo_root / src_p
    # The following filesystem operations use paths proven to remain within the
    # repo/transfer roots. CodeQL does not model Path.is_relative_to as a guard.
    src_p = src_p.resolve()  # codeql[py/path-injection]
    if not src_p.is_relative_to(repo_root):
        return {"ok": False, "output": f"push: src must be inside the repo: {src}"}
    if not src_p.is_file():  # codeql[py/path-injection]
        return {"ok": False, "output": f"push: source not found: {src_p}"}
    d = Path(os.path.expanduser(str(cfg["transfer_dir"]))).resolve()
    if not d.is_dir():
        return {"ok": False, "output": f"transfer_dir does not exist: {d}"}
    target = (d / (dest or src_p.name)).resolve()  # codeql[py/path-injection]
    if not target.is_relative_to(d):
        return {"ok": False, "output": f"push: dest escapes transfer_dir: {dest}"}
    try:
        shutil.copy2(src_p, target)  # codeql[py/path-injection]
    except OSError as e:
        return {"ok": False, "output": f"push failed: {e}"}
    return {
        "ok": True,
        "output": f"staged {src_p.name} -> {target} (guest sees it via the share)",
        "source": str(src_p),
        "destination": str(target),
    }


def do_capture(command: str | None = None) -> dict:
    """Collect installer/test state: optionally exec a command for its stdout,
    and always grab a `virsh screenshot` of the guest console (GUI installers
    show state on-screen, not on stdout). Screenshot returned base64."""
    cfg = read() or {}
    result: dict = {"ok": True}
    if command and str(command).strip():
        result["exec"] = do_exec(command)
        result["ok"] = bool(result["exec"].get("ok"))
    if m := _missing(cfg, "domain"):
        result["ok"] = False
        result["screenshot_error"] = m
        return result
    shot = Path(tempfile.gettempdir()) / f"sc_vm_{cfg['domain']}.ppm"
    ok, out = _run(_virsh(cfg, "screenshot", str(cfg["domain"]), str(shot)), timeout=30)
    if ok and shot.exists():
        data = shot.read_bytes()
        result["screenshot_b64"] = base64.b64encode(data).decode()
        result["screenshot_bytes"] = len(data)
        result["screenshot_format"] = "ppm"
        try:
            shot.unlink()
        except OSError:
            pass
    else:
        result["ok"] = False
        result["screenshot_error"] = out or "virsh screenshot produced no file"
    return result


# -- MCP tunnel (the GUI seam — broker-owned ssh forward, #263) ---------------
#
# Sandboxed seats cannot hold a live MCP session against the guest's Windows-MCP
# server: no ssh, no key, no route across libvirt NAT, and a host-loopback
# tunnel is invisible to the container. The seam: the broker (host-side, where
# the key lives) opens ONE `ssh -N -L` that forwards a UNIX SOCKET in the
# bind-mounted run/ dir straight to the guest's localhost-bound Windows-MCP
# port. OpenSSH does the byte plumbing — no HTTP proxying in the broker, so
# SSE/chunked streaming passes through untouched. In-sandbox, vm_mcp_relay.py
# bridges TCP→socket because `claude mcp add --transport http` only speaks TCP.

def _ssh_executable() -> str:
    return shutil.which("ssh") or "ssh"


def _tunnel_process() -> dict | None:
    return _owned_process(
        MCP_PIDFILE,
        kind="vm-mcp-tunnel",
        expected_executable=_ssh_executable(),
        required_token=str(MCP_SOCKET),
    )


def _tunnel_ready() -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.2)
    try:
        client.connect(str(MCP_SOCKET))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _tunnel_pid() -> int | None:
    """The verified tunnel pid, or None for absent/legacy/recycled state."""
    state = _tunnel_process()
    return state["pid"] if state else None


def mcp_status() -> dict:
    state = _tunnel_process()
    pid = state["pid"] if state else None
    listening = _tunnel_ready()
    running = state is not None
    owns_listener = bool(
        state
        and listening
        and _process_owns_unix_listener(state["pid"], MCP_SOCKET)
    )
    return {"ok": True, "running": running, "pid": pid,
            "socket": str(MCP_SOCKET) if listening else None,
            "listening": listening,
            "unverified": listening and not owns_listener}


def do_mcp_up(wait: float = 15) -> dict:
    """Open the MCP tunnel. Idempotent — an already-live tunnel is reported, not
    doubled. The forward target is the SAVED block's `mcp_port` (default 8000,
    what `windows_vm_gui`'s guest prep bakes), never a caller-named port — same
    rule as every other verb: the sandbox names an action, not a destination."""
    cfg = read() or {}
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "output": m}
    with _process_state_lock(MCP_LOCKFILE):
        state = _tunnel_process()
        ready = _tunnel_ready()
        if state and ready:
            if _process_record_matches(
                state,
                expected_executable=_ssh_executable(),
                required_token=str(MCP_SOCKET),
            ) and _process_owns_unix_listener(state["pid"], MCP_SOCKET):
                return {
                    "ok": True,
                    "running": True,
                    "listening": True,
                    "unverified": False,
                    "output": f"tunnel already up (pid {state['pid']})",
                    "socket": str(MCP_SOCKET),
                    "pid": state["pid"],
                    "port": int(state.get("port", cfg.get("mcp_port", 8000))),
                }
            return {
                "ok": False,
                "running": True,
                "unverified": True,
                "socket": str(MCP_SOCKET),
                "output": "tunnel socket is held by an unverified process; refusing to start",
            }
        if state is None and ready:
            return {
                "ok": False,
                "running": True,
                "unverified": True,
                "socket": str(MCP_SOCKET),
                "output": "tunnel socket is held by an unverified process; refusing to start",
            }
        if state and not _terminate_owned_process(
            state,
            expected_executable=_ssh_executable(),
            required_token=str(MCP_SOCKET),
        ):
            return {"ok": False, "output": "verified stale tunnel did not stop"}
        MCP_PIDFILE.unlink(missing_ok=True)
        MCP_SOCKET.unlink(missing_ok=True)

        port = int(cfg.get("mcp_port", 8000))
        key = os.path.expanduser(str(cfg.get("ssh_key_path", "")))
        argv = [
            "ssh", "-i", key,
            "-p", str(cfg.get("ssh_port", 22)),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "StreamLocalBindUnlink=yes",
            "-o", "StreamLocalBindMask=0177",
            "-N", "-L", f"{MCP_SOCKET}:127.0.0.1:{port}",
            f"{cfg.get('ssh_user')}@{cfg.get('ssh_host')}",
        ]
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(MCP_LOG, "wb") as log:
                p = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
        except FileNotFoundError as e:
            return {
                "ok": False,
                "output": f"command not found: {e.filename} — is ssh installed on the host?",
            }
        state = _new_process_state(
            p.pid,
            kind="vm-mcp-tunnel",
            expected_executable=_ssh_executable(),
            required_token=str(MCP_SOCKET),
            port=port,
        )
        if state is None:
            return {
                "ok": False,
                "output": _unverified_process_error(
                    p,
                    label="ssh tunnel",
                    expected_executable=_ssh_executable(),
                    log_path=MCP_LOG,
                ),
            }
        _atomic_write_process_state(MCP_PIDFILE, state)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if p.poll() is not None:
                err = _bounded_log_tail(MCP_LOG)
                MCP_PIDFILE.unlink(missing_ok=True)
                MCP_SOCKET.unlink(missing_ok=True)
                return {
                    "ok": False,
                    "output": f"ssh tunnel exited (rc {p.returncode}): {err or '(no output)'}",
                }
            if _tunnel_ready():
                if not _process_record_matches(
                    state,
                    expected_executable=_ssh_executable(),
                    required_token=str(MCP_SOCKET),
                ):
                    error = _unverified_process_error(
                        p,
                        label="ssh tunnel",
                        expected_executable=_ssh_executable(),
                        log_path=MCP_LOG,
                    )
                    MCP_PIDFILE.unlink(missing_ok=True)
                    remaining = _tunnel_ready()
                    if not remaining:
                        MCP_SOCKET.unlink(missing_ok=True)
                    return {
                        "ok": False,
                        "running": remaining,
                        "unverified": remaining,
                        "socket": str(MCP_SOCKET) if remaining else None,
                        "output": (
                            f"{error}; tunnel socket remains live and unverified"
                            if remaining else error
                        ),
                    }
                if _process_owns_unix_listener(p.pid, MCP_SOCKET):
                    return {
                        "ok": True,
                        "running": True,
                        "listening": True,
                        "unverified": False,
                        "output": f"tunnel up — {MCP_SOCKET} -> guest 127.0.0.1:{port}",
                        "socket": str(MCP_SOCKET),
                        "pid": p.pid,
                        "port": port,
                    }
            time.sleep(0.2)
        stopped = _terminate_owned_process(
            state,
            expected_executable=_ssh_executable(),
            required_token=str(MCP_SOCKET),
        )
        err = _bounded_log_tail(MCP_LOG)
        if stopped:
            MCP_PIDFILE.unlink(missing_ok=True)
            MCP_SOCKET.unlink(missing_ok=True)
        detail = f": {err}" if err else ""
        return {
            "ok": False,
            "output": f"tunnel socket did not appear within {wait}s{detail}",
        }


def do_mcp_down() -> dict:
    """Close the MCP tunnel. Idempotent — safe to call with nothing running."""
    with _process_state_lock(MCP_LOCKFILE):
        state = _tunnel_process()
        stale = MCP_PIDFILE.exists() and state is None
        ready = _tunnel_ready()
        if state and not _terminate_owned_process(
            state,
            expected_executable=_ssh_executable(),
            required_token=str(MCP_SOCKET),
        ):
            return {
                "ok": False,
                "output": f"verified tunnel did not stop (pid {state['pid']})",
            }
        MCP_PIDFILE.unlink(missing_ok=True)
        if state is None and ready:
            return {
                "ok": False,
                "running": True,
                "unverified": True,
                "socket": str(MCP_SOCKET),
                "output": "unverified tunnel is still listening; state removed, process not signaled",
            }
        MCP_SOCKET.unlink(missing_ok=True)
        if state:
            output = f"tunnel stopped (pid {state['pid']})"
        elif stale:
            output = "tunnel not running (stale state removed)"
        else:
            output = "tunnel not running"
        return {"ok": True, "output": output}


# -- client: HTTP over the broker's unix socket ------------------------------


class BrokerConnectionError(ConnectionError):
    """The broker transport failed; request_sent marks uncertain mutations."""

    def __init__(self, message: str, *, request_sent: bool) -> None:
        super().__init__(message)
        self.request_sent = request_sent


class BrokerTimeoutError(BrokerConnectionError):
    """The broker transport exceeded its deadline."""


class BrokerResponseError(ConnectionError):
    """The broker returned an empty, partial, or malformed HTTP/JSON response."""

def broker_call(method: str, path: str, body: dict | None = None,
                timeout: int = 130) -> dict:
    """Speak HTTP/1.1 to the broker over its unix socket and return parsed JSON.
    Raises ConnectionError if the broker is not listening (so callers can render
    a 'start the broker' hint). Used by the in-sandbox server to proxy validate."""
    payload = b"" if body is None else json.dumps(body).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: vm-broker\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + payload
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    request_sent = False
    try:
        s.connect(str(SOCKET))
        # Once send begins, a state-changing request may have reached the
        # broker even if the connection fails before sendall returns.
        request_sent = True
        s.sendall(req)
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except TimeoutError as e:
        raise BrokerTimeoutError(
            f"vm-broker timed out after {timeout}s",
            request_sent=request_sent,
        ) from e
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise BrokerConnectionError(
            f"vm-broker not reachable at {SOCKET}: {e}",
            request_sent=request_sent,
        ) from e
    finally:
        s.close()

    raw = b"".join(chunks)
    raw_head, separator, raw_body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise BrokerResponseError("broker response did not contain HTTP headers")
    content_length: int | None = None
    for line in raw_head.split(b"\r\n")[1:]:
        name, colon, value = line.partition(b":")
        if colon and name.strip().lower() == b"content-length":
            try:
                content_length = int(value.strip())
            except ValueError as exc:
                raise BrokerResponseError("broker response had invalid Content-Length") from exc
    if content_length is not None and len(raw_body) != content_length:
        raise BrokerResponseError("broker response body was incomplete")
    if not raw_body:
        raise BrokerResponseError("broker response body was empty")
    try:
        decoded = json.loads(raw_body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerResponseError("broker response was not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise BrokerResponseError("broker response JSON was not an object")
    return decoded


def _bounded(value: object) -> object:
    """Bound public error details and exclude accidental unbounded structures."""
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        return {str(key)[:80]: _bounded(item) for key, item in list(value.items())[:12]}
    if isinstance(value, list):
        return [_bounded(item) for item in value[:20]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def operation_success(operation: str, result: dict) -> dict:
    """Stable public success envelope shared by every model-facing VM client."""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "operation": operation,
        "result": result,
        "error": None,
    }


def operation_error(operation: str, code: str, message: str,
                    details: dict | None = None) -> dict:
    """Stable public error envelope with bounded, credential-free details."""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": False,
        "operation": operation,
        "result": None,
        "error": {
            "code": code,
            "message": message[:500],
            "details": _bounded(details or {}),
        },
    }


class CaptureArtifactError(ValueError):
    """A safe, structured capture validation or materialization failure."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _capture_target(output: str | None, screenshot_format: str) -> Path:
    root = CAPTURE_ARTIFACT_ROOT.resolve()
    if output is None:
        target = root / f"capture-{time.time_ns()}.{screenshot_format}"
    else:
        target = Path(os.path.expanduser(output))
        if not target.is_absolute():
            target = ports.ENGINE.parent / target
    try:
        target = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureArtifactError(
            "capture_output_invalid",
            "the capture output path is invalid",
            {"allowed_root": str(root)},
        ) from exc
    if target == root or not target.is_relative_to(root):
        raise CaptureArtifactError(
            "capture_output_not_allowed",
            "the capture output must stay inside the capture artifact area",
            {"allowed_root": str(root)},
        )
    return target


def _decode_capture(response: dict) -> tuple[bytes, str, str]:
    encoded = response.get("screenshot_b64")
    declared_bytes = response.get("screenshot_bytes")
    screenshot_format = response.get("screenshot_format")
    if (
        not isinstance(encoded, str)
        or isinstance(declared_bytes, bool)
        or not isinstance(declared_bytes, int)
        or not isinstance(screenshot_format, str)
    ):
        raise CaptureArtifactError(
            "capture_response_invalid",
            "the broker did not return complete capture metadata",
        )
    screenshot_format = screenshot_format.lower()
    mime_type = CAPTURE_MIME_TYPES.get(screenshot_format)
    if mime_type is None or declared_bytes <= 0:
        raise CaptureArtifactError(
            "capture_response_invalid",
            "the broker returned invalid capture metadata",
        )
    encoded_limit = 4 * ((MAX_CAPTURE_BYTES + 2) // 3)
    if declared_bytes > MAX_CAPTURE_BYTES or len(encoded) > encoded_limit:
        raise CaptureArtifactError(
            "capture_too_large",
            f"the capture exceeds the {MAX_CAPTURE_BYTES}-byte limit",
            {"max_bytes": MAX_CAPTURE_BYTES, "reported_bytes": declared_bytes},
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CaptureArtifactError(
            "capture_response_invalid",
            "the broker returned invalid capture data",
        ) from exc
    if len(data) > MAX_CAPTURE_BYTES:
        raise CaptureArtifactError(
            "capture_too_large",
            f"the capture exceeds the {MAX_CAPTURE_BYTES}-byte limit",
            {"max_bytes": MAX_CAPTURE_BYTES, "reported_bytes": declared_bytes},
        )
    if len(data) != declared_bytes:
        raise CaptureArtifactError(
            "capture_response_invalid",
            "the capture byte count did not match the broker metadata",
            {"reported_bytes": declared_bytes, "decoded_bytes": len(data)},
        )
    return data, screenshot_format, mime_type


def _atomic_write_capture(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _materialize_capture(response: dict, output: str | None,
                         validated_target: Path | None) -> dict:
    try:
        data, screenshot_format, mime_type = _decode_capture(response)
        target = validated_target or _capture_target(output, screenshot_format)
        _atomic_write_capture(target, data)
    except CaptureArtifactError as exc:
        return operation_error("capture", exc.code, str(exc), exc.details)
    except OSError:
        return operation_error(
            "capture",
            "capture_write_failed",
            "the capture artifact could not be saved",
        )
    return operation_success("capture", {
        "path": str(target),
        "bytes": len(data),
        "format": screenshot_format,
        "mime_type": mime_type,
    })


def active_mcp_adapter() -> dict:
    """Describe the launched harness's declared Windows MCP capability."""
    harness = os.environ.get("SC_HARNESS") or ports.resolve(persist=False).get(
        "harness"
    )
    if not harness:
        return {
            "harness": None,
            "state": "unknown",
            "supported": False,
            "reason": "SC_HARNESS is not set",
            "server_name": None,
        }
    path = ports.ENGINE / "adapters" / harness / "adapter.json"
    try:
        adapter = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "harness": harness,
            "state": "unknown",
            "supported": False,
            "reason": "the active harness adapter could not be read",
            "server_name": None,
        }
    streamable = (adapter.get("mcp") or {}).get("streamable_http") or {}
    supported = streamable.get("supported") is True
    managed = streamable.get("managed_server") or {}
    return {
        "harness": harness,
        "state": "supported" if supported else "unsupported",
        "supported": supported,
        "reason": None if supported else str(
            streamable.get("reason") or "streamable HTTP MCP is unsupported"
        ),
        "server_name": managed.get("name") if supported else None,
    }


def _mcp_response_message(raw: bytes, content_type: str) -> dict | None:
    """Extract one bounded JSON-RPC response from JSON or SSE transport output."""
    candidates = [raw]
    if content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
        candidates = [
            line.removeprefix(b"data:").strip()
            for line in raw.splitlines()
            if line.startswith(b"data:")
        ]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("id") == 1:
            return value
    return None


def _close_mcp_probe_session(port: int, session_id: str, protocol: str) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "DELETE",
            MCP_ENDPOINT_PATH,
            headers={
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": protocol,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        response.read(1024)
        return int(response.status) in {200, 202, 204, 404, 405}
    except (OSError, TimeoutError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def mcp_endpoint_status(port: int = MCP_RELAY_PORT) -> dict:
    """Verify the managed endpoint with a bounded MCP initialization handshake."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "super-coder-endpoint-probe",
                    "version": "1",
                },
            },
        }).encode()
        connection.request(
            "POST",
            MCP_ENDPOINT_PATH,
            body=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MCP_PROBE_LIMIT)
        status = int(response.status)
        message = _mcp_response_message(
            raw, str(response.getheader("Content-Type") or "")
        )
        result = message.get("result") if message else None
        protocol = result.get("protocolVersion") if isinstance(result, dict) else None
        ready = status == 200 and isinstance(protocol, str) and bool(protocol)
        session_id = response.getheader("Mcp-Session-Id")
        if ready and session_id:
            ready = _close_mcp_probe_session(port, session_id, protocol)
            if not ready:
                error = "MCP initialized but its probe session did not close"
            else:
                error = None
        elif ready:
            error = None
        elif message and isinstance(message.get("error"), dict):
            error = str(message["error"].get("message") or "MCP initialization failed")
        elif status != 200:
            error = f"unexpected HTTP status {status}"
        else:
            error = "response did not contain an MCP initialization result"
        return {
            "url": f"http://127.0.0.1:{port}{MCP_ENDPOINT_PATH}",
            "ready": ready,
            "http_status": status,
            "error": None if ready else error[:500],
        }
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        return {
            "url": f"http://127.0.0.1:{port}{MCP_ENDPOINT_PATH}",
            "ready": False,
            "http_status": None,
            "error": str(exc)[:500],
        }
    finally:
        connection.close()


def _relay_module():
    import vm_mcp_relay

    return vm_mcp_relay


def _public_tunnel(response: dict) -> dict:
    return {
        "running": bool(response.get("running")),
        "listening": bool(response.get("listening")),
        "unverified": bool(response.get("unverified")),
    }


def _public_relay(response: dict) -> dict:
    return {
        "running": bool(response.get("running")),
        "listening": bool(response.get("listening")),
        "unverified": bool(response.get("unverified")),
        "port": response.get("port", MCP_RELAY_PORT),
    }


def _mcp_snapshot(tunnel_response: dict) -> dict:
    relay = _relay_module().status(MCP_RELAY_PORT)
    tunnel = _public_tunnel(tunnel_response)
    public_relay = _public_relay(relay)
    endpoint = (
        mcp_endpoint_status(MCP_RELAY_PORT)
        if tunnel["running"]
        and tunnel["listening"]
        and not tunnel["unverified"]
        and public_relay["running"]
        and public_relay["listening"]
        and not public_relay["unverified"]
        else {
            "url": f"http://127.0.0.1:{MCP_RELAY_PORT}{MCP_ENDPOINT_PATH}",
            "ready": False,
            "http_status": None,
            "error": "tunnel and relay are not both verified",
        }
    )
    return {
        "adapter": active_mcp_adapter(),
        "tunnel": tunnel,
        "relay": public_relay,
        "endpoint": endpoint,
    }


def _mcp_broker_call(method: str, path: str) -> tuple[dict | None, dict | None]:
    try:
        return (
            broker_call(method, path, None, timeout=DEFAULT_CLIENT_TIMEOUT),
            None,
        )
    except BrokerTimeoutError:
        return None, operation_error(
            "mcp", "mcp_timeout", "the MCP broker operation timed out"
        )
    except BrokerConnectionError:
        return None, operation_error(
            "mcp", "broker_unreachable", "the VM broker is not reachable"
        )
    except BrokerResponseError:
        return None, operation_error(
            "mcp", "broker_response_invalid",
            "the VM broker did not return a complete response",
        )


def run_mcp_operation(action: str) -> dict:
    """Inspect or control the complete managed Windows MCP transport."""
    operation = f"mcp_{action}"
    if action not in {"status", "up", "down"}:
        return operation_error(
            operation, "operation_unknown", "unknown MCP operation"
        )

    adapter = active_mcp_adapter()
    if action == "up" and not adapter["supported"]:
        return operation_error(
            operation,
            "mcp_adapter_unsupported",
            adapter["reason"] or "the active harness does not support Windows MCP",
            {"harness": adapter["harness"], "adapter_state": adapter["state"]},
        )

    if action == "status":
        tunnel_response, error = _mcp_broker_call("GET", "/mcp/status")
        if error:
            error["operation"] = operation
            return error
        if not tunnel_response or not tunnel_response.get("ok"):
            return _broker_failure(operation, tunnel_response or {})
        return operation_success(operation, _mcp_snapshot(tunnel_response))

    relay_module = _relay_module()
    if action == "up":
        tunnel_response, error = _mcp_broker_call("POST", "/mcp/up")
        if error:
            error["operation"] = operation
            return error
        if not tunnel_response or not tunnel_response.get("ok"):
            return _broker_failure(operation, tunnel_response or {})

        relay_response = relay_module.up(MCP_RELAY_PORT)
        if not relay_response.get("ok"):
            tunnel_cleanup, _ = _mcp_broker_call("POST", "/mcp/down")
            return operation_error(
                operation,
                "mcp_relay_failed",
                str(relay_response.get("output") or "the MCP relay did not start"),
                {
                    "relay": _public_relay(relay_response),
                    "tunnel_cleanup": _public_tunnel(tunnel_cleanup or {}),
                },
            )

        endpoint = mcp_endpoint_status(MCP_RELAY_PORT)
        if not endpoint["ready"]:
            relay_cleanup = relay_module.down(MCP_RELAY_PORT)
            tunnel_cleanup, _ = _mcp_broker_call("POST", "/mcp/down")
            return operation_error(
                operation,
                "mcp_endpoint_unavailable",
                "the managed Windows MCP endpoint did not answer a verification probe",
                {
                    "endpoint": endpoint,
                    "relay_cleanup": relay_cleanup,
                    "tunnel_cleanup": tunnel_cleanup or {},
                },
            )

        return operation_success(operation, {
            "adapter": adapter,
            "tunnel": _public_tunnel(tunnel_response),
            "relay": _public_relay(relay_response),
            "endpoint": endpoint,
        })

    relay_response = relay_module.down(MCP_RELAY_PORT)
    tunnel_response, error = _mcp_broker_call("POST", "/mcp/down")
    if error:
        error["operation"] = operation
        error["error"]["details"] = {
            "relay_cleanup": _bounded(relay_response),
        }
        return error
    if not relay_response.get("ok") or not (tunnel_response or {}).get("ok"):
        return operation_error(
            operation,
            "mcp_down_incomplete",
            "the MCP relay and tunnel did not both confirm shutdown",
            {
                "relay_cleanup": relay_response,
                "tunnel_cleanup": tunnel_response or {},
            },
        )
    return operation_success(operation, {
        "adapter": adapter,
        "relay_cleanup": relay_response,
        "tunnel_cleanup": tunnel_response,
        "endpoint": {
            "url": f"http://127.0.0.1:{MCP_RELAY_PORT}{MCP_ENDPOINT_PATH}",
            "ready": False,
            "http_status": None,
            "error": "transport stopped",
        },
    })


def _broker_failure(operation: str, response: dict) -> dict:
    if response.get("error") == "vm_busy":
        return operation_error(
            operation,
            "vm_busy",
            str(response.get("output") or "another VM mutation is still running"),
            {"wait_seconds": response.get("wait_seconds")},
        )
    if operation == "reset" and response.get("reset_outcome") == "unknown":
        return operation_error(
            operation,
            "reset_result_unknown",
            str(
                response.get("output")
                or "the snapshot reset could not be confirmed"
            ),
            {"domain_state": response.get("domain_state", "unknown")},
        )
    if operation == "exec":
        exit_code = response.get("exit")
        stderr = response.get("stderr")
        error_code = response.get("error")
        if error_code not in {
            "exec_validation_failed",
            "exec_unavailable",
            "exec_timeout",
        }:
            error_code = "exec_failed"
        return operation_error(
            operation,
            error_code,
            (
                stderr
                if isinstance(stderr, str) and stderr
                else "the exec operation failed"
            ),
            {
                "exit_code": (
                    exit_code
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else -1
                ),
            },
        )
    details: dict = {}
    for key in ("domain_state", "attempts", "last_readiness_error"):
        if key in response:
            details[key] = response[key]
    return operation_error(
        operation,
        f"{operation}_failed",
        str(
            response.get("output")
            or response.get("screenshot_error")
            or response.get("error")
            or f"{operation} failed"
        ),
        details,
    )


def run_operation(operation: str, *, command: str | None = None,
                  src: str | None = None, dest: str | None = None,
                  output: str | None = None) -> dict:
    """Call one core broker operation once and normalize its public result."""
    validated_target: Path | None = None
    if operation == "exec" and (not isinstance(command, str) or not command.strip()):
        return operation_error(
            operation, "exec_command_invalid", "the guest command is empty"
        )
    if operation == "push" and (not isinstance(src, str) or not src):
        return operation_error(
            operation, "push_source_invalid", "the push source is empty"
        )
    if operation == "capture" and output is not None:
        try:
            validated_target = _capture_target(output, "ppm")
        except CaptureArtifactError as exc:
            return operation_error(operation, exc.code, str(exc), exc.details)
    calls = {
        "status": ("GET", "/status", None, DEFAULT_CLIENT_TIMEOUT),
        "start": ("POST", "/start", None, START_CLIENT_TIMEOUT),
        "reset": ("POST", "/reset", {"running": False}, RESET_CLIENT_TIMEOUT),
        "exec": ("POST", "/exec", {"command": command}, EXEC_CLIENT_TIMEOUT),
        "push": (
            "POST", "/push", {"src": src, "dest": dest}, DEFAULT_CLIENT_TIMEOUT
        ),
        "capture": ("POST", "/capture", {}, CAPTURE_CLIENT_TIMEOUT),
    }
    if operation not in calls:
        return operation_error(operation, "operation_unknown", "unknown VM operation")
    method, path, body, timeout = calls[operation]
    try:
        response = broker_call(method, path, body, timeout=timeout)
    except BrokerTimeoutError as exc:
        if operation == "reset" and exc.request_sent:
            return operation_error(
                operation,
                "reset_result_unknown",
                "the broker connection closed before reset could be confirmed",
                {"domain_state": "unknown"},
            )
        if operation == "reset":
            return operation_error(
                operation,
                "broker_unreachable",
                "the VM broker is not reachable",
                {},
            )
        return operation_error(
            operation,
            f"{operation}_timeout",
            f"the VM {operation} operation timed out after {timeout}s",
            {"timeout_seconds": timeout},
        )
    except BrokerConnectionError as exc:
        if operation == "reset" and exc.request_sent:
            return operation_error(
                operation,
                "reset_result_unknown",
                "the broker connection closed before reset could be confirmed",
                {"domain_state": "unknown"},
            )
        return operation_error(
            operation,
            "broker_unreachable",
            "the VM broker is not reachable",
            {},
        )
    except BrokerResponseError:
        if operation == "reset":
            return operation_error(
                operation,
                "reset_result_unknown",
                "the broker connection closed before reset could be confirmed",
                {"domain_state": "unknown"},
            )
        return operation_error(
            operation,
            "broker_response_invalid",
            "the VM broker did not return a complete response",
            {},
        )

    if response.get("error") == "vm_busy":
        return _broker_failure(operation, response)
    if operation == "exec" and response.get("ran") is True:
        exit_code = response.get("exit")
        stdout = response.get("stdout")
        stderr = response.get("stderr")
        if (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
        ):
            return operation_error(
                operation,
                "broker_response_invalid",
                "the VM broker did not return a complete response",
            )
        return operation_success(
            operation,
            {"exit_code": exit_code, "stdout": stdout, "stderr": stderr},
        )
    if not response.get("ok"):
        return _broker_failure(operation, response)
    try:
        if operation == "status":
            mcp = _mcp_snapshot({
                "running": response["mcp_tunnel_running"],
                "listening": response.get(
                    "mcp_tunnel_listening",
                    response["mcp_tunnel_running"]
                    or response.get("mcp_tunnel_unverified", False),
                ),
                "unverified": response.get("mcp_tunnel_unverified", False),
            })
            return operation_success(operation, {
                "broker": {"ready": True},
                "domain": {
                    "name": response["domain"],
                    "state": response["domain_state"],
                },
                "ssh": {
                    "ready": bool(response["ssh_ready"]),
                    "last_error": response.get("ssh_error"),
                },
                "mcp": {
                    "tunnel_running": mcp["tunnel"]["running"],
                    "tunnel_listening": mcp["tunnel"]["listening"],
                    "unverified": mcp["tunnel"]["unverified"],
                },
                "relay": mcp["relay"],
                "endpoint": mcp["endpoint"],
                "adapter": mcp["adapter"],
            })
        if operation == "start":
            return operation_success(operation, {
                "domain": {
                    "name": response["domain"],
                    "state": response["domain_state"],
                },
                "started": bool(response["started"]),
                "ssh": {
                    "ready": True,
                    "attempts": int(response["attempts"]),
                    "last_error": None,
                },
            })
        if operation == "push":
            source = response["source"]
            destination = response["destination"]
            if not isinstance(source, str) or not isinstance(destination, str):
                raise TypeError
            return operation_success(operation, {
                "source": source,
                "destination": destination,
            })
        if operation == "capture":
            return _materialize_capture(response, output, validated_target)
        return operation_success(operation, {
            "domain": {
                "name": response["domain"],
                "state": response["domain_state"],
            },
            "snapshot": response["snapshot"],
        })
    except (KeyError, TypeError, ValueError):
        if operation == "reset":
            return operation_error(
                operation,
                "reset_result_unknown",
                "the broker connection closed before reset could be confirmed",
                {"domain_state": "unknown"},
            )
        return operation_error(
            operation,
            "broker_response_invalid",
            "the VM broker did not return the required result fields",
            {},
        )


def _human_result(value: dict) -> str:
    if not value["ok"]:
        error = value["error"]
        return f"✗ {value['operation']} [{error['code']}]: {error['message']}"
    operation = value["operation"]
    result = value["result"]
    if operation == "status":
        ssh = "ready" if result["ssh"]["ready"] else "not ready"
        mcp = (
            "MCP tunnel unverified"
            if result["mcp"]["unverified"]
            else (
                "MCP tunnel running"
                if result["mcp"]["tunnel_running"]
                else "MCP tunnel not running"
            )
        )
        relay = (
            "relay unverified"
            if result["relay"]["unverified"]
            else ("relay running" if result["relay"]["running"] else "relay stopped")
        )
        endpoint = (
            "endpoint ready" if result["endpoint"]["ready"] else "endpoint unavailable"
        )
        adapter = result["adapter"]
        adapter_text = (
            f"{adapter['harness']} adapter supported"
            if adapter["supported"]
            else f"{adapter['harness'] or 'unknown'} adapter {adapter['state']}"
        )
        return (
            f"VM {result['domain']['state']} · SSH {ssh} · broker ready · "
            f"{mcp} · {relay} · {endpoint} · {adapter_text}"
        )
    if operation == "mcp_status":
        tunnel = result["tunnel"]
        relay = result["relay"]
        return (
            f"MCP tunnel {'unverified' if tunnel['unverified'] else ('running' if tunnel['running'] else 'stopped')} · "
            f"relay {'unverified' if relay['unverified'] else ('running' if relay['running'] else 'stopped')} · "
            f"endpoint {'ready' if result['endpoint']['ready'] else 'unavailable'} · "
            f"adapter {result['adapter']['state']}"
        )
    if operation == "mcp_up":
        return (
            f"Windows MCP ready at {result['endpoint']['url']} · "
            f"adapter {result['adapter']['harness']}"
        )
    if operation == "mcp_down":
        return "Windows MCP relay stopped · tunnel stopped"
    if operation == "start":
        action = "started" if result["started"] else "already running"
        return f"VM {action} · SSH ready after {result['ssh']['attempts']} attempt(s)"
    if operation == "exec":
        lines = [f"Guest command exited {result['exit_code']}"]
        if result["stdout"]:
            lines.append(f"stdout:\n{result['stdout'].rstrip()}")
        if result["stderr"]:
            lines.append(f"stderr:\n{result['stderr'].rstrip()}")
        return "\n".join(lines)
    if operation == "push":
        return f"Staged {result['source']} -> {result['destination']}"
    if operation == "capture":
        return (
            f"Capture saved to {result['path']} "
            f"({result['bytes']} bytes, {result['mime_type']})"
        )
    return f"VM reset to '{result['snapshot']}' · {result['domain']['state']}"


def client_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="./sc vm",
        description="Observe and control the configured Windows test VM.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    status = commands.add_parser(
        "status",
        help="observe VM readiness without mutation",
        description=(
            "Read-only status. JSON result fields: broker.ready; domain.name, "
            "domain.state; ssh.ready, ssh.last_error; mcp.tunnel_running, "
            "mcp.tunnel_listening, mcp.unverified; relay.running, "
            "relay.listening, relay.unverified; endpoint.ready, "
            "endpoint.http_status; adapter.state, adapter.supported."
        ),
    )
    status.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    start = commands.add_parser(
        "start",
        help="start only when off and wait for SSH",
        description=(
            "Non-resetting start. JSON result fields: domain.name, domain.state; "
            "started; ssh.ready, ssh.attempts, ssh.last_error."
        ),
    )
    start.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    reset = commands.add_parser(
        "reset",
        help="restore the testing snapshot and leave the VM off",
        description=(
            "Powered-off reset. JSON result fields: domain.name, domain.state; "
            "snapshot."
        ),
    )
    reset.add_argument(
        "--off",
        action="store_true",
        required=True,
        help="required: leave the restored VM powered off",
    )
    reset.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    push = commands.add_parser(
        "push",
        help="stage a permitted local artifact for the guest",
        description=(
            "Stage a file from this repo through the configured transfer area. "
            "JSON result fields: source, destination."
        ),
    )
    push.add_argument("src", help="source file inside the repo")
    push.add_argument("dest", nargs="?", help="optional path inside transfer_dir")
    push.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    execute = commands.add_parser(
        "exec",
        help="execute one exact guest command",
        description=(
            "Execute arguments after -- or the exact UTF-8 contents of one "
            "--command-file. JSON result fields: exit_code, stdout, stderr."
        ),
    )
    execute.add_argument(
        "--command-file",
        help="read the exact guest command from this UTF-8 file",
    )
    execute.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    execute.add_argument(
        "command", nargs=argparse.REMAINDER, metavar="COMMAND",
        help="guest command arguments following --",
    )
    capture = commands.add_parser(
        "capture",
        help="save a bounded screenshot as a local artifact",
        description=(
            "Atomically save a mode-0600 screenshot under "
            ".sc-state/local/vm-captures by default. An explicit --output must "
            "stay under .sc-state/local/vm-captures. JSON result fields: path, "
            "bytes, format, mime_type."
        ),
    )
    capture.add_argument(
        "--output", help="artifact path under .sc-state/local/vm-captures"
    )
    capture.add_argument(
        "--json", action="store_true", help="print one JSON result object"
    )
    mcp = commands.add_parser(
        "mcp",
        help="inspect or control the managed Windows MCP transport",
        description=(
            "Manage the adapter-declared Windows MCP endpoint without starting "
            "or resetting the VM. status is read-only; up verifies tunnel, "
            "relay, and HTTP endpoint; down reports relay and tunnel cleanup "
            "separately."
        ),
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_action", required=True)
    for action, help_text in (
        ("status", "inspect adapter, tunnel, relay, and endpoint"),
        ("up", "start verified tunnel and relay, then probe the endpoint"),
        ("down", "stop verified relay and tunnel instances"),
    ):
        command = mcp_commands.add_parser(action, help=help_text)
        command.add_argument(
            "--json", action="store_true", help="print one JSON result object"
        )
    args = parser.parse_args(argv)
    if args.operation == "mcp":
        value = run_mcp_operation(args.mcp_action)
    elif args.operation == "exec":
        command_parts = args.command
        if command_parts[:1] == ["--"]:
            command_parts = command_parts[1:]
        if args.command_file and command_parts:
            value = operation_error(
                "exec",
                "exec_arguments_invalid",
                "use either arguments after -- or --command-file, not both",
            )
        elif args.command_file:
            try:
                command = Path(args.command_file).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                value = operation_error(
                    "exec",
                    "exec_command_file_invalid",
                    "the command file could not be read as UTF-8",
                )
            else:
                value = run_operation("exec", command=command)
        else:
            value = run_operation("exec", command=" ".join(command_parts))
    elif args.operation == "push":
        value = run_operation("push", src=args.src, dest=args.dest)
    elif args.operation == "capture":
        value = run_operation("capture", output=args.output)
    else:
        value = run_operation(args.operation)
    if args.json:
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(_human_result(value), file=sys.stdout if value["ok"] else sys.stderr)
    return 0 if value["ok"] else 1


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) ------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "client":
        return client_main(argv[1:])
    if mode == "sock":
        print(SOCKET)
    elif mode == "configured":
        # exit 0 if this fork has linked a VM (so the launch hook can self-skip)
        return 0 if read() else 1
    elif mode == "exec":
        print(json.dumps(do_exec(" ".join(argv[1:]))))
    elif mode == "reset":
        # `vm.py reset` boots clean; `vm.py reset off` lands clean + powered off
        print(json.dumps(do_reset(running=(argv[1:2] != ["off"]))))
    elif mode == "push":
        print(json.dumps(do_push(argv[1] if len(argv) > 1 else "",
                                 argv[2] if len(argv) > 2 else None)))
    elif mode == "capture":
        print(json.dumps(do_capture(" ".join(argv[1:]) or None)))
    elif mode == "bake":
        r = do_bake()
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    elif mode == "mcp-sock":
        print(MCP_SOCKET)
    elif mode == "mcp-up":
        r = do_mcp_up()
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    elif mode == "mcp-down":
        print(json.dumps(do_mcp_down()))
    elif mode == "mcp-status":
        print(json.dumps(mcp_status()))
    elif mode == "validate":
        print(json.dumps(validate(argv[1] if len(argv) > 1 else "", read() or {})))
    else:
        sys.exit("usage: vm.py [client <status|start|push|exec|capture|reset --off>|sock|exec <cmd>|reset|bake|push <src> [dest]|capture [cmd]"
                 "|mcp-sock|mcp-up|mcp-down|mcp-status|validate <check>]")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
