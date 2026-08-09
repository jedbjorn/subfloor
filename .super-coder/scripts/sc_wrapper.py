"""Manage the user-local, checkout-selecting ``sc`` host wrapper.

The wrapper is shared by every subfloor installation owned by one host user.
Install roots are therefore registered outside any one checkout, while the
wrapper itself deliberately selects only the caller's current Git toplevel.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

REGISTRY_SCHEMA = 1
WRAPPER_VERSION = 1
REGISTRY_NAME = "installs.json"
LOCK_NAME = "installs.lock"
PATH_BEGIN = "# >>> subfloor managed PATH >>>"
PATH_END = "# <<< subfloor managed PATH <<<"
PATH_BLOCK = (
    f"{PATH_BEGIN}\n"
    'case ":$PATH:" in\n'
    '  *:"$HOME/.local/bin":*) ;;\n'
    '  *) export PATH="$HOME/.local/bin:$PATH" ;;\n'
    "esac\n"
    f"{PATH_END}\n"
)
WRAPPER_TEXT = """#!/bin/sh
# managed-by: subfloor sc-wrapper v1
top=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "sc: current directory is not inside a Git checkout; use ./sc from a subfloor checkout" >&2
  exit 127
}
if ! git -C "$top" ls-files --error-unmatch -- sc >/dev/null 2>&1; then
  echo "sc: Git checkout has no tracked sc launcher: $top/sc; use ./sc after installing subfloor in this checkout" >&2
  exit 127
fi
if [ ! -x "$top/sc" ]; then
  echo "sc: tracked launcher is not executable: $top/sc; repair it, then use ./sc" >&2
  exit 127
fi
exec "$top/sc" "$@"
"""
WRAPPER_BYTES = WRAPPER_TEXT.encode()
WRAPPER_DIGEST = hashlib.sha256(WRAPPER_BYTES).hexdigest()


class WrapperError(RuntimeError):
    """Managed wrapper state is unsafe or cannot be updated."""


def _home(environ: Mapping[str, str]) -> Path:
    raw = environ.get("HOME")
    if not raw:
        raise WrapperError("HOME is unset; use ./sc from this checkout")
    return Path(raw).expanduser()


def state_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ or os.environ
    raw = env.get("XDG_STATE_HOME")
    return Path(raw).expanduser() / "super-coder" if raw else _home(env) / ".local/state/super-coder"


def wrapper_path(environ: Mapping[str, str] | None = None) -> Path:
    return _home(environ or os.environ) / ".local/bin/sc"


def _canonical_root(repo_root: Path) -> str:
    return str(repo_root.expanduser().resolve())


def _new_registry() -> dict[str, object]:
    return {
        "schema": REGISTRY_SCHEMA,
        "wrapper": {"version": WRAPPER_VERSION, "sha256": WRAPPER_DIGEST},
        "installs": [],
    }


def _read_registry(path: Path) -> dict[str, object]:
    if not path.exists():
        return _new_registry()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WrapperError(f"managed wrapper registry is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
        raise WrapperError(f"unsupported managed wrapper registry schema: {path}")
    wrapper = data.get("wrapper")
    if wrapper != {"version": WRAPPER_VERSION, "sha256": WRAPPER_DIGEST}:
        raise WrapperError(f"managed wrapper registry metadata does not match this installer: {path}")
    installs = data.get("installs")
    if not isinstance(installs, list) or any(not isinstance(root, str) for root in installs):
        raise WrapperError(f"managed wrapper registry has invalid install roots: {path}")
    if len(installs) != len(set(installs)):
        raise WrapperError(f"managed wrapper registry has duplicate install roots: {path}")
    return data


def _wrapper_state(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "non-file"
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise WrapperError(f"cannot read wrapper target {path}: {exc}") from exc
    return "managed" if content == WRAPPER_BYTES else "unrelated"


def _require_compatible_wrapper(path: Path) -> str:
    state = _wrapper_state(path)
    if state in {"absent", "managed"}:
        return state
    raise WrapperError(
        f"refusing to overwrite {state} command at {path}; keep it and use ./sc "
        "from this checkout, or move it aside before retrying"
    )


def _profile_paths(environ: Mapping[str, str]) -> tuple[Path, Path]:
    home = _home(environ)
    return home / ".profile", home / ".zprofile"


def _profile_update(path: Path) -> bytes | None:
    try:
        current = path.read_text() if path.exists() else ""
    except OSError as exc:
        raise WrapperError(f"cannot read login profile {path}: {exc}") from exc
    begins = current.count(PATH_BEGIN)
    ends = current.count(PATH_END)
    if begins != ends or begins > 1:
        raise WrapperError(
            f"malformed subfloor PATH block in {path}; repair the sentinel pair and retry"
        )
    if begins == 1:
        start = current.index(PATH_BEGIN)
        end = current.index(PATH_END, start) + len(PATH_END)
        if current[start:end] != PATH_BLOCK.rstrip("\n"):
            raise WrapperError(
                f"modified subfloor PATH block in {path}; preserve it and use ./sc, "
                "or restore the managed block before retrying"
            )
        return None
    separator = "" if not current or current.endswith("\n") else "\n"
    prefix = "" if not current else "\n"
    return (current + separator + prefix + PATH_BLOCK).encode()


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_create(path: Path, content: bytes, mode: int) -> None:
    """Publish a new managed file without replacing a racing user target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            _require_compatible_wrapper(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = directory / LOCK_NAME
    with lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def check_install(environ: Mapping[str, str] | None = None) -> None:
    """Read-only conflict check used before the installer mutates a checkout."""
    env = environ or os.environ
    registry_path = state_dir(env) / REGISTRY_NAME
    if registry_path.exists():
        _read_registry(registry_path)
    _require_compatible_wrapper(wrapper_path(env))
    for profile in _profile_paths(env):
        _profile_update(profile)


def register_install(
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Register one install and ensure the shared wrapper and login PATH."""
    env = environ or os.environ
    directory = state_dir(env)
    registry_path = directory / REGISTRY_NAME
    target = wrapper_path(env)
    root = _canonical_root(repo_root)
    with _locked(directory):
        registry = _read_registry(registry_path)
        wrapper_state = _require_compatible_wrapper(target)
        profile_updates = [
            (profile, update)
            for profile in _profile_paths(env)
            if (update := _profile_update(profile)) is not None
        ]
        registered_roots = registry["installs"]
        assert isinstance(registered_roots, list)
        installs = sorted({*registered_roots, root})
        registry["installs"] = installs

        if wrapper_state == "absent":
            _atomic_create(target, WRAPPER_BYTES, 0o755)
        elif not os.access(target, os.X_OK):
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for profile, content in profile_updates:
            _atomic_write(profile, content, 0o600)
        payload = (json.dumps(registry, indent=2, sort_keys=True) + "\n").encode()
        _atomic_write(registry_path, payload, 0o600)
    return f"registered {root}; managed wrapper ready at {target}"


def unregister_install(
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Drop one install and remove only an unchanged last-owner wrapper."""
    env = environ or os.environ
    directory = state_dir(env)
    registry_path = directory / REGISTRY_NAME
    target = wrapper_path(env)
    root = _canonical_root(repo_root)
    with _locked(directory):
        registry = _read_registry(registry_path)
        registered = root in registry["installs"]  # type: ignore[operator]
        if not registered:
            return f"no registration for {root}; preserved shared wrapper"
        installs = [item for item in registry["installs"] if item != root]  # type: ignore[index]
        registry["installs"] = installs
        result = f"removed registration for {root}"
        if not installs:
            wrapper_state = _wrapper_state(target)
            if wrapper_state == "managed":
                target.unlink()
                result += f"; removed unchanged managed wrapper {target}"
            elif wrapper_state == "absent":
                result += "; managed wrapper was already absent"
            else:
                result += f"; preserved {wrapper_state} wrapper at {target}"
        payload = (json.dumps(registry, indent=2, sort_keys=True) + "\n").encode()
        _atomic_write(registry_path, payload, 0o600)
    return result
