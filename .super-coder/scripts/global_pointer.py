"""Conservative retirement of legacy user-global instructions; no API or DB access.

The module name is retained for installed lifecycle imports. No writer remains.
Directory locks coordinate updated installations without creating config/lock
files. Non-cooperating user writers are detected by a final snapshot check;
this is not a guarantee against writes after that check.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SENTINEL = "<!-- managed by super-coder — hand edits are overwritten -->"
CATALOGUE = ENGINE / "assets" / "legacy-global-pointers.json"


@dataclass(frozen=True)
class Snapshot:
    identity: tuple
    content: bytes
    mode: int


@dataclass(frozen=True)
class Result:
    path: Path
    status: str
    reason: str = ""


@contextmanager
def _parent(path: Path):
    """Open every ancestor without following symlinks, including config roots."""
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        readable = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = readable
        yield fd
    finally:
        os.close(fd)


def _snapshot(fd: int, name: str) -> Snapshot | None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{name}: symlink or nonregular file")
    if not info.st_mode & 0o444:
        raise ValueError(f"{name}: unreadable file")
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("changed-during-cleanup")
        content = stream.read()
        after = os.fstat(stream.fileno())
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
        raise ValueError("changed-during-cleanup")
    return Snapshot((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns), content, stat.S_IMODE(after.st_mode))


def _known(catalogue: dict) -> set[bytes]:
    contents = [item["content"].encode() for item in catalogue["templates"]]
    return {variant for content in contents for variant in (content, content.replace(b"\n", b"\r\n"))}


def _disposition(target: Snapshot | None, backup: Snapshot | None, known: set[bytes]) -> str:
    if target is None:
        return "clean"
    if target.content not in known:
        if SENTINEL.encode() in target.content:
            raise ValueError("edited or unknown managed pointer; operator edit required")
        return "preserved-user-content"
    if backup is None:
        return "removable"
    if backup.content in known or SENTINEL.encode() in backup.content:
        raise ValueError("backup is managed or ambiguous; operator edit required")
    return "restorable"


def _validate(path: Path, fd: int, target: Snapshot, backup: Snapshot | None) -> None:
    with _parent(path) as current:
        old, new = os.fstat(fd), os.fstat(current)
        if (old.st_dev, old.st_ino) != (new.st_dev, new.st_ino):
            raise ValueError("changed-during-cleanup")
    if _snapshot(fd, path.name) != target or _snapshot(fd, path.name + ".pre-sc.bak") != backup:
        raise ValueError("changed-during-cleanup")


def _apply(path: Path, fd: int, target: Snapshot, backup: Snapshot | None) -> None:
    if backup is None:
        _validate(path, fd, target, backup)
        os.unlink(path.name, dir_fd=fd)
        os.fsync(fd)
        return
    # Use the pinned directory descriptor for staging as well as replacement.
    temp_name = ".sc-harness-cleanup-" + secrets.token_hex(16)
    temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
    try:
        with os.fdopen(temp_fd, "wb") as stream:
            stream.write(backup.content)
            os.fchmod(stream.fileno(), backup.mode & 0o777)
            stream.flush()
            os.fsync(stream.fileno())
        _validate(path, fd, target, backup)
        os.replace(temp_name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=fd)
        except FileNotFoundError:
            pass


def _inspect(path: Path, known: set[bytes], apply: bool) -> Result:
    inspected = False
    try:
        with _parent(path) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX if apply else fcntl.LOCK_SH)
            inspected = True
            target = _snapshot(fd, path.name)
            # An absent or user-owned target never resurrects/inspects a backup.
            if target is None or target.content not in known:
                return Result(path, _disposition(target, None, known))
            backup = _snapshot(fd, path.name + ".pre-sc.bak")
            status = _disposition(target, backup, known)
            if apply:
                _apply(path, fd, target, backup)
                return Result(path, "clean", "restored backup (retained)" if backup else "removed managed pointer")
            return Result(path, status)
    except FileNotFoundError:
        if inspected:
            return Result(path, "unresolved", "changed-during-cleanup")
        # A missing ancestor is clean; a disappearing captured file is a conflict
        # inside _validate, where None fails the snapshot comparison.
        return Result(path, "clean", "config directory absent")
    except (OSError, ValueError) as exc:
        return Result(path, "unresolved", str(exc))


def _config_root(value: str) -> tuple[str, Path]:
    harness, separator, raw = value.partition("=")
    if not separator or harness not in {"claude", "codex", "opencode"} or not Path(raw).is_absolute():
        raise ValueError("--config-root requires claude|codex|opencode=<absolute-directory>")
    if ".." in Path(raw).parts:
        raise ValueError("config roots must not contain '..'")
    return harness, Path(raw)


def _effective_root(raw: str, home: str) -> str:
    # Legacy CODEX_HOME accepted expanduser and cwd-relative paths. Resolve
    # lexically only; _parent still refuses symlinks and '..' is unresolved.
    if raw == "~" or raw.startswith("~/"):
        return str(Path(home) / raw[2:]) if raw != "~" else home
    path = Path(raw)
    if raw.startswith("~"):
        return raw  # another user's home requires an explicit absolute root
    return str(path if path.is_absolute() else Path.cwd() / path)


def reconcile(*, apply: bool = True, home: Path | None = None,
              environ: Mapping[str, str] | None = None,
              config_roots: tuple[str, ...] = (), report: bool = True) -> list[Result]:
    """Inspect known defaults and configured roots; lifecycle callers may continue.

    Sandbox processes must never clean mounted host configuration. An explicit
    operator invocation reports that limitation rather than claiming success.
    """
    env = os.environ if environ is None else environ
    if env.get("IS_SANDBOX") or env.get("SC_SANDBOX"):
        results = [Result(Path("<host-config>"), "unresolved", "run harness-cleanup on the host; sandbox configuration is not inspected")]
    else:
        results = _reconcile(apply, home, env, config_roots)
    if report:
        for result in results:
            suffix = f" — {result.reason}" if result.reason else ""
            print(f"harness-cleanup: {result.path}: {result.status}{suffix}")
        if any(item.status == "unresolved" for item in results):
            print("harness-cleanup: incomplete; bare-session coexistence is not established")
    return results


def _reconcile(apply, home, env, config_roots) -> list[Result]:
    raw_home = str(home) if home is not None else env.get("HOME", "")
    if not raw_home or not Path(raw_home).is_absolute() or ".." in Path(raw_home).parts:
        return [Result(Path("<HOME>"), "unresolved", "HOME must be an absolute directory without '..'")]
    try:
        catalogue = json.loads(CATALOGUE.read_text())
        known = _known(catalogue)
        targets = catalogue["targets"]
        paths = [Path(raw_home) / relative for relative in targets.values()]
        roots = list(config_roots)
        for harness, variable in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("opencode", "OPENCODE_CONFIG_DIR")):
            if env.get(variable):
                roots.append(f"{harness}={_effective_root(env[variable], raw_home)}")
        if env.get("XDG_CONFIG_HOME"):
            roots.append(f"opencode={_effective_root(env['XDG_CONFIG_HOME'], raw_home)}/opencode")
    except (OSError, ValueError, KeyError) as exc:
        return [Result(Path(raw_home), "unresolved", str(exc))]
    invalid = []
    for raw in roots:
        try:
            harness, root = _config_root(raw)
        except ValueError as exc:
            invalid.append(Result(Path(raw), "unresolved", str(exc)))
            continue
        paths.append(root / Path(targets[harness]).name)
    return invalid + [_inspect(path, known, apply) for path in dict.fromkeys(paths)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retire recognized legacy global harness pointers without an API or shell identity.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only inspection (default)")
    mode.add_argument("--apply", action="store_true", help="remove exact pointers or restore unambiguous backups")
    parser.add_argument("--config-root", action="append", default=[], metavar="HARNESS=/absolute/directory")
    args = parser.parse_args(argv)
    results = reconcile(apply=args.apply, config_roots=tuple(args.config_root))
    return int(any(item.status == "unresolved" for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
