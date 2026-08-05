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
import fcntl
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
RESET_COMMAND_TIMEOUT = 60
START_READINESS_TIMEOUT = 90
START_READINESS_INTERVAL = 2
RESET_CLIENT_TIMEOUT = 130
START_CLIENT_TIMEOUT = START_READINESS_TIMEOUT + 25
DEFAULT_CLIENT_TIMEOUT = 30
RESULT_SCHEMA_VERSION = 1


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
    """Run one command in the guest over SSH. Returns {ok, exit, stdout, stderr}."""
    cfg = read() or {}
    if m := _missing(cfg, "ssh_host", "ssh_user", "ssh_key_path"):
        return {"ok": False, "exit": -1, "stdout": "", "stderr": m}
    if not str(command).strip():
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "exec: empty command"}
    try:
        # errors="replace": guest output is routinely non-UTF-8 (UTF-16 files,
        # OEM codepages). A strict decode turned the whole exec into a 500 with
        # no exit code or partial output (#261) — lossy beats fatal here; callers
        # needing byte-exact output base64 it guest-side.
        p = subprocess.run(_ssh_argv(cfg, command), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except FileNotFoundError as e:
        return {"ok": False, "exit": 127, "stdout": "",
                "stderr": f"command not found: {e.filename} — is ssh installed on the host?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": 124, "stdout": "", "stderr": f"timed out (>{timeout}s)"}


def _domain_state(cfg: dict) -> tuple[bool, str]:
    """Return a stable domain-state name without mutating the guest."""
    ok, output = _run(_virsh(cfg, "domstate", str(cfg["domain"])), timeout=15)
    if not ok:
        return False, output
    state = output.strip().lower()
    if state == "shut off":
        state = "powered_off"
    return True, state.replace(" ", "_")


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
        ok, output = _run(_virsh(cfg, "start", str(cfg["domain"])), timeout=30)
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
    state = "running" if running else "powered off"
    state_ok, observed = _domain_state(cfg)
    expected = "running" if running else "powered_off"
    confirmed = ok and state_ok and observed == expected
    return {
        "ok": confirmed,
        "output": out or f"reverted '{cfg['domain']}' to '{cfg['snapshot']}' ({state})",
        "domain": str(cfg["domain"]),
        "snapshot": str(cfg["snapshot"]),
        "domain_state": observed if state_ok else "unknown",
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
    src_p = src_p.resolve()
    if not src_p.is_relative_to(repo_root):
        return {"ok": False, "output": f"push: src must be inside the repo: {src}"}
    if not src_p.is_file():
        return {"ok": False, "output": f"push: source not found: {src_p}"}
    d = Path(os.path.expanduser(str(cfg["transfer_dir"]))).resolve()
    if not d.is_dir():
        return {"ok": False, "output": f"transfer_dir does not exist: {d}"}
    target = (d / (dest or src_p.name)).resolve()
    if not target.is_relative_to(d):
        return {"ok": False, "output": f"push: dest escapes transfer_dir: {dest}"}
    try:
        shutil.copy2(src_p, target)
    except OSError as e:
        return {"ok": False, "output": f"push failed: {e}"}
    return {"ok": True, "output": f"staged {src_p.name} -> {target} (guest sees it via the share)"}


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
    running = state is not None and listening
    return {"ok": True, "running": running, "pid": pid,
            "socket": str(MCP_SOCKET) if listening else None,
            "listening": listening,
            "unverified": state is None and listening}


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


class BrokerResponseError(ValueError):
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
        raise TimeoutError(f"vm-broker timed out after {timeout}s") from e
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


def _broker_failure(operation: str, response: dict) -> dict:
    details: dict = {}
    for key in ("domain_state", "attempts", "last_readiness_error"):
        if key in response:
            details[key] = response[key]
    return operation_error(
        operation,
        f"{operation}_failed",
        str(response.get("output") or response.get("error") or f"{operation} failed"),
        details,
    )


def run_operation(operation: str) -> dict:
    """Call one core broker operation once and normalize its public result."""
    calls = {
        "status": ("GET", "/status", None, DEFAULT_CLIENT_TIMEOUT),
        "start": ("POST", "/start", None, START_CLIENT_TIMEOUT),
        "reset": ("POST", "/reset", {"running": False}, RESET_CLIENT_TIMEOUT),
    }
    if operation not in calls:
        return operation_error(operation, "operation_unknown", "unknown VM operation")
    method, path, body, timeout = calls[operation]
    try:
        response = broker_call(method, path, body, timeout=timeout)
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
    except (TimeoutError, BrokerResponseError):
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

    if not response.get("ok"):
        return _broker_failure(operation, response)
    try:
        if operation == "status":
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
                "mcp": {"tunnel_running": bool(response["mcp_tunnel_running"])},
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
        return f"VM {result['domain']['state']} · SSH {ssh} · broker ready"
    if operation == "start":
        action = "started" if result["started"] else "already running"
        return f"VM {action} · SSH ready after {result['ssh']['attempts']} attempt(s)"
    return f"VM reset to '{result['snapshot']}' · {result['domain']['state']}"


def client_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="./sc vm")
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("status", "start"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
    reset = commands.add_parser("reset")
    reset.add_argument("--off", action="store_true", required=True)
    reset.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
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
        sys.exit("usage: vm.py [client <status|start|reset --off>|sock|exec <cmd>|reset|bake|push <src> [dest]|capture [cmd]"
                 "|mcp-sock|mcp-up|mcp-down|mcp-status|validate <check>]")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
