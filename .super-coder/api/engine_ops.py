#!/usr/bin/env python3
"""Engine operations the browser operator may start: update, restart, and
harness update (Feature #93, spec #270 "Engine controls").

This is the core the host-side engine-ops service (`engine_ops_service.py`)
serves: the fixed verb table, the service's host-private state, and the
detached runner. Each operation is an existing CLI verb run from the main
checkout, on the host, under either runtime.

Where state lives, and why. On the docker runtime the whole checkout is
bind-mounted writable into the sandbox (`dispatch.sh`, `-v "$here:$here"`), so
every file under it is sandbox-writable: `instance.json`, `run/`, `logs/`. A
sandboxed shell must not be able to steer this service, so nothing the service
DECIDES from lives there:

* Its port, the GUI origin it accepts, its own pid, the operation status, and
  the operation logs live in a host-private directory outside the bind mount
  (`<XDG state home>/subfloor/engine-ops/<repo>-<path hash>/`, mode 0700,
  owned by the host user). The path is derived from the checkout's path, never
  from a file in it.
* The directory itself must resolve outside the checkout and outside every
  directory the launcher mounts into the sandbox; otherwise nothing starts.
* The GUI origin the service accepts is never taken from the checkout. It is
  pinned by an explicit operator act in a host terminal (`./sc engine-ops-pin`)
  and until then the service refuses every operation and says why. The port
  is pinned on first use; `instance.json` only tells that walk which ports to
  step past.
* The service never opens a path taken from a status record, and opens the
  files it does write with O_NOFOLLOW.
* Two things are PUBLISHED into the checkout for the review API, which may be
  inside the sandbox: `engine_ops_port` in `instance.json` and a small hint
  file. The service writes them and never reads them back.

`./sc restart` and `./sc down` stop the review server and the brokers, this
service among them. So the verb never runs as an ordinary child of the
service; it is detached from it:

* It runs under a small runner (`engine_ops.py run ...`, this file) that owns
  the status file. The runner is the only writer once it has started.
* The runner is started in its own session with stdio on a log file, never on
  inherited pipes.
* A new session is not enough when a systemd unit is the thing stopped (the
  host review server always is): stopping a unit kills every process left in
  its control group. So the runner is also placed in its own transient scope
  (`systemd-run --user --scope`) whenever a user manager answers.

The verb table is fixed. A request selects a key of `VERBS` and contributes
nothing else to the command line. Tests replace the table in code
(`mock.patch.dict(engine_ops.VERBS, ...)`); no request field and no environment
variable can.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
BIND = "127.0.0.1"

# Host-private state directory. None = derived from the checkout path
# (`private_dir`). Tests point it at a temporary directory, in code.
PRIVATE_DIR: Path | None = None
STATUS_NAME = "engine-op.json"       # current or last operation
CONFIG_NAME = "config.json"          # pinned {port, gui_port}
SERVICE_NAME = "service.json"        # the running service's identity
SERVICE_LOG_NAME = "service.log"

# Published for the review API (which may be inside the sandbox, where the
# service's port is unreachable). Written by the service, never read by it.
HINT_PARTS = (".super-coder", "run", "engine-ops.json")
ENGINE_REF_PARTS = (".sc-state", "engine.ref")

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
# How long a POST waits for the runner to record its pid before answering.
START_WAIT_SECONDS = 5.0
# How long `up` waits for the service to answer, and `down` for it to exit.
SERVICE_WAIT_SECONDS = 10.0
SERVICE_SCRIPT = Path(__file__).resolve().with_name("engine_ops_service.py")

_OP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_REF = re.compile(r"^[0-9A-Za-z._/-]{1,80}$")

_start_lock = threading.Lock()


class UnknownOp(KeyError):
    """The requested operation is not in the verb table."""


class Busy(Exception):
    """An operation is already running."""

    def __init__(self, operation: dict):
        super().__init__(f"engine operation {operation.get('op')} is running")
        self.operation = operation


class StartFailed(Exception):
    """The runner process could not be started."""


class Unpinned(Exception):
    """No operator has pinned the GUI origin yet; nothing may be started."""


class ForeignOrigin(Exception):
    """The request's origin is not the pinned GUI origin."""


class StateError(Exception):
    """The host-private state directory cannot be trusted or used."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def command(op: str) -> list[str]:
    """The fixed argv for one operation. Raises UnknownOp for anything else."""
    args = VERBS.get(op)
    if args is None:
        raise UnknownOp(op)
    return [str(REPO_ROOT / "sc"), *args]


def in_sandbox() -> bool:
    return bool(os.environ.get("SC_SANDBOX")) or any(
        os.path.exists(marker) for marker in ("/.dockerenv", "/run/.containerenv"))


# ── host-private state ───────────────────────────────────────────────────────

def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    home = Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state")
    if not home.is_absolute():
        raise StateError("XDG state home must be absolute")
    return home


def _own_directory(path: Path) -> None:
    """Create `path` 0700 if absent; require a real directory owned by us."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise StateError(f"cannot create {path}: {exc}") from exc
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StateError(f"{path} is not a directory")
    if info.st_uid != os.geteuid():
        raise StateError(f"{path} is not owned by this user")


def sandbox_visible_roots() -> list[Path]:
    """Real paths of every host location a sandbox of this install can write.

    The checkout; the owner-local instance state collection (this install's
    directory in it is mounted, and which one is named by a file in the
    checkout, so the whole collection is excluded); the engine DB's directory
    when it resolves; and every home bind mount the launcher makes, taken from
    the launcher's own list (`sandbox_devkit.HOME_BIND_MOUNTS`, the one the
    `docker run` arguments are built from), so a mount added there is excluded
    here without anyone remembering to.
    """
    try:
        import sandbox_devkit  # noqa: PLC0415  (scripts/, on sys.path)

        home_mounts = list(sandbox_devkit.home_bind_paths(Path.home()))
    except Exception as exc:  # noqa: BLE001 — unknown mounts: fail closed
        raise StateError(
            f"cannot read the launcher's sandbox mount list: {exc}") from exc
    roots = [REPO_ROOT]
    try:
        roots.append(_state_home() / "subfloor" / "instances")
    except StateError:
        pass
    try:
        import instance_state  # noqa: PLC0415  (scripts/, on sys.path)

        roots.append(Path(instance_state.active_database_path(ENGINE)).parent)
    except Exception:  # noqa: BLE001 — advisory: it can only add a root
        pass
    roots.extend(home_mounts)
    return [Path(os.path.realpath(root)) for root in roots]


_host_private_checked: set[tuple[str, str]] = set()


def _require_host_private(path: Path) -> None:
    """Refuse a state directory that a sandbox can reach.

    Checked once per process for each (path, real path) pair: the mounts are
    fixed for a process's lifetime, and a path that starts resolving somewhere
    else is checked again.
    """
    real = Path(os.path.realpath(path))
    key = (str(path), str(real))
    if key in _host_private_checked:
        return
    for root in sandbox_visible_roots():
        if real == root or root in real.parents:
            raise StateError(
                f"the engine-ops state directory {path} resolves to {real}, "
                f"inside {root}, which is mounted into the sandbox. It must be "
                "host-private: unset XDG_STATE_HOME or point it outside the "
                "checkout and the sandbox mounts.")
    _host_private_checked.add(key)


def private_dir(path: Path | None = None) -> Path:
    """The service's state directory: outside the sandbox's reach, owner-only.

    Named from the checkout's own path. Nothing inside the checkout, which the
    sandbox can write, takes part in choosing it. Its real path must lie
    outside the checkout and every directory mounted into the sandbox, and the
    leaf must be a real directory owned by this user with no group or other
    access.
    """
    if path is None:
        path = PRIVATE_DIR
    if path is None:
        root = str(REPO_ROOT.resolve())
        key = hashlib.sha256(root.encode()).hexdigest()[:16]
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", REPO_ROOT.name) or "repo"
        home = _state_home()
        _require_host_private(home / "subfloor" / "engine-ops" / f"{name}-{key}")
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"cannot create {home}: {exc}") from exc
        path = home
        for part in ("subfloor", "engine-ops", f"{name}-{key}"):
            path = path / part
            _own_directory(path)
    else:
        _require_host_private(path)
        _own_directory(path)
    _require_host_private(path)
    info = os.lstat(path)
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def status_path() -> Path:
    return private_dir() / STATUS_NAME


def log_path(op) -> Path | None:
    """The one log file an operation may have. Never taken from a record."""
    if not isinstance(op, str) or not _OP_NAME.match(op):
        return None
    return private_dir() / f"engine-op-{op}.log"


def write_private(path: Path, record: dict) -> None:
    """Replace a file in the private directory atomically.

    The temporary file is created exclusively and without following a link,
    so even a planted name cannot redirect the write.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_private(path: Path) -> dict | None:
    """A JSON object from the private directory; a link is never followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_status(path: Path | None = None) -> dict | None:
    try:
        return read_private(path or status_path())
    except StateError:
        return None


# ── files in the checkout (sandbox-writable): fixed names, no links ──────────

def _open_dir_beneath(parts: tuple[str, ...], *, create: bool = False) -> int:
    """Open a directory under the checkout root, refusing any symlink on the way."""
    fd = os.open(REPO_ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


def publish_hint(record: dict | None) -> None:
    """Write (or with None, remove) the hint the review API reads. Best-effort.

    The hint is in the checkout, so it is written with no link followed at any
    level and is never read back by the service.
    """
    *dirs, name = HINT_PARTS
    try:
        dir_fd = _open_dir_beneath(tuple(dirs), create=record is not None)
    except OSError:
        return
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        if record is None:
            os.unlink(name, dir_fd=dir_fd)
            return
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=dir_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.write("\n")
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def read_hint() -> dict:
    """The published hint, as the review API sees it (possibly in the sandbox)."""
    try:
        record = json.loads(REPO_ROOT.joinpath(*HINT_PARTS).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def engine_ref() -> str | None:
    """The installed engine pin, when this install has one.

    The pin file is in the checkout: it is read without following a link, as
    one short line, and reported only if it looks like a ref.
    """
    *dirs, name = ENGINE_REF_PARTS
    try:
        dir_fd = _open_dir_beneath(tuple(dirs))
    except OSError:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        ref = os.read(fd, 128).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return ref if _REF.match(ref) else None


# ── runtime ──────────────────────────────────────────────────────────────────

def detect_runtime() -> str:
    """Which runtime this install's lifecycle verbs drive: `host` or `docker`.

    The `instance.json` selection (scripts/runtime.py); an absent key is the
    docker sandbox, as it is for the dispatcher. It only words the GUI's
    confirmations; nothing the service does depends on it.
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


# ── process identity ─────────────────────────────────────────────────────────

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


def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def pid_alive(pid, start_ticks=None) -> bool:
    """Whether the recorded process is still running.

    A zombie has exited, and a pid whose start time differs from the recorded
    one has been reused by another process; neither is the one recorded.
    """
    if type(pid) is not int or pid <= 0:
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
    if type(start_ticks) is int:
        try:
            return int(fields[19]) == start_ticks
        except (IndexError, ValueError):
            return True
    return True


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def runs_script(pid: int, script: Path) -> bool:
    """Whether `pid` is a Python interpreter running exactly `script`.

    The command line must be `<python> <script> ...` with the script resolving
    to that file of this install. A process that merely carries the name as
    some later argument, or runs a same-named file elsewhere, is not it.
    """
    argv = _cmdline(pid)
    if len(argv) < 2 or not Path(argv[0]).name.startswith("python"):
        return False
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = "/"
    candidate = Path(argv[1])
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return os.path.realpath(candidate) == os.path.realpath(script)


def known_service_scripts() -> list[Path]:
    """Long-lived services `./sc launch` starts and leaves running by design."""
    api = ENGINE / "api"
    return [api / f"{name}_broker.py" for name in ("vm", "ts", "pm2", "db")] + [
        SERVICE_SCRIPT]


def _cgroup_of(pid: int) -> str | None:
    """The cgroup-v2 path of `pid`, or None."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:].strip() or None
    except OSError:
        pass
    return None


def operation_pids(record: dict | None) -> list[int]:
    """Live processes still doing a recorded operation's work.

    Everything the operation started: processes in the runner's session, and,
    when the runner had its own systemd scope, in that scope. Three kinds are
    not the operation's work and are left out: zombies; the runner itself once
    it has recorded the result; and the known long-lived services a restart's
    `launch` step starts on purpose (the brokers and this service), told apart
    by their exact interpreter-plus-script identity — together with everything
    those services run. A broker serves requests for as long as the engine is
    up, some of them from the sandbox, and a worker it spawns for one is the
    broker's work, not the finished operation's.
    """
    if not record:
        return []
    session = record.get("pid")
    ticks = record.get("pid_start_ticks")
    if type(session) is not int or type(ticks) is not int:
        return []
    recorded_boot = record.get("boot_id")
    if recorded_boot and recorded_boot != boot_id():
        return []
    candidates: set[int] = set()
    try:
        entries = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        entries = []
    # A live process holding the runner's pid with another start time means
    # that pid, and so that session id, now belongs to something unrelated.
    leader = _proc_stat(session)
    try:
        session_reused = bool(leader) and int(leader[19]) != ticks
    except (IndexError, ValueError):
        session_reused = False
    for pid in ([] if session_reused else entries):
        fields = _proc_stat(pid)
        try:
            # Same session as the runner, and not older than it (a session id
            # is the runner's pid; an older process cannot belong to it).
            if fields and int(fields[3]) == session and int(fields[19]) >= ticks:
                candidates.add(pid)
        except (IndexError, ValueError):
            continue
    scope = record.get("cgroup")
    if isinstance(scope, str) and scope.endswith(".scope") and "sc-engine-op-" in scope:
        try:
            text = Path("/sys/fs/cgroup" + scope, "cgroup.procs").read_text()
            candidates.update(int(line) for line in text.split())
        except (OSError, ValueError):
            pass
    services = known_service_scripts()
    is_service: dict[int, bool] = {}

    def service(pid: int) -> bool:
        if pid not in is_service:
            is_service[pid] = any(runs_script(pid, script) for script in services)
        return is_service[pid]

    def under_a_service(pid: int) -> bool:
        """`pid` is a known service, or has one among its ancestors."""
        seen = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if service(pid):
                return True
            fields = _proc_stat(pid)
            try:
                pid = int(fields[1]) if fields else 0
            except (IndexError, ValueError):
                return False
        return False

    live = []
    for pid in sorted(candidates):
        fields = _proc_stat(pid)
        if not fields or fields[0] == "Z":
            continue
        if pid == session and record.get("state") == "finished":
            continue
        if under_a_service(pid):
            continue
        live.append(pid)
    return live


# ── operation status ─────────────────────────────────────────────────────────

def _age_seconds(stamp) -> float | None:
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def effective_state(record: dict | None) -> str | None:
    """`starting`, `running`, `finished`, or `interrupted` for a stored record.

    `running` holds while anything the operation started is still doing its
    work (`operation_pids`): the runner, the command, or any process either
    left behind. So a killed runner, or a killed command whose child is still
    building, is still an operation in flight, whatever the record says.
    `interrupted` is a record that never reached `finished` although all of
    that is gone (killed, crashed, host rebooted). It does not block.
    """
    if not record:
        return None
    state = record.get("state")
    recorded_boot = record.get("boot_id")
    same_boot = not recorded_boot or recorded_boot == boot_id()
    if state == "finished":
        return "running" if operation_pids(record) else "finished"
    if not same_boot:
        return "interrupted"
    if state == "running":
        if pid_alive(record.get("pid"), record.get("pid_start_ticks")):
            return "running"
        if pid_alive(record.get("cmd_pid"), record.get("cmd_start_ticks")):
            return "running"
        return "running" if operation_pids(record) else "interrupted"
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
    op = record.get("op")
    log = log_path(op)
    return {
        "op": op,
        "state": state,
        "cli": CLI.get(op) if isinstance(op, str) else None,
        "pid": record.get("pid"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "exit_code": exit_code,
        "ok": (exit_code == 0) if state == "finished" else None,
        "log": str(log) if log else None,
        "detach": record.get("detach"),
        # Set only when the runner itself could not be started.
        "error": record.get("error"),
    }


def log_tail(record: dict | None, limit: int = LOG_TAIL_BYTES) -> tuple[str | None, bool]:
    """The last `limit` bytes of the operation's log and whether more preceded.

    The file is the fixed one for the record's operation, in the private
    directory. A path stored in a record is never opened.
    """
    path = log_path((record or {}).get("op"))
    if path is None:
        return None, False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, False
        size = info.st_size
        os.lseek(fd, max(0, size - limit), os.SEEK_SET)
        data = os.read(fd, limit)
    except OSError:
        return None, False
    finally:
        os.close(fd)
    return data.decode("utf-8", "replace"), size > limit


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


PIN_CLI = "./sc engine-ops-pin"


def unpinned_status() -> dict:
    """What the service says before an operator has pinned the GUI origin."""
    return {
        "ok": True,
        "service": "engine-ops",
        "pinned": False,
        "gui_origin": None,
        "pin_cli": PIN_CLI,
        "reason": (
            "engine operations are refused until the operator confirms this "
            f"install's GUI origin in a host terminal: {PIN_CLI}"),
        "running": False,
        "operation": None,
    }


def status(origin: str | None = None) -> dict:
    """The service's status, decided against the pin read once, here.

    Unpinned: the stateless refusal, whoever asks. Pinned: the full status,
    and a request that names an origin must name the pinned one — a request
    admitted while no pin existed gets nothing once one does.
    """
    gui_port = pinned_gui_port()
    if gui_port is None:
        return unpinned_status()
    if origin is not None and not origin_allowed(origin, gui_port):
        raise ForeignOrigin(origin)
    runtime = detect_runtime()
    record = read_status()
    tail, truncated = log_tail(record)
    return {
        "ok": True,
        "service": "engine-ops",
        "pinned": True,
        "gui_origin": f"http://{BIND}:{gui_port}",
        "pin_cli": PIN_CLI,
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


# ── pinned configuration: the service's port and the GUI origin ──────────────

def _valid_port(value) -> bool:
    return type(value) is int and 0 < value < 65536


def _allocate_port() -> int:
    """Derive the service port like the review port, from its own seed.

    `instance.json` (this fork's and its siblings') only tells the walk which
    ports to step past; a sandbox that edits it can change which free port is
    chosen and nothing else.
    """
    import ports  # noqa: PLC0415  (scripts/, on sys.path)

    avoid = set(ports._sibling_ports())
    try:
        cfg = ports.resolve(persist=False)
    except Exception:  # noqa: BLE001 — advisory input only
        cfg = {}
    avoid.update(cfg[key] for key in ("port", "dev_port", "browser_port",
                                      "browser_proxy_port")
                 if type(cfg.get(key)) is int)
    return ports._resolve_offset(
        ports._offset(str(REPO_ROOT) + ":engine-ops"), avoid)


def suggested_gui_port() -> int | None:
    """The review port `instance.json` names. A suggestion to show the
    operator, never a value to pin by itself: the sandbox can write that file."""
    try:
        import ports  # noqa: PLC0415  (scripts/, on sys.path)

        port = ports.resolve(persist=False).get("port")
    except Exception:  # noqa: BLE001 — advisory input only
        return None
    return port if _valid_port(port) else None


def _publish_port(port: int) -> None:
    """Tell the review API the port, through the instance config. Best-effort."""
    try:
        import ports  # noqa: PLC0415  (scripts/, on sys.path)

        if ports.stored_engine_ops_port() != port:
            ports.update({"engine_ops_port": port})
    except Exception:  # noqa: BLE001 — discovery is a convenience, not a gate
        pass


def _read_config() -> dict:
    return read_private(private_dir() / CONFIG_NAME) or {}


def ensure_config() -> dict:
    """The pinned `{port, gui_port}`. Allocates the port on first use.

    `gui_port` is None until an operator pins it (`pin_gui_port`); this never
    fills it in. Neither value is re-read from `instance.json`.
    """
    stored = _read_config()
    config = dict(stored)
    if not _valid_port(config.get("port")):
        config["port"] = _allocate_port()
    if not _valid_port(config.get("gui_port")) or not config.get("pinned_at"):
        config["gui_port"] = None
        config.pop("pinned_at", None)
    if config != stored:
        write_private(private_dir() / CONFIG_NAME, config)
    _publish_port(config["port"])
    return config


def pinned_gui_port() -> int | None:
    """The GUI port an operator pinned, from private state; None if unpinned."""
    try:
        config = _read_config()
    except StateError:
        return None
    port = config.get("gui_port")
    # Only a value an operator pinned counts; `pinned_at` is that act's mark.
    if not _valid_port(port) or not config.get("pinned_at"):
        return None
    return port


def pin_gui_port(port: int) -> dict:
    """Record the operator's explicit choice of the GUI port. Host act only."""
    if not _valid_port(port):
        raise ValueError("the GUI port must be an integer from 1 to 65535")
    config = ensure_config()
    config.update({"gui_port": port, "pinned_at": _now()})
    write_private(private_dir() / CONFIG_NAME, config)
    return config


def origin_allowed(origin, gui_port) -> bool:
    """THE origin check: is `origin` the GUI origin for pinned port `gui_port`?

    One place, so that whatever records the pin later (spec #271's host-run
    enrolment) and whatever else needs the decision reuse exactly this.
    """
    return isinstance(origin, str) and origin in gui_origins(gui_port)


def gui_origins(gui_port) -> frozenset[str]:
    if not _valid_port(gui_port):
        return frozenset()
    return frozenset(f"http://{host}:{gui_port}" for host in (BIND, "localhost"))


# ── discovery (served by the review API) ─────────────────────────────────────

def _port_answers(port: int) -> bool:
    try:
        socket.create_connection((BIND, port), timeout=0.5).close()
    except OSError:
        return False
    return True


def discovery(sandboxed: bool = False) -> dict:
    """What the GUI needs to find the service: its port, URL, and whether it is up.

    On the host the port is probed. Inside the sandbox it cannot be (the
    container has no route to host loopback, by design), so `up` is whether the
    service's published hint names this port; a service that was killed without
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
    elif sandboxed:
        up, source = read_hint().get("port") == port, "state_file"
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


# ── start an operation ───────────────────────────────────────────────────────

def scope_prefix(op: str) -> list[str]:
    """argv prefix that puts the runner in its own systemd user scope, or [].

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
    """The runner's environment: the engine's own, never a launched shell's."""
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


def _spawn(argv: list[str], stdout=subprocess.DEVNULL) -> subprocess.Popen:
    proc = subprocess.Popen(
        argv, cwd=str(REPO_ROOT), env=child_env(),
        stdin=subprocess.DEVNULL, stdout=stdout,
        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    # Reap it if it ends while this process still lives, so a finished child
    # never lingers as a zombie that reads as a live pid.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _wait_recorded(proc: subprocess.Popen, path: Path, started_at: str,
                   timeout: float) -> dict | None:
    """The record once the runner has taken it over, or None."""
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


def start(op: str, origin: str | None) -> dict:
    """Start one operation for a request from `origin`. Returns its public record.

    Raises UnknownOp, Unpinned, ForeignOrigin, Busy, or StartFailed. `op` only
    selects a row of the verb table.

    The origin decision is made here, once, against the pin as it is at the
    moment of starting — not earlier in the request. Whatever let the request
    reach this point (including the rule that lets any loopback page read the
    unpinned refusal) authorises nothing: no pin, or an origin that is not the
    pinned one, starts nothing.
    """
    argv = command(op)
    state_dir = private_dir()
    gui_port = pinned_gui_port()
    if gui_port is None:
        raise Unpinned(unpinned_status()["reason"])
    if not origin_allowed(origin, gui_port):
        raise ForeignOrigin(origin)
    path = state_dir / STATUS_NAME
    with _start_lock:
        current = read_status(path)
        if is_running(current):
            raise Busy(public_record(current))
        started_at = _now()
        base = {
            "op": op, "state": "starting", "pid": None, "pid_start_ticks": None,
            "cmd_pid": None, "cmd_start_ticks": None, "cgroup": None,
            "boot_id": boot_id(),
            "started_at": started_at, "finished_at": None, "exit_code": None,
        }
        prefix = scope_prefix(op)
        attempts = [(prefix, "systemd-scope")] if prefix else []
        attempts.append(([], "session"))
        failure = "the runner did not start"
        for lead, detach in attempts:
            write_private(path, {**base, "detach": detach})
            runner = [
                sys.executable, str(Path(__file__).resolve()), "run",
                "--op", op, "--state-dir", str(state_dir),
                "--cwd", str(REPO_ROOT), "--started-at", started_at,
                "--detach", detach, "--", *argv,
            ]
            try:
                proc = _spawn(lead + runner)
            except OSError as exc:
                failure = str(exc)
                continue
            record = _wait_recorded(proc, path, started_at, START_WAIT_SECONDS)
            if record is not None:
                return public_record(record)
            if proc.poll() is None:
                # Alive but slow to record: the claim stands for the grace window.
                return public_record(read_status(path))
            failure = f"the runner exited with status {proc.returncode} before recording"
        write_private(path, {
            **base, "state": "finished", "finished_at": _now(),
            "detach": None, "error": failure,
        })
        raise StartFailed(failure)


# ── the service process: identity, start, stop ───────────────────────────────

def service_record() -> dict | None:
    """The running service's recorded identity, only if that process is it.

    Proven three ways: the recorded pid is alive, its kernel start time is the
    recorded one (so the pid was not reused), and it is a Python interpreter
    running exactly this install's service script. The record is in the
    private directory; a pid written anywhere in the checkout is never
    consulted.
    """
    try:
        record = read_private(private_dir() / SERVICE_NAME)
    except StateError:
        return None
    if not record:
        return None
    pid, ticks = record.get("pid"), record.get("pid_start_ticks")
    if type(pid) is not int or type(ticks) is not int:
        return None
    if not pid_alive(pid, ticks):
        return None
    if not runs_script(pid, SERVICE_SCRIPT):
        return None
    return record


def register_service(port: int) -> None:
    """Record this process as the running service (called by the service)."""
    write_private(private_dir() / SERVICE_NAME, {
        "service": "engine-ops", "pid": os.getpid(),
        "pid_start_ticks": pid_start_ticks(os.getpid()),
        "port": port, "started_at": _now(),
    })
    publish_hint({"service": "engine-ops", "port": port, "started_at": _now()})


def unregister_service() -> None:
    try:
        path = private_dir() / SERVICE_NAME
        if (read_private(path) or {}).get("pid") == os.getpid():
            os.unlink(path)
    except (OSError, StateError):
        pass
    publish_hint(None)


def service_answers(port: int) -> bool:
    """Whether the engine-ops service answers `GET /health` on `port`."""
    import http.client  # noqa: PLC0415

    conn = http.client.HTTPConnection(BIND, port, timeout=2)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = json.loads(response.read() or b"{}")
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()
    return response.status == 200 and body.get("service") == "engine-ops"


def service_argv() -> list[str]:
    return [sys.executable, str(SERVICE_SCRIPT)]


def service_up() -> tuple[int, str]:
    """Start the service in the background, once. Returns (exit code, message)."""
    if in_sandbox():
        return 0, "→ engine-ops: NOT started — it is a host process and this is the sandbox"
    config = ensure_config()
    port = config["port"]
    url = f"http://{BIND}:{port}"
    pin = pin_summary(pinned_gui_port())
    if service_answers(port):
        return 0, f"→ engine-ops already serving {url}\n{pin}"
    log = private_dir() / SERVICE_LOG_NAME
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        proc = _spawn(service_argv(), stdout=fd)
    except OSError as exc:
        return 1, f"✗ engine-ops did not start: {exc}"
    finally:
        os.close(fd)
    deadline = time.monotonic() + SERVICE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if service_answers(port):
            return 0, (f"→ engine-ops up (pid {proc.pid}) · {url} "
                       f"(host loopback only) · log {log}\n{pin}")
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
    return 1, f"✗ engine-ops did not start; see {log}"


def pin_summary(gui_port) -> str:
    if _valid_port(gui_port):
        return f"  engine-ops accepts the GUI at http://{BIND}:{gui_port}"
    return ("  engine-ops: GUI origin NOT pinned — the GUI's engine controls are "
            f"refused until you run: {PIN_CLI}")


def pin_command(argv: list[str]) -> int:
    """`./sc engine-ops-pin [<port>|--show]` — the operator's explicit act."""
    current = pinned_gui_port()
    if argv[:1] == ["--show"]:
        print(pin_summary(current).strip())
        return 0 if current is not None else 1
    suggestion = suggested_gui_port()
    if argv:
        if len(argv) != 1 or not argv[0].isdigit():
            print("usage: ./sc engine-ops-pin [<review GUI port> | --show]",
                  file=sys.stderr)
            return 2
        chosen = int(argv[0])
    else:
        if not sys.stdin.isatty():
            print("✗ engine-ops-pin needs a terminal, or the port as an argument: "
                  "./sc engine-ops-pin <review GUI port>", file=sys.stderr)
            return 1
        print("The engine-ops service runs ./sc update, restart, and "
              "update-harnesses when the review GUI asks.")
        print("It accepts those requests from ONE origin: this install's review GUI.")
        if current is not None:
            print(f"  currently pinned : http://{BIND}:{current}")
        if suggestion is not None:
            print(f"  instance.json says the review GUI is at http://{BIND}:{suggestion}")
            print("  (that file is writable from the sandbox — check it against "
                  "the address in your browser)")
        answer = input("Type the review GUI's port to pin it (anything else aborts): ").strip()
        if not answer.isdigit():
            print("→ engine-ops-pin aborted (nothing changed)")
            return 1
        chosen = int(answer)
    try:
        config = pin_gui_port(chosen)
    except ValueError as exc:
        print(f"✗ engine-ops-pin: {exc}", file=sys.stderr)
        return 1
    print(f"→ pinned: engine-ops accepts the GUI at http://{BIND}:{config['gui_port']} "
          "(takes effect immediately)")
    return 0


def service_down() -> tuple[int, str]:
    """Stop the service this install started, and only that process."""
    record = service_record()
    if record is None:
        try:
            port = (read_private(private_dir() / CONFIG_NAME) or {}).get("port")
        except StateError:
            port = None
        if _valid_port(port) and service_answers(port):
            return 0, ("→ engine-ops answers but its recorded identity does not "
                       "match a running service — leaving it")
        return 0, "→ engine-ops not running"
    pid, ticks = record["pid"], record["pid_start_ticks"]
    try:
        # Hold the process itself, prove identity again on that handle, and
        # signal through it: the pid cannot be reused between the proof and
        # the signal.
        handle = os.pidfd_open(pid)
    except (AttributeError, OSError):
        handle = None
    try:
        if not pid_alive(pid, ticks) or not runs_script(pid, SERVICE_SCRIPT):
            return 0, "→ engine-ops not running"
        if handle is not None:
            signal.pidfd_send_signal(handle, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return 1, f"✗ engine-ops (pid {pid}) could not be signalled: {exc}"
    finally:
        if handle is not None:
            os.close(handle)
    deadline = time.monotonic() + SERVICE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid, ticks):
            return 0, "→ engine-ops stopped"
        time.sleep(0.05)
    return 1, f"✗ engine-ops (pid {pid}) did not stop after SIGTERM"


# ── runner (detached; `engine_ops.py run ...`) ───────────────────────────────

def run_wrapper(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="engine_ops.py run")
    parser.add_argument("--op", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--detach", default="session")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("a command is required after --")
    if not _OP_NAME.match(args.op):
        parser.error("invalid operation name")

    try:
        os.setsid()
    except OSError:
        pass  # already a session leader (the service started us that way)

    state_dir = private_dir(Path(args.state_dir))
    status_file = state_dir / STATUS_NAME
    log_file = state_dir / f"engine-op-{args.op}.log"
    record = {
        "op": args.op, "state": "running", "pid": os.getpid(),
        "pid_start_ticks": pid_start_ticks(os.getpid()),
        "cmd_pid": None, "cmd_start_ticks": None,
        # Its own systemd scope, when it has one: everything the operation
        # starts stays in it, even a process that leaves the session.
        "cgroup": (_cgroup_of(os.getpid())
                   if args.detach == "systemd-scope" else None),
        "boot_id": boot_id(),
        "started_at": args.started_at, "finished_at": None, "exit_code": None,
        "detach": args.detach,
    }
    fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                 0o600)
    with os.fdopen(fd, "wb") as log:
        log.write(f"[{_now()}] engine operation {args.op} started\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT)
        except OSError as exc:
            write_private(status_file, record)
            log.write(f"could not run the command: {exc}\n".encode())
            exit_code = 127
        else:
            # The command's own identity: if this runner is killed, the gate
            # still holds for as long as the command lives.
            record["cmd_pid"] = proc.pid
            record["cmd_start_ticks"] = pid_start_ticks(proc.pid)
            write_private(status_file, record)
            exit_code = proc.wait()
        log.write(
            f"[{_now()}] engine operation {args.op} finished with exit code "
            f"{exit_code}\n".encode())
        log.flush()
    write_private(status_file, {
        **record, "state": "finished", "finished_at": _now(),
        "exit_code": exit_code,
    })
    return 0


USAGE = """usage: engine_ops.py <command>
  up      start the engine-ops service in the background (host only)
  down    stop the service this install started
  alive   exit 0 when the service answers
  port    print the service's pinned host loopback port
  pin     pin the GUI origin the service accepts (operator, host terminal)
  run     (internal) run one operation detached"""


def main(argv: list[str]) -> int:
    command_name = argv[0] if argv else ""
    if command_name == "run":
        return run_wrapper(argv[1:])
    if command_name in ("up", "port", "pin", "alive", "down") and in_sandbox():
        if command_name == "up":
            print(service_up()[1])
            return 0
        print("engine-ops is a host service; this is the sandbox", file=sys.stderr)
        return 1
    try:
        if command_name == "up":
            code, message = service_up()
        elif command_name == "down":
            code, message = service_down()
        elif command_name == "alive":
            return 0 if service_answers(ensure_config()["port"]) else 1
        elif command_name == "port":
            print(ensure_config()["port"])
            return 0
        elif command_name == "pin":
            return pin_command(argv[1:])
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except StateError as exc:
        print(f"✗ engine-ops: {exc}", file=sys.stderr)
        return 1
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.path.insert(0, str(ENGINE / "scripts"))
    raise SystemExit(main(sys.argv[1:]))
