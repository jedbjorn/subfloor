"""Target-aware, bounded Git freshness projection for shell boot."""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

TARGET_ISOLATED_SHELL = "isolated_shell_base"
TARGET_ACTIVE_BRANCH = "active_branch"
TARGET_REVIEWER_HEAD = "reviewer_exact_head"
TARGET_SHARED_WORK = "shared_work_repo"
TARGET_LIVE_ENGINE = "live_engine_checkout"
TARGET_DETACHED = "detached_head"
TARGET_UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessProjection:
    path: str
    target: str
    branch: str | None
    head: str | None
    upstream: str
    dirty: bool | None
    ahead: int | None
    behind: int | None
    remote: str
    action: str
    detail: str


@dataclass(frozen=True)
class _LocalState:
    branch: str | None
    head: str | None
    dirty: bool


def _git(
    repo: Path,
    *args: str,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _default_branch(environ: Mapping[str, str]) -> str:
    return (environ.get("SC_PROTECTED_BRANCHES") or "main").split()[0]


def _canonical_root(repo: Path) -> Path:
    result = _git(repo, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("not a Git checkout")
    return Path(result.stdout.strip()).resolve()


def _local_state(repo: Path) -> _LocalState:
    branch_result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    head_result = _git(repo, "rev-parse", "--verify", "HEAD")
    status_result = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if head_result.returncode != 0 or status_result.returncode != 0:
        raise ValueError("local Git state unavailable")
    return _LocalState(
        branch=branch_result.stdout.strip() if branch_result.returncode == 0 else None,
        head=head_result.stdout.strip(),
        dirty=bool(status_result.stdout.strip()),
    )


def _common_git_dir(repo: Path) -> Path:
    result = _git(repo, "rev-parse", "--git-common-dir")
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("Git common directory unavailable")
    path = Path(result.stdout.strip())
    return path.resolve() if path.is_absolute() else (repo / path).resolve()


@contextlib.contextmanager
def _refresh_lock(repo: Path) -> Iterator[None]:
    lock_path = _common_git_dir(repo) / "sc-freshness.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TimeoutError("shared-repo freshness lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _refresh_remote(repo: Path, remote: str, branch: str, timeout: int) -> None:
    upstream = f"{remote}/{branch}"
    remote_result = _git(repo, "remote", "get-url", remote)
    if remote_result.returncode != 0:
        raise ValueError(f"missing remote {remote}")
    with _refresh_lock(repo):
        result = _git(
            repo,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            timeout=timeout,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ValueError(f"fetch {upstream} failed{suffix}")
    verify = _git(repo, "rev-parse", "--verify", "--quiet", upstream)
    if verify.returncode != 0:
        raise ValueError(f"missing refreshed ref {upstream}")


def _counts(repo: Path, upstream: str) -> tuple[int, int]:
    result = _git(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if result.returncode != 0:
        raise ValueError(f"cannot compare HEAD with {upstream}")
    values = result.stdout.split()
    if len(values) != 2:
        raise ValueError(f"invalid ahead/behind count for {upstream}")
    return int(values[0]), int(values[1])


def _actual_target(policy: str, local: _LocalState, expected_branch: str | None) -> str:
    if policy == TARGET_ISOLATED_SHELL:
        if local.branch is None:
            return TARGET_DETACHED
        if local.branch != expected_branch:
            return TARGET_ACTIVE_BRANCH
    return policy


def project(
    repo: Path,
    *,
    policy: str,
    expected_branch: str | None = None,
    allow_auto_advance: bool = False,
    remote: str = "origin",
    default_branch: str | None = None,
    fetch_timeout: int = 20,
    environ: Mapping[str, str] | None = None,
) -> FreshnessProjection:
    """Observe one checkout and optionally advance one disposable shell base."""
    env = environ or os.environ
    branch_name = default_branch or _default_branch(env)
    upstream = f"{remote}/{branch_name}"
    try:
        root = _canonical_root(repo)
        local = _local_state(root)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return FreshnessProjection(
            path=str(repo.expanduser().resolve()),
            target=TARGET_UNKNOWN,
            branch=None,
            head=None,
            upstream=upstream,
            dirty=None,
            ahead=None,
            behind=None,
            remote="unverified",
            action="preserved",
            detail=str(exc),
        )

    actual_target = _actual_target(policy, local, expected_branch)
    base = FreshnessProjection(
        path=str(root),
        target=actual_target,
        branch=local.branch,
        head=local.head,
        upstream=upstream,
        dirty=local.dirty,
        ahead=None,
        behind=None,
        remote="unverified",
        action="preserved",
        detail="freshness unverified",
    )
    try:
        _refresh_remote(root, remote, branch_name, fetch_timeout)
        ahead, behind = _counts(root, upstream)
    except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError) as exc:
        return replace(base, detail=f"freshness unverified: {exc}")

    verified = replace(
        base,
        ahead=ahead,
        behind=behind,
        remote="verified",
        detail="remote freshness verified; state preserved",
    )
    if not allow_auto_advance or actual_target != TARGET_ISOLATED_SHELL:
        return verified
    if local.branch != expected_branch:
        return verified
    if local.dirty:
        return replace(verified, detail="remote freshness verified; dirty state preserved")
    if ahead:
        return replace(
            verified,
            detail="remote freshness verified; local-only commits preserved",
        )
    if behind == 0:
        return replace(verified, action="current", detail="remote freshness verified; current")

    # Re-read every destructive precondition immediately before reset. A fetch
    # or local actor may have changed the worktree since the first observation.
    try:
        current = _local_state(root)
        current_ahead, current_behind = _counts(root, upstream)
        if (
            current.branch != expected_branch
            or current.dirty
            or current_ahead
            or current.head != local.head
        ):
            return replace(
                verified,
                detail="remote freshness verified; state changed during check and was preserved",
            )
        reset = _git(root, "reset", "--hard", upstream)
        if reset.returncode != 0:
            detail = (reset.stderr or reset.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            return replace(
                verified,
                detail=f"remote freshness verified; auto-advance failed{suffix}",
            )
        advanced = _local_state(root)
        return replace(
            verified,
            head=advanced.head,
            ahead=0,
            behind=0,
            action="auto_advanced",
            detail=f"auto-advanced clean isolated base by {current_behind} commit(s)",
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return replace(verified, detail=f"remote freshness verified; auto-advance failed: {exc}")


def render(projection: FreshnessProjection) -> str:
    """Render one exact, target-named status line for boot and CLI output."""
    identity = (
        f"branch `{projection.branch}`"
        if projection.branch
        else f"detached `{(projection.head or 'unknown')[:12]}`"
    )
    cleanliness = (
        "dirty" if projection.dirty else "clean" if projection.dirty is not None else "state unknown"
    )
    relation = (
        f"ahead {projection.ahead}, behind {projection.behind}"
        if projection.ahead is not None and projection.behind is not None
        else "ahead/behind unknown"
    )
    guidance = ""
    if projection.target == TARGET_SHARED_WORK:
        guidance = " · first action: follow the git skill clean-base gate; never auto-sync this checkout"
    elif projection.target == TARGET_ACTIVE_BRANCH:
        guidance = " · first action: follow the git skill without rewriting this active branch"
    elif projection.target == TARGET_ISOLATED_SHELL and projection.action == "preserved":
        guidance = " · first action: surface local state, then follow the git skill clean-base gate"
    elif projection.target == TARGET_REVIEWER_HEAD:
        guidance = " · reviewer head pinned; base freshness is reported separately"
    elif projection.target == TARGET_LIVE_ENGINE:
        guidance = " · FnB owns pull, reconcile, and restart"
    elif projection.target == TARGET_DETACHED:
        guidance = " · identify the detached head before any Git action"
    return (
        f"{projection.target}: `{projection.path}` · {identity} · {cleanliness} · "
        f"upstream `{projection.upstream}` · {relation} · remote {projection.remote} · "
        f"{projection.action}: {projection.detail}{guidance}"
    )
