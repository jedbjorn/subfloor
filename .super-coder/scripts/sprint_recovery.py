"""Pause, resume, abort, and restart coordination for Sprints v2."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import conversation_broker
from github_pull_requests import GitHubPullRequestReader, PullRequest
from sprint_domain import (
    AbortReceipt,
    LifecycleActor,
    PauseReceipt,
    ResumeReceipt,
    SprintLifecycleStore,
)
from sprint_pr_watcher import SprintPRWatcher


@dataclass(frozen=True)
class _ObservedPR:
    registered_pr_id: int
    pull_request: PullRequest


class SprintRecoveryCoordinator:
    """Keep external reads outside SQLite and commit one reconciled resume."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        repo_root: str | Path,
        reader_factory: Callable[[str], Any] | None = None,
        interrupt_run: Callable[[int], bool] | None = None,
        notify_commit: Callable[[], bool] | None = None,
        native_reconciler: Callable[[tuple[int, ...]], tuple[str, ...]] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.repo_root = Path(repo_root)
        self.reader_factory = reader_factory or (
            lambda repository: GitHubPullRequestReader(
                self.repo_root,
                repository=repository,
            )
        )
        self.native_reconciler = native_reconciler or self._reconcile_native_runs
        self.lifecycle = SprintLifecycleStore(
            con,
            interrupt_run=interrupt_run,
            notify_commit=notify_commit,
        )
        self.watcher = SprintPRWatcher(
            con,
            repo_root=self.repo_root,
            reader_factory=self.reader_factory,
        )

    def pause(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
        detail: dict | None = None,
    ) -> PauseReceipt:
        return self.lifecycle.pause(
            sprint_id,
            actor,
            reason=reason,
            detail=detail,
        )

    def resume(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str | None = None,
    ) -> ResumeReceipt:
        native_run_ids = tuple(
            int(row[0])
            for row in self.con.execute(
                "SELECT DISTINCT r.run_id FROM conversation_runs r "
                "JOIN sprint_participant_conversations pc "
                "ON pc.conversation_id=r.conversation_id "
                "JOIN sprint_participants p "
                "ON p.participant_id=pc.sprint_participant_id "
                "WHERE p.sprint_id=? AND r.state IN ('leased','starting','running') "
                "ORDER BY r.run_id",
                (sprint_id,),
            )
        )
        native_anomalies = self.native_reconciler(native_run_ids)
        observations, pr_anomalies = self._read_registered_prs(sprint_id)
        anomalies = (*native_anomalies, *pr_anomalies)
        resolved_review_message_ids: list[int] = []

        def reconcile(_con: sqlite3.Connection) -> None:
            for observation in observations:
                receipt = self.watcher.observe_in_transaction(
                    observation.registered_pr_id,
                    observation.pull_request,
                    trigger="resume",
                    dispatch=False,
                )
                if receipt is not None:
                    resolved_review_message_ids.extend(
                        receipt.resolved_review_message_ids
                    )

        receipt = self.lifecycle.resume(
            sprint_id,
            actor,
            reason=reason,
            reconcile_in_transaction=reconcile,
            external_anomalies=anomalies,
        )
        return replace(
            receipt,
            resolved_review_message_ids=tuple(
                sorted(
                    set(receipt.resolved_review_message_ids)
                    | set(resolved_review_message_ids)
                )
            ),
        )

    @staticmethod
    def _reconcile_native_runs(run_ids: tuple[int, ...]) -> tuple[str, ...]:
        if not run_ids:
            return ()
        broker = conversation_broker.service()
        if broker is None or not broker.is_alive():
            return (
                (
                    "native-run reconciliation deferred: conversation broker "
                    "unavailable for run(s) "
                    f"{','.join(str(run_id) for run_id in run_ids)}"
                ),
            )
        broker.notify()
        return ()

    def abort(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
        terminal_outcome: str = "aborted",
    ) -> AbortReceipt:
        return self.lifecycle.abort(
            sprint_id,
            actor,
            reason=reason,
            terminal_outcome=terminal_outcome,
        )

    def recover_on_startup(self, sprint_id: int) -> str:
        """Recover one non-terminal state; only armed state performs PR reads."""
        row = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        lifecycle = str(row["lifecycle"])
        if lifecycle not in {"prepared", "armed", "paused"}:
            return lifecycle
        self.lifecycle.recover_on_startup(sprint_id)
        if lifecycle == "armed":
            self.watcher.poll_once(startup=True)
        return lifecycle

    def _read_registered_prs(
        self,
        sprint_id: int,
    ) -> tuple[tuple[_ObservedPR, ...], tuple[str, ...]]:
        rows = self.con.execute(
            "SELECT registered_pr_id,repository,pr_number "
            "FROM sprint_registered_prs WHERE sprint_id=? "
            "ORDER BY registered_pr_id",
            (sprint_id,),
        ).fetchall()
        observations: list[_ObservedPR] = []
        anomalies: list[str] = []
        for row in rows:
            repository = str(row["repository"])
            pr_number = int(row["pr_number"])
            try:
                pull_request = self.reader_factory(repository).get(pr_number)
                if pull_request.number != pr_number:
                    raise RuntimeError("GitHub returned a different PR identity")
            except Exception as exc:  # noqa: BLE001 - external faults inform
                anomalies.append(
                    f"{repository}#{pr_number} reconciliation failed: "
                    f"{str(exc).strip() or exc.__class__.__name__}"
                )
                continue
            observations.append(
                _ObservedPR(int(row["registered_pr_id"]), pull_request)
            )
        return tuple(observations), tuple(anomalies)
