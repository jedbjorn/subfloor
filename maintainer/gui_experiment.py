#!/usr/bin/env python3
"""Exact committed-source GUI seat, without installing or starting production services.

Start: python3 maintainer/gui_experiment.py start --source-repo . --ref HEAD
       --receipt /absolute/evidence/run.json [--port N] [--runtime none]
Stop:  python3 maintainer/gui_experiment.py stop --receipt /absolute/evidence/run.json

Only the fixture bootstrap substitutes the runtime callback. Normal engine
startup and the dos-app promotion canary are untouched. A future copied
scripts/gui_experiment_runtime.py may implement start_fixture(**context),
returning a shutdown callable, for the explicit ``experimental`` mode.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

PREFIX = "subfloor-gui-experiment-"
MARKER = ".gui-experiment-owner.json"
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Independently located ownership authority: exported receipts are not trusted
# paths to roots/units. Kept after cleanup so repeated stop remains verifiable.
REGISTRY = Path("/tmp") / f"subfloor-gui-experiments-{os.geteuid()}"
SOURCE_FILES = (
    ".super-coder/schema.sql", ".super-coder/scripts/migrate.py",
    ".super-coder/api/server.py", ".super-coder/api/transport.py",
    ".super-coder/ui/index.html", ".super-coder/ui/app.js",
    ".super-coder/ui/style.css",
)
IDENTITY_KEYS = (
    "fixture_id", "source_sha", "archive_sha256", "root", "root_device",
    "root_inode", "unit", "ownership_nonce", "runtime", "port", "limits",
    "bootstrap_sha256",
)
MAX_LIFETIME = 1800
MAX_MEMORY_MIB = 512
MAX_TASKS = 64


class FixtureError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def command(argv: list[str], *, timeout: float = 20, check: bool = True,
            cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                                timeout=timeout, env=clean_environment(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FixtureError("COMMAND_UNAVAILABLE", f"{Path(argv[0]).name} failed or timed out") from exc
    if check and result.returncode:
        raise FixtureError("COMMAND_FAILED", f"{Path(argv[0]).name} exited {result.returncode}")
    return result


def clean_environment() -> dict[str, str]:
    """Do not inherit control-plane/provider credentials or Python import roots."""
    keep = {"PATH", "LANG", "TERM", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    return {key: value for key, value in os.environ.items()
            if key in keep or key.startswith("LC_")}


def private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise FixtureError("OWNERSHIP_INVALID", "expected an owner-only regular file")


def read_json(path: Path) -> dict[str, Any]:
    try:
        private_file(path)
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise TypeError("not object")
        return payload
    except (OSError, ValueError, TypeError) as exc:
        raise FixtureError("RECORD_INVALID", "ownership record is missing or invalid") from exc


def write_json(path: Path, payload: dict[str, Any]) -> None:
    # Both ledger and external receipt are atomic, owner-only, and never follow
    # a supplied symlink. Existing files must already belong to this helper.
    if path.exists() or path.is_symlink():
        private_file(path)
    fd, name = tempfile.mkstemp(prefix=".gui-experiment-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def registry() -> Path:
    try:
        REGISTRY.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = REGISTRY.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise FixtureError("OWNERSHIP_INVALID", "fixture registry must be an owner-only directory")
    return REGISTRY


def ledger_path(fixture_id: str) -> Path:
    if not ID_RE.fullmatch(fixture_id):
        raise FixtureError("RECORD_INVALID", "invalid fixture identity")
    return registry() / f"{fixture_id}.json"


@contextlib.contextmanager
def ownership_lock(fixture_id: str):
    path = ledger_path(fixture_id).with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def identity(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return {key: record[key] for key in IDENTITY_KEYS}
    except KeyError as exc:
        raise FixtureError("RECORD_INVALID", "incomplete fixture identity") from exc


def save(record: dict[str, Any], receipt: Path) -> None:
    write_json(ledger_path(record["fixture_id"]), record)
    write_json(receipt, record)


def canonical_receipt(receipt: Path) -> Path:
    """Aliases of an existing parent must share one lock and receipt identity."""
    try:
        parent = receipt.absolute().parent.resolve(strict=True)
    except OSError as exc:
        raise FixtureError("INPUT_INVALID", "receipt parent must already exist") from exc
    if not parent.is_dir():
        raise FixtureError("INPUT_INVALID", "receipt parent must be a directory")
    return parent / receipt.name


def verify_receipt(receipt: Path) -> dict[str, Any]:
    public = read_json(receipt)
    trusted = read_json(ledger_path(public.get("fixture_id", "")))
    if identity(public) != identity(trusted) or trusted.get("receipt") != str(receipt):
        raise FixtureError("OWNERSHIP_INVALID", "receipt does not match the retained ownership ledger")
    fid = trusted["fixture_id"]
    if (trusted["unit"] != f"{PREFIX}{fid}.service"
            or Path(trusted["root"]).name != f"{PREFIX}{fid}"
            or not SHA_RE.fullmatch(trusted["source_sha"])):
        raise FixtureError("OWNERSHIP_INVALID", "invalid derived resource identity")
    return trusted


def verify_root(record: dict[str, Any]) -> Path:
    root = Path(record["root"])
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino) != (record["root_device"], record["root_inode"])):
        raise FixtureError("OWNERSHIP_INVALID", "fixture root was replaced or has unsafe ownership")
    if identity(read_json(root / MARKER)) != identity(record):
        raise FixtureError("OWNERSHIP_INVALID", "fixture marker does not match the ownership ledger")
    return root


def description(record: dict[str, Any]) -> str:
    return f"Subfloor GUI experiment {record['fixture_id']} {record['ownership_nonce']}"


def unit_state(record: dict[str, Any]) -> dict[str, str]:
    result = command(["systemctl", "--user", "show", record["unit"],
                      "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                      "-p", "Description", "-p", "ControlGroup", "-p", "MainPID"],
                     check=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not values.get("LoadState"):
        raise FixtureError("SUPERVISOR_UNAVAILABLE", "systemd unit state could not be verified")
    return values


def owned_unit(record: dict[str, Any], state: dict[str, str]) -> bool:
    if state["LoadState"] == "not-found":
        return False
    if state.get("Description") != description(record):
        raise FixtureError("OWNERSHIP_INVALID", "systemd unit identity differs from the recorded fixture")
    return True


def cgroup_pids(path: str) -> list[int]:
    if not path:
        return []
    if not path.startswith("/") or ".." in Path(path).parts:
        raise FixtureError("CLEANUP_UNVERIFIED", "invalid systemd cgroup path")
    root = Path("/sys/fs/cgroup") / path.lstrip("/")
    if not root.exists():
        return []
    try:
        return sorted({int(line) for file in root.rglob("cgroup.procs")
                       for line in file.read_text().splitlines()})
    except OSError as exc:
        raise FixtureError("CLEANUP_UNVERIFIED", "owned cgroup cannot be inspected") from exc


def process_start_ticks(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        # comm may contain spaces and parentheses; fields after its final ')'
        # begin at field 3, making starttime (field 22) index 19.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise FixtureError("CLEANUP_UNVERIFIED", "recorded process identity cannot be inspected") from exc


def stop(receipt: Path) -> dict[str, Any]:
    receipt = canonical_receipt(receipt)
    initial = read_json(receipt)
    with ownership_lock(initial.get("fixture_id", "")):
        return stop_locked(receipt)


def stop_locked(receipt: Path) -> dict[str, Any]:
    record = verify_receipt(receipt)
    root = Path(record["root"])
    if root.exists() or root.is_symlink():
        verify_root(record)
    elif not record.get("cleanup", {}).get("root_removed"):
        raise FixtureError("OWNERSHIP_INVALID", "fixture root disappeared without a cleanup record")
    before = unit_state(record)
    exists = owned_unit(record, before)
    cgroup = before.get("ControlGroup", "") or record.get("control_group", "")
    if exists:
        command(["systemctl", "--user", "stop", record["unit"]], timeout=15)
    deadline = time.monotonic() + 10
    while True:
        state = unit_state(record)
        owned_unit(record, state)
        pids = cgroup_pids(cgroup)
        recorded_live = (record.get("main_pid", 0) > 0
                         and process_start_ticks(record["main_pid"])
                         == record.get("main_pid_start_ticks"))
        if (state.get("ActiveState") in {"inactive", "failed"}
                and state.get("MainPID", "0") == "0" and not pids and not recorded_live):
            break
        if time.monotonic() >= deadline:
            record["cleanup"] = {"complete": False, "unit_state": state.get("ActiveState"),
                                 "surviving_pids": pids, "root_removed": False,
                                 "recorded_process_exited": not recorded_live}
            save(record, receipt)
            raise FixtureError("CLEANUP_UNVERIFIED", "owned process/unit did not become inactive")
        time.sleep(.1)
    if root.exists():
        verify_root(record)
        shutil.rmtree(root)
    record["status"] = "stopped"
    record["cleanup"] = {"complete": True, "unit_load_state": state["LoadState"],
                         "unit_state": state.get("ActiveState"), "cgroup_empty": True,
                         "recorded_process_exited": True, "root_removed": not root.exists(),
                         "already_stopped": not exists}
    save(record, receipt)
    return record


def validate_limits(lifetime: int, memory_mib: int, tasks: int) -> dict[str, int]:
    if not (1 <= lifetime <= MAX_LIFETIME and 1 <= memory_mib <= MAX_MEMORY_MIB
            and 1 <= tasks <= MAX_TASKS):
        raise FixtureError("INPUT_INVALID", "limits exceed the released GUI envelope")
    return {"lifetime_seconds": lifetime, "memory_mib": memory_mib, "tasks": tasks,
            "term_grace_seconds": 5, "exit_verification_seconds": 10}


def select_port(port: int | None) -> int:
    if port is None and os.environ.get("SC_DEV_PORT"):
        try:
            port = int(os.environ["SC_DEV_PORT"])
        except ValueError as exc:
            raise FixtureError("INPUT_INVALID", "SC_DEV_PORT is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise FixtureError("INPUT_INVALID", "port must be 1..65535")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port or 0))
        except OSError as exc:
            raise FixtureError("BIND_CONFLICT", "requested loopback port is already occupied") from exc
        return int(sock.getsockname()[1])


def archive(source_repo: Path, ref: str) -> tuple[str, bytes]:
    sha = command(["git", "-C", str(source_repo), "rev-parse", "--verify", "--end-of-options",
                   f"{ref}^{{commit}}"], check=False).stdout.strip()
    if not SHA_RE.fullmatch(sha):
        raise FixtureError("REF_INVALID", "requested ref does not resolve to a full commit")
    result = subprocess.run(["git", "-C", str(source_repo), "archive", sha, ".super-coder"],
                            capture_output=True, env=clean_environment(), timeout=20, check=False)
    if result.returncode:
        raise FixtureError("SOURCE_INVALID", "committed engine archive is unavailable")
    return sha, result.stdout


def start(source_repo: Path, ref: str, receipt: Path, *, temp_parent: Path | None = None,
          port: int | None = None, runtime: str = "none", lifetime: int = MAX_LIFETIME,
          memory_mib: int = MAX_MEMORY_MIB, tasks: int = MAX_TASKS) -> dict[str, Any]:
    # Serialize starts using the same exported receipt, even before a random
    # fixture ID exists. A second concurrent start then sees the first receipt.
    if runtime not in {"none", "experimental"}:
        raise FixtureError("RUNTIME_UNAVAILABLE", "unknown fixture runtime mode")
    validate_limits(lifetime, memory_mib, tasks)
    receipt = canonical_receipt(receipt)
    receipt_key = hashlib.sha256(str(receipt).encode()).hexdigest()[:32]
    with ownership_lock(receipt_key):
        return start_serialized(source_repo, ref, receipt, temp_parent=temp_parent,
                                port=port, runtime=runtime, lifetime=lifetime,
                                memory_mib=memory_mib, tasks=tasks)


def start_serialized(source_repo: Path, ref: str, receipt: Path, *,
                     temp_parent: Path | None, port: int | None, runtime: str,
                     lifetime: int, memory_mib: int, tasks: int) -> dict[str, Any]:
    if runtime not in {"none", "experimental"}:
        raise FixtureError("RUNTIME_UNAVAILABLE", "unknown fixture runtime mode")
    limits = validate_limits(lifetime, memory_mib, tasks)
    receipt = receipt.absolute()
    if receipt.exists() or receipt.is_symlink() or not receipt.parent.is_dir():
        raise FixtureError("INPUT_INVALID", "receipt must be new and its parent must exist")
    selected_port = select_port(port)
    sha, raw = archive(source_repo, ref)
    if sys.version_info[:2] != (3, 14):
        raise FixtureError("SEAT_UNAVAILABLE", "fixture requires Python 3.14")
    command(["systemctl", "--user", "show-environment"])
    # Public resource identity is not an authentication secret. Keep actual
    # shell credentials and the independent ownership nonce on secrets below.
    fid = uuid.uuid4().hex
    with ownership_lock(fid):
        return start_locked(source_repo, sha, raw, receipt, temp_parent=temp_parent,
                            port=selected_port, runtime=runtime, limits=limits, fixture_id=fid)


def start_locked(source_repo: Path, sha: str, raw: bytes, receipt: Path, *,
                 temp_parent: Path | None, port: int, runtime: str,
                 limits: dict[str, int], fixture_id: str) -> dict[str, Any]:
    fid = fixture_id
    parent = Path(temp_parent or tempfile.gettempdir()).resolve()
    root = parent / f"{PREFIX}{fid}"
    root.mkdir(mode=0o700)
    info = root.lstat()
    record: dict[str, Any] = {
        "version": 1, "fixture_id": fid, "source_sha": sha,
        "archive_sha256": hashlib.sha256(raw).hexdigest(), "root": str(root),
        "root_device": info.st_dev, "root_inode": info.st_ino,
        "unit": f"{PREFIX}{fid}.service", "ownership_nonce": secrets.token_hex(32),
        "runtime": runtime, "port": port, "limits": limits,
        "bootstrap_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        "receipt": str(receipt), "status": "preparing", "cleanup": {"complete": False},
    }
    try:
        write_json(root / MARKER, identity(record))
        save(record, receipt)
    except (OSError, FixtureError):
        # Fresh root is still our in-memory allocation: no unit or child has
        # been created. A failure to persist ownership cannot strand it.
        shutil.rmtree(root)
        raise FixtureError("RECORD_INVALID", "could not retain fixture ownership before preparation") from None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(root, filter="data")
        for name in SOURCE_FILES:
            if not (root / name).is_file() or (root / name).is_symlink():
                raise FixtureError("SOURCE_INVALID", f"required source file is missing: {name}")
        if runtime == "experimental" and not (root / ".super-coder/scripts/gui_experiment_runtime.py").is_file():
            raise FixtureError("RUNTIME_UNAVAILABLE", "copied source has no experimental fixture runtime")
        record["source_files"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                  for name in SOURCE_FILES}
        # Script is copied from this maintainer helper, never imported through
        # ./sc or installed/global module resolution.
        bootstrap = root / "fixture_bootstrap.py"
        shutil.copyfile(Path(__file__).resolve(), bootstrap)
        (root / ".sc-state").mkdir()
        (root / ".sc-state/engine.ref").write_text(sha + "\n")
        save(record, receipt)
        if unit_state(record)["LoadState"] != "not-found":
            raise FixtureError("UNIT_CONFLICT", "generated fixture unit already exists")
        command(["systemd-run", "--user", "--quiet", "--collect", "--unit", record["unit"],
                 "--description", description(record), "-p", "Type=exec",
                 "-p", "KillMode=control-group", "-p", "SendSIGKILL=yes",
                 "-p", "TimeoutStopSec=5s", "-p", f"RuntimeMaxSec={limits['lifetime_seconds']}s",
                 "-p", f"MemoryMax={limits['memory_mib']}M", "-p", f"TasksMax={limits['tasks']}",
                 "-p", f"WorkingDirectory={root}",
                 "-p", f"StandardOutput=append:{root / 'server.log'}",
                 "-p", f"StandardError=append:{root / 'server.log'}",
                 sys.executable, "-I", str(bootstrap), "_serve", "--root", str(root)])
        deadline = time.monotonic() + 15
        url = f"http://127.0.0.1:{port}"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/api/experiment-fixture", timeout=.5) as response:
                    observed = json.load(response)
                if observed.get("fixture_id") != fid or observed.get("source_sha") != sha:
                    raise FixtureError("BIND_CONFLICT", "another listener answered the fixture identity request")
                with urllib.request.urlopen(url + "/api/health", timeout=.5) as response:
                    health = json.load(response)
                if health.get("ok") is not True:
                    raise FixtureError("STARTUP_FAILED", "fixture health did not report success")
                state = unit_state(record)
                if not owned_unit(record, state) or state.get("ActiveState") != "active":
                    raise FixtureError("STARTUP_FAILED", "fixture supervisor is not active")
                record.update({"status": "serving", "url": url, "loaded_source": observed,
                               "control_group": state.get("ControlGroup", ""),
                               "main_pid": int(state.get("MainPID", "0"))})
                record["main_pid_start_ticks"] = process_start_ticks(record["main_pid"])
                if record["main_pid_start_ticks"] is None:
                    raise FixtureError("STARTUP_FAILED", "fixture process exited before identity capture")
                save(record, receipt)
                return record
            except (OSError, ValueError, urllib.error.HTTPError):
                if unit_state(record).get("ActiveState") in {"inactive", "failed"}:
                    break
                time.sleep(.1)
        raise FixtureError("STARTUP_FAILED", "fixture did not become healthy within 15 seconds")
    except Exception as exc:
        record["status"] = "failed"
        record["error_code"] = exc.code if isinstance(exc, FixtureError) else "PREPARATION_FAILED"
        log = root / "server.log"
        if log.is_file():
            # Retain diagnostics outside removed state, without full inherited
            # environment, account inventories, prompts, or transcript content.
            text = log.read_text(errors="replace")[-12000:]
            text = re.sub(r"(?i)((?:token|secret|api[_-]?key|password)\s*[=:]\s*)\S+", r"\1[redacted]", text)
            record["diagnostics"] = text
        save(record, receipt)
        try:
            stopped = stop_locked(receipt)
            stopped["status"] = "failed"
            stopped["error_code"] = record["error_code"]
            save(stopped, receipt)
        except FixtureError:
            # Unverified cleanup keeps root, ledger and the failed receipt.
            raise FixtureError("CLEANUP_UNVERIFIED", "fixture failed and cleanup requires inspection") from exc
        if isinstance(exc, FixtureError):
            raise
        raise FixtureError("PREPARATION_FAILED", "fixture preparation failed; see retained receipt") from exc


def verified_bootstrap_root(root: Path, bootstrap: Path) -> tuple[Path, dict[str, Any]]:
    marker = read_json(root / MARKER)
    trusted = read_json(ledger_path(marker.get("fixture_id", "")))
    retained_root = Path(trusted["root"])
    if root.absolute() != retained_root:
        raise FixtureError("OWNERSHIP_INVALID", "bootstrap caller root differs from the retained fixture")
    verified = verify_root(trusted)
    if (bootstrap.absolute() != verified / "fixture_bootstrap.py" or bootstrap.is_symlink()
            or hashlib.sha256(bootstrap.read_bytes()).hexdigest() != trusted["bootstrap_sha256"]):
        raise FixtureError("OWNERSHIP_INVALID", "bootstrap file differs from the retained helper identity")
    return verified, trusted


def serve(root: Path) -> int:
    """Internal test-only bootstrap, executed from the marked archive."""
    root, trusted = verified_bootstrap_root(root, Path(__file__).absolute())
    sanitized = clean_environment()
    os.environ.clear()
    os.environ.update(sanitized)
    os.environ.update({"PATH": os.defpath, "SC_BIND": "127.0.0.1",
                       "XDG_STATE_HOME": str(root / "xdg-state"),
                       "XDG_CONFIG_HOME": str(root / "xdg-config"),
                       "XDG_DATA_HOME": str(root / "xdg-data"),
                       "PYTHONUNBUFFERED": "1"})
    sys.path[:] = [item for item in sys.path if item and (
        "site-packages" in item or item.startswith(sys.base_prefix))]
    engine = root / ".super-coder"
    # Some copied read APIs use Path.home/expanduser for harness inventory.
    # Keep those reads inside the synthetic seat without changing host HOME,
    # CODEX_HOME, account files, or the production modules.
    fixture_home = root / "home"
    fixture_home.mkdir()
    mock.patch.object(Path, "home", return_value=fixture_home).start()
    original_expanduser = os.path.expanduser
    mock.patch.object(os.path, "expanduser", side_effect=lambda value: (
        str(fixture_home) + value[1:] if isinstance(value, str)
        and (value == "~" or value.startswith("~/")) else original_expanduser(value))).start()
    sys.path.insert(0, str(engine / "scripts"))
    sys.path.insert(0, str(engine / "api"))
    import sqlite3

    import migrate
    db = engine / "shell_db.db"
    if db.exists():
        raise FixtureError("STATE_CONFLICT", "fixture database already exists")
    con = sqlite3.connect(db)
    con.executescript((engine / "schema.sql").read_text())
    con.commit()
    con.close()
    migrate.migrate(str(db))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO users(user_id,username,is_active) VALUES(1,'fixture-operator',1),(2,'fixture-other',0)")
    shell_prefix = "fx" + trusted["fixture_id"][:10]
    for sid, short, owner in ((1, shell_prefix + "a", 1), (2, shell_prefix + "b", 1),
                              (3, shell_prefix + "other", 2)):
        con.execute("INSERT INTO shells(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
                    "VALUES(?,?,?,'dev','Isolated GUI fixture',?,?)",
                    (sid, short, short, owner, secrets.token_hex(32)))
        (root / ".sc-worktrees" / short).mkdir(parents=True)
    con.commit()
    con.close()
    import server
    import transport
    actual = Path(server.__file__).resolve()
    if actual != engine / "api/server.py" or Path(server.DB_PATH).resolve() != db:
        raise FixtureError("SOURCE_IDENTITY_INVALID", "server loaded outside the marked fixture")
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    if hashes != trusted["source_files"]:
        raise FixtureError("SOURCE_IDENTITY_INVALID", "copied source differs from the retained archive")
    runtime_stop = None

    def start_selected() -> None:
        nonlocal runtime_stop
        if trusted["runtime"] == "none":
            return
        gui_experiment_runtime = importlib.import_module("gui_experiment_runtime")
        runtime_stop = gui_experiment_runtime.start_fixture(
            database=db, root=root, fixture_id=trusted["fixture_id"])
        if not callable(runtime_stop):
            raise FixtureError("RUNTIME_UNAVAILABLE", "experimental start_fixture must return a shutdown callable")

    base_dispatch = server.dispatch_http

    def dispatch(method: str, path: str, headers_raw: str, body: bytes) -> tuple:
        if path == "/api/experiment-fixture" and method == "GET":
            services = {name: getattr(getattr(server, name), "_SERVICE", None) is not None
                        for name in ("conversation_broker", "conversation_reaper",
                                     "sprint_runtime", "sprint_pr_watcher")}
            return (200, [("Content-Type", "application/json")], json.dumps({
                "fixture_id": trusted["fixture_id"], "source_sha": trusted["source_sha"],
                "bootstrap_sha256": trusted["bootstrap_sha256"],
                "server_file": str(actual), "transport_file": str(Path(transport.__file__).resolve()),
                "database": str(db), "source_files": hashes, "runtime": trusted["runtime"],
                "production_services_started": any(services.values()),
                "production_service_inventory": services,
                "fixture_home": str(fixture_home),
                "inherited_control_plane_environment": any(
                    name.startswith("SC_") and name != "SC_BIND" for name in os.environ),
            }).encode())
        return base_dispatch(method, path, headers_raw, body)

    server.start_runtime_services = start_selected
    server.dispatch_http = dispatch

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        return server.main(["--port", str(trusted["port"])])
    finally:
        if runtime_stop is not None:
            runtime_stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    launch = sub.add_parser("start")
    launch.add_argument("--source-repo", type=Path, required=True)
    launch.add_argument("--ref", required=True)
    launch.add_argument("--receipt", type=Path, required=True)
    launch.add_argument("--temp-parent", type=Path)
    launch.add_argument("--port", type=int)
    launch.add_argument("--runtime", default="none")
    launch.add_argument("--lifetime", type=int, default=MAX_LIFETIME)
    launch.add_argument("--memory-mib", type=int, default=MAX_MEMORY_MIB)
    launch.add_argument("--tasks", type=int, default=MAX_TASKS)
    cleanup = sub.add_parser("stop")
    cleanup.add_argument("--receipt", type=Path, required=True)
    internal = sub.add_parser("_serve", help=argparse.SUPPRESS)
    internal.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "_serve":
            return serve(args.root.absolute())
        if args.action == "stop":
            record = stop(args.receipt.absolute())
        else:
            record = start(args.source_repo.absolute(), args.ref, args.receipt,
                           temp_parent=args.temp_parent, port=args.port, runtime=args.runtime,
                           lifetime=args.lifetime, memory_mib=args.memory_mib, tasks=args.tasks)
        print(json.dumps({key: record[key] for key in ("fixture_id", "source_sha", "status", "cleanup")}
                         | {"receipt": record["receipt"], "url": record.get("url")}))
        return 0
    except FixtureError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps({"error": "SEAT_UNAVAILABLE", "message": "fixture filesystem or process operation failed"}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
