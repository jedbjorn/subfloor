"""Read-only, bounded Git and pull-request review projections.

The module owns no HTTP or database policy.  It resolves local evidence from a
server-selected worktree/ref pair, enriches exact pull requests through the
shared GitHub reader, and exposes deterministic fingerprints for the API layer
to paginate and revalidate in Feature #26 task #152.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import artifact_policy
from github_pull_requests import (
    GitHubPullRequestReader,
    GitHubReadError,
    PullRequest,
    lifecycle_status,
)

GIT_TIMEOUT_SECONDS = 12.0
_STATUS_OUTPUT_LIMIT = 4 * 1024 * 1024
_DIFF_OUTPUT_OVERHEAD = 64 * 1024
_READ_ONLY_ENV = {
    **os.environ,
    "GIT_EXTERNAL_DIFF": "",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ReviewLimits:
    max_files: int = 2_000
    max_patch_bytes: int = 1024 * 1024
    max_line_bytes: int = 32 * 1024
    max_commits: int = 500
    max_path_bytes: int = 4_096
    max_artifact_bytes: int = 1024 * 1024


DEFAULT_LIMITS = ReviewLimits()


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GitReadError(ReviewError):
    pass


@dataclass(frozen=True)
class FileProjection:
    path: str
    status: str
    old_path: str | None = None
    additions: int | None = None
    deletions: int | None = None
    staged: bool = False
    unstaged: bool = False
    binary: bool = False
    conflict: bool = False
    generated: bool = False
    submodule: bool = False
    oversized: bool = False


@dataclass(frozen=True)
class WorkspaceProjection:
    branch: str | None
    head_sha: str
    upstream: str | None
    remote_branch_sha: str | None
    pushed: bool
    ahead: int
    behind: int
    files: tuple[FileProjection, ...]
    files_truncated: bool
    fingerprint: str
    etag: str


@dataclass(frozen=True)
class FileSetProjection:
    base_sha: str
    head_sha: str
    merge_base_sha: str | None
    files: tuple[FileProjection, ...]
    files_truncated: bool
    fingerprint: str
    etag: str


@dataclass(frozen=True)
class CommitProjection:
    sha: str
    short_sha: str
    author: str
    authored_at: str
    subject: str


@dataclass(frozen=True)
class CommitSetProjection:
    base_sha: str
    head_sha: str
    commits: tuple[CommitProjection, ...]
    commits_truncated: bool
    fingerprint: str
    etag: str


@dataclass(frozen=True)
class PatchProjection:
    text: str | None
    sha256: str | None
    truncated: bool
    binary: bool
    unavailable_reason: str | None
    etag: str


@dataclass(frozen=True)
class RemoteProjection:
    pull_request: PullRequest
    lifecycle: str
    freshness: str
    fingerprint: str
    etag: str


@dataclass(frozen=True)
class RemoteCollection:
    pull_requests: tuple[RemoteProjection, ...]
    freshness: str
    error: str | None


@dataclass(frozen=True)
class CacheArtifact:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class CanonicalPatchSet:
    files: tuple[FileProjection, ...]
    file_patches: dict[str, str]
    files_truncated: bool
    patch_truncated: bool
    fingerprint: str
    etag: str


@dataclass(frozen=True)
class CanonicalPatchRead:
    patch: PatchProjection
    freshness: str
    artifact: CacheArtifact | None


def fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def etag(value: str) -> str:
    return f'"{value}"'


def _completed_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _run_read_process(
    args: list[str],
    *,
    cwd: str,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    text: bool,
    timeout: float,
    check: bool,
    env: dict[str, str],
    start_new_session: bool,
) -> subprocess.CompletedProcess:
    """Run one Git read in its own group and reap the group on timeout."""
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        env=env,
        start_new_session=start_new_session,
    )
    try:
        result_stdout, result_stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        result_stdout, result_stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            args,
            timeout,
            output=result_stdout,
            stderr=result_stderr,
        )
    completed = subprocess.CompletedProcess(
        args,
        process.returncode,
        result_stdout,
        result_stderr,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            args,
            output=result_stdout,
            stderr=result_stderr,
        )
    return completed


def _run_git(
    worktree: Path,
    args: Sequence[str],
    *,
    runner: Callable[..., Any] = _run_read_process,
    timeout: float = GIT_TIMEOUT_SECONDS,
    output_limit: int = _STATUS_OUTPUT_LIMIT,
    allow_truncate: bool = False,
) -> tuple[bytes, bool]:
    command = ["git", "-C", str(worktree), "--no-pager", *args]
    try:
        result = runner(
            command,
            cwd=str(worktree),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=timeout,
            check=False,
            env=_READ_ONLY_ENV,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitReadError(
            "REVIEW_TARGET_UNAVAILABLE",
            f"Git read failed: {exc}",
        ) from exc
    stdout = _completed_bytes(result.stdout)
    if result.returncode != 0:
        stderr = _completed_bytes(result.stderr).decode("utf-8", errors="replace")
        detail = stderr.strip().splitlines()
        raise GitReadError(
            "REVIEW_REF_MISSING",
            (detail[0][:240] if detail else "Git could not resolve the review target"),
        )
    if len(stdout) > output_limit:
        if not allow_truncate:
            raise ReviewError(
                "REVIEW_DIFF_TOO_LARGE",
                "Git output exceeded the hard limit",
            )
        return stdout[:output_limit], True
    return stdout, False


def _resolve_worktree(worktree: str | Path) -> Path:
    try:
        resolved = Path(worktree).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReviewError(
            "REVIEW_WORKTREE_MISSING",
            "Conversation worktree is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise ReviewError(
            "REVIEW_WORKTREE_MISSING",
            "Conversation worktree is unavailable",
        )
    try:
        inside, _ = _run_git(
            resolved,
            ("rev-parse", "--is-inside-work-tree"),
            output_limit=64,
        )
    except GitReadError as exc:
        raise ReviewError(
            "REVIEW_NOT_A_GIT_REPOSITORY",
            "Conversation worktree is not a Git repository",
        ) from exc
    if inside.strip() != b"true":
        raise ReviewError(
            "REVIEW_NOT_A_GIT_REPOSITORY",
            "Conversation worktree is not a Git repository",
        )
    return resolved


def _decode_path(raw: bytes, limits: ReviewLimits) -> str:
    if len(raw) > limits.max_path_bytes:
        raise ReviewError("REVIEW_PATH_INVALID", "Review path is too long")
    path = raw.decode("utf-8", errors="surrogateescape")
    if "\x00" in path:
        raise ReviewError("REVIEW_PATH_INVALID", "Review path is invalid")
    return path


def _status_name(code: str) -> str:
    if "U" in code:
        return "conflict"
    for marker, status_name in (
        ("D", "deleted"),
        ("A", "added"),
        ("R", "renamed"),
        ("C", "renamed"),
        ("T", "modified"),
        ("M", "modified"),
    ):
        if marker in code:
            return status_name
    return "modified"


def _parse_status(
    payload: bytes,
    limits: ReviewLimits,
) -> tuple[dict[str, Any], list[FileProjection], bool]:
    branch: dict[str, Any] = {
        "branch": None,
        "head_sha": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
    }
    files: list[FileProjection] = []
    records = payload.split(b"\0")
    index = 0
    while index < len(records):
        raw = records[index]
        index += 1
        if not raw:
            continue
        if raw.startswith(b"# "):
            line = raw[2:].decode("utf-8", errors="replace")
            key, _, value = line.partition(" ")
            if key == "branch.oid" and value != "(initial)":
                branch["head_sha"] = value
            elif key == "branch.head" and value != "(detached)":
                branch["branch"] = value
            elif key == "branch.upstream":
                branch["upstream"] = value
            elif key == "branch.ab":
                values = value.split()
                if len(values) == 2:
                    branch["ahead"] = int(values[0].lstrip("+") or "0")
                    branch["behind"] = int(values[1].lstrip("-") or "0")
            continue
        if raw.startswith(b"? "):
            files.append(
                FileProjection(
                    path=_decode_path(raw[2:], limits),
                    status="untracked",
                    unstaged=True,
                )
            )
            continue
        if raw.startswith(b"! "):
            continue
        kind = raw[:1]
        if kind not in {b"1", b"2", b"u"}:
            continue
        fields = raw.split(b" ")
        if kind == b"1" and len(fields) >= 9:
            xy = fields[1].decode("ascii", errors="replace")
            path = _decode_path(b" ".join(fields[8:]), limits)
            files.append(
                FileProjection(
                    path=path,
                    status=_status_name(xy),
                    staged=xy[0] != ".",
                    unstaged=xy[1] != ".",
                    submodule=fields[2] != b"N...",
                )
            )
        elif kind == b"2" and len(fields) >= 10:
            xy = fields[1].decode("ascii", errors="replace")
            new_path = _decode_path(b" ".join(fields[9:]), limits)
            old_path = (
                _decode_path(records[index], limits) if index < len(records) else None
            )
            index += 1
            files.append(
                FileProjection(
                    path=new_path,
                    old_path=old_path,
                    status="renamed",
                    staged=xy[0] != ".",
                    unstaged=xy[1] != ".",
                    submodule=fields[2] != b"N...",
                )
            )
        elif kind == b"u" and len(fields) >= 11:
            files.append(
                FileProjection(
                    path=_decode_path(b" ".join(fields[10:]), limits),
                    status="conflict",
                    staged=True,
                    unstaged=True,
                    conflict=True,
                    submodule=fields[2] != b"N...",
                )
            )
    truncated = len(files) > limits.max_files
    files = sorted(files, key=lambda item: item.path)[: limits.max_files]
    return branch, files, truncated


def _numstat(
    worktree: Path,
    diff_args: Sequence[str],
    *,
    limits: ReviewLimits,
    runner: Callable[..., Any],
) -> dict[str, tuple[int | None, int | None, bool]]:
    payload, _ = _run_git(
        worktree,
        (
            "diff",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--numstat",
            "-z",
            *diff_args,
            "--",
        ),
        runner=runner,
    )
    result: dict[str, tuple[int | None, int | None, bool]] = {}
    records = payload.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        additions_raw, deletions_raw, path_raw = fields
        if path_raw:
            path = _decode_path(path_raw, limits)
        else:
            # With -z, a rename carries an empty path then old/new records.
            index += 1
            if index >= len(records):
                break
            path = _decode_path(records[index], limits)
            index += 1
        binary = additions_raw == b"-" or deletions_raw == b"-"
        additions = None if binary else int(additions_raw or b"0")
        deletions = None if binary else int(deletions_raw or b"0")
        result[path] = (additions, deletions, binary)
    return result


def _name_status(
    worktree: Path,
    diff_args: Sequence[str],
    *,
    limits: ReviewLimits,
    runner: Callable[..., Any],
) -> list[FileProjection]:
    payload, _ = _run_git(
        worktree,
        (
            "diff",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--name-status",
            "-z",
            *diff_args,
            "--",
        ),
        runner=runner,
    )
    records = payload.split(b"\0")
    files: list[FileProjection] = []
    index = 0
    while index < len(records):
        status_raw = records[index]
        index += 1
        if not status_raw or index >= len(records):
            continue
        status_code = status_raw.decode("ascii", errors="replace")
        old_path: str | None = None
        if status_code.startswith(("R", "C")):
            old_path = _decode_path(records[index], limits)
            index += 1
            if index >= len(records):
                break
        path = _decode_path(records[index], limits)
        index += 1
        status_name = _status_name(status_code)
        files.append(
            FileProjection(
                path=path,
                old_path=old_path,
                status=status_name,
                conflict=status_name == "conflict",
            )
        )
    return files


def _merge_stats(
    files: Iterable[FileProjection],
    stats: dict[str, tuple[int | None, int | None, bool]],
) -> list[FileProjection]:
    result = []
    for item in files:
        additions, deletions, binary = stats.get(
            item.path,
            (item.additions, item.deletions, item.binary),
        )
        result.append(
            FileProjection(
                path=item.path,
                old_path=item.old_path,
                status=item.status,
                additions=additions,
                deletions=deletions,
                staged=item.staged,
                unstaged=item.unstaged,
                binary=binary,
                conflict=item.conflict,
                generated=item.generated,
                submodule=item.submodule,
                oversized=item.oversized,
            )
        )
    return result


def _batches(values: Sequence[str], size: int = 100) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _generated_paths(
    worktree: Path,
    paths: Sequence[str],
    *,
    runner: Callable[..., Any],
) -> set[str]:
    generated: set[str] = set()
    for batch in _batches(paths):
        payload, _ = _run_git(
            worktree,
            ("check-attr", "-z", "linguist-generated", "--", *batch),
            runner=runner,
        )
        fields = payload.split(b"\0")
        for index in range(0, len(fields) - 2, 3):
            path_raw, _attribute, value_raw = fields[index : index + 3]
            value = value_raw.decode("utf-8", errors="replace").lower()
            if value not in {"", "unspecified", "unset", "false"}:
                generated.add(path_raw.decode("utf-8", errors="surrogateescape"))
    return generated


def _submodule_paths(
    worktree: Path,
    paths: Sequence[str],
    *,
    runner: Callable[..., Any],
) -> set[str]:
    submodules: set[str] = set()
    for batch in _batches(paths):
        payload, _ = _run_git(
            worktree,
            ("ls-files", "-s", "-z", "--", *batch),
            runner=runner,
        )
        for record in payload.split(b"\0"):
            metadata, separator, path_raw = record.partition(b"\t")
            if separator and metadata.startswith(b"160000 "):
                submodules.add(path_raw.decode("utf-8", errors="surrogateescape"))
    return submodules


def _decorate_files(
    worktree: Path,
    files: Sequence[FileProjection],
    *,
    limits: ReviewLimits,
    runner: Callable[..., Any],
) -> list[FileProjection]:
    paths = [item.path for item in files]
    generated = _generated_paths(worktree, paths, runner=runner) if paths else set()
    submodules = _submodule_paths(worktree, paths, runner=runner) if paths else set()
    decorated: list[FileProjection] = []
    for item in files:
        oversized = item.oversized
        if item.status == "untracked":
            try:
                info = (worktree / item.path).lstat()
                oversized = stat.S_ISREG(info.st_mode) and (
                    info.st_size > limits.max_patch_bytes
                )
            except OSError:
                pass
        decorated.append(
            FileProjection(
                **{
                    **asdict(item),
                    "generated": item.generated or item.path in generated,
                    "submodule": item.submodule or item.path in submodules,
                    "oversized": oversized,
                }
            )
        )
    return decorated


def _sha(
    worktree: Path,
    ref: str,
    *,
    runner: Callable[..., Any],
) -> str:
    if (
        not isinstance(ref, str)
        or not ref
        or len(ref.encode("utf-8", errors="surrogateescape")) > 1024
        or ref.startswith("-")
        or any(ord(char) < 32 or ord(char) == 127 for char in ref)
    ):
        raise GitReadError("REVIEW_REF_MISSING", "Git ref is invalid")
    payload, _ = _run_git(
        worktree,
        ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"),
        runner=runner,
        output_limit=128,
    )
    value = payload.decode("ascii", errors="replace").strip().lower()
    if len(value) not in {40, 64} or any(char not in _HEX for char in value):
        raise GitReadError("REVIEW_REF_MISSING", "Git ref did not resolve to a commit")
    return value


def _optional_sha(
    worktree: Path,
    ref: str,
    *,
    runner: Callable[..., Any],
) -> str | None:
    try:
        return _sha(worktree, ref, runner=runner)
    except GitReadError:
        return None


def resolve_commit(
    worktree: str | Path,
    ref: str,
    *,
    runner: Callable[..., Any] = _run_read_process,
) -> str:
    """Resolve one server-selected ref to an exact commit SHA."""
    resolved = _resolve_worktree(worktree)
    return _sha(resolved, ref, runner=runner)


def commit_is_ancestor(
    worktree: str | Path,
    ancestor_ref: str,
    descendant_ref: str,
    *,
    runner: Callable[..., Any] = _run_read_process,
) -> bool:
    """Return whether ``ancestor_ref`` reaches ``descendant_ref``."""
    resolved = _resolve_worktree(worktree)
    ancestor_sha = _sha(resolved, ancestor_ref, runner=runner)
    descendant_sha = _sha(resolved, descendant_ref, runner=runner)
    merge_raw, _ = _run_git(
        resolved,
        ("merge-base", ancestor_sha, descendant_sha),
        runner=runner,
        output_limit=128,
    )
    return merge_raw.decode("ascii", errors="replace").strip().lower() == ancestor_sha


def collect_workspace(
    worktree: str | Path,
    *,
    limits: ReviewLimits = DEFAULT_LIMITS,
    runner: Callable[..., Any] = _run_read_process,
) -> WorkspaceProjection:
    resolved = _resolve_worktree(worktree)
    payload, _ = _run_git(
        resolved,
        ("status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal"),
        runner=runner,
    )
    branch, files, files_truncated = _parse_status(payload, limits)
    if branch["head_sha"] is None:
        branch["head_sha"] = _sha(resolved, "HEAD", runner=runner)
    remote_branch_sha = (
        _optional_sha(
            resolved,
            f"refs/remotes/origin/{branch['branch']}",
            runner=runner,
        )
        if branch["branch"]
        else None
    )
    branch["remote_branch_sha"] = remote_branch_sha
    branch["pushed"] = remote_branch_sha is not None
    stats = _numstat(resolved, ("HEAD",), limits=limits, runner=runner)
    files = _merge_stats(files, stats)
    files = _decorate_files(resolved, files, limits=limits, runner=runner)
    core = {
        **branch,
        "files": [asdict(item) for item in files],
        "files_truncated": files_truncated,
    }
    digest = fingerprint(core)
    return WorkspaceProjection(
        branch=branch["branch"],
        head_sha=branch["head_sha"],
        upstream=branch["upstream"],
        remote_branch_sha=remote_branch_sha,
        pushed=remote_branch_sha is not None,
        ahead=branch["ahead"],
        behind=branch["behind"],
        files=tuple(files),
        files_truncated=files_truncated,
        fingerprint=digest,
        etag=etag(digest),
    )


def _file_set(
    worktree: Path,
    *,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str | None,
    diff_args: Sequence[str],
    include_untracked: bool,
    limits: ReviewLimits,
    runner: Callable[..., Any],
) -> FileSetProjection:
    files = _name_status(worktree, diff_args, limits=limits, runner=runner)
    stats = _numstat(worktree, diff_args, limits=limits, runner=runner)
    files = _merge_stats(files, stats)
    if include_untracked:
        workspace = collect_workspace(worktree, limits=limits, runner=runner)
        known = {item.path for item in files}
        files.extend(
            item
            for item in workspace.files
            if item.status == "untracked" and item.path not in known
        )
    files = _decorate_files(worktree, files, limits=limits, runner=runner)
    files = sorted(files, key=lambda item: item.path)
    files_truncated = len(files) > limits.max_files
    files = files[: limits.max_files]
    core = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "files": [asdict(item) for item in files],
        "files_truncated": files_truncated,
    }
    digest = fingerprint(core)
    return FileSetProjection(
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        files=tuple(files),
        files_truncated=files_truncated,
        fingerprint=digest,
        etag=etag(digest),
    )


def review_files(
    worktree: str | Path,
    base_ref: str,
    *,
    head_ref: str = "HEAD",
    include_worktree: bool = True,
    limits: ReviewLimits = DEFAULT_LIMITS,
    runner: Callable[..., Any] = _run_read_process,
) -> FileSetProjection:
    """Project merge-base review changes, optionally through the worktree."""
    resolved = _resolve_worktree(worktree)
    base_sha = _sha(resolved, base_ref, runner=runner)
    head_sha = _sha(resolved, head_ref, runner=runner)
    merge_raw, _ = _run_git(
        resolved,
        ("merge-base", base_sha, head_sha),
        runner=runner,
        output_limit=128,
    )
    merge_base = merge_raw.decode("ascii", errors="replace").strip().lower()
    diff_args = (merge_base,) if include_worktree else (merge_base, head_sha)
    return _file_set(
        resolved,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base,
        diff_args=diff_args,
        include_untracked=include_worktree,
        limits=limits,
        runner=runner,
    )


def local_only_files(
    worktree: str | Path,
    selected_head_ref: str,
    *,
    limits: ReviewLimits = DEFAULT_LIMITS,
    runner: Callable[..., Any] = _run_read_process,
) -> FileSetProjection:
    """Project committed/index/worktree changes absent from a selected PR head."""
    resolved = _resolve_worktree(worktree)
    base_sha = _sha(resolved, selected_head_ref, runner=runner)
    head_sha = _sha(resolved, "HEAD", runner=runner)
    return _file_set(
        resolved,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=None,
        diff_args=(base_sha,),
        include_untracked=True,
        limits=limits,
        runner=runner,
    )


def review_commits(
    worktree: str | Path,
    base_ref: str,
    *,
    head_ref: str = "HEAD",
    limits: ReviewLimits = DEFAULT_LIMITS,
    runner: Callable[..., Any] = _run_read_process,
) -> CommitSetProjection:
    resolved = _resolve_worktree(worktree)
    base_sha = _sha(resolved, base_ref, runner=runner)
    head_sha = _sha(resolved, head_ref, runner=runner)
    merge_raw, _ = _run_git(
        resolved,
        ("merge-base", base_sha, head_sha),
        runner=runner,
        output_limit=128,
    )
    merge_base = merge_raw.decode("ascii", errors="replace").strip().lower()
    payload, _ = _run_git(
        resolved,
        (
            "log",
            f"--max-count={limits.max_commits + 1}",
            "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e",
            f"{merge_base}..{head_sha}",
            "--",
        ),
        runner=runner,
    )
    records = []
    for raw in payload.decode("utf-8", errors="replace").split("\x1e"):
        raw = raw.strip("\n")
        if not raw:
            continue
        fields = raw.split("\x1f", 4)
        if len(fields) != 5:
            continue
        records.append(CommitProjection(*fields))
    truncated = len(records) > limits.max_commits
    records = records[: limits.max_commits]
    core = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commits": [asdict(item) for item in records],
        "commits_truncated": truncated,
    }
    digest = fingerprint(core)
    return CommitSetProjection(
        base_sha=base_sha,
        head_sha=head_sha,
        commits=tuple(records),
        commits_truncated=truncated,
        fingerprint=digest,
        etag=etag(digest),
    )


def validate_review_path(path: str, limits: ReviewLimits = DEFAULT_LIMITS) -> str:
    encoded = path.encode("utf-8", errors="surrogateescape")
    candidate = PurePosixPath(path)
    if (
        not path
        or len(encoded) > limits.max_path_bytes
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\x00" in path
        or path.endswith("/")
    ):
        raise ReviewError("REVIEW_PATH_INVALID", "Review path is invalid")
    return candidate.as_posix()


def _patch_projection(
    payload: bytes,
    *,
    truncated: bool,
    limits: ReviewLimits,
    detect_binary: bool = True,
) -> PatchProjection:
    binary = detect_binary and (
        b"\0" in payload
        or b"GIT binary patch" in payload
        or b"Binary files " in payload
    )
    if binary:
        digest = fingerprint({"binary": True, "bytes": len(payload)})
        return PatchProjection(None, None, False, True, "binary", etag(digest))
    line_too_long = any(
        len(line) > limits.max_line_bytes for line in payload.splitlines()
    )
    truncated = truncated or line_too_long
    digest = hashlib.sha256(payload).hexdigest()
    if line_too_long:
        return PatchProjection(
            text=None,
            sha256=digest,
            truncated=True,
            binary=False,
            unavailable_reason="line_too_long",
            etag=etag(digest),
        )
    text = payload.decode("utf-8", errors="replace")
    return PatchProjection(
        text=text,
        sha256=digest,
        truncated=truncated,
        binary=False,
        unavailable_reason=None,
        etag=etag(digest),
    )


def read_file_patch(
    worktree: str | Path,
    old_ref: str,
    path: str,
    *,
    new_ref: str | None = None,
    limits: ReviewLimits = DEFAULT_LIMITS,
    runner: Callable[..., Any] = _run_read_process,
) -> PatchProjection:
    resolved = _resolve_worktree(worktree)
    safe_path = validate_review_path(path, limits)
    old_sha = _sha(resolved, old_ref, runner=runner)
    diff_args = [old_sha]
    if new_ref is not None:
        diff_args.append(_sha(resolved, new_ref, runner=runner))
    payload, truncated = _run_git(
        resolved,
        (
            "diff",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=80",
            *diff_args,
            "--",
            safe_path,
        ),
        runner=runner,
        output_limit=limits.max_patch_bytes,
        allow_truncate=True,
    )
    if payload or new_ref is not None:
        return _patch_projection(payload, truncated=truncated, limits=limits)

    # Git omits untracked content.  A selected regular file may be represented
    # as a synthetic new-file patch, but symlinks and paths outside the selected
    # worktree are never followed.
    candidate = resolved / safe_path
    try:
        info = candidate.lstat()
    except OSError:
        digest = fingerprint({"path": safe_path, "unavailable": True})
        return PatchProjection(None, None, False, False, "unavailable", etag(digest))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReviewError("REVIEW_PATH_INVALID", "Review path is not a regular file")
    if info.st_size > limits.max_patch_bytes:
        digest = fingerprint({"path": safe_path, "oversized": info.st_size})
        return PatchProjection(None, None, True, False, "oversized", etag(digest))
    content = candidate.read_bytes()
    if b"\0" in content:
        digest = fingerprint({"path": safe_path, "binary": info.st_size})
        return PatchProjection(None, None, False, True, "binary", etag(digest))
    line_count = len(content.splitlines())
    hunk = f"@@ -0,0 +1,{line_count} @@\n" if line_count else "@@ -0,0 +0,0 @@\n"
    body = (
        f"diff --git a/{safe_path} b/{safe_path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{safe_path}\n"
        f"{hunk}"
    ).encode() + b"".join(b"+" + line for line in content.splitlines(keepends=True))
    return _patch_projection(body, truncated=False, limits=limits)


def remote_projection(
    pull_request: PullRequest,
    *,
    freshness: str = "fresh",
) -> RemoteProjection:
    if freshness not in {"fresh", "cached", "unavailable"}:
        raise ValueError("invalid remote freshness")
    core = {**pull_request.as_dict(), "freshness": freshness}
    digest = fingerprint(core)
    return RemoteProjection(
        pull_request=pull_request,
        lifecycle=lifecycle_status(pull_request),
        freshness=freshness,
        fingerprint=digest,
        etag=etag(digest),
    )


def discover_pull_requests(
    reader: GitHubPullRequestReader,
    *,
    branch_name: str,
    head_sha: str | None = None,
    pr_number: int | None = None,
) -> tuple[RemoteProjection, ...]:
    """Return compatible candidates without newest-branch guessing."""
    if pr_number is not None:
        return (remote_projection(reader.get(pr_number)),)
    pull_requests = [item for item in reader.list() if item.head_ref == branch_name]
    pull_requests.sort(
        key=lambda item: (
            item.head_sha == head_sha if head_sha else False,
            item.number,
        ),
        reverse=True,
    )
    return tuple(remote_projection(item) for item in pull_requests)


def collect_pull_requests(
    reader: GitHubPullRequestReader,
    *,
    branch_name: str,
    head_sha: str | None = None,
    pr_number: int | None = None,
    cached: Sequence[PullRequest] = (),
) -> RemoteCollection:
    """Read compatible PRs or return explicit cached/unavailable freshness."""
    try:
        pull_requests = discover_pull_requests(
            reader,
            branch_name=branch_name,
            head_sha=head_sha,
            pr_number=pr_number,
        )
        return RemoteCollection(pull_requests, "fresh", None)
    except GitHubReadError as exc:
        compatible = [
            item
            for item in cached
            if (
                item.number == pr_number
                if pr_number is not None
                else item.head_ref == branch_name
            )
        ]
        compatible.sort(
            key=lambda item: (
                item.head_sha == head_sha if head_sha else False,
                item.number,
            ),
            reverse=True,
        )
        if compatible:
            return RemoteCollection(
                tuple(
                    remote_projection(item, freshness="cached") for item in compatible
                ),
                "cached",
                str(exc)[:240],
            )
        return RemoteCollection((), "unavailable", str(exc)[:240])


def canonical_pr_patch(
    reader: GitHubPullRequestReader,
    pr_number: int,
    *,
    limits: ReviewLimits = DEFAULT_LIMITS,
) -> PatchProjection:
    payload = reader.patch(pr_number).encode("utf-8")
    truncated = len(payload) > limits.max_patch_bytes
    return _patch_projection(
        payload[: limits.max_patch_bytes],
        truncated=truncated,
        limits=limits,
        detect_binary=False,
    )


def parse_canonical_patch(
    patch: PatchProjection,
    *,
    limits: ReviewLimits = DEFAULT_LIMITS,
) -> CanonicalPatchSet:
    """Split one bounded canonical patch into bounded per-file projections."""
    if patch.text is None:
        digest = fingerprint(
            {
                "patch": patch.sha256,
                "binary": patch.binary,
                "truncated": patch.truncated,
            }
        )
        return CanonicalPatchSet(
            (),
            {},
            False,
            patch.truncated,
            digest,
            etag(digest),
        )

    sections: list[list[str]] = []
    current: list[str] = []
    for line in patch.text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                sections.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append(current)

    files: list[FileProjection] = []
    patches: dict[str, str] = {}
    for section in sections[: limits.max_files]:
        try:
            header = shlex.split(section[0].strip())
        except ValueError:
            continue
        if len(header) < 4:
            continue
        old_path = header[2].removeprefix("a/")
        new_path = header[3].removeprefix("b/")
        status_name = "modified"
        renamed_from: str | None = None
        binary = False
        additions = 0
        deletions = 0
        for line in section[1:]:
            if line.startswith("new file mode "):
                status_name = "added"
            elif line.startswith("deleted file mode "):
                status_name = "deleted"
            elif line.startswith("rename from "):
                status_name = "renamed"
                renamed_from = line.removeprefix("rename from ").rstrip("\n")
            elif line.startswith("rename to "):
                new_path = line.removeprefix("rename to ").rstrip("\n")
            elif line.startswith(("Binary files ", "GIT binary patch")):
                binary = True
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        path = new_path if status_name != "deleted" else old_path
        if not path:
            continue
        files.append(
            FileProjection(
                path=path,
                old_path=renamed_from,
                status=status_name,
                additions=None if binary else additions,
                deletions=None if binary else deletions,
                binary=binary,
            )
        )
        patches[path] = "".join(section)
    files_truncated = len(sections) > limits.max_files
    core = {
        "patch_sha256": patch.sha256,
        "files": [asdict(item) for item in files],
        "files_truncated": files_truncated,
        "patch_truncated": patch.truncated,
    }
    digest = fingerprint(core)
    return CanonicalPatchSet(
        tuple(files),
        patches,
        files_truncated,
        patch.truncated,
        digest,
        etag(digest),
    )


class MergedPatchCache:
    """Hash-validated, owner-only cache under the ignored artifact root."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        limits: ReviewLimits = DEFAULT_LIMITS,
    ) -> None:
        self.root = (
            Path(root) if root is not None else artifact_policy.review_patch_root()
        )
        self.limits = limits

    def store(
        self,
        repository: str,
        pr_number: int,
        patch: str,
    ) -> CacheArtifact:
        payload = patch.encode("utf-8")
        if len(payload) > self.limits.max_artifact_bytes:
            raise ReviewError(
                "REVIEW_DIFF_TOO_LARGE",
                "Canonical patch exceeds the cache limit",
            )
        if pr_number < 1:
            raise ValueError("PR number must be positive")
        repo_key = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:20]
        digest = hashlib.sha256(payload).hexdigest()
        relative = PurePosixPath(repo_key) / f"pr-{pr_number}-{digest[:16]}.patch"
        if self.root.exists() and self.root.is_symlink():
            raise ReviewError(
                "REVIEW_PATH_INVALID",
                "Patch cache root is invalid",
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        root = self.root.resolve(strict=True)
        destination = root / Path(relative)
        if destination.parent.exists() and destination.parent.is_symlink():
            raise ReviewError(
                "REVIEW_PATH_INVALID",
                "Patch cache directory is invalid",
            )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=".patch-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_raw)
        os.chmod(temporary, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return CacheArtifact(relative.as_posix(), digest)

    def load(self, relative_path: str, expected_sha256: str) -> str | None:
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(expected_sha256) != 64
            or any(char not in _HEX for char in expected_sha256)
        ):
            raise ReviewError("REVIEW_PATH_INVALID", "Patch cache identity is invalid")
        try:
            if self.root.is_symlink():
                raise ReviewError(
                    "REVIEW_PATH_INVALID",
                    "Patch cache root is invalid",
                )
            root = self.root.resolve(strict=True)
        except FileNotFoundError:
            return None
        candidate = root / Path(relative)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReviewError("REVIEW_PATH_INVALID", "Patch cache entry is invalid")
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ReviewError(
                "REVIEW_PATH_INVALID",
                "Patch cache entry escapes the artifact root",
            ) from exc
        if info.st_size > self.limits.max_artifact_bytes:
            raise ReviewError("REVIEW_DIFF_TOO_LARGE", "Patch cache entry is oversized")
        payload = candidate.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            return None
        return payload.decode("utf-8", errors="replace")


def read_canonical_pr_patch(
    reader: GitHubPullRequestReader,
    pull_request: PullRequest,
    *,
    repository: str,
    cache: MergedPatchCache | None = None,
    cached_artifact: CacheArtifact | None = None,
    limits: ReviewLimits = DEFAULT_LIMITS,
) -> CanonicalPatchRead:
    """Read an exact canonical patch with durable merged-cache fallback."""
    if (
        pull_request.state == "MERGED"
        and cache is not None
        and cached_artifact is not None
    ):
        cached = cache.load(
            cached_artifact.relative_path,
            cached_artifact.sha256,
        )
        if cached is not None:
            payload = cached.encode("utf-8")
            return CanonicalPatchRead(
                _patch_projection(
                    payload,
                    truncated=False,
                    limits=limits,
                    detect_binary=False,
                ),
                "cached",
                cached_artifact,
            )
    try:
        projection = canonical_pr_patch(
            reader,
            pull_request.number,
            limits=limits,
        )
    except GitHubReadError:
        if cache is not None and cached_artifact is not None:
            cached = cache.load(
                cached_artifact.relative_path,
                cached_artifact.sha256,
            )
            if cached is not None:
                return CanonicalPatchRead(
                    _patch_projection(
                        cached.encode("utf-8"),
                        truncated=False,
                        limits=limits,
                        detect_binary=False,
                    ),
                    "cached",
                    cached_artifact,
                )
        raise

    artifact = cached_artifact
    if (
        pull_request.state == "MERGED"
        and cache is not None
        and projection.text is not None
        and not projection.truncated
        and not projection.binary
    ):
        artifact = cache.store(
            repository,
            pull_request.number,
            projection.text,
        )
    return CanonicalPatchRead(projection, "fresh", artifact)
