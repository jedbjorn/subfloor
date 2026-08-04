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
from sprint_domain import SprintInvariantError
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


@dataclass(frozen=True)
class RegistrationReceipt:
    registered_pr_id: int
    created: bool


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
        """Register, then take the required initial snapshot after commit."""
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
        if receipt.created and row is not None:
            self._observe_rows((row,), "registration", ignore_backoff=True)
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
        with db_driver.write_transaction(self.con, "sprint.pr.observe"):
            receipt = self.observe_in_transaction(
                int(registered["subscription_id"]),
                pull_request,
                trigger=trigger,
            )
        if receipt is not None:
            self._backoff.pop(int(registered["subscription_id"]), None)
        return receipt

    def observe_in_transaction(
        self,
        subscription_id: int,
        pull_request: PullRequest,
        *,
        trigger: str,
        dispatch: bool = True,
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
            self._backoff.pop(subscription_id, None)
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
            "WHERE l.registered_pr_id=? ORDER BY l.work_unit_id",
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

        instructions = {
            "red": "Your PR went red; fix it.",
            "green": "Your PR is green; judge readiness and pass the baton to review.",
            "closed": (
                "Your PR was closed without merge; judge it and inform the Planner "
                "if this is a real problem."
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
        previous = self._backoff.get(subscription_id)
        failures = 1 if previous is None else previous.failures + 1
        detail = (str(error).strip() or error.__class__.__name__)[:500]
        delay = min(MAX_BACKOFF_SECONDS, PULSE_SECONDS * (2 ** failures))
        if "rate" in detail.lower():
            delay = max(delay, RATE_BACKOFF_SECONDS)
        self._backoff[subscription_id] = _FailureBackoff(
            failures, self.monotonic() + delay
        )
        with db_driver.write_transaction(self.con, "sprint.pr.poll_failure"):
            current = self.con.execute(
                "SELECT subscription.subscription_id,registered.sprint_id "
                "FROM pr_subscriptions subscription "
                "LEFT JOIN sprint_registered_prs registered "
                "ON registered.registered_pr_id="
                "subscription.sprint_registered_pr_id "
                "WHERE subscription.subscription_id=?",
                (subscription_id,),
            ).fetchone()
            if current is None:
                return
            self.con.execute(
                "INSERT INTO pr_subscription_poll_failures "
                "(subscription_id,failure_count,backoff_seconds,trigger,error_detail) "
                "VALUES (?,?,?,?,?)",
                (
                    subscription_id,
                    failures,
                    delay,
                    trigger,
                    detail,
                ),
            )
            if current["sprint_id"] is not None:
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
