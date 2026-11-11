#!/usr/bin/env python3
"""Engine operations the browser operator may start: update, restart, and
harness update (Feature #93, spec #270 "Engine controls").

This is the core the host-side engine-ops service (`engine_ops_service.py`)
serves: the fixed verb table, the status file, and the detached runner. Each
operation is an existing CLI verb run from the main checkout, on the host,
under either runtime.

`./sc restart`, `./sc update`, and `./sc down` stop the review server and the
brokers, this service among them. So the verb never runs as an ordinary child
of the service; it is detached from it:

* It runs under a small wrapper (`engine_ops.py run ...`, this file) that owns
  the status file. The wrapper is the only writer once it has started.
* The wrapper is started in its own session with stdio on a log file, never on
  inherited pipes. `./sc down` stops a broker by signalling the one pid in its
  pidfile, which a separate session survives.
* A new session is not enough when a systemd unit is the thing stopped (the
  host review server always is; a broker may be): stopping a unit kills every
  process left in its control group. So the wrapper is also placed in its own
  transient scope (`systemd-run --user --scope`) whenever a user manager
  answers.

The verb table is fixed. A request selects a key of `VERBS` and contributes
nothing else to the command line. Tests replace the table in code
(`mock.patch.dict(engine_ops.VERBS, ...)`); no request field and no environment
variable can.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
# The instance's local, gitignored log directory (the server's LOG_DIR).
LOG_DIR = ENGINE / "logs"
STATUS_NAME = "engine-op.json"
# Written by the running service, removed when it stops. In the bind-mounted
# engine directory so the review API can see it from inside the sandbox, where
# the service's port itself is unreachable.
STATE_FILE = ENGINE / "run" / "engine-ops.json"
BIND = "127.0.0.1"
ENGINE_REF = REPO_ROOT / ".sc-state" / "engine.ref"

HOST = "host"
DOCKER = "docker"

# op -> arguments after `./sc`. `restart` prompts on a terminal; `-y` is the
# verb's own flag for scripted callers (dispatch.sh `restart)`).
VERBS: dict[str, tuple[str, ...]] = {
    "update": ("update",),
    "restart": ("restart", "-y"),
    "update-harnesses": ("update-harnesses",),
}
# What the operator would type, shown when the service is not running.
CLI: dict[str, str] = {
    "update": "./sc update",
    "restart": "./sc restart",
    "update-harnesses": "./sc update-harnesses",
}
# Operations that stop the review server while they run.
STOPS_ENGINE = frozenset({"update", "restart"})
# A failed update leaves the server down by design; this brings it back.
RECOVERY_CLI = "./sc launch"

LOG_TAIL_BYTES = 16 * 1024
# How long a claimed-but-not-yet-running operation still counts as running.
STARTING_GRACE_SECONDS = 30.0
# How long a POST waits for the wrapper to record its pid before answering.
START_WAIT_SECONDS = 5.0

_start_lock = threading.Lock()


class UnknownOp(KeyError):
    """The requested operation is not in the verb table."""


class Busy(Exception):
    """An operation is already running."""

    def __init__(self, operation: dict):
        super().__init__(f"engine operation {operation.get('op')} is running")
        self.operation = operation


class StartFailed(Exception):
    """The wrapper process could not be started."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_path() -> Path:
    return LOG_DIR / STATUS_NAME


def log_path(op: str) -> Path:
    return LOG_DIR / f"engine-op-{op}.log"


def command(op: str) -> list[str]:
    """The fixed argv for one operation. Raises UnknownOp for anything else."""
    args = VERBS.get(op)
    if args is None:
        raise UnknownOp(op)
    return [str(REPO_ROOT / "sc"), *args]


# ── runtime ──────────────────────────────────────────────────────────────────

def detect_runtime() -> str:
    """Which runtime this install's lifecycle verbs drive: `host` or `docker`.

    The `instance.json` selection (scripts/runtime.py); an absent key is the
    docker sandbox, as it is for the dispatcher.
    """
    try:
        import runtime as runtime_mod  # noqa: PLC0415  (scripts/, on sys.path)
    except ImportError:
        return DOCKER
    return HOST if runtime_mod.is_host(ENGINE / "instance.json") else DOCKER


def op_facts(op: str, runtime: str) -> dict:
    """What one operation does to a running engine, for the confirmation text.

    `ends_sessions`: `all` — the sandbox container is stopped or removed and
    every session in it ends; `browser` — the host review server stops and the
    browser turns it owns end, while terminal sessions on the host keep running
    without an API until it returns; `none`.
    `takes_effect`: `now`, or `next_restart` for the docker harness update,
    which rebuilds the image and leaves the running sandbox on the old one.
    """
    stops = op in STOPS_ENGINE
    if not stops:
        ends = "none"
    elif runtime == DOCKER:
        ends = "all"
    else:
        ends = "browser"
    return {
        "cli": CLI.get(op, f"./sc {op}"),
        "stops_engine": stops,
        "ends_sessions": ends,
        "takes_effect": ("next_restart"
                         if runtime == DOCKER and op == "update-harnesses"
                         else "now"),
    }


# ── status file ──────────────────────────────────────────────────────────────

def write_status(path: Path, record: dict) -> None:
    """Replace the status file atomically: a reader sees old or new, never part."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_status(path: Path | None = None) -> dict | None:
    try:
        record = json.loads((path or status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _proc_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name, or None."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rpartition(")")[2].split()


def pid_start_ticks(pid: int) -> int | None:
    fields = _proc_stat(pid)
    try:
        return int(fields[19]) if fields else None
    except (IndexError, ValueError):
        return None


def pid_alive(pid, start_ticks=None) -> bool:
    """Whether the recorded wrapper is still running.

    A zombie has exited, and a pid whose start time differs from the recorded
    one has been reused by another process; neither is the wrapper.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # exists, owned by someone else — judged by /proc below
    fields = _proc_stat(pid)
    if fields is None:
        # No /proc: the signal probe is all there is.
        return not Path("/proc").is_dir()
    if fields[0] == "Z":
        return False
    if isinstance(start_ticks, int):
        try:
            return int(fields[19]) == start_ticks
        except (IndexError, ValueError):
            return True
    return True


def _age_seconds(stamp) -> float | None:
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def effective_state(record: dict | None) -> str | None:
    """`starting`, `running`, `finished`, or `interrupted` for a stored record.

    `interrupted` is a record that never reached `finished` although its
    wrapper is gone (killed, crashed, host rebooted). It does not block.
    """
    if not record:
        return None
    state = record.get("state")
    if state == "finished":
        return "finished"
    if state == "running":
        if pid_alive(record.get("pid"), record.get("pid_start_ticks")):
            return "running"
        return "interrupted"
    if state == "starting":
        age = _age_seconds(record.get("started_at"))
        if age is not None and 0 <= age < STARTING_GRACE_SECONDS:
            return "starting"
        return "interrupted"
    return "interrupted"


def is_running(record: dict | None) -> bool:
    return effective_state(record) in ("starting", "running")


def public_record(record: dict | None) -> dict | None:
    """The stored record as the API reports it, with its state resolved."""
    if not record:
        return None
    exit_code = record.get("exit_code")
    state = effective_state(record)
    return {
        "op": record.get("op"),
        "state": state,
        "cli": CLI.get(record.get("op")),
        "pid": record.get("pid"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "exit_code": exit_code,
        "ok": (exit_code == 0) if state == "finished" else None,
        "log": record.get("log"),
        "detach": record.get("detach"),
        # Set only when the wrapper itself could not be started.
        "error": record.get("error"),
    }


def log_tail(record: dict | None, limit: int = LOG_TAIL_BYTES) -> tuple[str | None, bool]:
    """The last `limit` bytes of the operation's log and whether more preceded."""
    name = (record or {}).get("log")
    if not name:
        return None, False
    try:
        with open(name, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read(limit)
    except OSError:
        return None, False
    return data.decode("utf-8", "replace"), size > limit


# ── status payload ───────────────────────────────────────────────────────────

def engine_ref() -> str | None:
    """The installed engine pin, when this install has one."""
    try:
        ref = ENGINE_REF.read_text().strip()
    except OSError:
        return None
    return ref or None


def live_sessions() -> dict | None:
    """Live harness sessions of this install, read from the OS.

    `count` is every live harness process whose working directory is in this
    repository (terminal and browser sessions alike); `browser` is the subset
    the API itself owns; `indeterminate` counts harness processes that could
    not be attributed. None when the host offers no such read.
    """
    try:
        import shell_liveness  # noqa: PLC0415  (scripts/, on sys.path)
        snapshot = shell_liveness.compute()
    except Exception:  # noqa: BLE001 — a count is optional; status must answer
        return None
    if not snapshot.get("supported"):
        return None
    processes = snapshot.get("processes") or []
    return {
        "count": len(processes),
        "browser": sum(1 for p in processes if p.get("browser_conversation")),
        "indeterminate": int(snapshot.get("indeterminate") or 0),
    }


def status() -> dict:
    runtime = detect_runtime()
    record = read_status()
    tail, truncated = log_tail(record)
    return {
        "ok": True,
        "service": "engine-ops",
        "runtime": runtime,
        "ops": {op: op_facts(op, runtime) for op in VERBS},
        "recovery_cli": RECOVERY_CLI,
        "running": is_running(record),
        "operation": public_record(record),
        "log_tail": tail,
        "log_truncated": truncated,
        "log_tail_limit": LOG_TAIL_BYTES,
        "engine_ref": engine_ref(),
        "live_sessions": live_sessions(),
    }


# ── discovery (served by the review API) ─────────────────────────────────────

def _port_answers(port: int) -> bool:
    import socket  # noqa: PLC0415

    try:
        socket.create_connection((BIND, port), timeout=0.5).close()
    except OSError:
        return False
    return True


def discovery(in_sandbox: bool = False) -> dict:
    """What the GUI needs to find the service: its port, URL, and whether it is up.

    On the host the port is probed. Inside the sandbox it cannot be (the
    container has no route to host loopback, by design), so `up` is whether the
    service's state file names this port; a service that was killed without
    cleanup can leave that file behind, and `up_source` says which answer this
    is. The browser's own `GET <url>/status` is the authority either way.
    """
    try:
        import ports as ports_mod  # noqa: PLC0415  (scripts/, on sys.path)
        port = ports_mod.stored_engine_ops_port()
    except ImportError:
        port = None
    runtime = detect_runtime()
    if port is None:
        up, source = False, "unallocated"
    elif in_sandbox:
        state = read_status(STATE_FILE) or {}
        up, source = state.get("port") == port, "state_file"
    else:
        up, source = _port_answers(port), "probe"
    return {
        "port": port,
        "url": f"http://{BIND}:{port}" if port is not None else None,
        "up": up,
        "up_source": source,
        "runtime": runtime,
        "ops": {op: op_facts(op, runtime) for op in VERBS},
        "recovery_cli": RECOVERY_CLI,
        "start_cli": "./sc launch",
    }


# ── start ────────────────────────────────────────────────────────────────────

def scope_prefix(op: str) -> list[str]:
    """argv prefix that puts the wrapper in its own systemd user scope, or [].

    Empty when no user manager answers: then nothing here is a systemd unit
    either, and there is no control group to leave.
    """
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not systemd_run or not systemctl:
        return []
    try:
        probe = subprocess.run(
            [systemctl, "--user", "show-environment"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if probe.returncode != 0:
        return []
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{REPO_ROOT.name}-{op}")
    unit = f"sc-engine-op-{name}-{os.getpid()}-{int(time.time())}.scope"
    return [systemd_run, "--user", "--scope", "--quiet", "--collect",
            "--unit", unit, "--"]


def child_env() -> dict[str, str]:
    """The wrapper's environment: the engine's own, never a launched shell's."""
    env = dict(os.environ)
    try:
        import engine_identity  # noqa: PLC0415  (scripts/, on sys.path)
        env = engine_identity.engine_internal_env(env)
    except ImportError:
        for name in ("SC_API_TOKEN", "SC_API_BASE"):
            env.pop(name, None)
    # A review server's bind selection is its own; `./sc launch` chooses again.
    env.pop("SC_BIND", None)
    return env


def _spawn(argv: list[str]) -> subprocess.Popen:
    proc = subprocess.Popen(
        argv, cwd=str(REPO_ROOT), env=child_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    # Reap it if it ends while this service still lives, so a finished wrapper
    # never lingers as a zombie that reads as a live pid.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _wait_recorded(proc: subprocess.Popen, path: Path, started_at: str,
                   timeout: float) -> dict | None:
    """The record once the wrapper has taken it over, or None."""
    deadline = time.monotonic() + timeout
    while True:
        record = read_status(path)
        if (record and record.get("started_at") == started_at
                and record.get("state") in ("running", "finished")):
            return record
        if proc.poll() is not None or time.monotonic() >= deadline:
            record = read_status(path)
            if (record and record.get("started_at") == started_at
                    and record.get("state") in ("running", "finished")):
                return record
            return None
        time.sleep(0.05)


def start(op: str) -> dict:
    """Start one operation. Returns its public record.

    Raises UnknownOp, Busy, or StartFailed. `op` only selects a row of the
    verb table.
    """
    argv = command(op)
    path = status_path()
    with _start_lock:
        current = read_status(path)
        if is_running(current):
            raise Busy(public_record(current))
        log = log_path(op)
        started_at = _now()
        base = {
            "op": op, "state": "starting", "pid": None, "pid_start_ticks": None,
            "started_at": started_at, "finished_at": None, "exit_code": None,
            "log": str(log),
        }
        prefix = scope_prefix(op)
        attempts = [(prefix, "systemd-scope")] if prefix else []
        attempts.append(([], "session"))
        failure = "the wrapper did not start"
        for lead, detach in attempts:
            write_status(path, {**base, "detach": detach})
            wrapper = [
                sys.executable, str(Path(__file__).resolve()), "run",
                "--op", op, "--status", str(path), "--log", str(log),
                "--cwd", str(REPO_ROOT), "--started-at", started_at,
                "--detach", detach, "--", *argv,
            ]
            try:
                proc = _spawn(lead + wrapper)
            except OSError as exc:
                failure = str(exc)
                continue
            record = _wait_recorded(proc, path, started_at, START_WAIT_SECONDS)
            if record is not None:
                return public_record(record)
            if proc.poll() is None:
                # Alive but slow to record: the claim stands for the grace window.
                return public_record(read_status(path))
            failure = f"the wrapper exited with status {proc.returncode} before recording"
        write_status(path, {
            **base, "state": "finished", "finished_at": _now(),
            "detach": None, "error": failure,
        })
        raise StartFailed(failure)


# ── wrapper (runs detached; `engine_ops.py run ...`) ─────────────────────────

def run_wrapper(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="engine_ops.py run")
    parser.add_argument("--op", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--detach", default="session")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("a command is required after --")

    try:
        os.setsid()
    except OSError:
        pass  # already a session leader (the service started us that way)

    status_file = Path(args.status)
    log_file = Path(args.log)
    record = {
        "op": args.op, "state": "running", "pid": os.getpid(),
        "pid_start_ticks": pid_start_ticks(os.getpid()),
        "started_at": args.started_at, "finished_at": None, "exit_code": None,
        "log": str(log_file), "detach": args.detach,
    }
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "wb") as log:
        log.write(f"[{_now()}] engine operation {args.op} started\n".encode())
        log.flush()
        write_status(status_file, record)
        try:
            exit_code = subprocess.run(
                cmd, cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, check=False).returncode
        except OSError as exc:
            log.write(f"could not run the command: {exc}\n".encode())
            exit_code = 127
        log.write(
            f"[{_now()}] engine operation {args.op} finished with exit code "
            f"{exit_code}\n".encode())
        log.flush()
    write_status(status_file, {
        **record, "state": "finished", "finished_at": _now(),
        "exit_code": exit_code,
    })
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"]:
        return run_wrapper(argv[1:])
    print("usage: engine_ops.py run --op <op> --status <file> --log <file> "
          "--cwd <dir> --started-at <utc> -- <command...>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
