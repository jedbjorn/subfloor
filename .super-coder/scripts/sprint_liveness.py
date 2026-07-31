"""Armed-only Sprint liveness evaluation and escalation policy.

Accepted actionable Sprint messages are durable work expectations.  This
module evaluates their evidence on a five-minute cadence, keeps one restart-
safe silence episode per expectation, nudges the worker once after ten minutes
of ambiguous silence, and escalates once to the Planner after a further ten
minutes.  External delivery remains the wake outbox's responsibility.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import db_driver
import run as run_mod
import shell_liveness
import sprint_participant_chats
from sprint_message_delivery import SprintMessageStore

EVALUATION_INTERVAL = timedelta(minutes=5)
GRACE_WINDOW = timedelta(minutes=10)
ESCALATION_WINDOW = timedelta(minutes=10)

_NATIVE_EVIDENCE_EVENTS = (
    "session.started",
    "run.started",
    "assistant.delta",
    "tool.started",
    "tool.completed",
    "permission.requested",
    "input.requested",
    "usage",
    "run.completed",
    "run.failed",
    "run.interrupted",
    "run.unknown",
)
_PROVIDER_ALIASES = {"kimi": "moonshot", "kimi-for-coding": "moonshot"}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _provider(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class Evidence:
    key: str
    kind: str
    observed_at: datetime
    strength: str
    detail: str

    def summary(self) -> dict[str, str]:
        return {
            "key": self.key,
            "kind": self.kind,
            "observed_at": _stamp(self.observed_at),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EvidenceSnapshot:
    strong: Evidence | None
    supporting: tuple[Evidence, ...]
    failure: Evidence | None
    unreadable: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationOutcome:
    message_id: int
    action: str
    silence_episode: int


@dataclass(frozen=True)
class QuotaState:
    provider: str | None
    captured_at: datetime | None
    exhausted: bool
    healthy: bool
    unreadable: str | None
    key: str | None


class SprintEvidenceCollector:
    """Collect deterministic strong evidence plus cautious supporting signals."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        process_probe: Callable[
            [sqlite3.Row, datetime, datetime],
            tuple[Evidence | None, Evidence | None, str | None],
        ]
        | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.process_probe = process_probe or self._launch_process_probe

    def collect(self, expectation: sqlite3.Row, now: datetime) -> EvidenceSnapshot:
        accepted_at = _parse(expectation["accepted_at"])
        participant_id = int(expectation["participant_id"])
        strong = self._latest_strong(participant_id, accepted_at)
        failures = list(self._terminal_failures(participant_id, accepted_at))
        supporting: list[Evidence] = []
        unreadable: list[str] = []

        participant = self.con.execute(
            "SELECT p.*,sh.shortname FROM sprint_participants p "
            "JOIN shells sh ON sh.shell_id=p.shell_id "
            "WHERE p.participant_id=?",
            (participant_id,),
        ).fetchone()
        if participant is None:
            unreadable.append("participant row disappeared")
        else:
            process_support, process_failure, process_unreadable = self.process_probe(
                participant, accepted_at, now
            )
            if process_support is not None:
                supporting.append(process_support)
            if process_failure is not None:
                failures.append(process_failure)
            if process_unreadable:
                unreadable.append(process_unreadable)

        git_support = self._git_support(participant_id, accepted_at, now)
        if git_support is not None:
            supporting.append(git_support)

        quota = self.quota_state_for_participant(participant_id, now)
        if quota.exhausted and quota.captured_at and quota.key:
            failures.append(
                Evidence(
                    quota.key,
                    "quota.exhausted",
                    now,
                    "failure",
                    f"provider {quota.provider} has an exhausted current quota window",
                )
            )
        elif quota.healthy and quota.captured_at and quota.key:
            supporting.append(
                Evidence(
                    quota.key,
                    "quota.available",
                    quota.captured_at,
                    "supporting",
                    f"provider {quota.provider} has a non-exhausted current snapshot",
                )
            )
        elif quota.unreadable:
            unreadable.append(quota.unreadable)

        failure = max(failures, key=lambda item: item.observed_at, default=None)
        return EvidenceSnapshot(
            strong=strong,
            supporting=tuple(sorted(supporting, key=lambda item: item.key)),
            failure=failure,
            unreadable=tuple(sorted(set(unreadable))),
        )

    def quota_state_for_participant(
        self, participant_id: int, now: datetime
    ) -> QuotaState:
        row = self.con.execute(
            "SELECT c.provider FROM sprint_participants p "
            "LEFT JOIN conversations c ON c.conversation_id=p.current_conversation_id "
            "WHERE p.participant_id=?",
            (participant_id,),
        ).fetchone()
        return self.quota_state(_provider(row["provider"] if row else None), now)

    def quota_state(self, provider: str | None, now: datetime) -> QuotaState:
        provider = _provider(provider)
        if provider is None:
            return QuotaState(None, None, False, False, "quota provider is unknown", None)
        rows = self.con.execute(
            "SELECT w.window_pk,w.used_percent,w.used,w.limit_value,w.resets_at,"
            "w.captured_at,w.status FROM harness_quota_window w "
            "JOIN harness_quota_account a ON a.account_pk=w.account_pk "
            "WHERE a.provider=? AND datetime(w.captured_at)=("
            "SELECT MAX(datetime(latest.captured_at)) "
            "FROM harness_quota_window latest "
            "JOIN harness_quota_account latest_account "
            "ON latest_account.account_pk=latest.account_pk "
            "WHERE latest_account.provider=?) ORDER BY w.window_pk",
            (provider, provider),
        ).fetchall()
        if not rows:
            return QuotaState(
                provider, None, False, False, f"quota snapshot unavailable for {provider}", None
            )
        captured_at = max(_parse(row["captured_at"]) for row in rows)
        statuses = {str(row["status"]) for row in rows}
        if statuses != {"ok"}:
            return QuotaState(
                provider,
                captured_at,
                False,
                False,
                f"quota snapshot for {provider} is unreadable ({','.join(sorted(statuses))})",
                f"quota:{provider}:{_stamp(captured_at)}",
            )

        fresh = now - captured_at <= GRACE_WINDOW

        def active(row: sqlite3.Row) -> bool:
            return row["resets_at"] is None or _parse(row["resets_at"]) > now

        exhausted = any(
            active(row)
            and (fresh or row["resets_at"] is not None)
            and (
                (row["used_percent"] is not None and float(row["used_percent"]) >= 100)
                or (
                    row["used"] is not None
                    and row["limit_value"] is not None
                    and int(row["limit_value"]) > 0
                    and int(row["used"]) >= int(row["limit_value"])
                )
            )
            for row in rows
        )
        key = "quota:{}:{}:{}".format(
            provider,
            _stamp(captured_at),
            ",".join(str(row["window_pk"]) for row in rows),
        )
        if not exhausted and not fresh:
            return QuotaState(
                provider,
                captured_at,
                False,
                False,
                f"quota snapshot for {provider} is stale",
                key,
            )
        return QuotaState(provider, captured_at, exhausted, not exhausted, None, key)

    def _latest_strong(
        self, participant_id: int, accepted_at: datetime
    ) -> Evidence | None:
        threshold = _stamp(accepted_at)
        candidates: list[Evidence] = []
        event_types = ",".join("?" for _ in _NATIVE_EVIDENCE_EVENTS)
        row = self.con.execute(
            "SELECT e.event_id,e.event_type,e.created_at FROM conversation_events e "
            "JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=e.conversation_id "
            f"WHERE pc.sprint_participant_id=? AND e.event_type IN ({event_types}) "
            "AND e.created_at>=? ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1",
            (participant_id, *_NATIVE_EVIDENCE_EVENTS, threshold),
        ).fetchone()
        if row is not None:
            candidates.append(
                Evidence(
                    f"conversation.event:{row['event_id']}",
                    str(row["event_type"]),
                    _parse(row["created_at"]),
                    "strong",
                    "normalized native conversation event",
                )
            )

        row = self.con.execute(
            "SELECT r.run_id,r.heartbeat_at FROM conversation_runs r "
            "JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=r.conversation_id "
            "WHERE pc.sprint_participant_id=? AND r.heartbeat_at>=? "
            "AND r.harness_session_after IS NOT NULL AND r.runner_ref IS NOT NULL "
            "ORDER BY r.heartbeat_at DESC,r.run_id DESC LIMIT 1",
            (participant_id, threshold),
        ).fetchone()
        if row is not None:
            candidates.append(
                Evidence(
                    f"run.heartbeat:{row['run_id']}:{row['heartbeat_at']}",
                    "run.heartbeat",
                    _parse(row["heartbeat_at"]),
                    "strong",
                    "fresh broker lease heartbeat bound to an exact native run",
                )
            )

        row = self.con.execute(
            "SELECT message_id,message_kind,created_at FROM sprint_messages "
            "WHERE from_participant_id=? AND created_at>=? "
            "ORDER BY created_at DESC,message_id DESC LIMIT 1",
            (participant_id, threshold),
        ).fetchone()
        if row is not None:
            candidates.append(
                Evidence(
                    f"sprint.message:{row['message_id']}",
                    f"sprint_message.{row['message_kind']}",
                    _parse(row["created_at"]),
                    "strong",
                    "durable Sprint message authored by the participant",
                )
            )

        participant = self.con.execute(
            "SELECT sprint_id,shell_id FROM sprint_participants WHERE participant_id=?",
            (participant_id,),
        ).fetchone()
        if participant is not None:
            row = self.con.execute(
                "SELECT event_id,event_type,created_at FROM sprint_events "
                "WHERE sprint_id=? AND actor_kind='participant' AND actor_shell_id=? "
                "AND created_at>=? ORDER BY created_at DESC,event_id DESC LIMIT 1",
                (participant["sprint_id"], participant["shell_id"], threshold),
            ).fetchone()
            if row is not None:
                candidates.append(
                    Evidence(
                        f"sprint.event:{row['event_id']}",
                        str(row["event_type"]),
                        _parse(row["created_at"]),
                        "strong",
                        "durable participant Sprint state transition",
                    )
                )

        row = self.con.execute(
            "SELECT t.transition_id,t.normalized_state,t.observed_at "
            "FROM sprint_pr_transitions t JOIN sprint_registered_prs pr "
            "ON pr.registered_pr_id=t.registered_pr_id "
            "WHERE pr.owner_participant_id=? AND t.observed_at>=? "
            "ORDER BY t.observed_at DESC,t.transition_id DESC LIMIT 1",
            (participant_id, threshold),
        ).fetchone()
        if row is not None:
            candidates.append(
                Evidence(
                    f"pr.transition:{row['transition_id']}",
                    f"pr.{row['normalized_state']}",
                    _parse(row["observed_at"]),
                    "strong",
                    "registered PR transition",
                )
            )

        row = self.con.execute(
            "SELECT target_id,latest_head_sha,last_seen_at "
            "FROM conversation_git_targets target "
            "JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=target.conversation_id "
            "WHERE pc.sprint_participant_id=? AND target.last_seen_at>=? "
            "ORDER BY target.last_seen_at DESC,target.target_id LIMIT 1",
            (participant_id, threshold),
        ).fetchone()
        if row is not None:
            candidates.append(
                Evidence(
                    f"git.head:{row['target_id']}:{row['latest_head_sha']}",
                    "git.head",
                    _parse(row["last_seen_at"]),
                    "strong",
                    "durable local Git head observation",
                )
            )
        return max(candidates, key=lambda item: (item.observed_at, item.key), default=None)

    def _terminal_failures(
        self, participant_id: int, accepted_at: datetime
    ) -> tuple[Evidence, ...]:
        row = self.con.execute(
            "SELECT r.run_id,r.state,r.ended_at,COALESCE(r.error_code,'') error_code "
            "FROM conversation_runs r JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=r.conversation_id "
            "WHERE pc.sprint_participant_id=? AND r.state IN ('failed','unknown') "
            "AND r.ended_at>=? ORDER BY r.ended_at DESC,r.run_id DESC LIMIT 1",
            (participant_id, _stamp(accepted_at)),
        ).fetchone()
        if row is None:
            return ()
        return (
            Evidence(
                f"run.failure:{row['run_id']}:{row['state']}",
                f"run.{row['state']}",
                _parse(row["ended_at"]),
                "failure",
                f"proven terminal native run {row['state']} {row['error_code']}".strip(),
            ),
        )

    def _git_support(
        self, participant_id: int, accepted_at: datetime, now: datetime
    ) -> Evidence | None:
        threshold = max(accepted_at, now - GRACE_WINDOW)
        row = self.con.execute(
            "SELECT target.target_id,target.last_seen_at "
            "FROM conversation_git_targets target "
            "JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=target.conversation_id "
            "WHERE pc.sprint_participant_id=? AND target.last_seen_at>=? "
            "ORDER BY target.last_seen_at DESC,target.target_id LIMIT 1",
            (participant_id, _stamp(threshold)),
        ).fetchone()
        if row is None:
            return None
        return Evidence(
            f"worktree.observed:{row['target_id']}:{row['last_seen_at']}",
            "worktree.observed",
            _parse(row["last_seen_at"]),
            "supporting",
            "worktree Git target was observed",
        )

    def _launch_process_probe(
        self, participant: sqlite3.Row, accepted_at: datetime, now: datetime
    ) -> tuple[Evidence | None, Evidence | None, str | None]:
        try:
            claim = self.con.execute(
                "SELECT pid,start_ticks,worktree,launched_at "
                "FROM shell_launch_records WHERE shell_id=?",
                (participant["shell_id"],),
            ).fetchone()
        except sqlite3.OperationalError:
            return None, None, "shell launch evidence is unavailable"
        if claim is None or _parse(claim["launched_at"]) < accepted_at:
            return None, None, None
        claim_dict = dict(claim)
        if shell_liveness.claim_live(claim_dict):
            return (
                Evidence(
                    f"process.present:{claim['pid']}:{claim['start_ticks']}",
                    "process.present",
                    now,
                    "supporting",
                    "accepted work's claimed harness process is present",
                ),
                None,
                None,
            )
        return (
            None,
            Evidence(
                f"process.missing:{claim['pid']}:{claim['start_ticks']}",
                "process.missing",
                now,
                "failure",
                "accepted work's exact claimed harness process is missing",
            ),
            None,
        )


class PlannerEscalationRouter:
    """Move escalation delivery only when the Planner's provider is exhausted."""

    def __init__(self, con: sqlite3.Connection, collector: SprintEvidenceCollector) -> None:
        self.con = con
        self.collector = collector

    def prepare(
        self,
        sprint_id: int,
        *,
        expectation_message_id: int,
        silence_episode: int,
        now: datetime,
        reason: str,
    ) -> tuple[int, str]:
        planner = self.con.execute(
            "SELECT p.participant_id,p.harness,p.model,p.effort,sh.flavor,"
            "c.harness current_harness,c.provider current_provider "
            "FROM sprint_participants p JOIN shells sh ON sh.shell_id=p.shell_id "
            "LEFT JOIN conversations c ON c.conversation_id=p.current_conversation_id "
            "WHERE p.sprint_id=? AND p.role='planner'",
            (sprint_id,),
        ).fetchone()
        if planner is None:
            raise RuntimeError("armed Sprint has no Planner participant")
        participant_id = int(planner["participant_id"])
        primary = self.collector.quota_state(_provider(planner["current_provider"]), now)
        if not primary.exhausted:
            return participant_id, "primary"

        candidates = self.con.execute(
            "SELECT harness,model FROM flavor_defaults WHERE flavor=? "
            "AND harness<>? ORDER BY is_default DESC,harness",
            (planner["flavor"], planner["current_harness"] or planner["harness"]),
        ).fetchall()
        for candidate in candidates:
            candidate_provider = _provider(
                run_mod.session_provider(candidate["harness"], candidate["model"])
            )
            if candidate_provider == primary.provider:
                continue
            if self.collector.quota_state(candidate_provider, now).exhausted:
                continue
            sprint_participant_chats.create_planner_fallback(
                self.con,
                participant_id=participant_id,
                reason=reason,
                harness=candidate["harness"],
                model=candidate["model"],
                effort=planner["effort"],
                idempotency_key=(
                    f"sprint:{sprint_id}:liveness:{expectation_message_id}:"
                    f"episode:{silence_episode}:planner-fallback"
                ),
            )
            return participant_id, f"fallback:{candidate['harness']}"
        return participant_id, "unavailable"


class SprintLivenessMonitor:
    """Evaluate due expectations and atomically commit nudge/escalation facts."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        now: Callable[[], datetime] | None = None,
        collector: SprintEvidenceCollector | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.collector = collector or SprintEvidenceCollector(con)
        self.messages = SprintMessageStore(con)
        self.router = PlannerEscalationRouter(con, self.collector)

    def evaluate(self, sprint_id: int) -> tuple[EvaluationOutcome, ...]:
        now = self.now().astimezone(timezone.utc)
        if not self._armed(sprint_id):
            return ()
        due = self.con.execute(
            "SELECT e.* FROM sprint_liveness_expectations e "
            "JOIN sprint_messages m ON m.message_id=e.message_id "
            "WHERE e.sprint_id=? AND e.resolved_at IS NULL "
            "AND e.next_evaluation_at IS NOT NULL "
            "AND e.next_evaluation_at<=? AND m.disposition='accepted' "
            "ORDER BY e.message_id",
            (sprint_id, _stamp(now)),
        ).fetchall()
        outcomes = []
        for expectation in due:
            snapshot = self.collector.collect(expectation, now)
            outcome = self._apply(int(expectation["message_id"]), snapshot, now)
            if outcome is not None:
                outcomes.append(outcome)
        return tuple(outcomes)

    def resolve(self, message_id: int, resolution: str) -> bool:
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("liveness expectation resolution is empty")
        with db_driver.write_transaction(self.con, "sprint.liveness.resolve"):
            changed = self.con.execute(
                "UPDATE sprint_liveness_expectations SET resolved_at=?,resolution=?,"
                "next_evaluation_at=NULL WHERE message_id=? AND resolved_at IS NULL",
                (_stamp(self.now()), resolution, message_id),
            ).rowcount
        return changed == 1

    def _apply(
        self, message_id: int, snapshot: EvidenceSnapshot, now: datetime
    ) -> EvaluationOutcome | None:
        with db_driver.write_transaction(self.con, "sprint.liveness.evaluate"):
            row = self.con.execute(
                "SELECT e.* FROM sprint_liveness_expectations e "
                "JOIN sprints s ON s.sprint_id=e.sprint_id "
                "WHERE e.message_id=? AND e.resolved_at IS NULL "
                "AND s.lifecycle='armed' AND e.next_evaluation_at<=?",
                (message_id, _stamp(now)),
            ).fetchone()
            if row is None:
                return None
            episode = int(row["silence_episode"])
            last_strong_at = _parse(row["last_strong_at"])
            fresh_strong = self._fresh_strong(row, snapshot.strong)
            failure = snapshot.failure

            if fresh_strong is not None and (
                failure is None or fresh_strong.observed_at > failure.observed_at
            ):
                episode += 1
                self.con.execute(
                    "UPDATE sprint_liveness_expectations SET last_strong_at=?,"
                    "last_strong_key=?,silence_episode=?,nudge_at=NULL,"
                    "escalated_at=NULL,last_failure_key=NULL,last_evaluated_at=?,"
                    "next_evaluation_at=?,last_supporting=?,last_unreadable=? "
                    "WHERE message_id=?",
                    (
                        _stamp(fresh_strong.observed_at),
                        fresh_strong.key,
                        episode,
                        _stamp(now),
                        _stamp(now + EVALUATION_INTERVAL),
                        _json([item.summary() for item in snapshot.supporting]),
                        _json(list(snapshot.unreadable)),
                        message_id,
                    ),
                )
                return EvaluationOutcome(message_id, "strong-evidence", episode)

            if failure is not None and (
                failure.observed_at >= last_strong_at
                or failure.kind in {"quota.exhausted", "process.missing"}
            ):
                if row["escalated_at"] is None:
                    return self._escalate(row, episode, snapshot, now, failure=failure)

            accepted_silence = now - last_strong_at
            if row["nudge_at"] is None and accepted_silence >= GRACE_WINDOW:
                receipt = self.messages.send_in_transaction(
                    int(row["sprint_id"]),
                    to_participant_id=int(row["participant_id"]),
                    message_kind="nudge",
                    body=(
                        f"Liveness check for accepted Sprint message #{message_id}: "
                        "please confirm activity or record the current blocker."
                    ),
                    idempotency_key=(
                        f"liveness:{message_id}:episode:{episode}:nudge"
                    ),
                    actionable=False,
                    active=True,
                )
                self._event(
                    int(row["sprint_id"]),
                    "liveness.nudged",
                    {
                        "expectation_message_id": message_id,
                        "silence_episode": episode,
                        "nudge_message_id": receipt.message_id,
                    },
                )
                self._update_observation(
                    message_id,
                    now,
                    snapshot,
                    nudge_at=_stamp(now),
                    failure_key=failure.key if failure else None,
                )
                return EvaluationOutcome(message_id, "nudged", episode)

            nudge_at = _parse(row["nudge_at"]) if row["nudge_at"] else None
            if (
                row["escalated_at"] is None
                and nudge_at is not None
                and now - nudge_at >= ESCALATION_WINDOW
                and not snapshot.supporting
            ):
                return self._escalate(row, episode, snapshot, now, failure=None)

            self._update_observation(
                message_id,
                now,
                snapshot,
                failure_key=failure.key if failure else None,
            )
            action = "supporting-evidence" if snapshot.supporting else "observed"
            return EvaluationOutcome(message_id, action, episode)

    def _escalate(
        self,
        row: sqlite3.Row,
        episode: int,
        snapshot: EvidenceSnapshot,
        now: datetime,
        *,
        failure: Evidence | None,
    ) -> EvaluationOutcome:
        reason = failure.detail if failure else "continued silence after one nudge"
        planner_id, route = self.router.prepare(
            int(row["sprint_id"]),
            expectation_message_id=int(row["message_id"]),
            silence_episode=episode,
            now=now,
            reason=f"Liveness escalation delivery: {reason}",
        )
        body = _json(
            {
                "expectation_message_id": int(row["message_id"]),
                "participant_id": int(row["participant_id"]),
                "silence_episode": episode,
                "accepted_at": row["accepted_at"],
                "last_strong_at": row["last_strong_at"],
                "reason": reason,
                "failure": failure.summary() if failure else None,
                "supporting_evidence": [
                    item.summary() for item in snapshot.supporting
                ],
                "unreadable_signals": list(snapshot.unreadable),
                "planner_delivery_route": route,
                "pause_option": route == "unavailable",
            }
        )
        receipt = self.messages.send_in_transaction(
            int(row["sprint_id"]),
            to_participant_id=planner_id,
            message_kind="escalation",
            body=body,
            idempotency_key=(
                f"liveness:{row['message_id']}:episode:{episode}:planner-escalation"
            ),
            actionable=False,
            active=route != "unavailable",
        )
        event_type = (
            "liveness.escalation_delivery_unavailable"
            if route == "unavailable"
            else "liveness.escalated"
        )
        self._event(
            int(row["sprint_id"]),
            event_type,
            {
                "expectation_message_id": int(row["message_id"]),
                "silence_episode": episode,
                "escalation_message_id": receipt.message_id,
                "planner_delivery_route": route,
            },
        )
        self._update_observation(
            int(row["message_id"]),
            now,
            snapshot,
            escalated_at=_stamp(now),
            failure_key=failure.key if failure else None,
        )
        return EvaluationOutcome(int(row["message_id"]), "escalated", episode)

    def _update_observation(
        self,
        message_id: int,
        now: datetime,
        snapshot: EvidenceSnapshot,
        *,
        nudge_at: str | None = None,
        escalated_at: str | None = None,
        failure_key: str | None = None,
    ) -> None:
        assignments = [
            "last_evaluated_at=?",
            "next_evaluation_at=?",
            "last_supporting=?",
            "last_unreadable=?",
            "last_failure_key=?",
        ]
        values: list[Any] = [
            _stamp(now),
            _stamp(now + EVALUATION_INTERVAL),
            _json([item.summary() for item in snapshot.supporting]),
            _json(list(snapshot.unreadable)),
            failure_key,
        ]
        if nudge_at is not None:
            assignments.append("nudge_at=?")
            values.append(nudge_at)
        if escalated_at is not None:
            assignments.append("escalated_at=?")
            values.append(escalated_at)
        values.append(message_id)
        self.con.execute(
            f"UPDATE sprint_liveness_expectations SET {','.join(assignments)} "
            "WHERE message_id=?",
            values,
        )

    @staticmethod
    def _fresh_strong(row: sqlite3.Row, evidence: Evidence | None) -> Evidence | None:
        if evidence is None:
            return None
        current_at = _parse(row["last_strong_at"])
        if evidence.observed_at > current_at:
            return evidence
        if evidence.observed_at == current_at and evidence.key != row["last_strong_key"]:
            return evidence
        return None

    def _armed(self, sprint_id: int) -> bool:
        row = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        return row is not None and row["lifecycle"] == "armed"

    def _event(self, sprint_id: int, event_type: str, payload: dict[str, Any]) -> None:
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,payload) VALUES (? ,?,'system',?)",
            (sprint_id, event_type, _json(payload)),
        )
