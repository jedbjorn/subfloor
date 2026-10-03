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
import math
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
    "sc",
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
def ownership_lock(fixture_id: str, *, deadline: float | None = None):
    if deadline is not None and time.monotonic()>=deadline:
        raise FixtureError('OWNERSHIP_DEADLINE','fixture ownership observation expired')
    path = ledger_path(fixture_id).with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(path)
        if deadline is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            while True:
                if time.monotonic() >= deadline:
                    raise FixtureError('OWNERSHIP_DEADLINE','fixture ownership observation expired')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.01,max(0,deadline-time.monotonic())))
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


def unit_state(record: dict[str, Any], *, timeout: float = 20) -> dict[str, str]:
    result = command(["systemctl", "--user", "show", record["unit"],
                      "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                      "-p", "Description", "-p", "ControlGroup", "-p", "MainPID"],
                     check=False, timeout=timeout)
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


def native_description(record: dict, native: dict) -> str:
    return description(record) + " native " + native["generation_id"]


def verify_native(record: dict, native: dict) -> None:
    gid = native.get("generation_id", "")
    if (not ID_RE.fullmatch(gid) or native.get("harness") not in {"codex", "claude"}
            or native.get("unit") != f"{PREFIX}{record['fixture_id']}-native-{gid}.service"
            or native.get("root") != str(Path(record["root"]) / "runtime" / gid)
            or native.get("endpoint") != str(native_endpoint(record["fixture_id"],gid))):
        raise FixtureError("OWNERSHIP_INVALID", "native resource identity is not fixture-derived")
    root = Path(native["root"])
    if root.exists() or root.is_symlink():
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (native["root_device"], native["root_inode"])):
            raise FixtureError("OWNERSHIP_INVALID", "native state root was replaced")


def native_endpoint(fixture_id: str,generation_id: str) -> Path:
    # Linux pathname sockets have a 108-byte address cap. Keep their endpoint
    # in the fixed private registry, independent of a long fixture temp-parent.
    key=hashlib.sha256((fixture_id+":"+generation_id).encode()).hexdigest()[:32]
    return registry()/f"{key}.sock"


def native_unit_state(native: dict, *, timeout: float=20) -> dict[str, str]:
    return unit_state({"unit":native["unit"]},timeout=timeout)


def listener_remaining(deadline: float) -> float:
    if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline):
        raise FixtureError('STARTUP_FAILED','finite controller listener budget required')
    value=deadline-time.monotonic()
    if value<=0:raise FixtureError('STARTUP_FAILED','controller listener budget expired; cleanup retained')
    return value


def native_listener_identity(record: dict,native: dict,deadline: float) -> dict:
    """Observe the recorded controller only, without a lease or native action."""
    remaining=listener_remaining(deadline)
    verify_native(record,native)
    state=native_unit_state(native,timeout=min(1,remaining))
    pid=int(state.get('MainPID','0'));ticks=process_start_ticks(pid)
    group=state.get('ControlGroup','')
    if (time.monotonic()>=deadline or state.get('Description')!=native_description(record,native)
            or state.get('ActiveState')!='active' or ticks is None or not group
            or f'0::{group}' not in Path(f'/proc/{pid}/cgroup').read_text().splitlines()):
        raise FixtureError('OWNERSHIP_INVALID','controller listener unit identity unavailable')
    info=Path(native['endpoint']).lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600):
        raise FixtureError('OWNERSHIP_INVALID','controller listener socket differs')
    listener_remaining(deadline)
    return {'generation_id':native['generation_id'],'endpoint':native['endpoint'],'unit':native['unit'],
            'main_pid':pid,'main_pid_start_ticks':ticks,'control_group':group,
            'endpoint_device':info.st_dev,'endpoint_inode':info.st_ino}


def stop_native_unit(record: dict, native: dict, *, deadline: float | None = None) -> None:
    verify_native(record, native)
    bounded = deadline is not None
    limit=deadline if deadline is not None else time.monotonic()+25
    if bounded and (not math.isfinite(limit) or limit <= time.monotonic()):
        raise FixtureError("CLEANUP_UNVERIFIED", "setup cleanup deadline expired")
    def observe():
        return unit_state(native, timeout=max(.001, min(1, limit-time.monotonic()))) if bounded else native_unit_state(native)
    state = observe()
    if state["LoadState"] != "not-found":
        if state.get("Description") != native_description(record, native):
            raise FixtureError("OWNERSHIP_INVALID", "native unit description differs")
        command(["systemctl", "--user", "stop", native["unit"]],
                timeout=max(.001,min(2,limit-time.monotonic())) if bounded else 15)
    cgroup = state.get("ControlGroup", "") or native.get("control_group", "")
    limit = limit if bounded else time.monotonic() + 10
    while True:
        state = observe()
        if state["LoadState"] != "not-found" and state.get("Description") != native_description(record, native):
            raise FixtureError("OWNERSHIP_INVALID", "native unit identity changed during cleanup")
        pids = cgroup_pids(cgroup)
        live = native.get("main_pid", 0) > 0 and process_start_ticks(native["main_pid"]) == native.get("main_pid_start_ticks")
        child=native.get("setup_child",{})
        child_live=child.get("pid",0)>0 and process_start_ticks(child["pid"])==child.get("start_ticks")
        if state.get("ActiveState") in {"inactive", "failed"} and state.get("MainPID", "0") == "0" and not pids and not live and not child_live:
            endpoint=Path(native['endpoint'])
            if endpoint.exists() or endpoint.is_symlink():
                info=endpoint.lstat()
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.geteuid()
                        or (info.st_dev,info.st_ino)!=(native.get('endpoint_device'),native.get('endpoint_inode'))):
                    raise FixtureError("CLEANUP_UNVERIFIED", "native socket identity changed; retaining fixture state")
                endpoint.unlink()
            native["os_cleanup"] = {"complete":True,"cgroup_empty":True,"recorded_process_exited":True}
            return
        if time.monotonic() >= limit:
            native["os_cleanup"] = {"complete":False,"surviving_pids":pids}
            raise FixtureError("CLEANUP_UNVERIFIED", "native unit survivors retain fixture state")
        time.sleep(min(.1,max(0,limit-time.monotonic())))


class NativeSupervisor:
    """Fixture-only fixed controller launch; independent retained ledger owner.

    No arbitrary executable/unit/root operation. Native definition cleanup
    remains in the controller/DB even when this OS cleanup succeeds.
    """
    def __init__(self, receipt: Path):
        self.receipt = canonical_receipt(receipt)

    def inventory(self) -> list[dict]:
        record = verify_receipt(self.receipt)
        verify_root(record)
        return record.get("native_units", [])

    def preparation_identity(self, *, deadline: float | None = None) -> dict:
        """Capture only the current marked API's canonical preparation owner."""
        record=verify_receipt(self.receipt)
        verify_root(record)
        if deadline is not None and time.monotonic() >= deadline:
            raise FixtureError('OWNERSHIP_DEADLINE','preparation ownership observation expired')
        state=unit_state(record,timeout=min(3,deadline-time.monotonic())) if deadline is not None else unit_state(record)
        if (not owned_unit(record,state) or int(state.get('MainPID','0'))!=os.getpid()
                or process_start_ticks(os.getpid()) is None or not state.get('ControlGroup')
                or deadline is not None and time.monotonic() >= deadline):
            raise FixtureError('OWNERSHIP_INVALID','canonical preparation requires the marked API unit')
        return {'pid':os.getpid(),'start_ticks':process_start_ticks(os.getpid()),
                'unit':record['unit'],'control_group':state['ControlGroup']}

    def record_codegen_child(self, process, control_group: str, *, deadline: float) -> bool:
        """Persist an inert child's identity before its fixed codegen gate opens.

        The registered API unit owns all descendants. This record cannot name
        a cleanup target or grant arbitrary process/unit operations.
        """
        owner=self.preparation_identity(deadline=deadline)
        if type(process.pid) is not int or process.pid<=0 or type(process.start_ticks) is not int:
            return False
        initial=read_json(self.receipt)
        with ownership_lock(initial['fixture_id'],deadline=deadline):
            record=verify_receipt(self.receipt)
            verify_root(record)
            fields=Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()
            groups=Path(f'/proc/{process.pid}/cgroup').read_text().splitlines()
            if (int(fields[19])!=process.start_ticks or int(fields[2])!=process.pid
                    or control_group!=owner['control_group'] or f'0::{control_group}' not in groups
                    or self.preparation_identity(deadline=deadline)!=owner
                    or record['status']!='serving' or record['runtime']!='experimental'):
                return False
            children=record.setdefault('codegen_children',[])
            allocation=record.get('codegen_pending')
            if (not allocation or allocation.get('api_owner')!=owner
                    or len(children)>=32 or time.monotonic()>=deadline):
                return False
            children.append({'purpose':'codex_schema','api_owner':owner,
                             'allocation_id':allocation['allocation_id'],
                             'pid':process.pid,'start_ticks':process.start_ticks,
                             'control_group':control_group,'registered_at':time.time()})
            save(record,self.receipt)
            return time.monotonic()<deadline

    def codegen_clean(self) -> bool:
        record=verify_receipt(self.receipt)
        verify_root(record)
        return not record.get('codegen_pending')

    def begin_codegen(self, allocation_id: str, *, deadline: float) -> None:
        owner=self.preparation_identity(deadline=deadline)
        if not ID_RE.fullmatch(allocation_id):
            raise FixtureError('INPUT_INVALID','fixed schema allocation identity required')
        initial=read_json(self.receipt)
        with ownership_lock(initial['fixture_id'],deadline=deadline):
            record=verify_receipt(self.receipt)
            verify_root(record)
            if (record.get('codegen_pending') or record['runtime']!='experimental'
                    or record['status']!='serving' or time.monotonic()>=deadline):
                raise FixtureError('CLEANUP_PENDING','previous schema ownership remains unresolved')
            record['codegen_pending']={'allocation_id':allocation_id,'api_owner':owner}
            save(record,self.receipt) # Before any output-directory allocation/fork.

    def finish_codegen(self, allocation_id: str, receipt: dict, *, deadline: float) -> bool:
        owner=self.preparation_identity(deadline=deadline)
        initial=read_json(self.receipt)
        with ownership_lock(initial['fixture_id'],deadline=deadline):
            record=verify_receipt(self.receipt)
            verify_root(record)
            if (record.get('codegen_pending')!={'allocation_id':allocation_id,'api_owner':owner}
                    or receipt.get('cleanup_complete') is not True or receipt.get('files_removed') is not True):
                return False
            children=[item for item in record.get('codegen_children',[]) if item.get('allocation_id')==allocation_id]
            if receipt.get('child_started') is True:
                if (len(children)!=1 or not all(receipt.get(name) is True for name in
                        ('child_reaped','process_group_exited','child_registered'))):
                    return False
                child=children[0]
                if (type(child.get('pid')) is not int or child['pid']<=0
                        or type(child.get('start_ticks')) is not int or child['start_ticks']<=0
                        or child['api_owner']!=owner or child['control_group']!=owner['control_group']
                        or process_start_ticks(child['pid'])==child['start_ticks']):
                    return False
                for pid in cgroup_pids(owner['control_group']):
                    try:fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
                    except FileNotFoundError:continue
                    if int(fields[2])==child['pid'] and fields[0]!='Z':return False
            elif receipt.get('child_started') is not False or children:
                return False
            if self.preparation_identity(deadline=deadline)!=owner or time.monotonic()>=deadline:
                return False
            record['codegen_pending']=None
            save(record,self.receipt)
            return True

    def preparation_exited(self, identity: dict) -> bool:
        """PID death alone cannot release helpers left in the old API cgroup."""
        record=verify_receipt(self.receipt)
        verify_root(record)
        if identity.get('unit')!=record['unit']:
            return False
        if process_start_ticks(identity.get('pid',0))==identity.get('start_ticks'):
            return False
        return any(proof.get('identity')==identity and proof.get('cgroup_empty') is True
                   for proof in record.get('api_exit_proofs',[]))

    def register(self, generation_id: str, harness: str, *, test_transport: bool=False, purpose: str="native",
                 deadline: float | None=None) -> dict:
        initial = read_json(self.receipt)
        with ownership_lock(initial["fixture_id"],deadline=deadline):
            record = verify_receipt(self.receipt)
            root = verify_root(record)
            if (record["runtime"] not in ({"none", "experimental"} if purpose=="claude_setup" else {"experimental"})
                    or record["status"] not in {"preparing", "serving"}):
                raise FixtureError("RUNTIME_UNAVAILABLE", "native units require an active experimental fixture")
            if (not ID_RE.fullmatch(generation_id) or harness not in {"codex", "claude"}
                    or purpose not in {"native","claude_setup"}
                    or (purpose=="claude_setup" and (harness!="claude" or test_transport))):
                raise FixtureError("INPUT_INVALID", "native generation/harness invalid")
            units = record.setdefault("native_units", [])
            for native in units:
                if native["generation_id"] == generation_id:
                    verify_native(record, native)
                    if (native["harness"] != harness or native.get("test_transport",False) != test_transport
                            or native.get("purpose","native") != purpose):
                        raise FixtureError("OWNERSHIP_INVALID", "generation harness changed")
                    return native
            if sum(item["harness"] == harness and not item.get("os_cleanup", {}).get("complete") for item in units) >= 2:
                raise FixtureError("RESOURCE_LIMIT", "two roots per harness already retained")
            state_parent = root / "runtime"
            state_parent.mkdir(mode=0o700, exist_ok=True)
            parent_info = state_parent.lstat()
            if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
                raise FixtureError("OWNERSHIP_INVALID", "native state parent is unsafe")
            state_root = state_parent / generation_id
            state_root.mkdir(mode=0o700)
            info = state_root.lstat()
            native = {"generation_id":generation_id,"harness":harness,"root":str(state_root),
                      "endpoint":str(native_endpoint(record["fixture_id"],generation_id)),
                      "root_device":info.st_dev,"root_inode":info.st_ino,
                      "unit":f"{PREFIX}{record['fixture_id']}-native-{generation_id}.service",
                      "limits":{"memory_mib":2048,"tasks":128,"term_grace_seconds":5},
                      "registered_at":time.time(),"status":"registered"}
            native["test_transport"] = test_transport
            native["purpose"] = purpose
            units.append(native)
            save(record,self.receipt)  # durable before systemd-run or native writes
            return native

    def listener_identity(self,generation_id: str, *, deadline: float) -> dict:
        initial=read_json(self.receipt)
        with ownership_lock(initial['fixture_id'],deadline=deadline):
            record=verify_receipt(self.receipt);verify_root(record)
            native=next((n for n in record.get('native_units',[]) if n['generation_id']==generation_id),None)
            if native is None or native.get('status')!='active':
                raise FixtureError('STARTUP_FAILED','captured listener unavailable; cleanup retained')
            observed=native_listener_identity(record,native,deadline)
            if any(native.get(k)!=observed[k] for k in observed):
                raise FixtureError('OWNERSHIP_INVALID','recorded listener identity changed')
            return observed

    def launch(self, generation_id: str, *, deadline: float | None=None) -> dict:
        deadline=min(deadline,time.monotonic()+10) if deadline is not None else time.monotonic()+10
        listener_remaining(deadline)
        initial = read_json(self.receipt)
        with ownership_lock(initial["fixture_id"],deadline=deadline):
            record = verify_receipt(self.receipt)
            root = verify_root(record)
            native = next((n for n in record.get("native_units", []) if n["generation_id"] == generation_id), None)
            if native is None:
                raise FixtureError("OWNERSHIP_INVALID", "native unit was not registered before launch")
            verify_native(record,native)
            if native.get('purpose','native') != 'native':
                raise FixtureError('INPUT_INVALID','setup ownership cannot launch a conversation controller')
            if native["status"] != "registered" or native_unit_state(native,timeout=listener_remaining(deadline))["LoadState"] != "not-found":
                raise FixtureError("GENERATION_TERMINAL", "native launch never restarts an existing generation")
            script = root / ".super-coder/scripts/conversation_runtime_controller.py"
            if not script.is_file() or script.is_symlink():
                raise FixtureError("SOURCE_INVALID", "copied controller is unavailable")
            native["status"] = "starting"
            save(record,self.receipt)
            remaining=math.ceil(record["expires_at"]-time.time())
            if remaining<=0:
                raise FixtureError("RESOURCE_LIMIT","fixture deadline reached before native launch")
            argv=["systemd-run","--user","--quiet","--collect","--unit",native["unit"],
                     "--description",native_description(record,native),"-p","Type=exec",
                     "-p","KillMode=control-group","-p","SendSIGKILL=yes","-p","TimeoutStopSec=5s",
                     "-p",f"RuntimeMaxSec={remaining}s",
                     "-p","MemoryMax=2048M","-p","TasksMax=128","-p",f"WorkingDirectory={root}",
                     "-p",f"StandardOutput=append:{native['root']}/controller.log",
                     "-p",f"StandardError=append:{native['root']}/controller.log",
                     sys.executable,str(script),"--generation",generation_id,"--root",native["root"],
                     "--endpoint",native["endpoint"],
                     "--harness",native["harness"]]
            if native.get("test_transport"):
                argv.append("--test-transport")
            if time.monotonic()>=deadline:raise FixtureError("STARTUP_FAILED","native launch budget expired")
            command(argv,timeout=listener_remaining(deadline))
            while time.monotonic()<deadline:
                state = native_unit_state(native,timeout=min(1,listener_remaining(deadline)))
                if state.get("Description") != native_description(record,native):
                    raise FixtureError("OWNERSHIP_INVALID", "launched native unit identity differs")
                pid = int(state.get("MainPID", "0"))
                ticks = process_start_ticks(pid)
                if state.get("ActiveState") == "active" and ticks is not None and Path(native["endpoint"]).is_socket():
                    observed=native_listener_identity(record,native,deadline)
                    native.update({k:v for k,v in observed.items() if k not in {'generation_id','endpoint','unit'}})
                    save(record,self.receipt)  # identity retained even when listening is inconclusive
                    import conversation_runtime
                    if Path(conversation_runtime.__file__).resolve()!=root/'.super-coder/scripts/conversation_runtime.py':
                        raise FixtureError('SOURCE_INVALID','listener client is not the captured copied source')
                    client=conversation_runtime.RuntimeClient(Path(native['endpoint']),generation_id,
                        controller_pid=pid,controller_start_ticks=ticks)
                    try:
                        client.wait_listener(deadline=deadline,endpoint_device=observed['endpoint_device'],endpoint_inode=observed['endpoint_inode'])
                    except (conversation_runtime.RuntimeContractError,OSError,ValueError) as exc:
                        raise FixtureError('STARTUP_FAILED','controller listener inconclusive; ledger retains cleanup') from exc
                    if native_listener_identity(record,native,deadline)!=observed:
                        raise FixtureError('OWNERSHIP_INVALID','controller listener changed after status')
                    native['status']='active';save(record,self.receipt)
                    return native
                if state.get("ActiveState") in {"inactive","failed"}:
                    break
                time.sleep(min(.1,max(0,deadline-time.monotonic())))
            raise FixtureError("STARTUP_FAILED", "native controller did not become ready; ledger retains cleanup")

    def launch_claude_pretrust(self, generation_id: str, *, deadline: float) -> dict:
        """Fixed trust-only canonical preparation; no native process or TUI."""
        setup=importlib.import_module('claude_setup')
        try:
            return self.launch_claude_setup(generation_id, deadline=deadline, _pretrust=True)
        except setup.RuntimeContractError:
            raise FixtureError('TRUST_INCONCLUSIVE','scoped trust preparation unavailable') from None

    def launch_claude_setup(self, generation_id: str, *, deadline: float, _pretrust: bool=False) -> dict:
        """Fixed local operator TUI; registered ownership before preparation.

        No arbitrary argv/environment/main-root input and no model prompt.
        Its qualified observation is diagnostic, never ordinary admission.
        """
        setup=importlib.import_module("claude_setup")
        budget,digest=setup.budget,setup.digest
        budget(deadline)
        bus=setup.service_bus()
        trust=importlib.import_module('claude_trust') if _pretrust else None
        override=trust.config_override(os.environ.get('CLAUDE_CONFIG_DIR')) if trust else None
        if not _pretrust and (not os.isatty(0) or not os.isatty(1)):
            raise FixtureError("SETUP_INCONCLUSIVE","native operator terminal required")
        # Reusing a generation is forbidden even if its previous unit exited.
        initial=verify_receipt(self.receipt)
        if any(n['generation_id']==generation_id for n in initial.get('native_units',[])):
            raise FixtureError("GENERATION_TERMINAL","setup never replays an existing generation")
        native=self.register(generation_id,'claude',purpose='claude_setup',deadline=deadline-3)
        launcher=None
        try:
            with ownership_lock(initial['fixture_id'],deadline=deadline-3):
                record=verify_receipt(self.receipt)
                root=verify_root(record)
                native=next(n for n in record['native_units'] if n['generation_id']==generation_id)
                verify_native(record,native)
                helper=root/'maintainer/claude_setup.py'
                executable=shutil.which('claude')
                if not executable or not helper.is_file() or helper.is_symlink():
                    raise FixtureError("SETUP_INCONCLUSIVE","captured setup source or native executable unavailable")
                path=Path(executable).resolve()
                if root.resolve()!=root or not (root/'.git').is_dir() or (root/'.git').is_symlink():
                    raise FixtureError("SETUP_INCONCLUSIVE","validated canonical Git MAIN root required")
                native.update(status='starting',setup_helper_sha256=digest(helper),
                              setup_executable_sha256=digest(path),setup_deadline=deadline)
                if trust:
                    trust_helper=root/'maintainer/claude_trust.py'
                    native['trust_helper_sha256']=digest(trust_helper)
                save(record,self.receipt)
                budget(deadline)
                remaining=min(27, math.floor(deadline-time.monotonic()-3))
                if remaining<=0:
                    raise FixtureError("SETUP_INCONCLUSIVE","setup preparation budget expired")
                argv=['systemd-run','--user','--quiet','--collect','--wait','--pipe' if _pretrust else '--pty',
                      '--unit',native['unit'],'--description',native_description(record,native),
                      '-p','Type=exec','-p','KillMode=control-group','-p','SendSIGKILL=yes',
                      '-p','TimeoutStopSec=1s','-p',f'RuntimeMaxSec={remaining}s',
                      '-p','MemoryMax=2048M','-p','TasksMax=128','-p',f'WorkingDirectory={root}',
                      '/usr/bin/env','-i','PATH='+os.defpath,
                      'XDG_RUNTIME_DIR='+bus['XDG_RUNTIME_DIR'],'DBUS_SESSION_BUS_ADDRESS='+bus['DBUS_SESSION_BUS_ADDRESS'],
                      *(['CLAUDE_CONFIG_DIR='+str(override)] if override else []),
                      sys.executable,'-I',str(helper),'_pretrust' if _pretrust else '_setup','--receipt',str(self.receipt),
                      '--generation',generation_id,'--executable',str(path),
                      '--sha256',native['setup_executable_sha256'],'--deadline',str(deadline-3)]
                launcher=subprocess.Popen(argv,env=clean_environment())
            # Unit wrapper cannot prepare until exact OS ownership is durable.
            while time.monotonic()<deadline-3:
                with ownership_lock(initial['fixture_id'],deadline=deadline-3):
                    record=verify_receipt(self.receipt)
                    verify_root(record)
                    native=next(n for n in record['native_units'] if n['generation_id']==generation_id)
                    verify_native(record,native)
                    if record['status'] not in {'preparing','serving'} or native['status']!='starting':
                        raise FixtureError('SETUP_INCONCLUSIVE','setup owner changed before preparation')
                    state=unit_state(native,timeout=min(1,max(.001,deadline-time.monotonic())))
                    if state.get('Description')!=native_description(record,native):
                        raise FixtureError('OWNERSHIP_INVALID','setup unit identity differs')
                    pid=int(state.get('MainPID','0'));ticks=process_start_ticks(pid)
                    if state.get('ActiveState')=='active' and ticks is not None:
                        native.update(status='active',main_pid=pid,main_pid_start_ticks=ticks,control_group=state['ControlGroup'])
                        save(record,self.receipt)
                        break
                if launcher.poll() is not None:
                    raise FixtureError('SETUP_INCONCLUSIVE','setup unit exited before recorded ownership')
                time.sleep(.02)
            else:
                raise FixtureError('SETUP_INCONCLUSIVE','setup start deadline expired')
            launcher.wait(timeout=max(.001,deadline-time.monotonic()-3))
        finally:
            try:
                with ownership_lock(initial['fixture_id'],deadline=deadline):
                    record=verify_receipt(self.receipt)
                    native=next(n for n in record['native_units'] if n['generation_id']==generation_id)
                    try:
                        stop_native_unit(record,native,deadline=deadline)
                        native['status']='stopped'
                        native['setup_transcript_evidence']=setup.transcript_turn_evidence(
                            Path(native['root']),deadline=deadline)
                        native['setup_diagnostic']=setup.setup_diagnostic(
                            Path(native['root']),generation_id,native['setup_helper_sha256'],deadline=deadline)
                        if trust:
                            native['trust_result']=trust.read_result(Path(native['root']),generation_id,
                                native['trust_helper_sha256'],deadline=deadline)
                        # The unit may have been killed during preparation.
                        # Finalize only its scoped synthetic setup row after
                        # whole-unit exit, retaining failures for review.
                        database=Path(record['root'])/'.super-coder/shell_db.db'
                        if database.is_file():
                            setup.close_setup_conversation(database,'cv_fixture_setup_'+generation_id,
                                                           Path(record['root']),generation_id)
                    finally:
                        native['setup_state']='setup_inconclusive'
                        save(record,self.receipt)
            finally:
                if launcher is not None and launcher.poll() is None:
                    launcher.terminate()
                    try:launcher.wait(timeout=max(.001,min(.2,deadline-time.monotonic())))
                    except subprocess.TimeoutExpired:
                        launcher.kill()
                        launcher.wait(timeout=.2)
        return native


    def stop(self, generation_id: str) -> dict:
        initial = read_json(self.receipt)
        with ownership_lock(initial["fixture_id"]):
            record = verify_receipt(self.receipt)
            verify_root(record)
            native = next((n for n in record.get("native_units",[]) if n["generation_id"] == generation_id),None)
            if native is None:
                raise FixtureError("OWNERSHIP_INVALID", "unknown fixture generation")
            stop_native_unit(record,native)
            native["status"] = "stopped"
            save(record,self.receipt)
            return native


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
    # Native controllers are independently supervised. Stop and independently
    # verify every pre-registered resource before removing API DB/source/state.
    for native in record.get("native_units", []):
        verify_native(record, native)
        stop_native_unit(record, native)
    save(record, receipt)
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
    paths=[".super-coder", "sc"]
    for setup in ('maintainer/claude_setup.py','maintainer/claude_trust.py'):
        present=command(["git","-C",str(source_repo),"cat-file","-e",f"{sha}:{setup}"],check=False)
        if present.returncode==0:paths.append(setup)
    result = subprocess.run(["git", "-C", str(source_repo), "archive", sha, *paths],
                            capture_output=True, env=clean_environment(), timeout=20, check=False)
    if result.returncode:
        raise FixtureError("SOURCE_INVALID", "committed engine archive is unavailable")
    return sha, result.stdout


def launch_api(record: dict,root: Path, *, resume: bool=False) -> None:
    remaining=math.ceil(record["expires_at"]-time.time())
    if remaining<=0:
        raise FixtureError("RESOURCE_LIMIT","fixture deadline reached before API launch")
    limits=record["limits"]
    bootstrap=root/"fixture_bootstrap.py"
    # Only the explicit experimental seat carries these noncredential paths
    # into its transient API process. Ordinary catalogue reads remain masked;
    # no host account/configuration data is copied into the fixture or ledger.
    native_environment=[]
    if record['runtime']=='experimental':
        import pwd
        native_environment += ['--setenv=SC_FIXTURE_NATIVE_HOME='+pwd.getpwuid(os.geteuid()).pw_dir]
        for harness in ('codex','claude'):
            executable=shutil.which(harness)
            if executable:
                native_environment += ['--setenv=SC_FIXTURE_NATIVE_'+harness.upper()+'='+str(Path(executable).absolute())]
        if os.environ.get('CODEX_HOME'):
            native_environment += ['--setenv=SC_FIXTURE_NATIVE_CODEX_HOME='+str(Path(os.environ['CODEX_HOME']).resolve())]
    argv=["systemd-run", "--user", "--quiet", "--collect", *native_environment, "--unit", record["unit"],
                 "--description", description(record), "-p", "Type=exec",
                 "-p", "KillMode=control-group", "-p", "SendSIGKILL=yes",
                 "-p", "TimeoutStopSec=5s", "-p", f"RuntimeMaxSec={remaining}s",
                 "-p", f"MemoryMax={limits['memory_mib']}M", "-p", f"TasksMax={limits['tasks']}",
                 "-p", f"WorkingDirectory={root}",
                 "-p", f"StandardOutput=append:{root / 'server.log'}",
                 "-p", f"StandardError=append:{root / 'server.log'}",
                 sys.executable, "-I", str(bootstrap), "_serve", "--root", str(root)]
    if resume:
        argv.append("--resume")
    command(argv)


def restart_api(receipt: Path) -> dict:
    receipt=canonical_receipt(receipt)
    initial=read_json(receipt)
    with ownership_lock(initial["fixture_id"]):
        record=verify_receipt(receipt)
        root=verify_root(record)
        if record["status"]!="serving":
            raise FixtureError("STATE_CONFLICT","API recovery requires a serving retained fixture")
        for native in record.get("native_units",[]):
            verify_native(record,native)
        state=unit_state(record)
        owned_unit(record,state)
        if state.get("ActiveState") not in {"inactive","failed"} or state.get("MainPID","0")!="0" or (
                record.get("main_pid",0)>0 and process_start_ticks(record["main_pid"])==record.get("main_pid_start_ticks")):
            raise FixtureError("STATE_CONFLICT","existing API must be stopped before recovery")
        old_group=state.get('ControlGroup','') or record.get('control_group','')
        if cgroup_pids(old_group):
            raise FixtureError('CLEANUP_UNVERIFIED','API preparation helpers remain; recovery retains ownership')
        if record.get('main_pid') and record.get('main_pid_start_ticks') and old_group:
            proof={'identity':{'pid':record['main_pid'],'start_ticks':record['main_pid_start_ticks'],
                              'unit':record['unit'],'control_group':old_group},'cgroup_empty':True,'verified_at':time.time()}
            record['api_exit_proofs']=(record.get('api_exit_proofs',[])+[proof])[-32:]
            save(record,receipt)  # verified before any replacement unit launch
        select_port(record["port"])
        launch_api(record,root,resume=True)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(record["url"]+"/api/experiment-fixture",timeout=.5) as response:
                    observed=json.load(response)
                if observed.get("fixture_id")!=record["fixture_id"] or observed.get("source_sha")!=record["source_sha"]:
                    raise FixtureError("OWNERSHIP_INVALID","recovered API identity differs")
                state=unit_state(record)
                if not owned_unit(record,state):
                    raise FixtureError("STARTUP_FAILED","recovered API unit disappeared")
                pid=int(state.get("MainPID","0"));ticks=process_start_ticks(pid)
                if state.get("ActiveState")=="active" and ticks is not None:
                    record.update(main_pid=pid,main_pid_start_ticks=ticks,control_group=state.get("ControlGroup",""))
                    record["api_recoveries"]=record.get("api_recoveries",0)+1
                    save(record,receipt)
                    return record
            except (OSError,ValueError,urllib.error.HTTPError):
                time.sleep(.1)
        raise FixtureError("STARTUP_FAILED","API recovery failed; fixture ownership retained")


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
        "expires_at":time.time()+limits["lifetime_seconds"],
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
        launch_api(record,root)
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
    caller_root = root.absolute()
    marker = read_json(root / MARKER)
    trusted = read_json(ledger_path(marker.get("fixture_id", "")))
    retained_root = Path(trusted["root"])
    if caller_root != retained_root:
        raise FixtureError("OWNERSHIP_INVALID", "bootstrap caller root differs from the retained fixture")
    verify_root(trusted)
    if (bootstrap.absolute() != caller_root / "fixture_bootstrap.py" or bootstrap.is_symlink()
            or hashlib.sha256(bootstrap.read_bytes()).hexdigest() != trusted["bootstrap_sha256"]):
        raise FixtureError("OWNERSHIP_INVALID", "bootstrap file differs from the retained helper identity")
    # Preserve the caller's public path after the independent identity proof.
    # Do not propagate a path read from a secret-bearing ownership container
    # into public migration diagnostics.
    return caller_root, trusted


def bootstrap_repository(root: Path) -> str:
    """Fresh synthetic Git ancestry; never fetch/modify the source checkout."""
    (root / ".gitignore").write_text(
        ".sc-state/\n.sc-worktrees/\nruntime/\nsynthetic-upstream/\nhome/\nxdg-*/\n"
        "fixture_bootstrap.py\n.gui-experiment-owner.json\n*.log\n"
        ".super-coder/*.db*\n.super-coder/instance.json\n.super-coder/db_backups/\n"
        ".super-coder/__pycache__/\n**/__pycache__/\nnode_modules/\n")
    command(["git","-C",str(root),"init","-q","-b","main"])
    command(["git","-C",str(root),"add","sc",".super-coder",".gitignore"])
    command(["git","-C",str(root),"-c","user.name=GUI fixture","-c","user.email=fixture@example.invalid",
             "-c","commit.gpgsign=false","commit","--no-verify","-qm","Synthetic exact-archive fixture"])
    upstream = root / "synthetic-upstream/subfloor.git"
    upstream.parent.mkdir(mode=0o700)
    command(["git","clone","--bare","--quiet",str(root),str(upstream)])
    command(["git","-C",str(root),"remote","add","origin",str(upstream)])
    command(["git","-C",str(root),"fetch","--quiet","origin","main"])
    command(["git","-C",str(root),"branch","--set-upstream-to=origin/main","main"])
    return command(["git","-C",str(root),"rev-parse","HEAD"]).stdout.strip()


def serve(root: Path, *, resume: bool=False) -> int:
    """Internal test-only bootstrap, executed from the marked archive."""
    root, trusted = verified_bootstrap_root(root, Path(__file__).absolute())
    # Capture fixed owner-issued native path bindings before erasing inherited
    # locators and masking normal GUI inventory. These stay ephemeral/private.
    native_bindings={name:os.environ.get('SC_FIXTURE_NATIVE_'+name,'')
                     for name in ('HOME','CODEX','CLAUDE','CODEX_HOME')}
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
    fixture_home.mkdir(exist_ok=resume)
    mock.patch.object(Path, "home", return_value=fixture_home).start()
    original_expanduser = os.path.expanduser
    mock.patch.object(os.path, "expanduser", side_effect=lambda value: (
        str(fixture_home) + value[1:] if isinstance(value, str)
        and (value == "~" or value.startswith("~/")) else original_expanduser(value))).start()
    sys.path.insert(0, str(engine / "scripts"))
    sys.path.insert(0, str(engine / "api"))
    import sqlite3

    import migrate
    synthetic_git_sha = (command(["git","-C",str(root),"rev-parse","HEAD"]).stdout.strip()
                         if resume else bootstrap_repository(root))
    db = engine / "shell_db.db"
    if db.exists() and not resume:
        raise FixtureError("STATE_CONFLICT", "fixture database already exists")
    if resume and not db.is_file():
        raise FixtureError("STATE_CONFLICT","fixture recovery database is missing")
    if not resume:
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
            command(["git","-C",str(root),"worktree","add","--quiet","-b",f"shell/{short}",
                     str(root / ".sc-worktrees" / short)])
        con.commit()
        con.close()
    # Only this synthetic installation owns these ports/state. No installed
    # profile, credentials, wrapper, or live instance is reconciled.
    if not resume:
        write_json(engine / "instance.json", {"repo":root.name,"port":trusted["port"],
               "dev_port":trusted["port"]+1 if trusted["port"]<65535 else 65534,
                   "browser":{"proxy_port":trusted["port"],"fixture_test_transport":True}})
    import instance_state
    instance_state.resolve(instance_config=engine / "instance.json",create=not resume)
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
            database=db, root=root, fixture_id=trusted["fixture_id"],
            supervisor=NativeSupervisor(Path(trusted["receipt"])),native_bindings=native_bindings)
        if not callable(runtime_stop):
            raise FixtureError("RUNTIME_UNAVAILABLE", "experimental start_fixture must return a shutdown callable")

    base_dispatch = server.dispatch_http

    def dispatch(method: str, path: str, headers_raw: str, body: bytes) -> tuple:
        if path == '/api/experiment-native-check' and trusted['runtime']=='experimental':
            import gui_experiment_runtime
            return gui_experiment_runtime.handle_check(method,headers_raw,body)
        if path.startswith("/mcp/"):
            import gui_experiment_readiness
            return gui_experiment_readiness.mcp_response(method=method,path=path,body=body,
                       database=db,fixture_id=trusted["fixture_id"],dispatch=base_dispatch)
        if path == "/api/experiment-readiness" and method == "POST":
            import gui_experiment_readiness
            args = json.loads(body)
            try:
                report = gui_experiment_readiness.prepare(database=db,root=root,
                         fixture_id=trusted["fixture_id"],harness=args["harness"],
                         shell_id=args.get("shell_id"))
                return 200, [("Content-Type","application/json")], json.dumps(report).encode()
            except (ValueError,OSError,RuntimeError,SystemExit):
                return 422, [("Content-Type","application/json")], b'{"error":"FIXTURE_READINESS_FAILED"}'
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
                "synthetic_git_sha":synthetic_git_sha,
                "mcp_transport":"fixture-test-only",
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
    recovery=sub.add_parser("restart-api",help="recover only a stopped marked fixture API; native units remain")
    recovery.add_argument("--receipt",type=Path,required=True)
    setup = sub.add_parser("claude-setup", help="fixed 30-second MAIN-root native initial trust TUI")
    setup.add_argument("--receipt", type=Path, required=True)
    pretrust = sub.add_parser('claude-pretrust', help='fixed registered MAIN trust-only operation; no native startup')
    pretrust.add_argument('--receipt', type=Path, required=True)
    internal = sub.add_parser("_serve", help=argparse.SUPPRESS)
    internal.add_argument("--root", type=Path, required=True)
    internal.add_argument("--resume",action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "_serve":
            return serve(args.root.absolute(),resume=args.resume)
        if args.action == "claude-setup":
            native=NativeSupervisor(canonical_receipt(args.receipt)).launch_claude_setup(uuid.uuid4().hex,deadline=time.monotonic()+30)
            print(json.dumps({"state":"setup_inconclusive","cleanup_complete":native.get("os_cleanup",{}).get("complete") is True,"native_inference_verified":False}))
            return 0
        if args.action == 'claude-pretrust':
            native=NativeSupervisor(canonical_receipt(args.receipt)).launch_claude_pretrust(uuid.uuid4().hex,deadline=time.monotonic()+30)
            result=native.get('trust_result',{})
            print(json.dumps({'state':result.get('state','trust_inconclusive'),
                'exact_main_trusted':result.get('exact_main_trusted') is True,
                'cleanup_complete':native.get('os_cleanup',{}).get('complete') is True,
                'native_started':False,'behavior_verified':False}))
            return 0
        if args.action == "restart-api":
            record=restart_api(args.receipt.absolute())
        elif args.action == "stop":
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
