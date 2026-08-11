#!/usr/bin/env python3
"""Durable successful-Sprint cleanup target scheduling and projection."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import Any

import active_chat_registry
import git_freshness
import git_prune
import run
import shell_liveness


class SprintCleanupInvariantError(ValueError):
    """The completed Sprint cannot be mapped to exact managed targets."""


class SprintCleanupSafetyError(RuntimeError):
    """An exact destructive-boundary check failed closed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SprintCleanupMutationError(RuntimeError):
    """One bounded external cleanup operation failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SprintCleanupWaiting(RuntimeError):
    """The exact target is safe but not currently dormant or claimable."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CleanupTargetDraft:
    shell_id: int | None
    target_kind: str
    canonical_path: str
    repository_root: str
    git_common_dir: str
    expected_base_branch: str | None


@dataclass(frozen=True)
class CleanupProjection:
    aggregate_state: str | None
    target_count: int
    worktree_count: int
    artifact_count: int
    pending_count: int
    running_count: int
    succeeded_count: int
    failed_count: int


@dataclass(frozen=True)
class CleanupScheduleReceipt:
    created: bool
    target_ids: tuple[int, ...]
    projection: CleanupProjection


@dataclass(frozen=True)
class CleanupClaim:
    cleanup_target_id: int
    sprint_id: int
    shell_id: int | None
    target_kind: str
    canonical_path: str
    repository_root: str
    git_common_dir: str
    expected_base_branch: str | None
    lease_owner: str
    claim_generation: int
    lease_expires_at: str


@dataclass(frozen=True)
class CleanupExecutionReceipt:
    cleanup_target_id: int | None
    sprint_id: int | None
    state: str
    code: str | None = None
    detail: str | None = None
    claim_generation: int | None = None
    attempt_count: int | None = None


IdentityProvider = Callable[[], tuple[Path, Path]]
Clock = Callable[[], datetime]
LivenessProbe = Callable[[CleanupClaim], str]
PruneBranches = Callable[[Path], dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def resolve_repository_identity() -> tuple[Path, Path]:
    """Resolve the canonical main checkout and its exact Git common directory."""
    configured_root = os.environ.get("SC_ROOT")
    probe = Path(configured_root) if configured_root else run.REPO_ROOT
    common_dir = git_freshness._common_git_dir(probe)
    repository_root = common_dir.parent.resolve()
    if git_freshness._canonical_root(repository_root) != repository_root:
        raise SprintCleanupInvariantError(
            "Git common directory does not identify the canonical main checkout"
        )
    return repository_root, common_dir.resolve()


class SprintCleanupTargetStore:
    """Prepare exact target identities and persist them inside lifecycle writes."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        identity_provider: IdentityProvider | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.identity_provider = identity_provider or resolve_repository_identity
        self.clock = clock or _utc_now

    def prepare_targets(self, sprint_id: int) -> tuple[CleanupTargetDraft, ...]:
        """Resolve filesystem identity before the lifecycle transaction begins."""
        repository_root, git_common_dir = self.identity_provider()
        repository_root = repository_root.resolve()
        git_common_dir = git_common_dir.resolve()
        participants = self.con.execute(
            "SELECT participant.shell_id,shell.shortname,shell.flavor "
            "FROM sprint_participants participant JOIN shells shell "
            "ON shell.shell_id=participant.shell_id "
            "WHERE participant.sprint_id=? "
            "AND COALESCE(shell.flavor,'')<>'admin' "
            "ORDER BY participant.shell_id",
            (sprint_id,),
        ).fetchall()
        if not participants:
            raise SprintCleanupInvariantError(
                "successful Sprint has no managed non-Admin participants"
            )

        drafts_by_path: dict[str, CleanupTargetDraft] = {}
        for participant in participants:
            shortname = str(participant["shortname"] or "").strip()
            if not shortname:
                raise SprintCleanupInvariantError(
                    f"Sprint participant shell {participant['shell_id']} has no shortname"
                )
            worktree = run.shell_work_dir(
                shortname,
                str(participant["flavor"] or ""),
                root=repository_root,
            )
            canonical_path = self._lexical_absolute(worktree)
            draft = CleanupTargetDraft(
                shell_id=int(participant["shell_id"]),
                target_kind="worktree",
                canonical_path=canonical_path,
                repository_root=str(repository_root),
                git_common_dir=str(git_common_dir),
                expected_base_branch=f"shell/{shortname.lower()}",
            )
            existing = drafts_by_path.get(canonical_path)
            if existing is not None and existing.shell_id != draft.shell_id:
                raise SprintCleanupInvariantError(
                    "multiple Sprint participants resolve to one managed worktree"
                )
            drafts_by_path[canonical_path] = draft

        artifact_path = self._lexical_absolute(
            repository_root / "shared" / "sprints" / f"sprint-{sprint_id}"
        )
        drafts_by_path[artifact_path] = CleanupTargetDraft(
            shell_id=None,
            target_kind="artifact_dir",
            canonical_path=artifact_path,
            repository_root=str(repository_root),
            git_common_dir=str(git_common_dir),
            expected_base_branch=None,
        )
        return tuple(
            sorted(
                drafts_by_path.values(),
                key=lambda draft: (draft.target_kind, draft.canonical_path),
            )
        )

    def schedule_in_transaction(
        self,
        sprint_id: int,
        targets: Iterable[CleanupTargetDraft],
    ) -> CleanupScheduleReceipt:
        """Insert one exact target set and its append-only scheduling evidence."""
        if not self.con.in_transaction:
            raise RuntimeError(
                "Sprint cleanup scheduling requires an active transaction"
            )
        targets = tuple(targets)
        self._validate_target_set(sprint_id, targets)
        existing = self._target_rows(sprint_id)
        if existing:
            self._require_exact_replay(existing, targets)
            projection = self.project(sprint_id)
            return CleanupScheduleReceipt(
                False,
                tuple(int(row["cleanup_target_id"]) for row in existing),
                projection,
            )

        target_ids: list[int] = []
        worktree_target_ids: list[int] = []
        artifact_target_ids: list[int] = []
        for target in targets:
            cursor = self.con.execute(
                "INSERT INTO sprint_cleanup_targets "
                "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
                "git_common_dir,expected_base_branch) VALUES (?,?,?,?,?,?,?)",
                (
                    sprint_id,
                    target.shell_id,
                    target.target_kind,
                    target.canonical_path,
                    target.repository_root,
                    target.git_common_dir,
                    target.expected_base_branch,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("cleanup target insert returned no durable identity")
            target_id = cursor.lastrowid
            target_ids.append(target_id)
            if target.target_kind == "worktree":
                worktree_target_ids.append(target_id)
            else:
                artifact_target_ids.append(target_id)

        projection = self.project(sprint_id)
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,payload) VALUES "
            "(?,'sprint.cleanup_scheduled','system',?)",
            (
                sprint_id,
                json.dumps(
                    {
                        "aggregate_state": projection.aggregate_state,
                        "artifact_target_ids": artifact_target_ids,
                        "target_count": projection.target_count,
                        "worktree_target_ids": worktree_target_ids,
                    },
                    sort_keys=True,
                ),
            ),
        )
        return CleanupScheduleReceipt(True, tuple(target_ids), projection)

    def project(self, sprint_id: int) -> CleanupProjection:
        counts = {state: 0 for state in ("pending", "running", "succeeded", "failed")}
        worktree_count = 0
        artifact_count = 0
        for row in self.con.execute(
            "SELECT target_kind,state,COUNT(*) AS target_count "
            "FROM sprint_cleanup_targets WHERE sprint_id=? "
            "GROUP BY target_kind,state",
            (sprint_id,),
        ):
            count = int(row["target_count"])
            counts[str(row["state"])] += count
            if row["target_kind"] == "worktree":
                worktree_count += count
            else:
                artifact_count += count
        target_count = worktree_count + artifact_count
        aggregate_state: str | None = None
        if counts["failed"]:
            aggregate_state = "failed"
        elif counts["pending"] or counts["running"]:
            aggregate_state = "pending"
        elif target_count and counts["succeeded"] == target_count:
            aggregate_state = "succeeded"
        return CleanupProjection(
            aggregate_state=aggregate_state,
            target_count=target_count,
            worktree_count=worktree_count,
            artifact_count=artifact_count,
            pending_count=counts["pending"],
            running_count=counts["running"],
            succeeded_count=counts["succeeded"],
            failed_count=counts["failed"],
        )

    def claim_next(
        self,
        owner: str,
        *,
        shell_id: int | None = None,
        lease_seconds: int = 120,
    ) -> CleanupClaim | None:
        """Claim one runnable target with a monotonically fenced generation."""
        if not owner.strip():
            raise ValueError("cleanup lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("cleanup lease duration must be positive")
        now = self.clock()
        now_stamp = _stamp(now)
        expires_at = _stamp(now + timedelta(seconds=lease_seconds))
        self._begin_write()
        try:
            params: list[object] = [now_stamp, now_stamp]
            shell_filter = ""
            if shell_id is not None:
                shell_filter = " AND target.shell_id=?"
                params.append(shell_id)
            row = self.con.execute(
                "SELECT target.* FROM sprint_cleanup_targets target "
                "JOIN sprints sprint ON sprint.sprint_id=target.sprint_id "
                "WHERE sprint.lifecycle='completed' AND ("
                "(target.state='pending' AND "
                " (target.lease_expires_at IS NULL OR target.lease_expires_at<=?)) "
                "OR (target.state='running' AND target.lease_expires_at<=?))"
                + shell_filter
                + " AND (target.target_kind='worktree' OR NOT EXISTS ("
                "SELECT 1 FROM sprint_cleanup_targets worktree "
                "WHERE worktree.sprint_id=target.sprint_id "
                "AND worktree.target_kind='worktree' "
                "AND worktree.state<>'succeeded')) "
                "ORDER BY CASE target.target_kind WHEN 'worktree' THEN 0 ELSE 1 END,"
                "target.sprint_id,target.cleanup_target_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                self.con.commit()
                return None
            generation = int(row["claim_generation"]) + 1
            changed = self.con.execute(
                "UPDATE sprint_cleanup_targets SET state='running',"
                "claim_generation=?,lease_owner=?,lease_expires_at=?,"
                "claimed_at=?,updated_at=?,waiting_reason=NULL "
                "WHERE cleanup_target_id=? AND claim_generation=? AND ("
                "(state='pending' AND "
                " (lease_expires_at IS NULL OR lease_expires_at<=?)) OR "
                "(state='running' AND lease_expires_at<=?))",
                (
                    generation,
                    owner,
                    expires_at,
                    now_stamp,
                    now_stamp,
                    row["cleanup_target_id"],
                    row["claim_generation"],
                    now_stamp,
                    now_stamp,
                ),
            ).rowcount
            if changed != 1:
                self.con.rollback()
                return None
            self.con.commit()
            return CleanupClaim(
                cleanup_target_id=int(row["cleanup_target_id"]),
                sprint_id=int(row["sprint_id"]),
                shell_id=(
                    int(row["shell_id"]) if row["shell_id"] is not None else None
                ),
                target_kind=str(row["target_kind"]),
                canonical_path=str(row["canonical_path"]),
                repository_root=str(row["repository_root"]),
                git_common_dir=str(row["git_common_dir"]),
                expected_base_branch=row["expected_base_branch"],
                lease_owner=owner,
                claim_generation=generation,
                lease_expires_at=expires_at,
            )
        except Exception:
            self.con.rollback()
            raise

    def renew(self, claim: CleanupClaim, *, lease_seconds: int = 120) -> bool:
        now = self.clock()
        return self._fenced_update(
            claim,
            "lease_expires_at=?,updated_at=?",
            (_stamp(now + timedelta(seconds=lease_seconds)), _stamp(now)),
        )

    def begin_attempt(self, claim: CleanupClaim) -> int | None:
        now_stamp = _stamp(self.clock())
        if not self._fenced_update(
            claim,
            "attempt_count=attempt_count+1,updated_at=?",
            (now_stamp,),
        ):
            return None
        row = self.con.execute(
            "SELECT attempt_count FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        return int(row["attempt_count"])

    def record_before(self, claim: CleanupClaim, evidence: dict[str, Any]) -> bool:
        return self._fenced_update(
            claim,
            "before_evidence=?,updated_at=?",
            (self._evidence(evidence), _stamp(self.clock())),
        )

    def release_waiting(
        self,
        claim: CleanupClaim,
        reason: str,
        *,
        retry_after_seconds: int = 0,
    ) -> bool:
        now = self.clock()
        retry_at = (
            _stamp(now + timedelta(seconds=retry_after_seconds))
            if retry_after_seconds
            else None
        )
        return self._fenced_update(
            claim,
            "state='pending',lease_owner=NULL,lease_expires_at=?,"
            "waiting_reason=?,updated_at=?",
            (retry_at, self._bounded(reason, 120), _stamp(now)),
        )

    def mark_succeeded(
        self,
        claim: CleanupClaim,
        evidence: dict[str, Any],
    ) -> bool:
        row = self._current_claim_row(claim)
        if row is None:
            return False
        if row["last_error_code"] is not None:
            evidence = dict(evidence)
            evidence["retry_evidence"] = {
                "failed_attempts": max(0, int(row["attempt_count"]) - 1),
                "last_error_code": str(row["last_error_code"]),
                "last_error_detail": str(row["last_error_detail"] or "")[:1000],
            }
        now_stamp = _stamp(self.clock())
        return self._fenced_update(
            claim,
            "state='succeeded',lease_owner=NULL,lease_expires_at=NULL,"
            "waiting_reason=NULL,after_evidence=?,last_error_code=NULL,"
            "last_error_detail=NULL,completed_at=?,updated_at=?",
            (self._evidence(evidence), now_stamp, now_stamp),
        )

    def fail_safety(
        self,
        claim: CleanupClaim,
        code: str,
        detail: str,
    ) -> bool:
        now_stamp = _stamp(self.clock())
        return self._fenced_update(
            claim,
            "state='failed',lease_owner=NULL,lease_expires_at=NULL,"
            "waiting_reason=NULL,last_error_code=?,last_error_detail=?,"
            "updated_at=?",
            (
                self._bounded(code, 120),
                self._bounded(detail, 2000),
                now_stamp,
            ),
        )

    def fail_mutation(
        self,
        claim: CleanupClaim,
        code: str,
        detail: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: int = 5,
    ) -> tuple[bool, str, int | None]:
        """Record a failed destructive attempt and schedule bounded recovery."""
        row = self._current_claim_row(claim)
        if row is None:
            return False, "stale", None
        attempts = int(row["attempt_count"])
        terminal = attempts >= max_attempts
        now = self.clock()
        state = "failed" if terminal else "pending"
        retry_at = (
            None
            if terminal
            else _stamp(now + timedelta(seconds=max(0, backoff_seconds)))
        )
        changed = self._fenced_update(
            claim,
            "state=?,lease_owner=NULL,lease_expires_at=?,waiting_reason=?,"
            "last_error_code=?,last_error_detail=?,updated_at=?",
            (
                state,
                retry_at,
                None if terminal else "retry_backoff",
                self._bounded(code, 120),
                self._bounded(detail, 2000),
                _stamp(now),
            ),
        )
        return changed, state if changed else "stale", attempts

    def claim_is_current(self, claim: CleanupClaim) -> bool:
        return self._current_claim_row(claim) is not None

    def _current_claim_row(self, claim: CleanupClaim) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM sprint_cleanup_targets WHERE cleanup_target_id=? "
            "AND state='running' AND lease_owner=? AND claim_generation=? "
            "AND lease_expires_at>?",
            (
                claim.cleanup_target_id,
                claim.lease_owner,
                claim.claim_generation,
                _stamp(self.clock()),
            ),
        ).fetchone()

    def _fenced_update(
        self,
        claim: CleanupClaim,
        assignments: str,
        values: tuple[object, ...],
    ) -> bool:
        if self.con.in_transaction:
            raise RuntimeError("cleanup fenced writes own their transaction")
        now_stamp = _stamp(self.clock())
        self._begin_write()
        try:
            changed = self.con.execute(
                "UPDATE sprint_cleanup_targets SET " + assignments + " "
                "WHERE cleanup_target_id=? AND state='running' "
                "AND lease_owner=? AND claim_generation=? "
                "AND lease_expires_at>?",
                values
                + (
                    claim.cleanup_target_id,
                    claim.lease_owner,
                    claim.claim_generation,
                    now_stamp,
                ),
            ).rowcount
            self.con.commit()
            return changed == 1
        except Exception:
            self.con.rollback()
            raise

    def _begin_write(self) -> None:
        if self.con.in_transaction:
            raise RuntimeError("cleanup execution requires an idle DB connection")
        self.con.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _evidence(value: dict[str, Any]) -> str:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(rendered) > 12000:
            raise ValueError("cleanup evidence exceeds the bounded record size")
        return rendered

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        return value.strip()[:limit]

    def _validate_target_set(
        self,
        sprint_id: int,
        targets: tuple[CleanupTargetDraft, ...],
    ) -> None:
        sprint = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if sprint["lifecycle"] != "completed":
            raise SprintCleanupInvariantError(
                "cleanup targets may be scheduled only for a completed Sprint"
            )
        artifact_targets = [
            target for target in targets if target.target_kind == "artifact_dir"
        ]
        worktree_targets = [
            target for target in targets if target.target_kind == "worktree"
        ]
        if len(artifact_targets) != 1 or not worktree_targets:
            raise SprintCleanupInvariantError(
                "cleanup schedule requires managed worktrees and one artifact target"
            )
        if len({target.canonical_path for target in targets}) != len(targets):
            raise SprintCleanupInvariantError("cleanup target paths are not distinct")
        repositories = {
            (target.repository_root, target.git_common_dir) for target in targets
        }
        if len(repositories) != 1:
            raise SprintCleanupInvariantError(
                "cleanup targets do not share one stored repository identity"
            )
        repository_root, git_common_dir = next(iter(repositories))
        participants = self.con.execute(
            "SELECT participant.shell_id,shell.shortname,shell.flavor "
            "FROM sprint_participants participant JOIN shells shell "
            "ON shell.shell_id=participant.shell_id "
            "WHERE participant.sprint_id=? "
            "AND COALESCE(shell.flavor,'')<>'admin' "
            "ORDER BY participant.shell_id",
            (sprint_id,),
        ).fetchall()
        expected_worktrees: dict[int, tuple[str, str]] = {}
        for participant in participants:
            shortname = str(participant["shortname"] or "").strip()
            if not shortname:
                raise SprintCleanupInvariantError(
                    f"Sprint participant shell {participant['shell_id']} has no shortname"
                )
            expected_worktrees[int(participant["shell_id"])] = (
                self._lexical_absolute(
                    run.shell_work_dir(
                        shortname,
                        str(participant["flavor"] or ""),
                        root=Path(repository_root),
                    )
                ),
                f"shell/{shortname.lower()}",
            )
        proposed_worktrees = {
            int(target.shell_id): (
                target.canonical_path,
                str(target.expected_base_branch),
            )
            for target in worktree_targets
            if target.shell_id is not None
        }
        if proposed_worktrees != expected_worktrees:
            raise SprintCleanupInvariantError(
                "Sprint participant identities changed after cleanup preparation"
            )
        expected_artifact = self._lexical_absolute(
            Path(repository_root) / "shared" / "sprints" / f"sprint-{sprint_id}"
        )
        artifact = artifact_targets[0]
        if (
            artifact.canonical_path != expected_artifact
            or artifact.repository_root != repository_root
            or artifact.git_common_dir != git_common_dir
        ):
            raise SprintCleanupInvariantError(
                "Sprint artifact target changed after cleanup preparation"
            )

    def _target_rows(self, sprint_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT cleanup_target_id,shell_id,target_kind,canonical_path,"
            "repository_root,git_common_dir,expected_base_branch "
            "FROM sprint_cleanup_targets WHERE sprint_id=? "
            "ORDER BY target_kind,canonical_path",
            (sprint_id,),
        ).fetchall()

    @staticmethod
    def _require_exact_replay(
        existing: list[sqlite3.Row],
        targets: tuple[CleanupTargetDraft, ...],
    ) -> None:
        stored = [
            (
                row["shell_id"],
                row["target_kind"],
                row["canonical_path"],
                row["repository_root"],
                row["git_common_dir"],
                row["expected_base_branch"],
            )
            for row in existing
        ]
        proposed = [
            (
                target.shell_id,
                target.target_kind,
                target.canonical_path,
                target.repository_root,
                target.git_common_dir,
                target.expected_base_branch,
            )
            for target in targets
        ]
        if stored != proposed:
            raise SprintCleanupInvariantError(
                "completed Sprint already has a different cleanup target set"
            )

    @staticmethod
    def _lexical_absolute(path: Path) -> str:
        return os.path.abspath(os.path.normpath(str(path)))


class SprintCleanupExecutor:
    """Run one exact cleanup target without widening its stored authority."""

    def __init__(
        self,
        store: SprintCleanupTargetStore,
        *,
        liveness_probe: LivenessProbe | None = None,
        branch_pruner: PruneBranches | None = None,
        fetch_timeout: int = 30,
        command_timeout: int = 30,
        lock_timeout: float = 5.0,
        lease_seconds: int = 120,
        max_attempts: int = 3,
    ) -> None:
        self.store = store
        self.con = store.con
        self.liveness_probe = liveness_probe or self._default_liveness
        self.branch_pruner = branch_pruner or (
            lambda repo: git_prune.prune(repo=repo, fetch=False)
        )
        self.fetch_timeout = fetch_timeout
        self.command_timeout = command_timeout
        self.lock_timeout = lock_timeout
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        if lease_seconds <= max(fetch_timeout, command_timeout) + 5:
            raise ValueError("cleanup lease must outlive every bounded command")

    def run_next(
        self,
        owner: str,
        *,
        shell_id: int | None = None,
    ) -> CleanupExecutionReceipt:
        claim = self.store.claim_next(
            owner,
            shell_id=shell_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return CleanupExecutionReceipt(None, None, "idle")
        return self.execute(claim)

    def execute(self, claim: CleanupClaim) -> CleanupExecutionReceipt:
        try:
            with self._repository_lock(claim):
                self._validate_under_lock(claim)
                if claim.target_kind == "artifact_dir":
                    return self._delete_artifacts(claim)
                return self._reset_worktree(claim)
        except SprintCleanupWaiting as exc:
            changed = self.store.release_waiting(claim, exc.code)
            return self._receipt(
                claim,
                "waiting" if changed else "stale",
                exc.code,
                exc.detail,
            )
        except SprintCleanupSafetyError as exc:
            changed = self.store.fail_safety(claim, exc.code, exc.detail)
            return self._receipt(
                claim,
                "failed" if changed else "stale",
                exc.code,
                exc.detail,
            )
        except SprintCleanupMutationError as exc:
            changed, state, attempts = self.store.fail_mutation(
                claim,
                exc.code,
                exc.detail,
                max_attempts=self.max_attempts,
            )
            return CleanupExecutionReceipt(
                claim.cleanup_target_id,
                claim.sprint_id,
                state if changed else "stale",
                exc.code,
                exc.detail,
                claim.claim_generation,
                attempts,
            )

    def _reset_worktree(self, claim: CleanupClaim) -> CleanupExecutionReceipt:
        repository = Path(claim.repository_root)
        target = Path(claim.canonical_path)
        try:
            git_freshness._refresh_remote(
                repository,
                "origin",
                "main",
                self.fetch_timeout,
            )
        except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError) as exc:
            raise SprintCleanupMutationError("fetch_failed", str(exc)) from exc
        if not self.store.renew(claim, lease_seconds=self.lease_seconds):
            return self._receipt(claim, "stale", "claim_superseded", None)

        refreshed_main = self._git_stdout(
            repository,
            "rev-parse",
            "--verify",
            "origin/main",
            code="refreshed_main_missing",
            mutation=True,
        )
        before = self._git_evidence(target)
        before["refreshed_main_sha"] = refreshed_main
        if not self.store.record_before(claim, before):
            return self._receipt(claim, "stale", "claim_superseded", None)

        # Fetch and evidence capture both take time. Re-read every authority and
        # ownership fact immediately before the first destructive Git command.
        self._validate_under_lock(claim)
        self._renew_or_stale(claim)
        attempt = self.store.begin_attempt(claim)
        if attempt is None:
            return self._receipt(claim, "stale", "claim_superseded", None)
        self._git(target, "reset", "--hard", "HEAD", code="reset_current_failed")
        self._renew_or_stale(claim)
        self._git(target, "clean", "-ffd", code="clean_current_failed")
        self._renew_or_stale(claim)
        self._git(
            target,
            "checkout",
            "--force",
            str(claim.expected_base_branch),
            code="base_checkout_failed",
        )
        self._renew_or_stale(claim)
        self._git(
            target,
            "reset",
            "--hard",
            refreshed_main,
            code="base_reset_failed",
        )
        self._renew_or_stale(claim)
        self._git(target, "clean", "-ffd", code="clean_base_failed")
        self._renew_or_stale(claim)
        self._restore_submodules(target, claim)
        self._renew_or_stale(claim)

        try:
            prune_result = self.branch_pruner(repository)
        except Exception as exc:
            raise SprintCleanupMutationError(
                "branch_prune_failed",
                f"proven-merged branch pruning failed: {exc}",
            ) from exc
        self._renew_or_stale(claim)
        after = self._git_evidence(target)
        final_main = self._git_stdout(
            repository,
            "rev-parse",
            "--verify",
            "origin/main",
            code="final_main_unreadable",
            mutation=True,
        )
        after.update(
            {
                "refreshed_main_sha": refreshed_main,
                "final_origin_main_sha": final_main,
                "prune_candidates": int(prune_result.get("candidates", 0)),
                "prune_error": bool(prune_result.get("error")),
                "pruned_branches": list(prune_result.get("deleted", []))[:25],
                "prune_failures": list(prune_result.get("failed", []))[:25],
            }
        )
        if after["branch"] != claim.expected_base_branch:
            raise SprintCleanupMutationError(
                "final_branch_mismatch",
                "cleanup did not finish on the stored shell base",
            )
        if after["head"] != refreshed_main or final_main != refreshed_main:
            raise SprintCleanupMutationError(
                "final_head_mismatch",
                "cleanup base does not equal refreshed origin/main",
            )
        if after["status_count"] != 0:
            raise SprintCleanupMutationError(
                "final_worktree_dirty",
                "cleanup worktree is not clean after reset",
            )
        self._renew_or_stale(claim)
        if not self.store.mark_succeeded(claim, after):
            return self._receipt(claim, "stale", "claim_superseded", None)
        return CleanupExecutionReceipt(
            claim.cleanup_target_id,
            claim.sprint_id,
            "succeeded",
            claim_generation=claim.claim_generation,
            attempt_count=attempt,
        )

    def _delete_artifacts(self, claim: CleanupClaim) -> CleanupExecutionReceipt:
        target = Path(claim.canonical_path)
        existed = target.exists()
        entry_count, count_truncated = self._bounded_entry_count(target)
        before = {
            "existed": existed,
            "entry_count": entry_count,
            "entry_count_truncated": count_truncated,
        }
        if not self.store.record_before(claim, before):
            return self._receipt(claim, "stale", "claim_superseded", None)
        self._validate_under_lock(claim)
        self._renew_or_stale(claim)
        attempt = self.store.begin_attempt(claim)
        if attempt is None:
            return self._receipt(claim, "stale", "claim_superseded", None)
        if existed:
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise SprintCleanupMutationError(
                    "artifact_delete_failed",
                    f"exact Sprint artifact deletion failed: {exc}",
                ) from exc
        if target.exists() or target.is_symlink():
            raise SprintCleanupMutationError(
                "artifact_delete_incomplete",
                "exact Sprint artifact path still exists after deletion",
            )
        after = {
            "existed": existed,
            "removed_entry_count": entry_count,
            "entry_count_truncated": count_truncated,
        }
        if not self.store.mark_succeeded(claim, after):
            return self._receipt(claim, "stale", "claim_superseded", None)
        return CleanupExecutionReceipt(
            claim.cleanup_target_id,
            claim.sprint_id,
            "succeeded",
            claim_generation=claim.claim_generation,
            attempt_count=attempt,
        )

    def _validate_under_lock(self, claim: CleanupClaim) -> None:
        if not self.store.claim_is_current(claim):
            raise SprintCleanupSafetyError(
                "claim_superseded",
                "cleanup claim expired or no longer owns its generation",
            )
        sprint = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?",
            (claim.sprint_id,),
        ).fetchone()
        if sprint is None or sprint["lifecycle"] != "completed":
            raise SprintCleanupSafetyError(
                "sprint_not_completed",
                "cleanup target no longer belongs to a completed Sprint",
            )
        self._validate_repository_identity(claim)
        if claim.target_kind == "artifact_dir":
            self._validate_artifact_identity(claim)
            return
        self._validate_worktree_identity(claim)
        try:
            liveness = self.liveness_probe(claim)
        except Exception as exc:
            raise SprintCleanupSafetyError(
                "liveness_probe_failed",
                f"target process liveness probe failed: {exc}",
            ) from exc
        if liveness == "live":
            raise SprintCleanupWaiting(
                "waiting_for_run_exit",
                "a verified harness process or active conversation run holds the target",
            )
        if liveness != "dormant":
            raise SprintCleanupSafetyError(
                "liveness_indeterminate",
                "target process liveness could not be proven dormant",
            )
        newer = self.con.execute(
            "SELECT sprint.sprint_id FROM sprints sprint "
            "JOIN sprint_participants participant "
            "ON participant.sprint_id=sprint.sprint_id "
            "WHERE participant.shell_id=? AND sprint.sprint_id>? "
            "AND sprint.lifecycle IN ('armed','paused') "
            "ORDER BY sprint.sprint_id LIMIT 1",
            (claim.shell_id, claim.sprint_id),
        ).fetchone()
        if newer is not None:
            raise SprintCleanupSafetyError(
                "newer_sprint_owns_target",
                f"newer Sprint {newer['sprint_id']} owns the cleanup shell",
            )

    def _validate_repository_identity(self, claim: CleanupClaim) -> None:
        repository = Path(claim.repository_root)
        common_dir = Path(claim.git_common_dir)
        if not repository.is_absolute() or repository.resolve() != repository:
            raise SprintCleanupSafetyError(
                "repository_identity_changed",
                "stored repository root is not one canonical absolute path",
            )
        if not common_dir.is_absolute() or common_dir.resolve() != common_dir:
            raise SprintCleanupSafetyError(
                "repository_identity_changed",
                "stored Git common directory is not one canonical absolute path",
            )
        try:
            actual_root = git_freshness._canonical_root(repository)
            actual_common = git_freshness._common_git_dir(repository)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise SprintCleanupSafetyError(
                "repository_identity_unreadable",
                str(exc),
            ) from exc
        if actual_root != repository or actual_common != common_dir:
            raise SprintCleanupSafetyError(
                "repository_identity_changed",
                "stored repository root or Git common-directory identity changed",
            )

    def _validate_worktree_identity(self, claim: CleanupClaim) -> None:
        if claim.shell_id is None or claim.expected_base_branch is None:
            raise SprintCleanupSafetyError(
                "worktree_identity_invalid",
                "worktree cleanup target lacks its stored shell identity",
            )
        shell = self.con.execute(
            "SELECT shortname,flavor FROM shells WHERE shell_id=?",
            (claim.shell_id,),
        ).fetchone()
        if shell is None or str(shell["flavor"] or "") == "admin":
            raise SprintCleanupSafetyError(
                "worktree_identity_invalid",
                "stored shell is missing or resolves to the Admin checkout",
            )
        shortname = str(shell["shortname"] or "").strip()
        expected = SprintCleanupTargetStore._lexical_absolute(
            run.shell_work_dir(
                shortname,
                str(shell["flavor"] or ""),
                root=Path(claim.repository_root),
            )
        )
        target = Path(claim.canonical_path)
        repository = Path(claim.repository_root)
        expected_base = f"shell/{shortname.lower()}"
        if (
            claim.canonical_path != expected
            or target.parent != repository / ".sc-worktrees"
            or target.name != shortname.lower()
            or claim.expected_base_branch != expected_base
        ):
            raise SprintCleanupSafetyError(
                "managed_path_mismatch",
                "stored worktree no longer equals the shell's exact managed path",
            )
        self._reject_symlink_components(target, repository)
        if target == repository or self._is_relative_to(repository, target):
            raise SprintCleanupSafetyError(
                "main_checkout_targeted",
                "cleanup target is the main checkout or one of its ancestors",
            )
        try:
            top = git_freshness._canonical_root(target)
            common = git_freshness._common_git_dir(target)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise SprintCleanupSafetyError(
                "worktree_identity_unreadable",
                str(exc),
            ) from exc
        if top != target or common != Path(claim.git_common_dir):
            raise SprintCleanupSafetyError(
                "git_common_dir_mismatch",
                "target is not the stored repository's exact registered worktree",
            )
        listed = self._git_stdout(
            repository,
            "worktree",
            "list",
            "--porcelain",
            code="worktree_membership_unreadable",
        )
        worktrees = {
            SprintCleanupTargetStore._lexical_absolute(Path(line[9:]))
            for line in listed.splitlines()
            if line.startswith("worktree ")
        }
        if claim.canonical_path not in worktrees:
            raise SprintCleanupSafetyError(
                "worktree_not_registered",
                "stored target is absent from the repository worktree registry",
            )

    def _validate_artifact_identity(self, claim: CleanupClaim) -> None:
        target = Path(claim.canonical_path)
        repository = Path(claim.repository_root)
        parent = repository / "shared" / "sprints"
        expected = parent / f"sprint-{claim.sprint_id}"
        if target != expected or target.parent != parent:
            raise SprintCleanupSafetyError(
                "artifact_path_mismatch",
                "artifact target is not the exact stored Sprint directory",
            )
        self._reject_symlink_components(parent, repository)
        if target.is_symlink():
            raise SprintCleanupSafetyError(
                "artifact_symlink_refused",
                "exact Sprint artifact path is a symbolic link",
            )
        if target.exists() and not target.is_dir():
            raise SprintCleanupSafetyError(
                "artifact_not_directory",
                "exact Sprint artifact target exists but is not a directory",
            )
        incomplete = self.con.execute(
            "SELECT 1 FROM sprint_cleanup_targets WHERE sprint_id=? "
            "AND target_kind='worktree' AND state<>'succeeded' LIMIT 1",
            (claim.sprint_id,),
        ).fetchone()
        if incomplete is not None:
            raise SprintCleanupWaiting(
                "waiting_for_worktrees",
                "artifact deletion waits for every worktree target to succeed",
            )

    def _default_liveness(self, claim: CleanupClaim) -> str:
        if claim.shell_id is None:
            return "dormant"
        live_run = self.con.execute(
            "SELECT run.run_id FROM conversation_runs run "
            "WHERE run.shell_id=? AND run.state IN ('leased','starting','running') "
            "LIMIT 1",
            (claim.shell_id,),
        ).fetchone()
        if live_run is not None:
            return "live"
        active = active_chat_registry.get(self.con, claim.shell_id)
        if active is not None and active.process_pid is not None:
            if active_chat_registry.has_live_process(active):
                return "live"
            try:
                os.kill(active.process_pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                return "indeterminate"
            else:
                # A live recycled pid does not inherit the stored identity.
                pass
        snapshot = shell_liveness.compute()
        if not snapshot.get("supported"):
            return "indeterminate"
        snapshot_root = Path(str(snapshot.get("repo", {}).get("root", "")))
        if not snapshot_root.is_absolute() or snapshot_root.resolve() != Path(
            claim.repository_root
        ):
            return "indeterminate"
        if snapshot.get("indeterminate"):
            return "indeterminate"
        shell = self.con.execute(
            "SELECT shortname FROM shells WHERE shell_id=?",
            (claim.shell_id,),
        ).fetchone()
        if shell is None:
            return "indeterminate"
        shortname = str(shell["shortname"] or "").lower()
        active_names = {
            str(value).lower() for value in snapshot.get("active_other_shells", [])
        }
        claimed_names = {
            str(value).lower() for value in snapshot.get("claimed_pids", {})
        }
        return "live" if shortname in active_names | claimed_names else "dormant"

    @contextmanager
    def _repository_lock(self, claim: CleanupClaim) -> Iterator[None]:
        common_dir = Path(claim.git_common_dir)
        if not common_dir.is_dir() or common_dir.resolve() != common_dir:
            raise SprintCleanupSafetyError(
                "repository_lock_identity_changed",
                "stored Git common directory cannot host the cleanup lock",
            )
        lock_path = common_dir / "sc-sprint-cleanup.lock"
        started = time.monotonic()
        try:
            handle = lock_path.open("a+")
        except OSError as exc:
            raise SprintCleanupSafetyError(
                "repository_lock_unavailable",
                f"cleanup repository lock cannot be opened: {exc}",
            ) from exc
        with handle:
            while True:
                try:
                    flock(handle.fileno(), LOCK_EX | LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= self.lock_timeout:
                        raise SprintCleanupWaiting(
                            "repository_lock_busy",
                            "another cleanup or repository operation owns the lock",
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                flock(handle.fileno(), LOCK_UN)

    def _restore_submodules(self, target: Path, claim: CleanupClaim) -> None:
        modules = target / ".gitmodules"
        if not modules.is_file():
            return
        self._git(
            target,
            "submodule",
            "sync",
            "--recursive",
            code="submodule_sync_failed",
        )
        self._git(
            target,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--force",
            code="submodule_update_failed",
            timeout=max(self.command_timeout, self.fetch_timeout),
        )
        self._renew_or_stale(claim)
        for submodule in self._submodule_paths(target):
            self._git(
                submodule, "reset", "--hard", "HEAD", code="submodule_reset_failed"
            )
            self._renew_or_stale(claim)
            self._git(submodule, "clean", "-ffd", code="submodule_clean_failed")
            self._renew_or_stale(claim)

    def _submodule_paths(self, root: Path) -> list[Path]:
        found: list[Path] = []
        pending = [root]
        while pending:
            parent = pending.pop()
            config = parent / ".gitmodules"
            if not config.is_file():
                continue
            output = self._git_stdout(
                parent,
                "config",
                "--file",
                str(config),
                "--get-regexp",
                r"^submodule\..*\.path$",
                code="submodule_config_failed",
                allowed=(0, 1),
                mutation=True,
            )
            for line in output.splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                child = parent / parts[1]
                try:
                    resolved = child.resolve(strict=True)
                    resolved.relative_to(root)
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    raise SprintCleanupMutationError(
                        "submodule_path_invalid",
                        f"tracked submodule path escaped the worktree: {parts[1]}",
                    ) from exc
                found.append(resolved)
                pending.append(resolved)
        return found

    def _git_evidence(self, repo: Path) -> dict[str, Any]:
        branch_result = self._run_git(
            repo, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
        head = self._git_stdout(
            repo,
            "rev-parse",
            "--verify",
            "HEAD",
            code="head_unreadable",
            mutation=True,
        )
        status = self._git_stdout(
            repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            code="status_unreadable",
            mutation=True,
        ).splitlines()
        return {
            "branch": branch,
            "head": head,
            "status_count": len(status),
            "status_sample": [line[:300] for line in status[:25]],
            "status_sample_truncated": len(status) > 25,
        }

    def _git(
        self,
        repo: Path,
        *args: str,
        code: str,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_git(repo, *args, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = (
                detail[-1][:1000] if detail else "Git command failed without detail"
            )
            raise SprintCleanupMutationError(code, suffix)
        return result

    def _git_stdout(
        self,
        repo: Path,
        *args: str,
        code: str,
        allowed: tuple[int, ...] = (0,),
        mutation: bool = False,
    ) -> str:
        try:
            result = self._run_git(repo, *args)
        except SprintCleanupMutationError as exc:
            if mutation:
                raise
            raise SprintCleanupSafetyError(code, exc.detail) from exc
        if result.returncode not in allowed:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = (
                detail[-1][:1000] if detail else "Git command failed without detail"
            )
            error_type = (
                SprintCleanupMutationError if mutation else SprintCleanupSafetyError
            )
            raise error_type(code, suffix)
        return result.stdout.strip()

    def _renew_or_stale(self, claim: CleanupClaim) -> None:
        if not self.store.renew(claim, lease_seconds=self.lease_seconds):
            raise SprintCleanupSafetyError(
                "claim_superseded",
                "cleanup claim expired or lost its generation before mutation",
            )

    def _run_git(
        self,
        repo: Path,
        *args: str,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=timeout or self.command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SprintCleanupMutationError(
                "git_command_unavailable",
                str(exc),
            ) from exc

    @staticmethod
    def _reject_symlink_components(path: Path, floor: Path) -> None:
        current = path
        while True:
            if current.is_symlink():
                raise SprintCleanupSafetyError(
                    "symlink_component_refused",
                    "cleanup target contains a symbolic-link component",
                )
            if current == floor:
                return
            if current == current.parent:
                raise SprintCleanupSafetyError(
                    "managed_path_mismatch",
                    "cleanup target is outside the stored repository",
                )
            current = current.parent

    @staticmethod
    def _is_relative_to(path: Path, other: Path) -> bool:
        try:
            path.relative_to(other)
        except ValueError:
            return False
        return True

    @staticmethod
    def _bounded_entry_count(path: Path, limit: int = 10000) -> tuple[int, bool]:
        if not path.exists():
            return 0, False
        count = 0
        for _root, directories, files in os.walk(path, followlinks=False):
            count += len(directories) + len(files)
            if count >= limit:
                return limit, True
        return count, False

    def _receipt(
        self,
        claim: CleanupClaim,
        state: str,
        code: str | None,
        detail: str | None,
    ) -> CleanupExecutionReceipt:
        row = self.con.execute(
            "SELECT attempt_count FROM sprint_cleanup_targets "
            "WHERE cleanup_target_id=?",
            (claim.cleanup_target_id,),
        ).fetchone()
        return CleanupExecutionReceipt(
            claim.cleanup_target_id,
            claim.sprint_id,
            state,
            code,
            detail,
            claim.claim_generation,
            int(row["attempt_count"]) if row is not None else None,
        )
