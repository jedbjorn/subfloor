"""Fixed owner-only generation artifact; no native reader/capture/seal/admission.

The existing owner supplies its captured generation root identity. This module
only transfers bounded typed ID/hash boundaries. Neither a successful read nor
a file/reference is source ownership, completed cleanup or history eligibility.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from conversation_runtime_contract import (
    MAX_HISTORY_BASELINE_BYTES,
    HistoryBaseline,
    RuntimeContractError,
)

FILENAME = 'history-baseline-v1.json'


def _refuse(code: str = 'HISTORY_BASELINE_FILE_INVALID') -> RuntimeContractError:
    return RuntimeContractError(code,'private generation baseline unavailable')


def _budget(deadline: float) -> None:
    import math
    if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline) or time.monotonic()>=deadline:
        raise _refuse('HISTORY_BASELINE_DEADLINE')


@dataclass(frozen=True,repr=False)
class GenerationBaselineRoot:
    path: Path
    generation_id: str
    device: int
    inode: int

    def __post_init__(self) -> None:
        if (not isinstance(self.path,Path) or not self.path.is_absolute() or '..' in self.path.parts
                or not isinstance(self.generation_id,str) or not 1<=len(self.generation_id)<=255
                or any(not (c.isascii() and (c.isalnum() or c in '._-')) for c in self.generation_id)
                or type(self.device) is not int or self.device<0 or type(self.inode) is not int or self.inode<=0):
            raise _refuse()


@dataclass(frozen=True)
class BaselineReference:
    generation_id: str
    sha256: str
    device: int
    inode: int
    size: int

    def __post_init__(self) -> None:
        if (not isinstance(self.generation_id,str) or not 1<=len(self.generation_id)<=255
                or not isinstance(self.sha256,str) or len(self.sha256)!=64
                or any(c not in '0123456789abcdef' for c in self.sha256)
                or any(type(v) is not int or v<0 for v in (self.device,self.inode,self.size))
                or not self.inode or not 0<self.size<=MAX_HISTORY_BASELINE_BYTES):
            raise _refuse()


def _open_directory(root: GenerationBaselineRoot, deadline: float) -> int:
    _budget(deadline)
    fd=os.open('/',os.O_PATH|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for part in root.path.parts[1:]:
            _budget(deadline)
            child=os.open(part,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd)
            fd=child
        child=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
        os.close(fd)
        fd=child
        current=os.fstat(fd)
        if ((current.st_dev,current.st_ino)!=(root.device,root.inode)
                or current.st_uid!=os.geteuid() or stat.S_IMODE(current.st_mode)!=0o700):
            raise _refuse()
        _budget(deadline)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _recheck_directory(root: GenerationBaselineRoot, deadline: float) -> None:
    os.close(_open_directory(root,deadline))


@contextlib.contextmanager
def _directory(root: GenerationBaselineRoot, deadline: float) -> Iterator[int]:
    fd=-1
    try:
        fd=_open_directory(root,deadline)
        while True:
            _budget(deadline)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.005,max(0,deadline-time.monotonic())))
        _recheck_directory(root,deadline)
        yield fd
    except OSError:
        # OS messages can contain private paths; emit only a static code.
        raise _refuse() from None
    finally:
        if fd>=0:
            os.close(fd)


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(
        after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)


def _pairs(pairs: list[tuple]) -> dict:
    result: dict={}
    for key,value in pairs:
        if key in result:
            raise _refuse()
        result[key]=value
    return result


def _read(fd: int, root: GenerationBaselineRoot, deadline: float,
          expected: BaselineReference | None) -> tuple[HistoryBaseline,BaselineReference]:
    _budget(deadline)
    file=os.open(FILENAME,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=fd)
    try:
        before=os.fstat(file)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1
                or not 0<before.st_size<=MAX_HISTORY_BASELINE_BYTES):
            raise _refuse()
        chunks=bytearray()
        while True:
            _budget(deadline)
            chunk=os.read(file,min(8192,MAX_HISTORY_BASELINE_BYTES+1-len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks)>MAX_HISTORY_BASELINE_BYTES:
                raise _refuse()
        after=os.fstat(file)
        named=os.stat(FILENAME,dir_fd=fd,follow_symlinks=False)
        if not _same_file(before,after) or not _same_file(after,named) or len(chunks)!=after.st_size:
            raise _refuse()
        _recheck_directory(root,deadline)
        reference=BaselineReference(root.generation_id,hashlib.sha256(chunks).hexdigest(),
                                    after.st_dev,after.st_ino,after.st_size)
        if expected is not None and reference!=expected:
            raise _refuse()
        try:
            baseline=HistoryBaseline.from_private_wire(json.loads(chunks,object_pairs_hook=_pairs))
        except (ValueError,UnicodeError,RecursionError):
            raise _refuse() from None
        if baseline.source_generation_id!=root.generation_id:
            raise _refuse()
        _budget(deadline)
        return baseline,reference
    finally:
        os.close(file)


def read_baseline(root: GenerationBaselineRoot, expected: BaselineReference, *, deadline: float) -> HistoryBaseline:
    """Read ONLY the fixed artifact with exact retained hash/inode/size binding."""
    if not isinstance(expected,BaselineReference) or expected.generation_id!=root.generation_id:
        raise _refuse()
    with _directory(root,deadline) as fd:
        return _read(fd,root,deadline,expected)[0]


def write_baseline(root: GenerationBaselineRoot, baseline: HistoryBaseline, *, deadline: float,
                   expected: BaselineReference | None = None) -> BaselineReference:
    """Atomic candidate bytes only; no sealing or cleanup-state upgrade.

    First write requires absence. Replacement requires the exact prior reference.
    The owner is responsible for writer/capture epochs and final Close sealing.
    """
    if not isinstance(baseline,HistoryBaseline) or baseline.source_generation_id!=root.generation_id:
        raise _refuse()
    raw=baseline.private_bytes()
    with _directory(root,deadline) as fd:
        if expected is not None:
            _read(fd,root,deadline,expected)
        else:
            try:
                os.stat(FILENAME,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _refuse()
        temporary='.history-baseline-'+uuid.uuid4().hex
        child=-1
        created=None
        try:
            _budget(deadline)
            child=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=fd)
            created=os.fstat(child)
            remaining=memoryview(raw)
            while remaining:
                _budget(deadline)
                count=os.write(child,remaining)
                if count<=0:
                    raise _refuse()
                remaining=remaining[count:]
            os.fsync(child)
            _budget(deadline)
            _recheck_directory(root,deadline)
            if expected is not None:
                _read(fd,root,deadline,expected)
            else:
                try:
                    os.stat(FILENAME,dir_fd=fd,follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise _refuse()
            _budget(deadline)
            named=os.stat(temporary,dir_fd=fd,follow_symlinks=False)
            if (named.st_dev,named.st_ino)!=(created.st_dev,created.st_ino):
                raise _refuse()
            if expected is None:
                # An absent first name cannot overwrite a newly appeared file.
                os.link(temporary,FILENAME,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                os.unlink(temporary,dir_fd=fd)
            else:
                os.rename(temporary,FILENAME,src_dir_fd=fd,dst_dir_fd=fd)
            os.fsync(fd)
            saved,reference=_read(fd,root,deadline,None)
            if saved!=baseline or reference.sha256!=hashlib.sha256(raw).hexdigest():
                raise _refuse()
            return reference
        finally:
            if child>=0:
                os.close(child)
            try:
                named=os.stat(temporary,dir_fd=fd,follow_symlinks=False)
                # Never remove a replaced temporary name belonging to another writer.
                if created is not None and (named.st_dev,named.st_ino)==(created.st_dev,created.st_ino):
                    os.unlink(temporary,dir_fd=fd)
            except FileNotFoundError:
                pass
