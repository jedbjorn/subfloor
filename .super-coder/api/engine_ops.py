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
* The GUI origin and the port are pinned in that directory the first time the
  service starts and are not re-read from `instance.json` afterwards
  (`./sc engine-ops-repin` re-pins after a deliberate port change).
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


def private_dir(path: Path | None = None) -> Path:
    """The service's state directory: outside the checkout, owner-only.

    Named from the checkout's own path. Nothing inside the checkout, which the
    sandbox can write, takes part in choosing it. The leaf must be a real
    directory owned by this user with no group or other access.
    """
    if path is None:
        path = PRIVATE_DIR
    if path is None:
        root = str(REPO_ROOT.resolve())
        key = hashlib.sha256(root.encode()).hexdigest()[:16]
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", REPO_ROOT.name) or "repo"
        home = _state_home()
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"cannot create {home}: {exc}") from exc
        path = home
        for part in ("subfloor", "engine-ops", f"{name}-{key}"):
            path = path / part
            _own_directory(path)
    else:
        _own_directory(path)
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

    `running` holds while the runner OR the command it started is alive: a
    killed runner leaves its command running, and that is still an operation
    in flight. `interrupted` is a record that never reached `finished` although
    both are gone (killed, crashed, host rebooted). It does not block.
    """
    if not record:
        return None
    state = record.get("state")
    if state == "finished":
        return "finished"
    recorded_boot = record.get("boot_id")
    if recorded_boot and recorded_boot != boot_id():
        return "interrupted"
    if state == "running":
        if pid_alive(record.get("pid"), record.get("pid_start_ticks")):
            return "running"
        if pid_alive(record.get("cmd_pid"), record.get("cmd_start_ticks")):
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


def _review_port() -> int | None:
    import ports  # noqa: PLC0415  (scripts/, on sys.path)

    port = ports.resolve(persist=False).get("port")
    return port if _valid_port(port) else None


def _publish_port(port: int) -> None:
    """Tell the review API the port, through the instance config. Best-effort."""
    try:
        import ports  # noqa: PLC0415  (scripts/, on sys.path)

        if ports.stored_engine_ops_port() != port:
            ports.update({"engine_ops_port": port})
    except Exception:  # noqa: BLE001 — discovery is a convenience, not a gate
        pass


def ensure_config(*, repin: bool = False) -> dict:
    """The pinned `{port, gui_port}`, creating it on first use.

    Both values are fixed in the private directory the first time they are
    needed and are not re-read from `instance.json` afterwards, so a sandbox
    that edits that file later cannot move the port or change which origin is
    accepted. `repin=True` (`./sc engine-ops-repin`, a host command) takes the
    review port from the instance config again.
    """
    path = private_dir() / CONFIG_NAME
    stored = read_private(path) or {}
    config = {"port": stored.get("port"), "gui_port": stored.get("gui_port")}
    if not _valid_port(config["port"]):
        config["port"] = _allocate_port()
    if repin or not _valid_port(config["gui_port"]):
        config["gui_port"] = _review_port()
    if config != {"port": stored.get("port"), "gui_port": stored.get("gui_port")}:
        write_private(path, config)
    _publish_port(config["port"])
    return config


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


def start(op: str) -> dict:
    """Start one operation. Returns its public record.

    Raises UnknownOp, Busy, or StartFailed. `op` only selects a row of the
    verb table.
    """
    argv = command(op)
    state_dir = private_dir()
    path = state_dir / STATUS_NAME
    with _start_lock:
        current = read_status(path)
        if is_running(current):
            raise Busy(public_record(current))
        started_at = _now()
        base = {
            "op": op, "state": "starting", "pid": None, "pid_start_ticks": None,
            "cmd_pid": None, "cmd_start_ticks": None, "boot_id": boot_id(),
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
    recorded one (so the pid was not reused), and its command line is this
    service. The record is in the private directory; a pid written anywhere in
    the checkout is never consulted.
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
    if not any(Path(arg).name == SERVICE_SCRIPT.name for arg in _cmdline(pid)):
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
    if service_answers(port):
        return 0, f"→ engine-ops already serving {url}"
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
                       f"(host loopback only) · log {log}")
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
    return 1, f"✗ engine-ops did not start; see {log}"


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
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return 1, f"✗ engine-ops (pid {pid}) could not be signalled: {exc}"
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
        "cmd_pid": None, "cmd_start_ticks": None, "boot_id": boot_id(),
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
  repin   pin the accepted GUI origin to the current review port
  run     (internal) run one operation detached"""


def main(argv: list[str]) -> int:
    command_name = argv[0] if argv else ""
    if command_name == "run":
        return run_wrapper(argv[1:])
    if command_name in ("up", "port", "repin", "alive", "down") and in_sandbox():
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
        elif command_name == "repin":
            config = ensure_config(repin=True)
            print(f"→ engine-ops accepts the GUI at http://{BIND}:{config['gui_port']} "
                  "(restart the service to apply: ./sc engine-ops-down; ./sc engine-ops-up)")
            return 0
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
