"""Engine-wide GitHub observation for shell-owned pull-request subscriptions.

Registration is a short database transaction.  GitHub reads happen outside
transactions; each result is revalidated against its subscription before its
append-only transition and routed messages+wakes commit together.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db_driver
import sprint_liveness
from github_pull_requests import (
    GITHUB_TIMEOUT_SECONDS,
    GitHubPullRequestReader,
    GitHubReadError,
    PullRequest,
)
from sprint_domain import (
    LifecycleActor,
    PauseReceipt,
    SprintAuthorityError,
    SprintInvariantError,
    SprintLifecycleStore,
)
from sprint_message_delivery import SprintMessageStore
from sprint_review_loop import SprintReviewLoopStore

PULSE_SECONDS = 5.0
HEARTBEAT_HISTORY_SECONDS = 60.0
WATCHER_DAEMON_NAME = "sprint-pr-watcher"
MAX_BACKOFF_SECONDS = 300.0
RATE_BACKOFF_SECONDS = 60.0
_REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_db_stamp(con: sqlite3.Connection, floor: str | None) -> str:
    """Return a DB timestamp strictly newer than the supplied durable floor."""
    row = con.execute(
        "SELECT CASE "
        "WHEN ? IS NOT NULL "
        "AND ?>=strftime('%Y-%m-%d %H:%M:%f','now') "
        "THEN strftime('%Y-%m-%d %H:%M:%f',?,'+0.001 seconds') "
        "ELSE strftime('%Y-%m-%d %H:%M:%f','now') END",
        (floor, floor, floor),
    ).fetchone()
    return str(row[0])


def derive_watcher_status(
    heartbeat: sqlite3.Row | dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str:
    """Project the watcher's current heartbeat as live, stale, or absent."""
    if heartbeat is None:
        return "never-started"
    observed_at = _parse_stamp(str(heartbeat["beat_at"]))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    threshold = 3 * (float(heartbeat["interval_s"]) + GITHUB_TIMEOUT_SECONDS)
    return "live" if (current - observed_at).total_seconds() <= threshold else "stale"


def _age_seconds(value: str, now: datetime) -> int:
    return max(0, int((now - _parse_stamp(value)).total_seconds()))


class WatcherHeartbeat:
    """Persist current watcher liveness and a coarse, bounded history."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        interval_seconds: float,
        history_seconds: float = HEARTBEAT_HISTORY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("watcher heartbeat interval must be positive")
        if history_seconds <= 0:
            raise ValueError("watcher heartbeat history interval must be positive")
        self.con = con
        self.interval_seconds = interval_seconds
        self.history_seconds = history_seconds
        self.monotonic = monotonic
        self._last_history_at: float | None = None

    def beat(
        self,
        subscriptions_scanned: int,
        *,
        force_history: bool = False,
        history_eligible: bool = True,
    ) -> None:
        if subscriptions_scanned < 0:
            raise ValueError("subscriptions_scanned must be non-negative")
        observed = self.monotonic()
        history_due = history_eligible and (
            force_history
            or (
                self._last_history_at is not None
                and observed - self._last_history_at >= self.history_seconds
            )
        )
        with db_driver.write_transaction(self.con, "sprint.pr.watcher.heartbeat"):
            self.con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES (?,datetime('now'),?) "
                "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
                "interval_s=excluded.interval_s",
                (WATCHER_DAEMON_NAME, self.interval_seconds),
            )
            if history_due:
                self.con.execute(
                    "INSERT INTO daemon_heartbeat_history "
                    "(name,subscriptions_scanned) VALUES (?,?)",
                    (WATCHER_DAEMON_NAME, subscriptions_scanned),
                )
        if history_due or self._last_history_at is None:
            self._last_history_at = observed


class WatcherStateStore:
    """Project bounded watcher evidence for one Sprint without side effects."""

    HISTORY_LIMIT = 50
    FAILURE_LIMIT = 20

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def for_sprint(
        self,
        sprint_id: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(sprint_id, int) or sprint_id < 1:
            raise ValueError("sprint_id must be a positive integer")
        sprint = self.con.execute(
            "SELECT sprint_id FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        heartbeat = self.con.execute(
            "SELECT name,beat_at,interval_s FROM daemon_heartbeats WHERE name=?",
            (WATCHER_DAEMON_NAME,),
        ).fetchone()
        history = self.con.execute(
            "SELECT heartbeat_id,beat_at,subscriptions_scanned "
            "FROM daemon_heartbeat_history WHERE name=? "
            "ORDER BY heartbeat_id DESC LIMIT ?",
            (WATCHER_DAEMON_NAME, self.HISTORY_LIMIT),
        ).fetchall()

        registered = self.con.execute(
            "SELECT registered.registered_pr_id,registered.repository,"
            "registered.pr_number,subscription.subscription_id,"
            "transition.transition_id,transition.normalized_state,"
            "transition.observed_head_sha,transition.observed_at "
            "FROM sprint_registered_prs registered "
            "LEFT JOIN pr_subscriptions subscription "
            "ON subscription.sprint_registered_pr_id=registered.registered_pr_id "
            "LEFT JOIN pr_subscription_transitions transition "
            "ON transition.transition_id=("
            "SELECT candidate.transition_id FROM pr_subscription_transitions candidate "
            "WHERE candidate.subscription_id=subscription.subscription_id "
            "ORDER BY candidate.transition_id DESC LIMIT 1) "
            "WHERE registered.sprint_id=? ORDER BY registered.registered_pr_id",
            (sprint_id,),
        ).fetchall()
        registered_prs = []
        for row in registered:
            latest = None
            if row["transition_id"] is not None:
                observed_at = str(row["observed_at"])
                latest = {
                    "transition_id": int(row["transition_id"]),
                    "normalized_state": str(row["normalized_state"]),
                    "observed_head_sha": row["observed_head_sha"],
                    "observed_at": observed_at,
                    "age_seconds": _age_seconds(observed_at, current),
                }
            registered_prs.append(
                {
                    "registered_pr_id": int(row["registered_pr_id"]),
                    "repository": str(row["repository"]),
                    "pr_number": int(row["pr_number"]),
                    "subscription_id": (
                        int(row["subscription_id"])
                        if row["subscription_id"] is not None
                        else None
                    ),
                    "has_observation": latest is not None,
                    "latest_transition": latest,
                }
            )

        failures = self.con.execute(
            "SELECT failure.failure_id,registered.registered_pr_id,"
            "subscription.subscription_id,registered.repository,"
            "registered.pr_number,failure.failure_count,failure.repeat_count,"
            "failure.backoff_seconds,failure.trigger,failure.error_detail,"
            "failure.failed_at,failure.last_seen_at "
            "FROM pr_subscription_poll_failures failure "
            "JOIN pr_subscriptions subscription "
            "ON subscription.subscription_id=failure.subscription_id "
            "JOIN sprint_registered_prs registered "
            "ON registered.registered_pr_id=subscription.sprint_registered_pr_id "
            "WHERE registered.sprint_id=? "
            "ORDER BY failure.failure_id DESC LIMIT ?",
            (sprint_id, self.FAILURE_LIMIT),
        ).fetchall()

        watcher = {
            "status": derive_watcher_status(heartbeat, now=current),
            "last_beat_at": None,
            "interval_seconds": None,
            "age_seconds": None,
            "history": [
                {
                    "heartbeat_id": int(row["heartbeat_id"]),
                    "beat_at": str(row["beat_at"]),
                    "subscriptions_scanned": int(row["subscriptions_scanned"]),
                }
                for row in history
            ],
        }
        if heartbeat is not None:
            watcher.update(
                {
                    "last_beat_at": str(heartbeat["beat_at"]),
                    "interval_seconds": int(heartbeat["interval_s"]),
                    "age_seconds": _age_seconds(str(heartbeat["beat_at"]), current),
                }
            )

        return {
            "sprint_id": sprint_id,
            "watcher": watcher,
            "registered_prs": registered_prs,
            "poll_failures": [
                {
                    "failure_id": int(row["failure_id"]),
                    "registered_pr_id": int(row["registered_pr_id"]),
                    "subscription_id": int(row["subscription_id"]),
                    "repository": str(row["repository"]),
                    "pr_number": int(row["pr_number"]),
                    "failure_count": int(row["failure_count"]),
                    "repeat_count": int(row["repeat_count"]),
                    "backoff_seconds": float(row["backoff_seconds"]),
                    "trigger": str(row["trigger"]),
                    "error_detail": str(row["error_detail"]),
                    "failed_at": str(row["failed_at"]),
                    "last_seen_at": (
                        str(row["last_seen_at"])
                        if row["last_seen_at"] is not None
                        else None
                    ),
                }
                for row in failures
            ],
        }


@dataclass(frozen=True)
class RegistrationReceipt:
    registered_pr_id: int
    created: bool


@dataclass(frozen=True)
class RegistrationReconciliationReceipt:
    registered_pr_id: int
    changed: bool
    from_sprint_id: int
    normalized_state: str
    head_sha: str
    merge_sha: str | None
    completed_work_unit_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SubscriptionReceipt:
    subscription_id: int
    created: bool


@dataclass(frozen=True)
class TransitionReceipt:
    transition_id: int
    normalized_state: str
    transition_key: str
    resolved_review_message_ids: tuple[int, ...] = ()


@dataclass
class _FailureBackoff:
    failures: int
    retry_at: float


class PRSubscriptionStore:
    """Own engine-wide PR observation registrations by Developer shell."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def subscribe(
        self,
        *,
        owner_shell_id: int,
        repository: str,
        pr_number: int,
        sprint_registered_pr_id: int | None = None,
    ) -> SubscriptionReceipt:
        repository = repository.strip().lower()
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must be owner/name")
        if not isinstance(pr_number, int) or pr_number < 1:
            raise ValueError("PR number must be a positive integer")
        owner = self.con.execute(
            "SELECT flavor FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
            (owner_shell_id,),
        ).fetchone()
        if owner is None or str(owner["flavor"]) != "dev":
            raise SprintInvariantError("PR subscription owner must be a Developer shell")
        existing = self.con.execute(
            "SELECT * FROM pr_subscriptions WHERE repository=? AND pr_number=?",
            (repository, pr_number),
        ).fetchone()
        if existing is not None:
            if int(existing["owner_shell_id"]) != owner_shell_id:
                raise SprintInvariantError(
                    "PR subscription identity was reused with different ownership"
                )
            linked_registration = existing["sprint_registered_pr_id"]
            if (
                linked_registration is not None
                and sprint_registered_pr_id is not None
                and int(linked_registration) != sprint_registered_pr_id
            ):
                raise SprintInvariantError(
                    "PR subscription identity was reused with different ownership"
                )
            if linked_registration is None and sprint_registered_pr_id is not None:
                self.con.execute(
                    "UPDATE pr_subscriptions SET sprint_registered_pr_id=?,"
                    "updated_at=datetime('now') WHERE subscription_id=?",
                    (sprint_registered_pr_id, existing["subscription_id"]),
                )
            return SubscriptionReceipt(int(existing["subscription_id"]), False)
        subscription_id = int(
            self.con.execute(
                "INSERT INTO pr_subscriptions "
                "(owner_shell_id,repository,pr_number,sprint_registered_pr_id) "
                "VALUES (?,?,?,?)",
                (
                    owner_shell_id,
                    repository,
                    pr_number,
                    sprint_registered_pr_id,
                ),
            ).lastrowid
        )
        return SubscriptionReceipt(subscription_id, True)


def normalize_state(pull_request: PullRequest) -> str:
    """Project one GitHub response onto the durable PR state vocabulary."""
    if (
        pull_request.state == "MERGED"
        or pull_request.merged_at is not None
        or pull_request.merge_sha is not None
    ):
        return "merged"
    if pull_request.state == "CLOSED":
        return "closed"
    if pull_request.checks_failed:
        return "red"
    if pull_request.checks == "SUCCESS":
        return "green"
    if pull_request.checks == "PENDING":
        return "pending"
    return "created"


class SprintPRRegistrationStore:
    """Validate and commit registered-PR ownership and work-unit linkage."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def register(
        self,
        sprint_id: int,
        *,
        owner_shell_id: int,
        repository: str,
        pr_number: int,
        work_unit_ids: Iterable[int],
        notify_service: bool = True,
    ) -> RegistrationReceipt:
        repository = repository.strip().lower()
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must be owner/name")
        if not isinstance(pr_number, int) or pr_number < 1:
            raise ValueError("PR number must be a positive integer")
        unit_ids = tuple(sorted({int(item) for item in work_unit_ids}))
        if not unit_ids or any(item < 1 for item in unit_ids):
            raise ValueError("registration requires positive work-unit IDs")
        if len(unit_ids) != 1:
            raise SprintInvariantError(
                "registered PR requires exactly one owning work unit"
            )

        with db_driver.write_transaction(self.con, "sprint.pr.register"):
            sprint = self.con.execute(
                "SELECT sprint_id FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()
            if sprint is None:
                raise KeyError(f"unknown Sprint: {sprint_id}")
            owner = self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=? AND role='developer'",
                (sprint_id, owner_shell_id),
            ).fetchone()
            if owner is None:
                raise SprintInvariantError(
                    "registered PR owner must be a Developer participant"
                )
            placeholders = ",".join("?" for _ in unit_ids)
            units = self.con.execute(
                "SELECT work_unit_id FROM sprint_work_units "
                f"WHERE sprint_id=? AND assigned_shell_id=? "
                f"AND work_unit_id IN ({placeholders})",
                (sprint_id, owner_shell_id, *unit_ids),
            ).fetchall()
            if {int(row[0]) for row in units} != set(unit_ids):
                raise SprintInvariantError(
                    "registered PR work units must belong to the owning Developer"
                )

            existing = self.con.execute(
                "SELECT registered_pr_id,sprint_id,owner_participant_id "
                "FROM sprint_registered_prs WHERE repository=? AND pr_number=?",
                (repository, pr_number),
            ).fetchone()
            if existing is not None:
                actual_units = {
                    int(row[0])
                    for row in self.con.execute(
                        "SELECT work_unit_id FROM sprint_pr_work_units "
                        "WHERE registered_pr_id=?",
                        (existing["registered_pr_id"],),
                    )
                }
                exact = (
                    int(existing["sprint_id"]) == sprint_id
                    and int(existing["owner_participant_id"])
                    == int(owner["participant_id"])
                    and actual_units == set(unit_ids)
                )
                if not exact:
                    raise SprintInvariantError(
                        "registered PR identity was reused with different ownership"
                    )
                return RegistrationReceipt(int(existing["registered_pr_id"]), False)

            registered_pr_id = int(
                self.con.execute(
                    "INSERT INTO sprint_registered_prs "
                    "(sprint_id,owner_participant_id,repository,pr_number) "
                    "VALUES (?,?,?,?)",
                    (sprint_id, owner["participant_id"], repository, pr_number),
                ).lastrowid
            )
            self.con.executemany(
                "INSERT INTO sprint_pr_work_units "
                "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
                (
                    (sprint_id, registered_pr_id, work_unit_id)
                    for work_unit_id in unit_ids
                ),
            )
            PRSubscriptionStore(self.con).subscribe(
                owner_shell_id=owner_shell_id,
                repository=repository,
                pr_number=pr_number,
                sprint_registered_pr_id=registered_pr_id,
            )
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,'pr.registered','participant',?,?)",
                (
                    sprint_id,
                    owner_shell_id,
                    json.dumps(
                        {
                            "registered_pr_id": registered_pr_id,
                            "repository": repository,
                            "pr_number": pr_number,
                            "work_unit_ids": unit_ids,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        if notify_service:
            notify_commit()
        return RegistrationReceipt(registered_pr_id, True)

    def reconcile_aborted_registration(
        self,
        sprint_id: int,
        *,
        actor: LifecycleActor,
        repository: str,
        pr_number: int,
        work_unit_id: int,
        reason: str,
        pull_request: PullRequest,
        notify_service: bool = True,
    ) -> RegistrationReconciliationReceipt:
        """Move one PR from an aborted Sprint, preserving actor provenance."""
        if actor.shell_id is None:
            raise SprintAuthorityError(
                "only the originating Planner or authenticated FnB may reconcile "
                "Sprint PR ownership"
            )
        repository = repository.strip().lower()
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must be owner/name")
        if not isinstance(pr_number, int) or pr_number < 1:
            raise ValueError("PR number must be a positive integer")
        if not isinstance(work_unit_id, int) or work_unit_id < 1:
            raise ValueError("work-unit ID must be a positive integer")
        reason = reason.strip()
        if not reason:
            raise ValueError("reconciliation reason is empty")
        if len(reason) > 2000:
            raise ValueError("reconciliation reason exceeds 2000 characters")
        if pull_request.number != pr_number:
            raise GitHubReadError("GitHub returned a different PR identity")
        state = normalize_state(pull_request)

        completed: tuple[int, ...] = ()
        with db_driver.write_transaction(self.con, "sprint.pr.reconcile"):
            authority = self.con.execute(
                "SELECT sp.originating_planner_shell_id,caller.flavor "
                "FROM sprints sp LEFT JOIN shells caller ON caller.shell_id=? "
                "AND COALESCE(caller.is_deleted,0)=0 WHERE sp.sprint_id=?",
                (actor.shell_id, sprint_id),
            ).fetchone()
            if authority is None:
                raise KeyError(f"unknown Sprint: {sprint_id}")
            is_fnb = actor.kind == "fnb" and authority["flavor"] == "admin"
            is_planner = (
                actor.kind == "planner"
                and int(authority["originating_planner_shell_id"])
                == actor.shell_id
                and authority["flavor"] is not None
            )
            if not is_fnb and not is_planner:
                raise SprintAuthorityError(
                    "only the originating Planner or authenticated FnB may reconcile "
                    "Sprint PR ownership"
                )
            target_sprint = self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
            if target_sprint is None:
                raise KeyError(f"unknown Sprint: {sprint_id}")
            if target_sprint["lifecycle"] != "paused":
                raise SprintInvariantError(
                    "PR ownership reconciliation requires a paused target Sprint"
                )
            target = self.con.execute(
                "SELECT u.disposition,u.output_kind,u.assigned_shell_id,"
                "p.participant_id FROM sprint_work_units u "
                "JOIN sprint_participants p ON p.sprint_id=u.sprint_id "
                "AND p.shell_id=u.assigned_shell_id AND p.role='developer' "
                "WHERE u.sprint_id=? AND u.work_unit_id=?",
                (sprint_id, work_unit_id),
            ).fetchone()
            if target is None:
                raise SprintInvariantError(
                    "reconciliation target must be a Developer-owned work unit"
                )
            if target["output_kind"] != "code":
                raise SprintInvariantError(
                    "PR ownership reconciliation requires a code work unit"
                )

            existing = self.con.execute(
                "SELECT pr.registered_pr_id,pr.sprint_id,pr.owner_participant_id,"
                "s.lifecycle,p.shell_id AS owner_shell_id "
                "FROM sprint_registered_prs pr "
                "JOIN sprints s ON s.sprint_id=pr.sprint_id "
                "JOIN sprint_participants p "
                "ON p.participant_id=pr.owner_participant_id "
                "WHERE pr.repository=? AND pr.pr_number=?",
                (repository, pr_number),
            ).fetchone()
            if existing is None:
                raise SprintInvariantError(
                    "PR ownership reconciliation requires an existing registration"
                )
            registered_pr_id = int(existing["registered_pr_id"])
            old_unit_ids = tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT work_unit_id FROM sprint_pr_work_units "
                    "WHERE registered_pr_id=? ORDER BY work_unit_id",
                    (registered_pr_id,),
                )
            )
            exact = (
                int(existing["sprint_id"]) == sprint_id
                and int(existing["owner_participant_id"])
                == int(target["participant_id"])
                and old_unit_ids == (work_unit_id,)
            )
            if exact:
                durable = self.con.execute(
                    "SELECT payload FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='pr.registration_reconciled' "
                    "AND json_extract(payload,'$.registered_pr_id')=? "
                    "AND json_extract(payload,'$.to_work_unit_id')=? "
                    "ORDER BY event_id DESC LIMIT 1",
                    (sprint_id, registered_pr_id, work_unit_id),
                ).fetchone()
                if durable is None:
                    raise SprintInvariantError(
                        "PR is already owned by the target without a recovery receipt"
                    )
                durable_payload = json.loads(durable["payload"])
                durable_state = str(durable_payload["normalized_state"])
                completed = (
                    (work_unit_id,)
                    if target["disposition"] == "completed"
                    and durable_state == "merged"
                    else ()
                )
                return RegistrationReconciliationReceipt(
                    registered_pr_id,
                    False,
                    int(durable_payload["from_sprint_id"]),
                    durable_state,
                    str(durable_payload["head_sha"]),
                    (
                        str(durable_payload["merge_sha"])
                        if durable_payload["merge_sha"] is not None
                        else None
                    ),
                    completed,
                )
            if state == "closed":
                raise SprintInvariantError(
                    "closed unmerged PR ownership cannot be reconciled"
                )
            if not pull_request.head_sha:
                raise SprintInvariantError(
                    "PR ownership reconciliation requires an exact head"
                )
            if state == "merged" and not pull_request.merge_sha:
                raise SprintInvariantError(
                    "merged PR reconciliation requires exact head and merge commit"
                )
            if existing["lifecycle"] != "aborted":
                raise SprintInvariantError(
                    "PR ownership may be reconciled only from an aborted Sprint"
                )
            if target["disposition"] != "active":
                raise SprintInvariantError(
                    "PR ownership reconciliation requires an active target work unit"
                )
            target_registration = self.con.execute(
                "SELECT registered_pr_id FROM sprint_pr_work_units "
                "WHERE sprint_id=? AND work_unit_id=?",
                (sprint_id, work_unit_id),
            ).fetchone()
            if target_registration is not None:
                raise SprintInvariantError(
                    "reconciliation target work unit already has a registered PR"
                )

            prior_sprint_id = int(existing["sprint_id"])
            self.con.execute(
                "DELETE FROM sprint_pr_work_units WHERE registered_pr_id=?",
                (registered_pr_id,),
            )
            self.con.execute(
                "UPDATE sprint_registered_prs SET sprint_id=?,owner_participant_id=? "
                "WHERE registered_pr_id=?",
                (sprint_id, target["participant_id"], registered_pr_id),
            )
            self.con.execute(
                "INSERT INTO sprint_pr_work_units "
                "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
                (sprint_id, registered_pr_id, work_unit_id),
            )
            subscription = self.con.execute(
                "SELECT subscription_id FROM pr_subscriptions "
                "WHERE repository=? AND pr_number=?",
                (repository, pr_number),
            ).fetchone()
            if subscription is None:
                subscription_id = PRSubscriptionStore(self.con).subscribe(
                    owner_shell_id=int(target["assigned_shell_id"]),
                    repository=repository,
                    pr_number=pr_number,
                    sprint_registered_pr_id=registered_pr_id,
                ).subscription_id
            else:
                subscription_id = int(subscription["subscription_id"])
                self.con.execute(
                    "UPDATE pr_subscriptions SET owner_shell_id=?,"
                    "sprint_registered_pr_id=?,updated_at=datetime('now') "
                    "WHERE subscription_id=?",
                    (
                        target["assigned_shell_id"],
                        registered_pr_id,
                        subscription_id,
                    ),
                )

            transition_key: str | None = None
            if state == "merged":
                transition_key = hashlib.sha256(
                    (
                        f"reconcile:{registered_pr_id}:{prior_sprint_id}:"
                        f"{sprint_id}:{work_unit_id}:{pull_request.head_sha}:"
                        f"{pull_request.merge_sha}"
                    ).encode()
                ).hexdigest()
                evidence = json.dumps(
                    {
                        "base_ref": pull_request.base_ref,
                        "base_sha": pull_request.base_sha,
                        "checks": pull_request.checks,
                        "checks_failed": pull_request.checks_failed,
                        "head_ref": pull_request.head_ref,
                        "merge_sha": pull_request.merge_sha,
                        "review_decision": pull_request.review_decision,
                        "state": pull_request.state,
                        "title": pull_request.title,
                        "trigger": f"{actor.kind}_reconciliation",
                        "url": pull_request.url,
                    },
                    sort_keys=True,
                )
                self.con.execute(
                    "INSERT INTO pr_subscription_transitions "
                    "(subscription_id,normalized_state,transition_key,"
                    "observed_head_sha,evidence) VALUES (?,?,?,?,?)",
                    (
                        subscription_id,
                        state,
                        transition_key,
                        pull_request.head_sha,
                        evidence,
                    ),
                )
                self.con.execute(
                    "INSERT INTO sprint_pr_transitions "
                    "(registered_pr_id,normalized_state,transition_key,"
                    "observed_head_sha,evidence) VALUES (?,?,?,?,?)",
                    (
                        registered_pr_id,
                        state,
                        transition_key,
                        pull_request.head_sha,
                        evidence,
                    ),
                )
                self.con.execute(
                    "UPDATE sprint_work_units SET disposition='completed',"
                    "completed_at=datetime('now'),updated_at=datetime('now') "
                    "WHERE work_unit_id=?",
                    (work_unit_id,),
                )
                completed = (work_unit_id,)

            payload = json.dumps(
                {
                    "from_sprint_id": prior_sprint_id,
                    "from_work_unit_ids": old_unit_ids,
                    "head_sha": pull_request.head_sha,
                    "merge_sha": pull_request.merge_sha,
                    "normalized_state": state,
                    "pr_number": pr_number,
                    "reason": reason,
                    "registered_pr_id": registered_pr_id,
                    "repository": repository,
                    "to_sprint_id": sprint_id,
                    "to_work_unit_id": work_unit_id,
                },
                sort_keys=True,
            )
            self.con.executemany(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,'pr.registration_reconciled',?,?,?)",
                (
                    (prior_sprint_id, actor.kind, actor.shell_id, payload),
                    (sprint_id, actor.kind, actor.shell_id, payload),
                ),
            )
            if completed:
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                    "VALUES (?,'work_unit.completed',?,?,?)",
                    (
                        sprint_id,
                        actor.kind,
                        actor.shell_id,
                        json.dumps(
                            {
                                "head_sha": pull_request.head_sha,
                                "merge_sha": pull_request.merge_sha,
                                "registered_pr_id": registered_pr_id,
                                "source": (
                                    "fnb.pr_recovery_override"
                                    if actor.kind == "fnb"
                                    else "planner.pr_recovery"
                                ),
                                "transition_key": transition_key,
                                "work_unit_id": work_unit_id,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
        if notify_service:
            notify_commit()
        return RegistrationReconciliationReceipt(
            registered_pr_id,
            True,
            prior_sprint_id,
            state,
            str(pull_request.head_sha),
            pull_request.merge_sha,
            completed,
        )


class SprintPRWatcher:
    """Observe every active PR subscription, regardless of Sprint state."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        repo_root: str | Path,
        reader_factory: Callable[[str], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.repo_root = Path(repo_root)
        self.reader_factory = reader_factory or (
            lambda repository: GitHubPullRequestReader(
                self.repo_root, repository=repository
            )
        )
        self.monotonic = monotonic
        self.registration = SprintPRRegistrationStore(con)
        self.subscriptions = PRSubscriptionStore(con)
        self.messages = SprintMessageStore(con)
        self.liveness = sprint_liveness.SprintLivenessMonitor(con)
        self.review_loop = SprintReviewLoopStore(
            con,
            repo_root=self.repo_root,
            reader_factory=self.reader_factory,
        )
        self._backoff: dict[int, _FailureBackoff] = {}
        self._trigger = "pulse"

    def subscribe(
        self,
        *,
        owner_shell_id: int,
        repository: str,
        pr_number: int,
    ) -> SubscriptionReceipt:
        """Register an engine-wide subscription and take its initial snapshot."""
        with db_driver.write_transaction(self.con, "pr.subscription.register"):
            receipt = self.subscriptions.subscribe(
                owner_shell_id=owner_shell_id,
                repository=repository,
                pr_number=pr_number,
            )
        row = self._subscription_row(receipt.subscription_id)
        if row is not None:
            self._observe_rows((row,), "registration", ignore_backoff=True)
        notify_commit()
        return receipt

    def register(
        self,
        sprint_id: int,
        *,
        owner_shell_id: int,
        repository: str,
        pr_number: int,
        work_unit_ids: Iterable[int],
    ) -> RegistrationReceipt:
        """Register or resubscribe, then snapshot the current PR after commit."""
        receipt = self.registration.register(
            sprint_id,
            owner_shell_id=owner_shell_id,
            repository=repository,
            pr_number=pr_number,
            work_unit_ids=work_unit_ids,
            notify_service=False,
        )
        row = self.con.execute(
            "SELECT subscription.* FROM pr_subscriptions subscription "
            "WHERE subscription.sprint_registered_pr_id=?",
            (receipt.registered_pr_id,),
        ).fetchone()
        if row is not None:
            self._observe_rows((row,), "registration", ignore_backoff=True)
        notify_commit()
        return receipt

    def reconcile_aborted_registration(
        self,
        sprint_id: int,
        *,
        actor: LifecycleActor,
        repository: str,
        pr_number: int,
        work_unit_id: int,
        reason: str,
    ) -> RegistrationReconciliationReceipt:
        """Read GitHub, then perform the bounded ownership repair."""
        authority = self.con.execute(
            "SELECT sp.originating_planner_shell_id,caller.flavor "
            "FROM sprints sp LEFT JOIN shells caller ON caller.shell_id=? "
            "AND COALESCE(caller.is_deleted,0)=0 WHERE sp.sprint_id=?",
            (actor.shell_id, sprint_id),
        ).fetchone()
        if authority is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        is_fnb = actor.kind == "fnb" and authority["flavor"] == "admin"
        is_planner = (
            actor.kind == "planner"
            and actor.shell_id is not None
            and int(authority["originating_planner_shell_id"]) == actor.shell_id
            and authority["flavor"] is not None
        )
        if not is_fnb and not is_planner:
            raise SprintAuthorityError(
                "only the originating Planner or authenticated FnB may reconcile "
                "Sprint PR ownership"
            )
        normalized_repository = repository.strip().lower()
        if not _REPOSITORY.fullmatch(normalized_repository):
            raise ValueError("repository must be owner/name")
        live = self.reader_factory(normalized_repository).get(pr_number)
        receipt = self.registration.reconcile_aborted_registration(
            sprint_id,
            actor=actor,
            repository=normalized_repository,
            pr_number=pr_number,
            work_unit_id=work_unit_id,
            reason=reason,
            pull_request=live,
            notify_service=False,
        )
        row = self.con.execute(
            "SELECT * FROM pr_subscriptions WHERE sprint_registered_pr_id=?",
            (receipt.registered_pr_id,),
        ).fetchone()
        if receipt.changed and row is not None:
            self._observe_rows((row,), "reconciliation", ignore_backoff=True)
        notify_commit()
        return receipt

    def poll_once(
        self,
        *,
        startup: bool = False,
        repository_complete: Callable[[int], None] | None = None,
    ) -> bool:
        self._trigger = "startup" if startup else "pulse"
        rows = self.con.execute(
            "SELECT subscription.* FROM pr_subscriptions subscription "
            "WHERE COALESCE(("
            "SELECT transition.normalized_state "
            "FROM pr_subscription_transitions transition "
            "WHERE transition.subscription_id=subscription.subscription_id "
            "ORDER BY transition.transition_id DESC LIMIT 1"
            "),'') NOT IN ('merged','closed') "
            "ORDER BY subscription.subscription_id"
        ).fetchall()
        if not rows:
            return False
        self._observe_rows(
            rows,
            self._trigger,
            repository_complete=repository_complete,
        )
        return True

    def _subscription_row(self, subscription_id: int) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM pr_subscriptions WHERE subscription_id=?",
            (subscription_id,),
        ).fetchone()

    def _observe_rows(
        self,
        rows: Iterable[sqlite3.Row],
        trigger: str,
        *,
        ignore_backoff: bool = False,
        repository_complete: Callable[[int], None] | None = None,
    ) -> None:
        now = self.monotonic()
        due = [
            row
            for row in rows
            if ignore_backoff
            or int(row["subscription_id"]) not in self._backoff
            or self._backoff[int(row["subscription_id"])].retry_at <= now
        ]
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in due:
            grouped[str(row["repository"])].append(row)
        subscriptions_scanned = 0
        for repository, repo_rows in grouped.items():
            try:
                reader = self.reader_factory(repository)
                if len(repo_rows) == 1:
                    self._read_exact(reader, repo_rows[0], trigger)
                    continue
                try:
                    listed = {item.number: item for item in reader.list()}
                except GitHubReadError as exc:
                    for row in repo_rows:
                        self._poll_failed(row, trigger, exc)
                    continue
                for row in repo_rows:
                    pull_request = listed.get(int(row["pr_number"]))
                    if pull_request is None:
                        self._read_exact(reader, row, trigger)
                        continue
                    self._observe(row, pull_request, trigger)
            finally:
                subscriptions_scanned += len(repo_rows)
                if repository_complete is not None:
                    repository_complete(subscriptions_scanned)

    def _read_exact(self, reader: Any, row: sqlite3.Row, trigger: str) -> None:
        try:
            pull_request = reader.get(int(row["pr_number"]))
        except GitHubReadError as exc:
            self._poll_failed(row, trigger, exc)
            return
        self._observe(row, pull_request, trigger)

    def _observe(
        self,
        registered: sqlite3.Row,
        pull_request: PullRequest,
        trigger: str,
    ) -> TransitionReceipt | None:
        if pull_request.number != int(registered["pr_number"]):
            self._poll_failed(
                registered,
                trigger,
                GitHubReadError("GitHub returned a different PR identity"),
            )
            return None
        pause_receipts: list[PauseReceipt] = []
        with db_driver.write_transaction(self.con, "sprint.pr.observe"):
            receipt = self.observe_in_transaction(
                int(registered["subscription_id"]),
                pull_request,
                trigger=trigger,
                pause_receipts=pause_receipts,
            )
        lifecycle = SprintLifecycleStore(self.con)
        for pause_receipt in pause_receipts:
            lifecycle.signal_pause_receipt(pause_receipt)
        self._backoff.pop(int(registered["subscription_id"]), None)
        return receipt

    def observe_in_transaction(
        self,
        subscription_id: int,
        pull_request: PullRequest,
        *,
        trigger: str,
        dispatch: bool = True,
        pause_receipts: list[PauseReceipt] | None = None,
    ) -> TransitionReceipt | None:
        """Apply a pre-read observation inside an owner reconciliation commit."""
        if not self.con.in_transaction:
            raise RuntimeError("PR observation requires an active transaction")
        subscription = self.con.execute(
            "SELECT * FROM pr_subscriptions WHERE subscription_id=?",
            (subscription_id,),
        ).fetchone()
        if subscription is None:
            raise KeyError(f"unknown active PR subscription: {subscription_id}")
        if pull_request.number != int(subscription["pr_number"]):
            raise GitHubReadError("GitHub returned a different PR identity")
        state = normalize_state(pull_request)
        evidence = {
            "base_ref": pull_request.base_ref,
            "base_sha": pull_request.base_sha,
            "checks": pull_request.checks,
            "checks_failed": pull_request.checks_failed,
            "head_ref": pull_request.head_ref,
            "merge_sha": pull_request.merge_sha,
            "review_decision": pull_request.review_decision,
            "state": pull_request.state,
            "title": pull_request.title,
            "trigger": trigger,
            "url": pull_request.url,
        }
        current = self.con.execute(
            "SELECT subscription.*,registered.sprint_id,"
            "registered.owner_participant_id,s.lifecycle "
            "FROM pr_subscriptions subscription "
            "LEFT JOIN sprint_registered_prs registered "
            "ON registered.registered_pr_id=subscription.sprint_registered_pr_id "
            "LEFT JOIN sprints s ON s.sprint_id=registered.sprint_id "
            "WHERE subscription.subscription_id=?",
            (subscription_id,),
        ).fetchone()
        if current is None:
            raise KeyError(f"unknown active PR subscription: {subscription_id}")
        latest_failure = self.con.execute(
            "SELECT last_seen_at FROM pr_subscription_poll_failures "
            "WHERE subscription_id=? ORDER BY failure_id DESC LIMIT 1",
            (subscription_id,),
        ).fetchone()
        success_floor = (
            str(latest_failure["last_seen_at"])
            if latest_failure is not None
            and latest_failure["last_seen_at"] is not None
            else None
        )
        self.con.execute(
            "UPDATE pr_subscriptions SET updated_at=? WHERE subscription_id=?",
            (_next_db_stamp(self.con, success_floor), subscription_id),
        )
        latest = self.con.execute(
            "SELECT transition_key,normalized_state,observed_head_sha "
            "FROM pr_subscription_transitions WHERE subscription_id=? "
            "ORDER BY transition_id DESC LIMIT 1",
            (subscription_id,),
        ).fetchone()
        if (
            latest is not None
            and latest["normalized_state"] == state
            and latest["observed_head_sha"] == pull_request.head_sha
        ):
            return None
        parent_key = latest["transition_key"] if latest is not None else "root"
        transition_key = hashlib.sha256(
            f"{subscription_id}:{parent_key}:{state}:{pull_request.head_sha}".encode()
        ).hexdigest()
        transition_id = int(
            self.con.execute(
                "INSERT INTO pr_subscription_transitions "
                "(subscription_id,normalized_state,transition_key,"
                "observed_head_sha,evidence) VALUES (?,?,?,?,?)",
                (
                    subscription_id,
                    state,
                    transition_key,
                    pull_request.head_sha,
                    json.dumps(evidence, sort_keys=True),
                ),
            ).lastrowid
        )
        registered_pr_id = current["sprint_registered_pr_id"]
        if registered_pr_id is not None:
            self.con.execute(
                "INSERT INTO sprint_pr_transitions "
                "(registered_pr_id,normalized_state,transition_key,"
                "observed_head_sha,evidence) VALUES (?,?,?,?,?)",
                (
                    registered_pr_id,
                    state,
                    transition_key,
                    pull_request.head_sha,
                    json.dumps(evidence, sort_keys=True),
                ),
            )
        resolved_review_message_ids = self._route_transition(
            current,
            transition_key,
            state,
            pull_request,
            previous_head_sha=(
                str(latest["observed_head_sha"])
                if latest is not None and latest["observed_head_sha"] is not None
                else None
            ),
        )
        if state == "merged" and registered_pr_id is not None:
            self.review_loop.observe_merge_in_transaction(
                int(registered_pr_id),
                transition_key=transition_key,
                dispatch=dispatch,
                pause_receipts=pause_receipts,
            )
        if current["sprint_id"] is not None:
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,payload) "
                "VALUES (?,'pr.transition','system',?)",
                (
                    current["sprint_id"],
                    json.dumps(
                        {
                            "normalized_state": state,
                            "registered_pr_id": registered_pr_id,
                            "subscription_id": subscription_id,
                            "transition_id": transition_id,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            if (
                trigger == "registration"
                and state == "created"
                and pull_request.checks is None
            ):
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,payload) "
                    "VALUES (?,'pr.no_checks_observed','system',?)",
                    (
                        current["sprint_id"],
                        json.dumps(
                            {
                                "observed_head_sha": pull_request.head_sha,
                                "registered_pr_id": registered_pr_id,
                                "subscription_id": subscription_id,
                                "transition_id": transition_id,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
        return TransitionReceipt(
            transition_id,
            state,
            transition_key,
            resolved_review_message_ids,
        )

    def _route_transition(
        self,
        registered: sqlite3.Row,
        transition_key: str,
        state: str,
        pull_request: PullRequest,
        previous_head_sha: str | None,
    ) -> tuple[int, ...]:
        sprint_id = (
            int(registered["sprint_id"])
            if registered["sprint_id"] is not None
            else None
        )
        registered_pr_id = registered["sprint_registered_pr_id"]
        unit_rows = self.con.execute(
            "SELECT l.work_unit_id,u.disposition "
            "FROM sprint_pr_work_units l JOIN sprint_work_units u "
            "ON u.sprint_id=l.sprint_id AND u.work_unit_id=l.work_unit_id "
            "WHERE l.registered_pr_id=? AND l.superseded_at IS NULL "
            "ORDER BY l.work_unit_id",
            (registered_pr_id,),
        ).fetchall() if registered_pr_id is not None else []
        work_unit_id = (
            int(unit_rows[0]["work_unit_id"]) if len(unit_rows) == 1 else None
        )
        resolved_review_message_ids: tuple[int, ...] = ()
        if state == "closed" and work_unit_id is not None:
            resolved_review_message_ids = (
                self.liveness.resolve_review_requests_for_work_unit_in_transaction(
                    work_unit_id,
                    "registered_pr.closed_without_merge",
                )
            )
        elif (
            state == "merged"
            and work_unit_id is not None
            and unit_rows[0]["disposition"] != "merge_ready"
        ):
            resolved_review_message_ids = (
                self.liveness.resolve_review_requests_for_work_unit_in_transaction(
                    work_unit_id,
                    "registered_pr.merged_grant_bypassed",
                )
            )
        head_changed = (
            previous_head_sha is not None
            and previous_head_sha != pull_request.head_sha
        )
        if (
            head_changed
            and pull_request.head_sha is not None
            and state not in {"merged", "closed"}
            and sprint_id is not None
            and registered_pr_id is not None
            and len(unit_rows) == 1
            and unit_rows[0]["disposition"] == "in_review"
        ):
            invalidated_message_id = (
                self.review_loop.invalidate_review_request_for_head_change_in_transaction(
                    int(sprint_id),
                    int(registered_pr_id),
                    pull_request.head_sha,
                )
            )
            if invalidated_message_id is not None:
                resolved_review_message_ids = (invalidated_message_id,)
        if (
            head_changed
            and state not in {"merged", "closed"}
            and len(unit_rows) == 1
            and unit_rows[0]["disposition"] == "merge_ready"
        ):
            work_unit_id = int(unit_rows[0]["work_unit_id"])
            invalidated_message_id = self._invalidate_stale_approval(
                sprint_id,
                work_unit_id,
            )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='fixing',"
                "updated_at=datetime('now') WHERE work_unit_id=? "
                "AND disposition='merge_ready'",
                (work_unit_id,),
            )
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,payload) "
                "VALUES (?,'review.approval_invalidated','system',?)",
                (
                    sprint_id,
                    json.dumps(
                        {
                            "head_sha": pull_request.head_sha,
                            "invalidated_message_id": invalidated_message_id,
                            "previous_head_sha": previous_head_sha,
                            "registered_pr_id": registered_pr_id,
                            "transition_key": transition_key,
                            "work_unit_id": work_unit_id,
                        },
                        sort_keys=True,
                    ),
                ),
            )

        active_sprint = registered["lifecycle"] in {"armed", "paused"}
        if active_sprint:
            instructions = {
                "red": "Your active Sprint PR went red; fix the failing checks.",
                "green": (
                    "Your active Sprint PR is green; pass the baton to review "
                    "when ready."
                ),
                "closed": (
                    "Your active Sprint PR was closed without merge; tell the "
                    "Planner if this blocks the Sprint."
                ),
            }
        else:
            instructions = {
                "red": (
                    "Your PR went red outside an active Sprint; fix it if it still "
                    "needs attention, otherwise no action is needed."
                ),
                "green": (
                    "Your PR is green outside an active Sprint; no action is needed."
                ),
                "closed": (
                    "Your PR was closed without merge outside an active Sprint; "
                    "no action is needed unless the closure was unexpected."
                ),
            }
        if state in instructions:
            head = pull_request.head_sha or "unknown"
            owner_shell_id = int(registered["owner_shell_id"])
            body = (
                "GitHub PR event: "
                f"repository={registered['repository']}, "
                f"number={registered['pr_number']}, "
                f"head_sha={head}, event={state}. {instructions[state]}"
            )
            self.messages.send_to_shell_in_transaction(
                owner_shell_id,
                message_kind="notification",
                body=body,
                declared_type="re-enter",
                idempotency_key=(
                    f"pr-transition:{transition_key}:shell:{owner_shell_id}"
                ),
            )
        return resolved_review_message_ids

    def _invalidate_stale_approval(
        self,
        sprint_id: int,
        work_unit_id: int,
    ) -> int | None:
        approval = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='review.approved' "
            "AND json_extract(payload,'$.work_unit_id')=? "
            "ORDER BY event_id DESC LIMIT 1",
            (sprint_id, work_unit_id),
        ).fetchone()
        if approval is None:
            return None
        message_id = json.loads(approval["payload"]).get("message_id")
        if not isinstance(message_id, int):
            return None
        self.messages.resolve_in_transaction(message_id)
        return message_id

    def _poll_failed(
        self,
        registered: sqlite3.Row,
        trigger: str,
        error: GitHubReadError,
    ) -> None:
        subscription_id = int(registered["subscription_id"])
        detail = (str(error).strip() or error.__class__.__name__)[:500]
        with db_driver.write_transaction(self.con, "sprint.pr.poll_failure"):
            current = self.con.execute(
                "SELECT subscription.subscription_id,subscription.updated_at,"
                "registered.sprint_id "
                "FROM pr_subscriptions subscription "
                "LEFT JOIN sprint_registered_prs registered "
                "ON registered.registered_pr_id="
                "subscription.sprint_registered_pr_id "
                "WHERE subscription.subscription_id=?",
                (subscription_id,),
            ).fetchone()
            if current is None:
                return
            latest_failure = self.con.execute(
                "SELECT failure_id,failure_count,backoff_seconds,trigger,"
                "error_detail,last_seen_at "
                "FROM pr_subscription_poll_failures WHERE subscription_id=? "
                "ORDER BY failure_id DESC LIMIT 1",
                (subscription_id,),
            ).fetchone()
            same_streak = (
                latest_failure is not None
                and latest_failure["last_seen_at"] is not None
                and str(current["updated_at"])
                <= str(latest_failure["last_seen_at"])
            )
            failures = (
                int(latest_failure["failure_count"]) + 1 if same_streak else 1
            )
            delay = min(MAX_BACKOFF_SECONDS, PULSE_SECONDS * (2 ** failures))
            if "rate" in detail.lower():
                delay = max(delay, RATE_BACKOFF_SECONDS)
            if same_streak:
                delay = max(delay, float(latest_failure["backoff_seconds"]))
            coalesced = (
                same_streak
                and latest_failure["trigger"] == trigger
                and latest_failure["error_detail"] == detail
            )
            stamp_floor = str(current["updated_at"])
            if latest_failure is not None and latest_failure["last_seen_at"] is not None:
                stamp_floor = max(stamp_floor, str(latest_failure["last_seen_at"]))
            failure_stamp = _next_db_stamp(self.con, stamp_floor)
            if coalesced:
                self.con.execute(
                    "UPDATE pr_subscription_poll_failures "
                    "SET failure_count=failure_count+1,backoff_seconds=?,"
                    "repeat_count=repeat_count+1,last_seen_at=? "
                    "WHERE failure_id=?",
                    (delay, failure_stamp, latest_failure["failure_id"]),
                )
            else:
                self.con.execute(
                    "INSERT INTO pr_subscription_poll_failures "
                    "(subscription_id,failure_count,backoff_seconds,trigger,"
                    "error_detail,failed_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        subscription_id,
                        failures,
                        delay,
                        trigger,
                        detail,
                        failure_stamp,
                        failure_stamp,
                    ),
                )
            if current["sprint_id"] is not None and not coalesced:
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,payload) "
                    "VALUES (?,'pr.poll_failed','system',?)",
                    (
                        current["sprint_id"],
                        json.dumps(
                            {
                                "backoff_seconds": delay,
                                "error": detail,
                                "failure_count": failures,
                                "pr_number": int(registered["pr_number"]),
                                "registered_pr_id": registered[
                                    "sprint_registered_pr_id"
                                ],
                                "repository": str(registered["repository"]),
                                "subscription_id": subscription_id,
                                "trigger": trigger,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
        self._backoff[subscription_id] = _FailureBackoff(
            failures, self.monotonic() + delay
        )


class SprintPRWatcherService(threading.Thread):
    """Installation-level always-on service started with the engine API."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        repo_root: str | Path,
        pulse_seconds: float = PULSE_SECONDS,
        history_seconds: float = HEARTBEAT_HISTORY_SECONDS,
        reader_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(name="sprint-pr-watcher", daemon=True)
        if pulse_seconds <= 0:
            raise ValueError("watcher pulse must be positive")
        if history_seconds <= 0:
            raise ValueError("watcher heartbeat history interval must be positive")
        self.db_path = Path(db_path)
        self.repo_root = Path(repo_root)
        self.pulse_seconds = pulse_seconds
        self.history_seconds = history_seconds
        self.reader_factory = reader_factory
        self._wake = threading.Event()
        self._stop_event = threading.Event()

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def _pulse(
        self,
        watcher: SprintPRWatcher,
        heartbeat: WatcherHeartbeat,
        *,
        startup: bool,
    ) -> bool:
        heartbeat.beat(
            0,
            force_history=startup,
            history_eligible=startup,
        )
        observed = watcher.poll_once(
            startup=startup,
            repository_complete=heartbeat.beat,
        )
        if not observed and not startup:
            heartbeat.beat(0)
        return observed

    def run(self) -> None:
        try:
            with closing(db_driver.connect(self.db_path)) as con:
                heartbeat = WatcherHeartbeat(
                    con,
                    interval_seconds=self.pulse_seconds,
                    history_seconds=self.history_seconds,
                )
                watcher = SprintPRWatcher(
                    con,
                    repo_root=self.repo_root,
                    reader_factory=self.reader_factory,
                )
                startup = True
                while not self._stop_event.is_set():
                    try:
                        self._pulse(watcher, heartbeat, startup=startup)
                    except Exception as exc:  # noqa: BLE001 - keep service alive
                        print(f"sprint-pr-watcher: pulse failed ({exc})", flush=True)
                    startup = False
                    self._wake.wait(self.pulse_seconds)
                    self._wake.clear()
        except Exception as exc:  # noqa: BLE001 - surface startup failure
            print(f"sprint-pr-watcher: service failed ({exc})", flush=True)


_SERVICE_LOCK = threading.Lock()
_SERVICE: SprintPRWatcherService | None = None


def start_service(
    db_path: str | Path,
    *,
    repo_root: str | Path,
    **kwargs: Any,
) -> SprintPRWatcherService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE.is_alive():
            return _SERVICE
        _SERVICE = SprintPRWatcherService(
            db_path, repo_root=repo_root, **kwargs
        )
        _SERVICE.start()
        return _SERVICE


def notify_commit() -> bool:
    with _SERVICE_LOCK:
        service = _SERVICE
    if service is None or not service.is_alive():
        return False
    service.notify()
    return True
