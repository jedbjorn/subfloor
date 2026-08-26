#!/usr/bin/env python3
"""Launch one managed DSH ToolExecution in an engine-owned cgroup-v2 domain."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

CONTRACT = "sc-dsh-linux-cgroup-v2-v3"
REGISTRY_CONTRACT = "sc-dsh-identity-registry-v1"
HOST_IDENTITY_CONTRACT = "sc-dsh-host-identity-v1"
DESCRIPTOR_FD = 198
# Linux UAPI values from <linux/fcntl.h>.  Some supported Python builds omit
# these names even though their kernels implement memfd sealing.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
REQUIRED_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008
ALIASES = (
    "DSH_SC_SHELL_ID",
    "DSH_SC_SHELL_SHORTNAME",
    "DSH_SC_SHELL_WORKTREE",
    "DSH_SC_API_BASE",
    "DSH_SC_MEM_CREDENTIAL_FILE",
    "DSH_SC_BINDING_GENERATION",
    "DSH_SC_PLUGIN_HEALTH_GENERATION",
)
DOMAIN_ID = re.compile(r"[a-f0-9]{32}")


class ExecutionDomainError(RuntimeError):
    """A stable refusal raised before model or user code can execute."""


def _owner_json(path: Path, label: str) -> dict[str, Any]:
    try:
        visible = path.lstat()
        if (
            stat.S_ISLNK(visible.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or visible.st_uid != os.geteuid()
            or visible.st_mode & 0o077
        ):
            raise ExecutionDomainError(f"{label} is not owner-only")
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
                raise ExecutionDomainError(f"{label} changed before read")
            payload = os.pread(descriptor, 65_537, 0)
            after = os.fstat(descriptor)
            if (
                not payload
                or len(payload) > 65_536
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise ExecutionDomainError(f"{label} changed during read")
        finally:
            os.close(descriptor)
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionDomainError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise ExecutionDomainError(f"{label} is not an object")
    return value


def _process_identity(pid: int, *, proc_root: Path = Path("/proc")) -> tuple[int, int]:
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError) as exc:
        raise ExecutionDomainError("process identity is unavailable") from exc


def _unified_membership(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    try:
        text = (proc_root / str(pid) / "cgroup").read_text()
    except OSError as exc:
        raise ExecutionDomainError("unified cgroup membership is unavailable") from exc
    rows = []
    for raw in text.splitlines():
        fields = raw.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            rows.append(fields[2])
    if len(rows) != 1 or not rows[0].startswith("/"):
        raise ExecutionDomainError("unified cgroup membership is ambiguous")
    return rows[0]


def _binding_snapshot(
    *,
    registry_path: Path,
    fork_id: str,
    profile_id: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    session_id = environment.get("DSH_SESSION_ID")
    if not session_id:
        raise ExecutionDomainError("ToolExecution has no DSH session identity")
    presented = {name for name in environment if name.startswith("DSH_SC_")}
    if presented != set(ALIASES) or any(not environment.get(name) for name in ALIASES):
        raise ExecutionDomainError("ToolExecution has a partial DSH identity")
    snapshot = _owner_json(registry_path, "identity registry")
    if (
        snapshot.get("contract") != REGISTRY_CONTRACT
        or snapshot.get("schema_version") != 1
        or snapshot.get("fork_id") != fork_id
        or snapshot.get("profile_id") != profile_id
        or snapshot.get("registry_path") != str(registry_path.resolve())
        or not isinstance(snapshot.get("snapshot_generation"), int)
        or not isinstance(snapshot.get("records"), dict)
        or not isinstance(snapshot.get("lineage"), dict)
    ):
        raise ExecutionDomainError("identity registry disagrees with this fork")
    lineage = snapshot["lineage"].get(session_id)
    root_session_id = session_id
    if lineage is not None:
        if not isinstance(lineage, dict) or not isinstance(
            lineage.get("root_session_id"), str
        ):
            raise ExecutionDomainError("ToolExecution lineage is malformed")
        root_session_id = lineage["root_session_id"]
    record = snapshot["records"].get(root_session_id)
    if not isinstance(record, dict) or record.get("state") != "active":
        raise ExecutionDomainError("ToolExecution binding is not active")
    if lineage is not None and (
        lineage.get("lifecycle_epoch") != record.get("lifecycle_epoch")
        or lineage.get("record_generation") != record.get("record_generation")
    ):
        raise ExecutionDomainError("ToolExecution lineage is stale")
    expected_aliases = {
        "DSH_SC_SHELL_ID": str(record.get("shell_id")),
        "DSH_SC_SHELL_SHORTNAME": record.get("shell_shortname"),
        "DSH_SC_SHELL_WORKTREE": record.get("shell_worktree"),
        "DSH_SC_API_BASE": record.get("api_base"),
        "DSH_SC_MEM_CREDENTIAL_FILE": record.get("credential_file"),
        "DSH_SC_BINDING_GENERATION": str(record.get("record_generation")),
        "DSH_SC_PLUGIN_HEALTH_GENERATION": record.get(
            "plugin_contract_generation"
        ),
    }
    if any(environment.get(name) != value for name, value in expected_aliases.items()):
        raise ExecutionDomainError("ToolExecution aliases disagree with the registry")
    if (
        not isinstance(record.get("record_generation"), int)
        or record["record_generation"] < 1
        or not isinstance(record.get("lifecycle_epoch"), int)
        or record["lifecycle_epoch"] < 1
    ):
        raise ExecutionDomainError("ToolExecution binding generation is malformed")
    return {
        "registry_snapshot_generation": snapshot["snapshot_generation"],
        "execution_session_id": session_id,
        "root_session_id": root_session_id,
        "conversation_id": record.get("conversation_id"),
        "lifecycle_epoch": record["lifecycle_epoch"],
        "shell_id": record.get("shell_id"),
        "shell_shortname": record.get("shell_shortname"),
        "shell_worktree": record.get("shell_worktree"),
        "api_base": record.get("api_base"),
        "credential_file": record.get("credential_file"),
        "binding_record_generation": record["record_generation"],
        "plugin_contract_generation": record.get("plugin_contract_generation"),
        "lineage_record_generation": (
            lineage.get("record_generation") if lineage is not None else None
        ),
    }


def _host_identity(
    *, host_identity_path: Path, fork_id: str, profile_id: str
) -> dict[str, Any]:
    identity = _owner_json(host_identity_path, "Host identity")
    host_pid = os.getppid()
    _parent, host_ticks = _process_identity(host_pid)
    if (
        identity.get("contract") != HOST_IDENTITY_CONTRACT
        or identity.get("fork_id") != fork_id
        or identity.get("profile_id") != profile_id
        or identity.get("host_pid") != host_pid
        or identity.get("host_start_ticks") != host_ticks
    ):
        raise ExecutionDomainError("launcher is not a direct child of the live Host")
    return {"host_pid": host_pid, "host_start_ticks": host_ticks}


def _domain_path(
    *, cgroup_root: Path, issuer_pid: int, domain_id: str
) -> tuple[str, Path, Path]:
    if DOMAIN_ID.fullmatch(domain_id) is None:
        raise ExecutionDomainError("execution domain identity is malformed")
    root = cgroup_root.resolve(strict=True)
    if (root / "cgroup.controllers").is_file() is False:
        raise ExecutionDomainError("cgroup root is not unified cgroup-v2")
    issuer_membership = _unified_membership(issuer_pid)
    parent = root / issuer_membership.lstrip("/") / "sc-dsh"
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ExecutionDomainError("execution-domain parent is unsafe")
    os.chmod(parent, 0o700)
    domain = parent / f"{domain_id}.scope"
    domain.mkdir(mode=0o700)
    membership = f"{issuer_membership.rstrip('/')}/sc-dsh/{domain_id}.scope"
    return membership, parent, domain


def _wait_membership(pid: int, expected: str, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _unified_membership(pid) == expected:
                return
        except ExecutionDomainError:
            pass
        time.sleep(0.005)
    raise ExecutionDomainError("child did not enter its execution domain")


def _seal_descriptor(fd: int, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > 65_536:
        raise ExecutionDomainError("execution descriptor is oversized")
    os.ftruncate(fd, 0)
    os.pwrite(fd, payload, 0)
    os.fsync(fd)
    fcntl.fcntl(fd, F_ADD_SEALS, REQUIRED_SEALS)
    if fcntl.fcntl(fd, F_GET_SEALS) & REQUIRED_SEALS != REQUIRED_SEALS:
        raise ExecutionDomainError("execution descriptor did not seal")


def _domain_empty(domain: Path) -> bool:
    try:
        return not (domain / "cgroup.procs").read_text().strip()
    except OSError:
        return not domain.exists()


def cleanup_domain(domain: Path, parent: Path, *, timeout: float = 2.0) -> None:
    """Bounded idempotent teardown of one execution domain."""
    if not domain.exists():
        return
    try:
        os.chmod(domain, 0o700)
        os.chmod(domain / "cgroup.procs", 0o600)
    except OSError:
        pass
    if not _domain_empty(domain):
        try:
            (domain / "cgroup.kill").write_text("1")
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _domain_empty(domain):
        time.sleep(0.01)
    if not _domain_empty(domain):
        raise ExecutionDomainError("execution domain did not quiesce")
    domain.rmdir()
    try:
        parent.rmdir()
    except OSError:
        pass


def _child_exec(
    *, gate: int, descriptor_fd: int, membership: str, argv: list[str]
) -> None:
    try:
        if os.read(gate, 1) != b"1":
            raise ExecutionDomainError("execution issuer did not release the child")
        if (
            fcntl.fcntl(descriptor_fd, F_GET_SEALS) & REQUIRED_SEALS
            != REQUIRED_SEALS
        ):
            raise ExecutionDomainError("execution descriptor is not sealed")
        if _unified_membership(os.getpid()) != membership:
            raise ExecutionDomainError("execution child is outside its issued domain")
        os.execvpe(argv[0], argv, os.environ)
    except (OSError, ExecutionDomainError) as exc:
        print(f"deepseek-execution-domain: {exc}", file=sys.stderr)
        os._exit(126)


def launch(args: argparse.Namespace) -> int:
    environment = dict(os.environ)
    binding = _binding_snapshot(
        registry_path=args.registry.resolve(strict=True),
        fork_id=args.fork_id,
        profile_id=args.profile_id,
        environment=environment,
    )
    host = _host_identity(
        host_identity_path=args.host_identity.resolve(strict=True),
        fork_id=args.fork_id,
        profile_id=args.profile_id,
    )
    membership, parent, domain = _domain_path(
        cgroup_root=args.cgroup_root,
        issuer_pid=os.getpid(),
        domain_id=args.domain_id,
    )
    memfd = os.memfd_create(
        "sc-dsh-execution", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    os.dup2(memfd, args.descriptor_fd, inheritable=True)
    if memfd != args.descriptor_fd:
        os.close(memfd)
    gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
    child = os.fork()
    if child == 0:
        os.close(gate_write)
        _child_exec(
            gate=gate_read,
            descriptor_fd=args.descriptor_fd,
            membership=membership,
            argv=args.command,
        )
        raise AssertionError("unreachable")
    os.close(gate_read)
    released = False
    try:
        (domain / "cgroup.procs").write_text(f"{child}\n")
        _wait_membership(child, membership)
        issuer_parent, issuer_ticks = _process_identity(os.getpid())
        if issuer_parent != host["host_pid"]:
            raise ExecutionDomainError("issuer lost its direct Host parent")
        root_parent, root_ticks = _process_identity(child)
        if root_parent != os.getpid():
            raise ExecutionDomainError("execution root lost its issuer parent")
        domain_metadata = domain.stat()
        descriptor_metadata = os.fstat(args.descriptor_fd)
        descriptor = {
            "contract": CONTRACT,
            "cgroup": membership,
            "domain_id": args.domain_id,
            "descriptor_device": descriptor_metadata.st_dev,
            "descriptor_inode": descriptor_metadata.st_ino,
            "expires_monotonic_ns": time.monotonic_ns()
            + args.ttl_seconds * 1_000_000_000,
            "non_delegated": True,
            "cgroup_device": domain_metadata.st_dev,
            "cgroup_inode": domain_metadata.st_ino,
            "cgroup_owner_uid": domain_metadata.st_uid,
            "issuer_pid": os.getpid(),
            "issuer_start_ticks": issuer_ticks,
            "host_pid": host["host_pid"],
            "host_start_ticks": host["host_start_ticks"],
            "root_pid": child,
            "root_start_ticks": root_ticks,
            "fork_id": args.fork_id,
            "profile_id": args.profile_id,
            **binding,
        }
        _seal_descriptor(args.descriptor_fd, descriptor)
        os.chmod(domain / "cgroup.procs", 0o444)
        os.chmod(domain, 0o555)
        os.write(gate_write, b"1")
        released = True
    except BaseException:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
        cleanup_domain(domain, parent)
        raise
    finally:
        os.close(gate_write)
    if not released:
        raise ExecutionDomainError("execution root was not released")
    _pid, status = os.waitpid(child, 0)
    cleanup_domain(domain, parent)
    if os.WIFSIGNALED(status):
        child_signal = os.WTERMSIG(status)
        signal.signal(child_signal, signal.SIG_DFL)
        os.kill(os.getpid(), child_signal)
        return 128 + child_signal
    return os.waitstatus_to_exitcode(status)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fork-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--host-identity", type=Path, required=True)
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--domain-id", required=True)
    parser.add_argument("--descriptor-fd", type=int, default=DESCRIPTOR_FD)
    parser.add_argument("--ttl-seconds", type=int, default=86_400)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if (
        not args.command
        or args.descriptor_fd != DESCRIPTOR_FD
        or args.ttl_seconds < 1
        or args.ttl_seconds > 86_400
    ):
        raise ExecutionDomainError("execution launcher arguments are invalid")
    try:
        return launch(args)
    except (OSError, ExecutionDomainError) as exc:
        print(f"deepseek-execution-domain: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
