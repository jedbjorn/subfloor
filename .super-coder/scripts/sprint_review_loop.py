"""Transactional Sprints v2 Developer and Reviewer handoff loop."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db_driver
import sprint_liveness
from github_pull_requests import GitHubPullRequestReader, PullRequest
from sprint_domain import (
    SprintAuthorityError,
    SprintInvariantError,
    SprintWorkUnitStore,
)
from sprint_message_delivery import MessageReceipt, SprintMessageStore


@dataclass(frozen=True)
class ReviewHandoffReceipt:
    work_unit_id: int
    message_id: int
    wake_id: int
    created: bool


@dataclass(frozen=True)
class ReviewOutcomeReceipt:
    work_unit_id: int
    conversation_id: str | None
    message_id: int
    wake_id: int
    disposition: str
    created: bool


@dataclass(frozen=True)
class MergeAuthorization:
    registered_pr_id: int
    repository: str
    pr_number: int
    head_sha: str


class SprintReviewLoopStore:
    """Compose review state from the existing authoritative Sprint records."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        repo_root: str | Path,
        reader_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.repo_root = Path(repo_root)
        self.reader_factory = reader_factory or (
            lambda repository: GitHubPullRequestReader(
                self.repo_root, repository=repository
            )
        )
        self.messages = SprintMessageStore(con)
        self.units = SprintWorkUnitStore(con)

    def request_review(
        self,
        sprint_id: int,
        registered_pr_id: int,
        developer_shell_id: int,
        *,
        readiness: str,
        idempotency_key: str,
    ) -> ReviewHandoffReceipt:
        """Record Developer readiness and actively hand the green PR to review."""
        readiness = self._required(readiness, "readiness judgment")
        idempotency_key = self._required(idempotency_key, "idempotency key", 255)
        with db_driver.write_transaction(self.con, "sprint.review.request"):
            lane = self._lane(sprint_id, registered_pr_id)
            self._require_armed(lane)
            self._require_developer(lane, developer_shell_id)
            transition = self._latest_transition(registered_pr_id)
            if transition["normalized_state"] != "green":
                raise SprintInvariantError("review handoff requires observed green checks")
            receipt = self.messages.send_in_transaction(
                sprint_id,
                to_participant_id=int(lane["reviewer_participant_id"]),
                from_participant_id=int(lane["developer_participant_id"]),
                work_unit_id=int(lane["work_unit_id"]),
                message_kind="review_request",
                body=readiness,
                actionable=True,
                declared_type="force-new",
                idempotency_key=idempotency_key,
            )
            if receipt.wake_id is None:
                raise SprintInvariantError("active review request has no wake")
            if not receipt.created:
                return self._handoff_receipt(lane, receipt)
            if lane["disposition"] not in {"active", "fixing"}:
                raise SprintInvariantError(
                    f"review handoff cannot start from {lane['disposition']}"
                )
            self._record_judgment(lane, developer_shell_id, "decision", readiness)
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='in_review',"
                "updated_at=datetime('now') WHERE work_unit_id=?",
                (lane["work_unit_id"],),
            )
            self._event(
                sprint_id,
                "review.requested",
                developer_shell_id,
                {
                    "head_sha": transition["observed_head_sha"],
                    "message_id": receipt.message_id,
                    "registered_pr_id": registered_pr_id,
                    "work_unit_id": int(lane["work_unit_id"]),
                },
            )
            return self._handoff_receipt(lane, receipt)

    def record_review(
        self,
        sprint_id: int,
        registered_pr_id: int,
        reviewer_shell_id: int,
        *,
        verdict: str,
        body: str,
        idempotency_key: str,
    ) -> ReviewOutcomeReceipt:
        """Record a Review verdict and enqueue its delivery-time wake."""
        if verdict not in {"changes_requested", "approved"}:
            raise ValueError("review verdict must be changes_requested or approved")
        body = self._required(body, "review body")
        idempotency_key = self._required(idempotency_key, "idempotency key", 255)
        with db_driver.write_transaction(self.con, "sprint.review.outcome"):
            lane = self._lane(sprint_id, registered_pr_id)
            self._require_armed(lane)
            if int(lane["reviewer_shell_id"]) != reviewer_shell_id:
                raise SprintAuthorityError("only the assigned Reviewer owns the verdict")
            review_message_id, requested_head = self._accepted_review_request(lane)
            disposition = "fixing" if verdict == "changes_requested" else "merge_ready"
            notification = (
                f"Review changes requested for PR #{lane['pr_number']}: {body}"
                if verdict == "changes_requested"
                else f"Review approved for PR #{lane['pr_number']}: {body}"
            )
            receipt = self.messages.send_in_transaction(
                sprint_id,
                to_participant_id=int(lane["developer_participant_id"]),
                from_participant_id=int(lane["reviewer_participant_id"]),
                work_unit_id=int(lane["work_unit_id"]),
                message_kind="notification",
                body=notification,
                actionable=verdict == "changes_requested",
                declared_type="re-enter",
                idempotency_key=idempotency_key,
            )
            if receipt.wake_id is None:
                raise SprintInvariantError("active review outcome has no wake")
            if not receipt.created:
                outcome = self._outcome_receipt(
                    lane, receipt, disposition
                )
            else:
                if lane["disposition"] != "in_review":
                    raise SprintInvariantError(
                        f"review verdict cannot start from {lane['disposition']}"
                    )
                transition = self._latest_transition(registered_pr_id)
                if transition["observed_head_sha"] != requested_head:
                    raise SprintInvariantError("review request is stale for the PR head")
                self._record_judgment(
                    lane,
                    reviewer_shell_id,
                    "issue" if verdict == "changes_requested" else "decision",
                    body,
                )
                self.con.execute(
                    "UPDATE sprint_work_units SET disposition=?,"
                    "updated_at=datetime('now') WHERE work_unit_id=?",
                    (disposition, lane["work_unit_id"]),
                )
                self._event(
                    sprint_id,
                    f"review.{verdict}",
                    reviewer_shell_id,
                    {
                        "head_sha": requested_head,
                        "message_id": receipt.message_id,
                        "registered_pr_id": registered_pr_id,
                        "work_unit_id": int(lane["work_unit_id"]),
                    },
                )
                outcome = self._outcome_receipt(
                    lane, receipt, disposition
                )
            sprint_liveness.SprintLivenessMonitor(self.con).resolve_in_transaction(
                review_message_id,
                f"review submitted: {verdict}",
            )
        return outcome

    def authorize_merge(
        self,
        sprint_id: int,
        registered_pr_id: int,
        developer_shell_id: int,
    ) -> MergeAuthorization:
        """Re-read GitHub, then authorize only the approved head with live green."""
        identity = self.con.execute(
            "SELECT repository,pr_number FROM sprint_registered_prs "
            "WHERE sprint_id=? AND registered_pr_id=?",
            (sprint_id, registered_pr_id),
        ).fetchone()
        if identity is None:
            raise KeyError(f"unknown registered PR: {registered_pr_id}")
        live = self.reader_factory(str(identity["repository"])).get(
            int(identity["pr_number"])
        )
        self._require_live_green(identity, live)
        with db_driver.write_transaction(self.con, "sprint.merge.authorize"):
            lane = self._lane(sprint_id, registered_pr_id)
            self._require_armed(lane)
            self._require_developer(lane, developer_shell_id)
            if int(lane["merge_grant_enabled"]) != 1:
                raise SprintInvariantError("Sprint merge grant is not enabled")
            if lane["disposition"] != "merge_ready":
                raise SprintInvariantError("merge requires an approved work unit")
            approved_head = self._approved_head(lane)
            if approved_head != live.head_sha:
                raise SprintInvariantError(
                    "review approval is stale for the live PR head"
                )
            self._event(
                sprint_id,
                "merge.authorized",
                developer_shell_id,
                {
                    "head_sha": live.head_sha,
                    "pr_number": live.number,
                    "registered_pr_id": registered_pr_id,
                    "work_unit_id": int(lane["work_unit_id"]),
                },
            )
        return MergeAuthorization(
            registered_pr_id,
            str(identity["repository"]),
            live.number,
            str(live.head_sha),
        )

    def observe_merge_in_transaction(
        self,
        registered_pr_id: int,
        *,
        transition_key: str,
        dispatch: bool = True,
    ) -> list[int]:
        if not self.con.in_transaction:
            raise RuntimeError("merge observation requires an active transaction")
        row = self.con.execute(
            "SELECT sprint_id FROM sprint_registered_prs WHERE registered_pr_id=?",
            (registered_pr_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown registered PR: {registered_pr_id}")
        unit_ids = [
            int(unit[0])
            for unit in self.con.execute(
                "SELECT work_unit_id FROM sprint_pr_work_units "
                "WHERE registered_pr_id=? ORDER BY work_unit_id",
                (registered_pr_id,),
            )
        ]
        return self.units.complete_from_merge_in_transaction(
            int(row["sprint_id"]),
            unit_ids,
            transition_key=transition_key,
            dispatch=dispatch,
        )

    def _lane(self, sprint_id: int, registered_pr_id: int) -> sqlite3.Row:
        rows = self.con.execute(
            "SELECT rp.sprint_id,rp.registered_pr_id,rp.repository,rp.pr_number,"
            "s.lifecycle,s.merge_grant_enabled,u.work_unit_id,u.disposition,"
            "u.assigned_shell_id,u.reviewer_shell_id,"
            "dev.participant_id AS developer_participant_id,"
            "rev.participant_id AS reviewer_participant_id "
            "FROM sprint_registered_prs rp "
            "JOIN sprints s ON s.sprint_id=rp.sprint_id "
            "JOIN sprint_pr_work_units link "
            "ON link.sprint_id=rp.sprint_id "
            "AND link.registered_pr_id=rp.registered_pr_id "
            "JOIN sprint_work_units u ON u.sprint_id=link.sprint_id "
            "AND u.work_unit_id=link.work_unit_id "
            "JOIN sprint_participants dev ON dev.sprint_id=rp.sprint_id "
            "AND dev.participant_id=rp.owner_participant_id "
            "AND dev.shell_id=u.assigned_shell_id AND dev.role='developer' "
            "JOIN sprint_participants rev ON rev.sprint_id=rp.sprint_id "
            "AND rev.shell_id=u.reviewer_shell_id AND rev.role='reviewer' "
            "WHERE rp.sprint_id=? AND rp.registered_pr_id=?",
            (sprint_id, registered_pr_id),
        ).fetchall()
        if not rows:
            raise KeyError(f"unknown registered PR: {registered_pr_id}")
        if len(rows) != 1:
            raise SprintInvariantError("review loop requires one work unit per PR")
        return rows[0]

    @staticmethod
    def _required(value: str, name: str, maximum: int = 8000) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{name} is empty")
        if len(value) > maximum:
            raise ValueError(
                f"{name} is {len(value)} characters; maximum is {maximum}"
            )
        return value

    @staticmethod
    def _require_armed(lane: sqlite3.Row) -> None:
        if lane["lifecycle"] != "armed":
            raise SprintInvariantError("review loop requires an armed Sprint")

    @staticmethod
    def _require_developer(lane: sqlite3.Row, shell_id: int) -> None:
        if int(lane["assigned_shell_id"]) != shell_id:
            raise SprintAuthorityError("only the owning Developer controls the PR")

    def _latest_transition(self, registered_pr_id: int) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT normalized_state,observed_head_sha FROM sprint_pr_transitions "
            "WHERE registered_pr_id=? ORDER BY transition_id DESC LIMIT 1",
            (registered_pr_id,),
        ).fetchone()
        if row is None:
            raise SprintInvariantError("registered PR has no observed state")
        if row["observed_head_sha"] is None:
            raise SprintInvariantError("registered PR has no exact observed head")
        return row

    def _accepted_review_request(self, lane: sqlite3.Row) -> tuple[int, str]:
        latest = self.con.execute(
            "SELECT message_id,disposition FROM wake_message WHERE sprint_id=? "
            "AND work_unit_id=? AND to_participant_id=? "
            "AND message_kind='review_request' "
            "ORDER BY message_id DESC LIMIT 1",
            (
                lane["sprint_id"],
                lane["work_unit_id"],
                lane["reviewer_participant_id"],
            ),
        ).fetchone()
        if latest is None or latest["disposition"] != "accepted":
            raise SprintInvariantError("review verdict requires an accepted request")
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='review.requested' "
            "AND json_extract(payload,'$.message_id')=? "
            "ORDER BY event_id DESC LIMIT 1",
            (lane["sprint_id"], latest["message_id"]),
        ).fetchone()
        if event is None:
            raise SprintInvariantError("accepted review request has no head evidence")
        head_sha = json.loads(event["payload"]).get("head_sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise SprintInvariantError("accepted review request has no exact head")
        return int(latest["message_id"]), head_sha

    def _record_judgment(
        self,
        lane: sqlite3.Row,
        shell_id: int,
        kind: str,
        body: str,
    ) -> None:
        participant_id = (
            lane["developer_participant_id"]
            if shell_id == int(lane["assigned_shell_id"])
            else lane["reviewer_participant_id"]
        )
        self.con.execute(
            "INSERT INTO sprint_judgments "
            "(sprint_id,participant_id,work_unit_id,kind,body) VALUES (?,?,?,?,?)",
            (
                self._sprint_id(lane),
                participant_id,
                lane["work_unit_id"],
                kind,
                body,
            ),
        )

    def _approved_head(self, lane: sqlite3.Row) -> str | None:
        row = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='review.approved' "
            "AND json_extract(payload,'$.work_unit_id')=? "
            "ORDER BY event_id DESC LIMIT 1",
            (self._sprint_id(lane), lane["work_unit_id"]),
        ).fetchone()
        return json.loads(row["payload"]).get("head_sha") if row is not None else None

    @staticmethod
    def _require_live_green(identity: sqlite3.Row, live: PullRequest) -> None:
        if live.number != int(identity["pr_number"]):
            raise SprintInvariantError("GitHub returned a different PR identity")
        if live.state != "OPEN" or live.head_sha is None:
            raise SprintInvariantError("merge requires an open PR with an exact head")
        if live.checks_failed or live.checks != "SUCCESS":
            raise SprintInvariantError("merge requires checks green at merge time")

    @staticmethod
    def _sprint_id(lane: sqlite3.Row) -> int:
        return int(lane["sprint_id"])

    @staticmethod
    def _handoff_receipt(
        lane: sqlite3.Row, receipt: MessageReceipt
    ) -> ReviewHandoffReceipt:
        return ReviewHandoffReceipt(
            int(lane["work_unit_id"]),
            receipt.message_id,
            int(receipt.wake_id),
            receipt.created,
        )

    @staticmethod
    def _outcome_receipt(
        lane: sqlite3.Row,
        receipt: MessageReceipt,
        disposition: str,
    ) -> ReviewOutcomeReceipt:
        return ReviewOutcomeReceipt(
            int(lane["work_unit_id"]),
            None,
            receipt.message_id,
            int(receipt.wake_id),
            disposition,
            receipt.created,
        )

    def _event(
        self,
        sprint_id: int,
        event_type: str,
        shell_id: int,
        payload: dict[str, Any],
    ) -> None:
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (?,?,'participant',?,?)",
            (sprint_id, event_type, shell_id, json.dumps(payload, sort_keys=True)),
        )
