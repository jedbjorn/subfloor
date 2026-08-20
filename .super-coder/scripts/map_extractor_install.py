#!/usr/bin/env python3
"""Validate and install one Cartographer-authored map extractor."""
from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

import artifact_policy
import map_repo


SAFE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\.py\Z")
RECEIPT_VERSION = 1


class ExtractorInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    path: Path
    name: str
    body: bytes
    digest: str
    source_path: str
    source_worktree: Path
    source_git_ref: str | None
    source_tracked: bool


@dataclass(frozen=True)
class InstallResult:
    target: Path
    receipt: Path
    digest: str
    old_digest: str | None
    source_tracked: bool
    source_path: str
    source_git_ref: str | None
    changed: bool


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _source_git_state(worktree: Path, source_path: str) -> tuple[bool, str | None]:
    top = _git(worktree, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree:
        return False, None
    head = _git(worktree, "rev-parse", "HEAD")
    git_ref = head.stdout.strip() if head.returncode == 0 else None
    tracked = _git(worktree, "ls-files", "--error-unmatch", "--", source_path)
    if tracked.returncode != 0:
        return False, git_ref
    status_result = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        source_path,
    )
    return status_result.returncode == 0 and not status_result.stdout.strip(), git_ref


def _validate_extract(body: str, source: Path) -> None:
    try:
        tree = ast.parse(body, filename=str(source))
    except SyntaxError as exc:
        detail = f"line {exc.lineno}: {exc.msg}" if exc.lineno else exc.msg
        raise ExtractorInstallError(f"Python syntax validation failed ({detail})") from exc
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "extract"
    ]
    if len(matches) != 1 or isinstance(matches[0], ast.AsyncFunctionDef):
        raise ExtractorInstallError(
            "extractor must define exactly one top-level synchronous extract function"
        )
    args = matches[0].args
    positional = [*args.posonlyargs, *args.args]
    if (
        [arg.arg for arg in positional] != ["con", "repo_root", "cfg"]
        or args.vararg is not None
        or args.kwarg is not None
        or args.kwonlyargs
    ):
        raise ExtractorInstallError(
            "extract must have exactly the positional parameters con, repo_root, cfg"
        )


def validate_candidate(
    raw_source: str,
    environ: Mapping[str, str] | None = None,
) -> Candidate:
    env = environ if environ is not None else os.environ
    flavor = env.get("SC_SHELL_FLAVOR", "")
    raw_worktree = env.get("SC_SHELL_WORKTREE", "")
    if not flavor or not raw_worktree:
        raise ExtractorInstallError(
            "missing launched-shell identity (SC_SHELL_FLAVOR and SC_SHELL_WORKTREE required)"
        )
    if flavor != "cartographer":
        raise ExtractorInstallError("only a launched Cartographer may install map extractors")
    try:
        worktree = Path(raw_worktree).resolve(strict=True)
    except OSError as exc:
        raise ExtractorInstallError(f"shell worktree is unavailable: {raw_worktree}") from exc
    expected_dir = worktree / ".sc-state" / "map_extractors"
    source = Path(raw_source).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    try:
        source_stat = source.lstat()
    except OSError as exc:
        raise ExtractorInstallError(f"candidate is not a readable regular file: {source}") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise ExtractorInstallError("candidate must be a regular, non-symlink file")
    try:
        resolved = source.resolve(strict=True)
        expected_resolved = expected_dir.resolve(strict=True)
    except OSError as exc:
        raise ExtractorInstallError(
            f"candidate must be inside {expected_dir}"
        ) from exc
    if resolved.parent != expected_resolved:
        raise ExtractorInstallError(
            f"candidate must be a direct child of {expected_dir}"
        )
    if resolved.name.startswith("_"):
        raise ExtractorInstallError("extractor names beginning with '_' are reserved")
    if resolved.suffix != ".py" or not SAFE_NAME.fullmatch(resolved.name):
        raise ExtractorInstallError(
            "extractor filename must match [A-Za-z][A-Za-z0-9_]*.py"
        )
    try:
        body = resolved.read_bytes()
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractorInstallError("extractor must be valid UTF-8") from exc
    except OSError as exc:
        raise ExtractorInstallError(f"cannot read candidate: {resolved}") from exc
    _validate_extract(text, resolved)
    source_path = resolved.relative_to(worktree).as_posix()
    tracked, git_ref = _source_git_state(worktree, source_path)
    return Candidate(
        path=resolved,
        name=resolved.name,
        body=body,
        digest=hashlib.sha256(body).hexdigest(),
        source_path=source_path,
        source_worktree=worktree,
        source_git_ref=git_ref,
        source_tracked=tracked,
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_directory_beneath(base: Path, relative: Path) -> Path:
    """Create a destination directory without following repo-local symlinks."""
    if relative.is_absolute() or ".." in relative.parts:
        raise ExtractorInstallError("internal install destination escaped its base")
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        raise ExtractorInstallError(f"unsafe install base: {base}")
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ExtractorInstallError(f"refusing symlinked install path: {current}")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise ExtractorInstallError(f"install path is not a directory: {current}")
    return current


def _atomic_replace_bytes(path: Path, body: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _persist_receipt(path: Path, payload: dict[str, object]) -> None:
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_replace_bytes(path, body, mode=0o600)


def _snapshot(path: Path) -> tuple[bool, bytes | None, int | None]:
    if path.is_symlink():
        raise ExtractorInstallError(f"refusing symlinked live state: {path}")
    if not path.exists():
        return False, None, None
    if not path.is_file():
        raise ExtractorInstallError(f"live state is not a regular file: {path}")
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore(path: Path, snapshot: tuple[bool, bytes | None, int | None]) -> None:
    existed, body, mode = snapshot
    if existed:
        assert body is not None and mode is not None
        _atomic_replace_bytes(path, body, mode=mode)
        return
    if path.exists() or path.is_symlink():
        path.unlink()
        _fsync_directory(path.parent)


@contextmanager
def _install_lock() -> Iterator[None]:
    lock_path = artifact_policy.map_extractor_install_lock_path()
    _prepare_directory_beneath(artifact_policy.LOCAL_DIR, Path("map"))
    with lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _receipt_payload(candidate: Candidate) -> dict[str, object]:
    return {
        "version": RECEIPT_VERSION,
        "extractor": candidate.name,
        "digest": candidate.digest,
        "source_path": candidate.source_path,
        "source_worktree": str(candidate.source_worktree),
        "source_git_ref": candidate.source_git_ref,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }


def _same_receipt(existing: bytes | None, desired: dict[str, object]) -> bool:
    if existing is None:
        return False
    try:
        payload = json.loads(existing)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    comparable = {key: value for key, value in desired.items() if key != "installed_at"}
    return all(payload.get(key) == value for key, value in comparable.items())


def install_extractor(candidate: Candidate) -> InstallResult:
    with _install_lock():
        target_dir = _prepare_directory_beneath(
            map_repo.MAP_ROOT,
            Path(".sc-state") / "map_extractors",
        )
        receipt_dir = _prepare_directory_beneath(
            artifact_policy.LOCAL_DIR,
            Path("map") / "extractor-receipts",
        )
        target = target_dir / candidate.name
        receipt = receipt_dir / f"{candidate.path.stem}.json"
        target_snapshot = _snapshot(target)
        receipt_snapshot = _snapshot(receipt)
        old_digest = (
            hashlib.sha256(target_snapshot[1]).hexdigest()
            if target_snapshot[1] is not None else None
        )
        payload = _receipt_payload(candidate)
        target_matches = old_digest == candidate.digest
        receipt_matches = _same_receipt(receipt_snapshot[1], payload)
        if target_matches and receipt_matches:
            return InstallResult(
                target, receipt, candidate.digest, old_digest,
                candidate.source_tracked, candidate.source_path,
                candidate.source_git_ref, False,
            )
        try:
            if not target_matches:
                _atomic_replace_bytes(target, candidate.body)
            _persist_receipt(receipt, payload)
        except Exception as exc:
            try:
                _restore(target, target_snapshot)
                _restore(receipt, receipt_snapshot)
            except Exception as rollback_exc:
                raise ExtractorInstallError(
                    f"receipt persistence failed and rollback failed: {rollback_exc}"
                ) from exc
            raise ExtractorInstallError(
                f"receipt persistence failed; prior extractor and receipt restored: {exc}"
            ) from exc
    return InstallResult(
        target, receipt, candidate.digest, old_digest,
        candidate.source_tracked, candidate.source_path,
        candidate.source_git_ref, True,
    )


def _print_result(result: InstallResult) -> None:
    print(f"installed: {result.target}")
    print(f"digest: {result.digest}")
    if result.old_digest is None:
        print("change: new extractor")
    elif result.old_digest == result.digest:
        print("change: receipt refreshed" if result.changed else "change: unchanged")
    else:
        print(f"change: updated {result.old_digest} -> {result.digest}")
    if result.source_tracked:
        suffix = f" at {result.source_git_ref}" if result.source_git_ref else ""
        print(f"source: tracked — {result.source_path}{suffix}")
    else:
        print(
            "source: pending — commit and push "
            f"{result.source_path}, then ask Admin to review and merge it"
        )


def main(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    if argv in (["-h"], ["--help"]):
        print("usage: sc map-extractor install <worktree-file>")
        return 0
    if len(argv) != 2 or argv[0] != "install":
        raise ExtractorInstallError(
            "usage: sc map-extractor install <worktree-file>"
        )
    candidate = validate_candidate(argv[1], environ)
    _print_result(install_extractor(candidate))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    try:
        raise SystemExit(run_cli(main, sys.argv[1:]))
    except ExtractorInstallError as exc:
        raise SystemExit(f"map-extractor: {exc}") from exc
