"""Fixed, zero-account schema codegen inside an already registered API unit.

The caller supplies the supervisor's live ownership guard. This is not a
general command runner; private generated files survive until parser cleanup.
The enclosing fixture remains responsible for its whole unit/cgroup teardown.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from conversation_runtime_contract import ExecutableBinding, ProcessIdentity

MAX_SECONDS = 20.0
CLEANUP_RESERVE = 1.0
EXEC_GATE = "import os,sys; token=os.read(0,1); sys.exit(1) if token!=b'G' else os.execve(sys.argv[1],sys.argv[1:],dict(os.environ))"


def _process(pid: int) -> tuple[int, str, int] | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[19]), fields[0], int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


def _cgroup(pid: int) -> str | None:
    try:
        for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines():
            if line.startswith('0::/'):
                return line[3:]
    except OSError:
        pass
    return None


def _inode(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private directory required')
    return info.st_dev, info.st_ino


def _group_live(group: int, cgroup: str) -> bool:
    """Inspect only bounded members of the captured API cgroup; zombies exited."""
    path = Path('/sys/fs/cgroup') / cgroup.lstrip('/') / 'cgroup.procs'
    data = path.read_bytes()
    if len(data) > 65536:
        raise ValueError('unbounded cgroup')
    pids = data.split()
    if len(pids) > 128:
        raise ValueError('unbounded cgroup')
    return any(value is not None and value[1] != 'Z' and value[2] == group
               for value in (_process(int(pid)) for pid in pids))


class OwnedCodegenRunner:
    def __init__(self, binding: ExecutableBinding, owned_output_root: Path,
                 verify_owner: Callable[[], Mapping[str, Any]],
                 record_child: Callable[[ProcessIdentity, str], bool] | None = None) -> None:
        self.binding = binding
        self.root = owned_output_root
        self.verify_owner = verify_owner
        self.record_child = record_child
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._root_inode: tuple[int, int] | None = None
        self._owner_key: tuple[Any, ...] | None = None
        self._created: dict[Path, tuple[int, int]] = {}
        self._used = False
        self._receipt: dict[str, Any] = {'code': 'NOT_RUN', 'account_access': False,
            'inference_count': 0, 'child_started': False, 'child_reaped': False,
            'process_group_exited': False, 'files_removed': False,
            'gate_released': False, 'child_registered': False,
            'wrapper_sha256': hashlib.sha256(EXEC_GATE.encode()).hexdigest()}

    @property
    def receipt(self) -> Mapping[str, Any]:
        with self._lock:
            return dict(self._receipt)

    def _owner(self) -> Mapping[str, Any]:
        owner = self.verify_owner()
        pid, ticks, unit, cgroup = (owner.get(key) for key in ('pid', 'start_ticks', 'unit', 'control_group'))
        if (type(pid) is not int or pid != os.getpid() or type(ticks) is not int
                or not isinstance(unit, str) or not unit.endswith('.service')
                or not isinstance(cgroup, str) or not cgroup.startswith('/')
                or '..' in Path(cgroup).parts or _cgroup(pid) != cgroup):
            raise ValueError('owned API identity required')
        process = _process(pid)
        if process is None or process[0] != ticks:
            raise ValueError('owned API identity changed')
        key = (pid, ticks, unit, cgroup)
        if self._owner_key is not None and key != self._owner_key:
            raise ValueError('owned API identity changed')
        self._owner_key = key
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise ValueError('canonical private root required')
        current = _inode(self.root)
        if self._root_inode is not None and current != self._root_inode:
            raise ValueError('owned root replaced')
        self._root_inode = current
        return owner

    def _binary(self) -> None:
        path = self.binding.path
        if (not path.is_absolute() or path.resolve() != path
                or hashlib.sha256(path.read_bytes()).hexdigest() != self.binding.sha256):
            raise ValueError('captured executable changed')

    def _files_bound(self) -> None:
        if any(_inode(path) != identity for path, identity in self._created.items()):
            raise ValueError('private files replaced')

    def _paths(self, argv: tuple[str, ...]) -> tuple[Path, Path]:
        if (len(argv) != 6 or argv[:5] != (str(self.binding.path), 'app-server',
                'generate-json-schema', '--experimental', '--out')):
            raise ValueError('fixed schema command required')
        output = Path(argv[5])
        # Direct, fresh child avoids aliasing an existing parser/foreign tree.
        if (not output.is_absolute() or output.parent != self.root
                or output.name in {'', '.', '..', 'home'} or output.exists() or output.is_symlink()):
            raise ValueError('fresh private output required')
        home = self.root / 'home'
        if home.exists() or home.is_symlink():
            raise ValueError('fresh private home required')
        return output, home

    def _stop(self, child: subprocess.Popen[bytes], ticks: int, cgroup: str,
              deadline: float) -> bool:
        # Keep the unreaped root identity as the group ownership anchor. Never
        # signal a reused OS PID or an unrelated API process/group.
        process = _process(child.pid)
        if process is None or process[0] != ticks or process[2] != child.pid or _cgroup(child.pid) != cgroup:
            return False
        for sig, window in ((signal.SIGTERM, .2), (signal.SIGKILL, .4)):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            until = min(deadline, time.monotonic()+window)
            while time.monotonic() < until:
                if not _group_live(child.pid, cgroup):
                    break
                time.sleep(.01)
            if not _group_live(child.pid, cgroup):
                break
        group_exited = not _group_live(child.pid, cgroup)
        try:
            child.wait(timeout=max(0, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        self._receipt['child_reaped'] = True
        self._receipt['process_group_exited'] = group_exited
        return group_exited

    def __call__(self, argv: tuple[str, ...], deadline: float) -> bool:
        with self._lock:
            if self._used:
                return False
            self._used = True
            deadline = min(deadline, time.monotonic()+MAX_SECONDS)
            child: subprocess.Popen[bytes] | None = None
            ticks: int | None = None
            cgroup = ''
            success = False
            try:
                owner = self._owner()
                output, home = self._paths(argv)
                self._binary()
                if self._cancel.is_set() or time.monotonic()+CLEANUP_RESERVE >= deadline:
                    raise ValueError('cleanup reserve unavailable')
                # The ownership callback precedes every filesystem mutation.
                for path in (home, output):
                    self._owner()
                    path.mkdir(mode=0o700)
                    self._created[path] = _inode(path)
                self._owner()
                self._files_bound()
                self._binary()
                if self._cancel.is_set() or time.monotonic()+CLEANUP_RESERVE >= deadline:
                    raise ValueError('cleanup reserve unavailable')
                child = subprocess.Popen((str(Path(sys.executable).resolve()), '-c', EXEC_GATE, *argv),
                    cwd=self.root, env={'HOME': str(home), 'PATH': os.defpath,
                    'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                self._receipt['child_started'] = True
                self._receipt['child_pid'] = child.pid
                process = _process(child.pid)
                cgroup = str(owner['control_group'])
                if process is None or _cgroup(child.pid) != cgroup:
                    raise ValueError('child identity unavailable')
                ticks = process[0]
                self._receipt['child_start_ticks'] = ticks
                # Inert gate belongs to the registered API cgroup before any
                # native codegen. PID/ticks stay unchanged across execve.
                self._owner()
                if self.record_child is None or self.record_child(ProcessIdentity(child.pid, ticks), cgroup) is not True:
                    raise ValueError('durable child receipt unavailable')
                self._receipt['child_registered'] = True
                self._owner()
                self._files_bound()
                self._binary()
                if self._cancel.is_set() or time.monotonic()+CLEANUP_RESERVE >= deadline or child.stdin is None:
                    raise ValueError('cleanup reserve unavailable')
                child.stdin.write(b'G')
                child.stdin.flush()
                child.stdin.close()
                self._receipt['gate_released'] = True
                while not self._cancel.is_set() and time.monotonic()+CLEANUP_RESERVE < deadline:
                    status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if status is not None:
                        success = status.si_code == os.CLD_EXITED and status.si_status == 0
                        break
                    time.sleep(.01)
                self._receipt['code'] = 'GENERATED' if success else 'GENERATION_UNAVAILABLE'
            except (OSError, ValueError, RuntimeError):
                self._receipt['code'] = 'OWNED_CODEGEN_UNAVAILABLE'
            finally:
                if child is not None and child.stdin is not None and not child.stdin.closed:
                    try:
                        child.stdin.close()
                    except OSError:
                        pass
                if child is not None and ticks is not None:
                    try:
                        success = self._stop(child, ticks, cgroup, deadline) and success
                    except (OSError, ValueError):
                        success = False
                if child is not None and not self._receipt['child_reaped']:
                    # Captured Popen child only; no unverified group signalling.
                    child.kill()
                    try:
                        child.wait(timeout=max(0, deadline-time.monotonic()))
                        self._receipt['child_reaped'] = True
                    except subprocess.TimeoutExpired:
                        pass
                    success = False
                try:
                    self._owner()
                    self._files_bound()
                    self._binary()
                except (OSError, ValueError, RuntimeError):
                    success = False
                if not success and self._receipt['code'] == 'GENERATED':
                    self._receipt['code'] = 'OWNED_CODEGEN_UNAVAILABLE'
            return success and time.monotonic() < deadline

    def cleanup(self, deadline: float) -> bool:
        self._cancel.set()
        if not self._lock.acquire(timeout=max(0, deadline-time.monotonic())):
            return False
        try:
            if self._receipt['child_started'] and not self._receipt['process_group_exited']:
                return False
            try:
                for path, identity in tuple(self._created.items()):
                    self._owner()
                    if time.monotonic() >= deadline or _inode(path) != identity:
                        return False
                    shutil.rmtree(path)
                    del self._created[path]
                self._receipt['files_removed'] = not self._created
                return not self._created
            except (OSError, ValueError, RuntimeError):
                return False
        finally:
            self._lock.release()


def make_owned_codegen_runner(binding: ExecutableBinding, owned_output_root: Path,
                              verify_owner: Callable[[], Mapping[str, Any]], *,
                              record_child: Callable[[ProcessIdentity, str], bool] | None = None) -> OwnedCodegenRunner:
    return OwnedCodegenRunner(binding, owned_output_root, verify_owner, record_child)
