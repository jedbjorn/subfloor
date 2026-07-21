#!/usr/bin/env python3
"""Harness process supervision and fenced session ownership.

The launcher used to ``exec`` a harness directly.  That left no engine-owned
parent able to forward a wrapper cancellation to descendants, and a detached
child could outlive the process that the caller stopped (#439).  This module
keeps a small supervisor in place, launches the harness in its own process
group, and records an exact ``(pid, Linux start ticks, generation)`` lease for
managed planner bindings.

Provider adapters own native-session creation and transport.  This module owns
only the provider-neutral floor: archive/binding identity, one writer, process
validation, signal forwarding, and crash reconciliation.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import session_control


PROC = Path("/proc")
EXIT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
FORWARDED_SIGNALS = EXIT_SIGNALS + (signal.SIGWINCH,)


class LeaseConflict(RuntimeError):
    """A validated owner (or its orphaned process group) still holds a binding."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    process_group: int
    command: tuple[str, ...]
    cwd: Path


def _stat_fields(pid: int, proc_root: Path = PROC) -> list[str]:
    """Return Linux ``/proc/<pid>/stat`` fields 3 onward.

    ``comm`` (field 2) may contain spaces or parentheses, so splitting the
    whole line is incorrect.  The final right parenthesis is the stable seam.
    """
    try:
        data = (proc_root / str(pid) / "stat").read_text()
    except (OSError, UnicodeDecodeError):
        return []
    end = data.rfind(")")
    return data[end + 2:].split() if end >= 0 else []


def read_process(pid: int, proc_root: Path = PROC) -> ProcessIdentity | None:
    fields = _stat_fields(pid, proc_root)
    try:
        # fields[0] is state (kernel field 3), fields[2] pgrp (field 5), and
        # fields[19] process start time in clock ticks (field 22).
        process_group = int(fields[2])
        start_ticks = int(fields[19])
        raw_cmd = (proc_root / str(pid) / "cmdline").read_bytes()
        cwd = Path(os.readlink(proc_root / str(pid) / "cwd")).resolve()
    except (IndexError, ValueError, OSError):
        return None
    command = tuple(p.decode(errors="replace") for p in raw_cmd.split(b"\0") if p)
    if not command:
        return None
    return ProcessIdentity(pid, start_ticks, process_group, command, cwd)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def command_matches(command: Iterable[str], expected: str) -> bool:
    """Match an adapter command without trusting presentation text.

    Script entry points commonly become ``python .../kimi`` or
    ``node .../claude/...`` after ``execve``.  Match path components from the
    kernel cmdline, not terminal output, while keeping the harness name exact.
    """
    wanted = Path(expected).name
    for token in command:
        for part in Path(token).parts:
            if part == wanted or part.startswith(wanted + "-"):
                return True
    return False


def process_matches(pid: int, start_ticks: int, *, expected_command: str,
                    expected_worktree: Path, proc_root: Path = PROC) -> bool:
    identity = read_process(pid, proc_root)
    return bool(
        identity
        and identity.start_ticks == start_ticks
        and command_matches(identity.command, expected_command)
        and _within(identity.cwd, expected_worktree)
    )


def process_group_members(process_group: int, *, expected_worktree: Path,
                          expected_command: str | None = None,
                          proc_root: Path = PROC) -> list[ProcessIdentity]:
    """Validated members of a recorded harness group, including descendants.

    A dead group leader with live members is the dangerous #439 shape.  The
    original start ticks can no longer be read, so reconciliation must not
    silently transfer ownership; the surviving group keeps the binding fenced
    until an operator verifies and removes it.
    """
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    members: list[ProcessIdentity] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = read_process(int(entry.name), proc_root)
        if (identity and identity.process_group == process_group
                and _within(identity.cwd, expected_worktree)
                and not (identity.pid == process_group and expected_command
                         and not command_matches(identity.command, expected_command))):
            members.append(identity)
    return sorted(members, key=lambda p: p.pid)


def expected_worktree(repo_root: Path, shortname: str | None,
                      flavor: str | None) -> Path:
    if flavor == "admin" or not shortname:
        return repo_root.resolve()
    return (repo_root / ".sc-worktrees" / shortname.lower()).resolve()


def ensure_binding(con, *, archive_id: int, shell_id: int, harness: str,
                   native_session_id: str | None = None,
                   control_endpoint: str | None = None,
                   capabilities: str = "{}", cli_version: str | None = None) -> dict:
    """Create the binding for an archive, or verify/reuse that exact binding."""
    row = con.execute(
        "SELECT * FROM shell_session_bindings WHERE archive_id=?", (archive_id,)
    ).fetchone()
    if row:
        if row["shell_id"] != shell_id or row["harness"] != harness:
            raise ValueError("archive binding belongs to a different shell or harness")
        if (native_session_id and row["native_session_id"]
                and row["native_session_id"] != native_session_id):
            raise ValueError("archive binding already names a different native session")
        if native_session_id and not row["native_session_id"]:
            con.execute(
                "UPDATE shell_session_bindings SET native_session_id=?, "
                "control_endpoint=COALESCE(?, control_endpoint), "
                "control_capabilities=?, cli_version=COALESCE(?, cli_version), "
                "updated_at=datetime('now') WHERE binding_id=?",
                (native_session_id, control_endpoint, capabilities, cli_version,
                 row["binding_id"]),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM shell_session_bindings WHERE binding_id=?",
                (row["binding_id"],),
            ).fetchone()
        return dict(row)

    cur = con.execute(
        "INSERT INTO shell_session_bindings "
        "(archive_id, shell_id, harness, native_session_id, control_endpoint, "
        "control_capabilities, cli_version, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'starting')",
        (archive_id, shell_id, harness, native_session_id, control_endpoint,
         capabilities, cli_version),
    )
    con.commit()
    return dict(con.execute(
        "SELECT * FROM shell_session_bindings WHERE binding_id=?",
        (cur.lastrowid,),
    ).fetchone())


def binding_for_resume(con, binding_id: int, *, shell_id: int,
                       harness: str) -> dict:
    row = con.execute(
        "SELECT b.*, a.session_id, a.model AS archive_model, "
        "a.provider AS archive_provider FROM shell_session_bindings b "
        "JOIN shell_memory_archives a ON a.archive_id=b.archive_id "
        "WHERE b.binding_id=?", (binding_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown session binding {binding_id}")
    if row["shell_id"] != shell_id or row["harness"] != harness:
        raise ValueError("session binding belongs to a different shell or harness")
    if row["state"] == "released":
        raise ValueError("session binding is released")
    return dict(row)


def register_native_session(con, binding_id: int, native_session_id: str, *,
                            control_endpoint: str | None = None,
                            capabilities: str = "{}",
                            cli_version: str | None = None) -> None:
    """Persist an ID returned by a provider API/launcher contract, never a TTY scrape."""
    row = con.execute(
        "SELECT native_session_id FROM shell_session_bindings WHERE binding_id=?",
        (binding_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown session binding {binding_id}")
    if row["native_session_id"] and row["native_session_id"] != native_session_id:
        raise ValueError("binding already names a different native session")
    con.execute(
        "UPDATE shell_session_bindings SET native_session_id=?, "
        "control_endpoint=COALESCE(?, control_endpoint), control_capabilities=?, "
        "cli_version=COALESCE(?, cli_version), updated_at=datetime('now') "
        "WHERE binding_id=?",
        (native_session_id, control_endpoint, capabilities, cli_version, binding_id),
    )
    con.commit()


def _binding_context(con, binding_id: int) -> dict:
    row = con.execute(
        "SELECT b.*, s.shortname, s.flavor FROM shell_session_bindings b "
        "JOIN shells s ON s.shell_id=b.shell_id WHERE b.binding_id=?",
        (binding_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown session binding {binding_id}")
    return dict(row)


def _recorded_owner_status(row: dict, *, repo_root: Path,
                           proc_root: Path = PROC) -> tuple[str, list[int]]:
    pid, ticks = row.get("lease_pid"), row.get("lease_start_ticks")
    if pid is None or ticks is None:
        return "vacant", []
    worktree = expected_worktree(repo_root, row.get("shortname"), row.get("flavor"))
    if process_matches(pid, ticks, expected_command=row["harness"],
                       expected_worktree=worktree, proc_root=proc_root):
        return "live", [pid]
    supervisor_pid = row.get("supervisor_pid")
    supervisor_ticks = row.get("supervisor_start_ticks")
    if (supervisor_pid is not None and supervisor_ticks is not None
            and process_matches(
                supervisor_pid, supervisor_ticks, expected_command="run.py",
                expected_worktree=worktree, proc_root=proc_root)):
        return "cleanup", [supervisor_pid]
    survivors = process_group_members(
        pid, expected_worktree=worktree, expected_command=row["harness"],
        proc_root=proc_root)
    if survivors:
        return "orphan-group", [p.pid for p in survivors]
    return "stale", []


def claim_lease(con, binding_id: int, pid: int, *, repo_root: Path,
                state: str, supervisor_pid: int | None = None,
                proc_root: Path = PROC) -> int:
    """Atomically fence one validated process as the binding owner."""
    if state not in ("foreground", "dispatching"):
        raise ValueError(f"invalid owner state {state!r}")
    con.execute("BEGIN IMMEDIATE")
    try:
        row = _binding_context(con, binding_id)
        worktree = expected_worktree(repo_root, row.get("shortname"), row.get("flavor"))
        identity = read_process(pid, proc_root)
        if not identity or not command_matches(identity.command, row["harness"]):
            raise ValueError("new lease PID does not match the binding harness")
        if not _within(identity.cwd, worktree):
            raise ValueError("new lease PID is outside the binding worktree")
        supervisor_identity = None
        if supervisor_pid is not None:
            supervisor_identity = read_process(supervisor_pid, proc_root)
            if (not supervisor_identity
                    or not command_matches(supervisor_identity.command, "run.py")):
                raise ValueError("supervisor PID does not match run.py")
            if not _within(supervisor_identity.cwd, worktree):
                raise ValueError("supervisor PID is outside the binding worktree")

        owner_status, owner_pids = _recorded_owner_status(
            row, repo_root=repo_root, proc_root=proc_root)
        if owner_status == "live":
            if (row["lease_pid"] == pid
                    and row["lease_start_ticks"] == identity.start_ticks):
                con.rollback()
                return row["lease_generation"]
            raise LeaseConflict(
                f"binding {binding_id} already has live owner pid {owner_pids[0]}")
        if owner_status == "orphan-group":
            raise LeaseConflict(
                f"binding {binding_id} has surviving process-group members "
                f"{','.join(map(str, owner_pids))}")
        if owner_status == "cleanup":
            raise LeaseConflict(
                f"binding {binding_id} is being cleaned by supervisor pid "
                f"{owner_pids[0]}")

        generation = row["lease_generation"] + 1
        session_control.transition_binding(
            con, binding_id, expected=row["state"], target=state)
        cur = con.execute(
            "UPDATE shell_session_bindings SET lease_pid=?, lease_start_ticks=?, "
            "supervisor_pid=?, supervisor_start_ticks=?, lease_generation=?, "
            "last_error=NULL, updated_at=datetime('now') "
            "WHERE binding_id=? AND lease_generation=?",
            (pid, identity.start_ticks,
             supervisor_identity.pid if supervisor_identity else None,
             supervisor_identity.start_ticks if supervisor_identity else None,
             generation, binding_id,
             row["lease_generation"]),
        )
        if cur.rowcount != 1:
            raise LeaseConflict(f"binding {binding_id} lease changed concurrently")
        con.commit()
        return generation
    except Exception:
        con.rollback()
        raise


def preflight_lease(con, binding_id: int, *, repo_root: Path,
                    proc_root: Path = PROC) -> None:
    """Refuse a known owner before spawning; the post-spawn claim stays atomic."""
    status = reconcile_binding(
        con, binding_id, repo_root=repo_root, proc_root=proc_root)
    if status not in ("vacant", "stale-cleared"):
        raise LeaseConflict(
            f"binding {binding_id} owner is not vacant ({status})")


def release_lease(con, binding_id: int, pid: int, start_ticks: int,
                  generation: int, *, error: str | None = None) -> bool:
    """Release only the exact generation this supervisor acquired."""
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT state, native_session_id, last_error "
            "FROM shell_session_bindings "
            "WHERE binding_id=? AND lease_pid=? AND lease_start_ticks=? "
            "AND lease_generation=?",
            (binding_id, pid, start_ticks, generation),
        ).fetchone()
        if not row:
            con.rollback()
            return False
        if row["state"] in ("error", "released"):
            target = row["state"]
            last_error = row["last_error"]
        else:
            target = "error" if error or not row["native_session_id"] else "dormant"
            last_error = error or (None if row["native_session_id"]
                                   else "native session id unavailable when owner exited")
        session_control.transition_binding(
            con, binding_id, expected=row["state"], target=target)
        cur = con.execute(
            "UPDATE shell_session_bindings SET lease_pid=NULL, lease_start_ticks=NULL, "
            "supervisor_pid=NULL, supervisor_start_ticks=NULL, last_error=?, "
            "updated_at=datetime('now') WHERE binding_id=? "
            "AND lease_pid=? AND lease_start_ticks=? AND lease_generation=?",
            (last_error, binding_id, pid, start_ticks, generation),
        )
        if cur.rowcount != 1:
            raise LeaseConflict(f"binding {binding_id} lease changed concurrently")
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise


def register_active_channel(con, binding_id: int, pid: int, *,
                            repo_root: Path, proc_root: Path = PROC) -> int:
    """Register a provider-local live delivery channel with an exact identity."""
    row = _binding_context(con, binding_id)
    worktree = expected_worktree(repo_root, row.get("shortname"), row.get("flavor"))
    identity = read_process(pid, proc_root)
    if not identity or not _within(identity.cwd, worktree):
        raise ValueError("active channel PID is outside the binding worktree")
    con.execute(
        "UPDATE shell_session_bindings SET active_channel_pid=?, "
        "active_channel_start_ticks=?, active_channel_heartbeat_at=datetime('now'), "
        "updated_at=datetime('now') WHERE binding_id=?",
        (pid, identity.start_ticks, binding_id),
    )
    con.commit()
    return identity.start_ticks


def heartbeat_active_channel(con, binding_id: int, pid: int,
                             start_ticks: int) -> bool:
    cur = con.execute(
        "UPDATE shell_session_bindings SET active_channel_heartbeat_at=datetime('now'), "
        "updated_at=datetime('now') WHERE binding_id=? AND active_channel_pid=? "
        "AND active_channel_start_ticks=?",
        (binding_id, pid, start_ticks),
    )
    con.commit()
    return cur.rowcount == 1


def clear_active_channel(con, binding_id: int, pid: int,
                         start_ticks: int) -> bool:
    cur = con.execute(
        "UPDATE shell_session_bindings SET active_channel_pid=NULL, "
        "active_channel_start_ticks=NULL, active_channel_heartbeat_at=NULL, "
        "updated_at=datetime('now') WHERE binding_id=? AND active_channel_pid=? "
        "AND active_channel_start_ticks=?",
        (binding_id, pid, start_ticks),
    )
    con.commit()
    return cur.rowcount == 1


def _reconcile_active_channel(con, row: dict, *, repo_root: Path,
                              proc_root: Path) -> bool:
    pid, ticks = row.get("active_channel_pid"), row.get("active_channel_start_ticks")
    if pid is None or ticks is None:
        return False
    worktree = expected_worktree(repo_root, row.get("shortname"), row.get("flavor"))
    identity = read_process(pid, proc_root)
    if identity and identity.start_ticks == ticks and _within(identity.cwd, worktree):
        return False
    con.execute(
        "UPDATE shell_session_bindings SET active_channel_pid=NULL, "
        "active_channel_start_ticks=NULL, active_channel_heartbeat_at=NULL, "
        "updated_at=datetime('now') WHERE binding_id=? AND active_channel_pid=? "
        "AND active_channel_start_ticks=?",
        (row["binding_id"], pid, ticks),
    )
    return True


def reconcile_binding(con, binding_id: int, *, repo_root: Path,
                      proc_root: Path = PROC) -> str:
    """Validate recorded ownership before a claim or dispatcher decision."""
    con.execute("BEGIN IMMEDIATE")
    try:
        row = _binding_context(con, binding_id)
        channel_cleared = _reconcile_active_channel(
            con, row, repo_root=repo_root, proc_root=proc_root)
        status, pids = _recorded_owner_status(
            row, repo_root=repo_root, proc_root=proc_root)
        if status in ("vacant", "live", "cleanup"):
            if channel_cleared:
                con.commit()
            else:
                con.rollback()
            return status
        if status == "orphan-group":
            target = "released" if row["state"] == "released" else "error"
            session_control.transition_binding(
                con, binding_id, expected=row["state"], target=target)
            con.execute(
                "UPDATE shell_session_bindings SET last_error=?, "
                "updated_at=datetime('now') WHERE binding_id=?",
                ("recorded owner exited but process group survives: "
                 + ",".join(map(str, pids)), binding_id),
            )
            con.commit()
            return status

        if row["state"] in ("released", "error"):
            target = row["state"]
        else:
            target = "dormant" if row.get("native_session_id") else "error"
        error = (None if row.get("native_session_id")
                 else "stale owner and no native session id")
        session_control.transition_binding(
            con, binding_id, expected=row["state"], target=target)
        con.execute(
            "UPDATE shell_session_bindings SET lease_pid=NULL, lease_start_ticks=NULL, "
            "supervisor_pid=NULL, supervisor_start_ticks=NULL, last_error=?, "
            "updated_at=datetime('now') WHERE binding_id=? "
            "AND lease_pid=? AND lease_start_ticks=?",
            (error, binding_id, row["lease_pid"], row["lease_start_ticks"]),
        )
        con.commit()
        return "stale-cleared"
    except Exception:
        con.rollback()
        raise


def _normalize_returncode(returncode: int, forwarded_signal: int | None) -> int:
    if forwarded_signal is not None:
        return 128 + forwarded_signal
    return 128 - returncode if returncode < 0 else returncode


def terminate_group(process_group: int, *, grace: float = 5.0,
                    killpg: Callable[[int, int], None] = os.killpg) -> None:
    """Remove descendants left behind after the group leader exits."""
    try:
        killpg(process_group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        try:
            killpg(process_group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    try:
        killpg(process_group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def supervise(cmd: list[str], *, cwd: Path, env: dict[str, str],
              on_pre_spawn: Callable[[], None] | None = None,
              on_started: Callable[[int], None] | None = None,
              on_exited: Callable[[int, int], None] | None = None,
              popen: Callable[..., subprocess.Popen] = subprocess.Popen,
              killpg: Callable[[int, int], None] = os.killpg,
              group_grace: float = 2.0) -> int:
    """Run one harness process group and forward cancellation to all children."""
    child: subprocess.Popen | None = None
    forwarded: int | None = None
    previous: dict[int, object] = {}

    def forward(signum, _frame) -> None:
        nonlocal forwarded
        if signum in EXIT_SIGNALS:
            forwarded = forwarded or signum
        if child is not None and child.poll() is None:
            try:
                killpg(child.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    try:
        # Install before Popen: cancellation in the fork/exec window is held in
        # ``forwarded`` and relayed immediately once the child PID is known.
        for sig in FORWARDED_SIGNALS:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, forward)
        if forwarded is not None:
            return _normalize_returncode(0, forwarded)
        if on_pre_spawn:
            on_pre_spawn()
        child = popen(cmd, cwd=str(cwd), env=env, start_new_session=True)
        if forwarded is not None:
            forward(forwarded, None)
        if on_started:
            on_started(child.pid)
        returncode = child.wait()
        # If the leader exits but a daemonized descendant remains in its group,
        # do not recreate #439 under a different PID.
        terminate_group(child.pid, grace=group_grace, killpg=killpg)
        return _normalize_returncode(returncode, forwarded)
    except BaseException:
        if child is not None:
            try:
                killpg(child.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                child.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None and on_exited:
            on_exited(child.pid, child.returncode if child.returncode is not None else -1)
