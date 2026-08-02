"""Failure-tolerant local Git observations for conversation review targets.

Git and filesystem reads finish before the short persistence transaction
begins.  This module records only durable local identity; the full read-only
review projection and GitHub enrichment belong to the Feature #26 review
service.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db_driver

_LOG = logging.getLogger("super_coder.conversation_git_targets")
GIT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class LocalGitObservation:
    branch_name: str
    base_ref: str | None
    head_sha: str
    observed_at: str


def _stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(
    worktree: Path,
    args: Sequence[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str | None:
    result = runner(
        ["git", "-C", str(worktree), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=_git_environment(),
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _base_ref(
    worktree: Path,
    *,
    runner: Callable[..., Any],
) -> str | None:
    remote_head = _run_git(
        worktree,
        ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
        runner=runner,
    )
    if remote_head:
        return remote_head
    for candidate in ("origin/main", "main", "origin/master", "master"):
        if _run_git(
            worktree,
            ("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"),
            runner=runner,
        ):
            return candidate
    return None


def observe_local_git(
    worktree: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    now: datetime | None = None,
) -> LocalGitObservation:
    resolved = Path(worktree).resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    branch = _run_git(
        resolved,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        runner=runner,
    )
    head = _run_git(
        resolved,
        ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
        runner=runner,
    )
    if branch is None or head is None:
        raise RuntimeError("conversation worktree has no observable branch head")
    return LocalGitObservation(
        branch_name=branch,
        base_ref=_base_ref(resolved, runner=runner),
        head_sha=head.lower(),
        observed_at=_stamp(now),
    )


def persist_local_observation(
    db_path: str | Path,
    conversation_id: str,
    expected_worktree: str,
    observation: LocalGitObservation,
    *,
    connect: Callable[[str | Path], Any] = db_driver.connect,
) -> str | None:
    """Persist one already-read observation in a database-only transaction."""
    con = connect(db_path)
    try:
        with db_driver.write_transaction(
            con,
            "conversation.git_target.observe",
        ):
            current = con.execute(
                "SELECT worktree FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if (
                current is None
                or current["worktree"] != expected_worktree
            ):
                return None
            target = con.execute(
                "SELECT target_id FROM conversation_git_targets "
                "WHERE conversation_id=? AND branch_name=? "
                "AND pr_number IS NULL "
                "ORDER BY last_seen_at DESC,target_id DESC LIMIT 1",
                (conversation_id, observation.branch_name),
            ).fetchone()
            if target is None:
                con.execute(
                    "INSERT INTO conversation_git_targets "
                    "(conversation_id,branch_name,base_ref,first_head_sha,"
                    "latest_head_sha,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        conversation_id,
                        observation.branch_name,
                        observation.base_ref,
                        observation.head_sha,
                        observation.head_sha,
                        observation.observed_at,
                        observation.observed_at,
                    ),
                )
                target = con.execute(
                    "SELECT target_id FROM conversation_git_targets "
                    "WHERE conversation_id=? AND branch_name=? "
                    "AND first_head_sha=? AND pr_number IS NULL",
                    (
                        conversation_id,
                        observation.branch_name,
                        observation.head_sha,
                    ),
                ).fetchone()
            else:
                con.execute(
                    "UPDATE conversation_git_targets "
                    "SET base_ref=?,latest_head_sha=?,last_seen_at=? "
                    "WHERE target_id=?",
                    (
                        observation.base_ref,
                        observation.head_sha,
                        observation.observed_at,
                        target["target_id"],
                    ),
                )
            return str(target["target_id"])
    finally:
        con.close()


def observe_and_persist(
    db_path: str | Path,
    conversation_id: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    connect: Callable[[str | Path], Any] = db_driver.connect,
    now: datetime | None = None,
) -> bool:
    """Observe and persist one conversation.

    Lifecycle callers use ``safely_observe_and_persist`` so observation cannot
    affect their already-committed result.
    """
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT worktree FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return False
        worktree = str(row["worktree"])
    finally:
        con.close()

    observation = observe_local_git(worktree, runner=runner, now=now)
    return (
        persist_local_observation(
            db_path,
            conversation_id,
            worktree,
            observation,
            connect=connect,
        )
        is not None
    )


def safely_observe_and_persist(
    db_path: str | Path,
    conversation_id: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    connect: Callable[[str | Path], Any] = db_driver.connect,
    now: datetime | None = None,
) -> bool:
    """Best-effort lifecycle hook; never changes its caller's outcome."""
    try:
        return observe_and_persist(
            db_path,
            conversation_id,
            runner=runner,
            connect=connect,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - observation is non-authoritative
        _LOG.warning(
            "conversation Git observation skipped conversation=%s: %s",
            conversation_id,
            exc,
        )
        return False
