"""One scoped Claude trust boolean for a genuinely prepared fixture MAIN.

No CLI or generic config writer. The fixed maintainer operation supplies the
registered setup context; this component never starts Claude or reads history.
Native 2.1.287 uses mkdir(selected global file + '.lock'), a fresh read, and
atomic replacement. We refuse every existing lock and never reclaim one.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from conversation_runtime_contract import RuntimeContext, RuntimeContractError

NATIVE_SHA256 = '3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0'
MAX_BYTES = 1024 * 1024
LOCK_SECONDS = 2.0  # below the captured native default stale interval (10s)


def refuse() -> None:
    raise RuntimeContractError('TRUST_INCONCLUSIVE', 'scoped trust observation unavailable')


def remaining(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        refuse()
    left = deadline - time.monotonic()
    if left <= 0:
        refuse()
    return left


def identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid


def safe_directory(path: Path) -> os.stat_result:
    if not path.is_absolute() or path.resolve() != path:
        refuse()
    # Root-owned ancestors and the sticky system temporary directory are safe;
    # the selected config parent itself must belong to this user.
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
            refuse()
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            refuse()
    info = path.lstat()
    if info.st_uid != os.getuid():
        refuse()
    return info


def config_override(value: str | None) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        refuse()
    path = Path(value)
    safe_directory(path)
    return path


def selected_config(home: Path, override: Path | None) -> Path:
    safe_directory(home)
    directory = override if override is not None else home / '.claude'
    if directory.exists() or directory.is_symlink():
        safe_directory(directory)
        legacy = directory / '.config.json'
        if legacy.exists() or legacy.is_symlink():
            return legacy
    elif override is not None:
        refuse()
    parent = override if override is not None else home
    safe_directory(parent)
    return parent / '.claude.json'


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            refuse()
        value[key] = item
    return value


def read_config(parent: int, name: str, deadline: float) -> tuple[dict[str, Any], os.stat_result | None]:
    remaining(deadline)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return {}, None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o077 or before.st_size > MAX_BYTES):
            refuse()
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            remaining(deadline)
            part = os.read(fd, min(65536, MAX_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        remaining(deadline)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if len(raw) != before.st_size or identity(before) != identity(after) or identity(before) != identity(current):
            refuse()
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: refuse())
        if not isinstance(result, dict):
            refuse()
        return result, before
    finally:
        os.close(fd)


def merge_trust(config: Path, main: Path, *, deadline: float, verify_owned: Callable[[], bool]) -> dict[str, Any]:
    """Bounded native-compatible lock/merge. Paths are internal prepared inputs."""
    remaining(deadline)
    if not verify_owned():
        refuse()
    remaining(deadline)
    parent_info = safe_directory(config.parent)
    parent = os.open(config.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = config.name + '.lock'
    lock_info = None
    temporary = None
    try:
        if identity(os.fstat(parent)) != identity(parent_info):
            refuse()
        remaining(deadline)
        # No retry/stale steal: even native's old lock remains an explicit
        # inconclusive prerequisite. mkdir is the actual native primitive.
        os.mkdir(lock, mode=0o700, dir_fd=parent)
        lock_info = os.stat(lock, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(lock_info.st_mode) or lock_info.st_uid != os.getuid() or stat.S_IMODE(lock_info.st_mode) != 0o700:
            refuse()
        until = min(deadline, time.monotonic() + LOCK_SECONDS)

        def check() -> None:
            remaining(until)
            held = os.stat(lock, dir_fd=parent, follow_symlinks=False)
            if identity(held) != identity(lock_info) or not stat.S_ISDIR(held.st_mode):
                refuse()
            current_parent = safe_directory(config.parent)
            # Directory mtime changes when our lock/temp is created; inode and
            # owner/mode, rather than mtime, bind the open directory.
            if (current_parent.st_dev, current_parent.st_ino, current_parent.st_uid, current_parent.st_mode) != (
                    parent_info.st_dev, parent_info.st_ino, parent_info.st_uid, parent_info.st_mode):
                refuse()

        check()
        value, before = read_config(parent, config.name, until)
        original = dict(value)
        projects = value.get('projects', {})
        if not isinstance(projects, dict):
            refuse()
        entry = projects.get(str(main), {})
        if not isinstance(entry, dict):
            refuse()
        already = entry.get('hasTrustDialogAccepted') is True
        if not already:
            value['projects'] = {**projects, str(main): {**entry, 'hasTrustDialogAccepted': True}}
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()
        if len(raw) > MAX_BYTES:
            refuse()
        if not verify_owned():
            refuse()
        check()  # a blocking owner query may have consumed the entire budget
        actual, current = read_config(parent, config.name, until)
        if (before is None) != (current is None) or (before is not None and (current is None or identity(before) != identity(current))):
            refuse()
        # A writer that ignored the native lock cannot silently lose changes.
        if actual != original:
            refuse()
        if not already:
            temporary = '.' + config.name + '.sc-trust-' + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         stat.S_IMODE(before.st_mode) if before is not None else 0o600, dir_fd=parent)
            try:
                written = 0
                while written < len(raw):
                    check()
                    written += os.write(fd, raw[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            if not verify_owned():
                refuse()
            check()
            # Revalidate the old target immediately before the atomic replace.
            try:
                current = os.stat(config.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if (before is None) != (current is None) or (before is not None and (current is None or identity(before) != identity(current))):
                refuse()
            check()
            os.replace(temporary, config.name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = None
            os.fsync(parent)
        check()
        observed, _ = read_config(parent, config.name, until)
        if observed != value:
            refuse()
        if not verify_owned():
            refuse()
        check()
        return {'state': 'already_trusted' if already else 'trust_written',
                'exact_main_trusted': True, 'target_sha256': hashlib.sha256(str(main).encode()).hexdigest(),
                'native_started': False, 'behavior_verified': False}
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=parent)
        if lock_info is not None:
            try:
                held = os.stat(lock, dir_fd=parent, follow_symlinks=False)
                if identity(held) == identity(lock_info):
                    os.rmdir(lock, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def prepared_trust(context: RuntimeContext, main: Path, *, deadline: float,
                   verify_owned: Callable[[], bool]) -> Mapping[str, Any]:
    setup = importlib.import_module('claude_setup')
    digest, validate = setup.digest, setup.validate

    validate(context, main, deadline)
    if context.executable.sha256 != NATIVE_SHA256 or digest(context.executable.path) != NATIVE_SHA256:
        refuse()
    if context.env.get('CLAUDE_CODE_CUSTOM_OAUTH_URL'):
        refuse()
    executable_info = context.executable.path.lstat()
    # Fresh fixed CLI processes, normal production config only. No cached
    # host-seeded global-file or OAuth deployment variant is guessed here.
    home = Path(context.env.get('HOME', ''))
    override = config_override(context.env.get('CLAUDE_CONFIG_DIR'))
    config = selected_config(home, override)
    def current() -> bool:
        valid = verify_owned()
        remaining(deadline)
        return (valid and selected_config(home, override) == config
                and identity(context.executable.path.lstat()) == identity(executable_info))

    if not current():
        refuse()
    result = merge_trust(config, main, deadline=deadline, verify_owned=current)
    if selected_config(home, override) != config:
        refuse()
    return result


def read_result(root: Path, generation: str, helper_sha256: str, *, deadline: float) -> dict[str, Any]:
    unavailable = {'state': 'trust_inconclusive', 'exact_main_trusted': False,
                   'native_started': False, 'behavior_verified': False}
    try:
        remaining(deadline)
        root_info = safe_directory(root)
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if identity(os.fstat(parent)) != identity(root_info):
                return unavailable
            info = os.stat('claude-trust-result.json', dir_fd=parent, follow_symlinks=False)
            if info.st_size > 512:
                return unavailable
            value, _ = read_config(parent, 'claude-trust-result.json', deadline)
            if identity(root.lstat()) != identity(root_info):
                return unavailable
        finally:
            os.close(parent)
        if (set(value) != {'state','exact_main_trusted','target_sha256','native_started',
                          'behavior_verified','generation','helper_sha256'}
                or value['generation'] != generation or value['helper_sha256'] != helper_sha256
                or value['state'] not in {'trust_written','already_trusted'}
                or value['exact_main_trusted'] is not True or value['native_started'] is not False
                or value['behavior_verified'] is not False or not isinstance(value['target_sha256'], str)
                or len(value['target_sha256']) != 64 or any(c not in '0123456789abcdef' for c in value['target_sha256'])):
            return unavailable
        remaining(deadline)
        return {key:value[key] for key in ('state','exact_main_trusted','target_sha256','native_started','behavior_verified')}
    except (OSError, ValueError, TypeError, RuntimeError):
        return unavailable
