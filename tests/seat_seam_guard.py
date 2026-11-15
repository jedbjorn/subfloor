"""Suite-level guard: seat tests never reach a real machine or a real broker.

The seat modules (`ts`, `vm`, `remote`) end in three real-world seams:

- a child process: `ssh`, `scp`, `virsh`, `tailscale`, the key tools, or the
  dispatcher (`sh .../dispatch.sh <verb>`, which is how a broker is started);
- the vm-broker's unix socket at its real, per-checkout path;
- the ts-broker's unix socket at its real path.

A test that forgets a mock used to run those for real: it probed the
developer's own tailnet and libvirt, and waited out broker-start timeouts.
Per-class patches only protect the classes that remember them. This guard is
installed once per test MODULE and covers every test in it, present and
future:

    import seat_seam_guard
    seat_seam_guard.install(globals())

It works under both runners the suite uses (`python tests/test_x.py` and
pytest), because both honour `setUpModule` / `tearDownModule`.

A guarded call raises AssertionError at the call site. Engine code sometimes
catches broad exceptions at a boundary, so every violation is also recorded
and re-raised from `tearDownModule` - a swallowed violation still fails the
run.

What stays allowed: any other child process (`docker`, `git`, the Python
interpreter), broker calls to a socket a test has pointed somewhere else (the
socket-transport tests run a real broker on a temporary socket), and broker
calls made while a test has replaced the socket class with a fake. A test
that replaces a seam with its own mock simply takes precedence for its
duration.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import ts  # noqa: E402
import vm  # noqa: E402

BLOCKED_PROGRAMS = frozenset({
    "ssh", "scp", "sftp", "ssh-keygen", "ssh-keyscan", "virsh", "tailscale",
})
REAL_SOCKETS = {"vm": str(vm.SOCKET), "ts": str(ts.SOCKET)}
_REAL_SOCKET_CLASS = socket.socket

_violations: list[str] = []
_active = 0
_saved: dict[str, object] = {}


def _violation(message: str) -> AssertionError:
    _violations.append(message)
    return AssertionError(
        f"seat seam guard: {message} - a test reached a real seam; mock it"
    )


def blocked_reason(args: object) -> str | None:
    """Why this argv must not run for real, or None."""
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        try:
            argv = [os.fsdecode(item) for item in args]  # type: ignore[union-attr]
        except TypeError:
            return None
    if not argv:
        return None
    program = os.path.basename(argv[0].split()[0]) if argv[0].strip() else ""
    if program in BLOCKED_PROGRAMS:
        return f"real `{program}` invocation: {argv[:4]}"
    if any(item.endswith("dispatch.sh") for item in argv):
        return f"real dispatcher invocation: {argv[:4]}"
    return None


class GuardedPopen(subprocess.Popen):
    """`subprocess.run`, `check_output` and friends all construct a Popen, so
    this one class covers every way of starting a child process."""

    def __init__(self, args, *positional, **keywords):
        reason = blocked_reason(args)
        if reason:
            raise _violation(reason)
        super().__init__(args, *positional, **keywords)


def _guarded_broker_call(module, name: str, original):
    def broker_call(*args, **keywords):
        # A test that replaces the socket class itself is exercising the
        # transport against a fake; nothing real can be reached that way.
        real_transport = module.socket.socket is _REAL_SOCKET_CLASS
        if real_transport and str(module.SOCKET) == REAL_SOCKETS[name]:
            raise _violation(
                f"{name}.broker_call{args[:2]} on the real socket "
                f"{REAL_SOCKETS[name]}"
            )
        return original(*args, **keywords)

    return broker_call


def start() -> None:
    global _active
    _active += 1
    if _active > 1:
        return
    _saved["Popen"] = subprocess.Popen
    _saved["vm"] = vm.broker_call
    _saved["ts"] = ts.broker_call
    subprocess.Popen = GuardedPopen  # type: ignore[misc]
    vm.broker_call = _guarded_broker_call(vm, "vm", vm.broker_call)
    ts.broker_call = _guarded_broker_call(ts, "ts", ts.broker_call)


def stop() -> None:
    global _active
    _active -= 1
    if _active > 0:
        return
    subprocess.Popen = _saved.pop("Popen")  # type: ignore[misc]
    vm.broker_call = _saved.pop("vm")
    ts.broker_call = _saved.pop("ts")
    if _violations:
        seen, _violations[:] = list(_violations), []
        raise AssertionError(
            "seat seam guard: real seams were reached during this module:\n  "
            + "\n  ".join(seen)
        )


def install(namespace: dict) -> None:
    """Guard every test in the calling module: `install(globals())`."""
    namespace["setUpModule"] = start
    namespace["tearDownModule"] = stop
