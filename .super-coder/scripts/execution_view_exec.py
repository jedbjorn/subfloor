"""Apply a Landlock filesystem view, then replace this process with a command."""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from pathlib import Path

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

READ_FILE = 1 << 2
READ_DIR = 1 << 3
FILE_ACCESS = (1 << 0) | (1 << 1) | READ_FILE | (1 << 14) | (1 << 15)
ABI_ACCESS_BITS = {
    1: 13,
    2: 14,
    3: 15,
    4: 15,
    5: 16,
}


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _syscall(number: int, *args) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(number, *args)
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(result)


def _add_rule(ruleset_fd: int, path: Path, access: int) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return
    if not stat.S_ISDIR(info.st_mode):
        access &= FILE_ACCESS
    if not access:
        return
    flags = os.O_PATH | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        attr = PathBeneathAttr(access, descriptor)
        _syscall(
            SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
    finally:
        os.close(descriptor)


def _mask_trie(paths: list[Path]) -> dict:
    root: dict = {}
    for path in paths:
        parts = Path(path).absolute().parts[1:]
        node = root
        for part in parts:
            node = node.setdefault(part, {})
        node[None] = {}
    return root


def _allow_frontier(
    ruleset_fd: int,
    directory: Path,
    trie: dict,
    all_access: int,
) -> None:
    # Do not grant rights on an ancestor: Landlock rules are recursive, so even
    # READ_DIR there would make a masked descendant directory listable. Direct
    # traversal remains possible; safe siblings receive their own full rule.
    try:
        children = tuple(os.scandir(directory))
    except OSError:
        return
    for child in children:
        branch = trie.get(child.name)
        child_path = directory / child.name
        if branch is None:
            _add_rule(ruleset_fd, child_path, all_access)
            continue
        if None in branch:
            continue
        try:
            is_directory = child.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_directory:
            _allow_frontier(ruleset_fd, child_path, branch, all_access)


def restrict(paths: list[Path]) -> None:
    abi = _syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        0,
        0,
        LANDLOCK_CREATE_RULESET_VERSION,
    )
    version = max(version for version in ABI_ACCESS_BITS if version <= abi)
    bits = ABI_ACCESS_BITS[version]
    all_access = (1 << bits) - 1
    attr = RulesetAttr(all_access)
    ruleset_fd = _syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    try:
        _allow_frontier(ruleset_fd, Path("/"), _mask_trie(paths), all_access)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        _syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def main(argv: list[str]) -> int:
    masks: list[Path] = []
    index = 0
    while index < len(argv) and argv[index] == "--mask":
        if index + 1 >= len(argv):
            raise ValueError("missing mask path")
        masks.append(Path(argv[index + 1]))
        index += 2
    if index >= len(argv) or argv[index] != "--" or index + 1 >= len(argv):
        raise ValueError("missing execution command")
    restrict(masks)
    command = argv[index + 1 :]
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":
    from cli_entry import run_cli

    try:
        raise SystemExit(run_cli(main, sys.argv[1:]))
    except (OSError, ValueError):
        raise SystemExit(
            "restricted_shell_view_unavailable: the required shell execution "
            "view could not be established; launch Admin to repair the "
            "Subfloor runtime"
        )
