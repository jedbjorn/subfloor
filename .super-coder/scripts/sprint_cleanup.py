#!/usr/bin/env python3
"""Durable successful-Sprint cleanup target scheduling and projection."""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import git_freshness
import run


class SprintCleanupInvariantError(ValueError):
    """The completed Sprint cannot be mapped to exact managed targets."""


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


IdentityProvider = Callable[[], tuple[Path, Path]]


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
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.identity_provider = identity_provider or resolve_repository_identity

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
            "AND COALESCE(shell.is_deleted,0)=0 "
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
            raise RuntimeError("Sprint cleanup scheduling requires an active transaction")
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
            target_id = int(
                self.con.execute(
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
                ).lastrowid
            )
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
            "AND COALESCE(shell.is_deleted,0)=0 "
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
