"""Pure, bounded Sprint progress-carrier health projection.

The projector reads one caller-owned SQLite snapshot.  It never performs an
external call, mutates lifecycle state, delivers a wake, or stores a copied
health result.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sprint_pr_watcher
import sprint_runtime

ATTENTION_AFTER = timedelta(minutes=30)
CI_STUCK_AFTER = timedelta(minutes=90)
QUOTA_FRESH_FOR = timedelta(minutes=10)
MAX_EVENTS = 2000
MAX_MESSAGE_REFS = 100
MAX_ROOT_CAUSES = 100
MAX_UNREADABLE_SIGNALS = 100

_SEVERITY = {
    "staged": 0,
    "terminal": 0,
    "paused": 0,
    "progressing": 1,
    "waiting_dependency": 2,
    "waiting_external": 3,
    "waiting_decision": 4,
    "attention": 5,
    "infrastructure": 6,
}
_ROOT_CONDITIONS = frozenset({"infrastructure", "attention", "waiting_decision"})
_LIVE_RUN_STATES = frozenset({"starting", "running"})
_PICKUP_RUN_STATES = frozenset({"leased", "starting", "running"})
_TERMINAL_UNITS = frozenset({"completed", "cancelled"})
_ACTIVE_PR_STATES = frozenset({"created", "pending", "red", "green"})
_EVENT_STAGE = {
    "planned": frozenset({"work_unit.created", "work_unit.replanned"}),
    "ready": frozenset({"work_unit.ready"}),
    "active": frozenset({"work_unit.accepted"}),
    "blocked": frozenset(),
    "in_review": frozenset({"review.requested"}),
    "fixing": frozenset({"review.changes_requested"}),
    "merge_ready": frozenset({"review.approved"}),
    "completed": frozenset({"work_unit.completed"}),
    "cancelled": frozenset({"work_unit.cancelled"}),
}
_NEXT_EVENT = {
    "planned": ("work_unit_ready", "the runtime dispatches the ready work unit"),
    "ready": ("assignment_accepted", "the Developer accepts or declines the assignment"),
    "active": ("developer_evidence", "Developer activity or a registered PR transition"),
    "blocked": ("blocker_resolved", "the blocker is answered or the Planner changes disposition"),
    "in_review": ("review_verdict", "the assigned Reviewer records a verdict"),
    "fixing": ("replacement_pr_transition", "Developer activity or a replacement PR transition"),
    "merge_ready": ("merge_observed", "the approved head is observed merged"),
    "completed": ("none", "the work unit is complete"),
    "cancelled": ("none", "the work unit is cancelled"),
}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _required_stamp(value: str | None) -> datetime:
    parsed = _parse(value)
    if parsed is None:
        raise ValueError("required durable timestamp is missing")
    return parsed


def _max_stamp(*values: datetime | None) -> datetime:
    present = [value for value in values if value is not None]
    if not present:
        raise ValueError("at least one timestamp is required")
    return max(present)


def _age(now: datetime, since: datetime) -> int:
    return max(0, int((now - since).total_seconds()))


@dataclass(frozen=True)
class Evidence:
    kind: str
    row_id: int
    at: datetime
    rank: int

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.row_id, "at": _stamp(self.at)}


@dataclass
class Candidate:
    condition: str
    cause: str
    since: datetime | None
    owner: dict[str, Any]
    last_evidence: Evidence | None
    next_code: str
    next_detail: str
    work_unit_id: int | None = None
    scope: str = "work_unit"
    message_refs: list[int] = field(default_factory=list)
    waiting_on: list[int] = field(default_factory=list)
    roots: list[int] = field(default_factory=list)
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    activity: str = "unknown"
    capacity: dict[str, Any] | None = None
    root_key: int | None = None

    def unit_public(self, now: datetime) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "cause": self.cause,
            "since": _stamp(self.since),
            "age_seconds": _age(now, self.since) if self.since else None,
            "owner": self.owner,
            "last_evidence": (
                self.last_evidence.public() if self.last_evidence else None
            ),
            "next_expected_event": {
                "code": self.next_code,
                "detail": self.next_detail,
            },
            "waiting_on_work_unit_ids": sorted(set(self.waiting_on)),
            "root_work_unit_ids": sorted(set(self.roots)),
            "message_refs": [
                {"message_id": value} for value in sorted(set(self.message_refs))
            ],
            "activity": self.activity,
            "capacity": self.capacity,
            "unreadable_signals": self.unreadable,
        }

    def root_public(self, now: datetime) -> dict[str, Any]:
        if self.scope == "work_unit":
            root_id = f"work_unit:{self.work_unit_id}:{self.cause}"
        elif self.root_key is not None:
            root_id = f"sprint:closeout:{self.root_key or 0}"
        else:
            root_id = f"sprint:message:{self.message_refs[0]}"
        return {
            "root_id": root_id,
            "scope": self.scope,
            "work_unit_id": self.work_unit_id,
            "condition": self.condition,
            "cause": self.cause,
            "since": _stamp(self.since),
            "age_seconds": _age(now, self.since) if self.since else None,
            "message_refs": [
                {"message_id": value} for value in sorted(set(self.message_refs))
            ],
            "owner": self.owner,
            "last_evidence": (
                self.last_evidence.public() if self.last_evidence else None
            ),
            "next_expected_event": {
                "code": self.next_code,
                "detail": self.next_detail,
            },
        }


class SprintHealthProjection:
    """Derive total Sprint and work-unit health from one read snapshot."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.sprint: sqlite3.Row | None = None
        self.participants: dict[int, dict[str, Any]] = {}
        self.units: dict[int, sqlite3.Row] = {}
        self.dependencies: dict[int, list[int]] = defaultdict(list)
        self.events: list[dict[str, Any]] = []
        self.events_by_unit: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.messages: list[dict[str, Any]] = []
        self.messages_by_unit: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.open_replies_by_unit: dict[int | None, list[dict[str, Any]]] = (
            defaultdict(list)
        )
        self.stage_message_ids: dict[int, int] = {}
        self.closeout_event: dict[str, Any] | None = None
        self.closeout_message_ids: set[int] = set()
        self.sprint_reply_root_count = 0
        self.sprint_reply_attention_count = 0
        self.prs: dict[int, dict[str, Any]] = {}
        self.unreadable: list[dict[str, Any]] = []
        self.unreadable_by_unit: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.runtime: dict[str, Any] = {}
        self.watcher: dict[str, Any] = {}
        self._quota_cache: dict[str | None, dict[str, Any]] = {}

    def project(self, sprint_id: int) -> dict[str, Any]:
        self._load(sprint_id)
        assert self.sprint is not None
        lifecycle = str(self.sprint["lifecycle"])
        unit_candidates: dict[int, Candidate] = {}

        if lifecycle != "armed":
            condition = {
                "prepared": "staged",
                "paused": "paused",
                "completed": "terminal",
                "aborted": "terminal",
            }[lifecycle]
            for unit_id, unit in self.units.items():
                unit_candidates[unit_id] = self._lifecycle_unit(unit, condition)
            return self._public(lifecycle, condition, unit_candidates, [])

        for unit_id in sorted(self.units):
            unit_candidates[unit_id] = self._classify_unit(unit_id)
        self._propagate_roots(unit_candidates)

        nonterminal = [
            candidate
            for unit_id, candidate in unit_candidates.items()
            if str(self.units[unit_id]["disposition"]) not in _TERMINAL_UNITS
        ]
        sprint_candidates = self._sprint_reply_candidates()
        if not nonterminal and self.units:
            sprint_candidates.append(self._closeout_candidate())
        aggregate_candidates = nonterminal + sprint_candidates
        return self._public(
            lifecycle,
            self._aggregate_condition(aggregate_candidates),
            unit_candidates,
            sprint_candidates,
        )

    def _load(self, sprint_id: int) -> None:
        self.sprint = self.con.execute(
            "SELECT * FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if self.sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")

        for row in self.con.execute(
            "SELECT p.*,sh.shortname,active.chat_id,c.provider "
            "FROM sprint_participants p JOIN shells sh ON sh.shell_id=p.shell_id "
            "LEFT JOIN active_shell_chats active ON active.shell_id=p.shell_id "
            "LEFT JOIN conversations c ON c.conversation_id=active.chat_id "
            "WHERE p.sprint_id=? ORDER BY p.participant_id",
            (sprint_id,),
        ):
            self.participants[int(row["shell_id"])] = dict(row)

        for row in self.con.execute(
            "SELECT * FROM sprint_work_units WHERE sprint_id=? ORDER BY work_unit_id",
            (sprint_id,),
        ):
            self.units[int(row["work_unit_id"])] = row
        for row in self.con.execute(
            "SELECT work_unit_id,depends_on_work_unit_id "
            "FROM sprint_work_unit_dependencies WHERE sprint_id=? "
            "ORDER BY work_unit_id,depends_on_work_unit_id",
            (sprint_id,),
        ):
            self.dependencies[int(row["work_unit_id"])].append(
                int(row["depends_on_work_unit_id"])
            )

        for row in self.con.execute(
            "SELECT event_id,event_type,payload,created_at FROM sprint_events "
            "WHERE sprint_id=? ORDER BY event_id DESC LIMIT ?",
            (sprint_id, MAX_EVENTS),
        ):
            event = dict(row)
            try:
                payload = json.loads(str(row["payload"]))
                if not isinstance(payload, dict):
                    raise TypeError
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
                self.unreadable.append(
                    {"kind": "sprint_event", "id": int(row["event_id"])}
                )
            event["payload"] = payload
            event["at"] = _parse(str(row["created_at"]))
            self.events.append(event)
            unit_id = payload.get("work_unit_id")
            if isinstance(unit_id, int) and unit_id in self.units:
                self.events_by_unit[unit_id].append(event)

        for unit_id, unit in self.units.items():
            stage_events = _EVENT_STAGE[str(unit["disposition"])]
            for event in self.events_by_unit[unit_id]:
                message_id = event["payload"].get("message_id")
                if event["event_type"] in stage_events and isinstance(message_id, int):
                    self.stage_message_ids[unit_id] = message_id
                    break

        self._load_current_closeout_message_ids(sprint_id)
        self._load_prs(sprint_id)
        self._load_messages(sprint_id)
        self._load_open_replies(sprint_id)
        self.runtime = sprint_runtime.runtime_status(self.con, now=self.now)
        heartbeat = self.con.execute(
            "SELECT beat_at,interval_s FROM daemon_heartbeats WHERE name=?",
            (sprint_pr_watcher.WATCHER_DAEMON_NAME,),
        ).fetchone()
        self.watcher = {
            "state": sprint_pr_watcher.derive_watcher_status(
                heartbeat, now=self.now
            ),
            "beat_at": str(heartbeat["beat_at"]) if heartbeat else None,
            "interval_seconds": float(heartbeat["interval_s"]) if heartbeat else None,
        }

    def _load_prs(self, sprint_id: int) -> None:
        for row in self.con.execute(
            "SELECT link.work_unit_id,pr.registered_pr_id,pr.registered_at,"
            "t.transition_id,t.normalized_state,t.observed_at "
            "FROM sprint_pr_work_units link "
            "JOIN sprint_registered_prs pr ON pr.registered_pr_id=link.registered_pr_id "
            "LEFT JOIN sprint_pr_transitions t ON t.transition_id=("
            " SELECT latest.transition_id FROM sprint_pr_transitions latest "
            " WHERE latest.registered_pr_id=pr.registered_pr_id "
            " ORDER BY latest.transition_id DESC LIMIT 1) "
            "WHERE link.sprint_id=? ORDER BY link.work_unit_id,pr.registered_pr_id DESC",
            (sprint_id,),
        ):
            unit_id = int(row["work_unit_id"])
            self.prs.setdefault(unit_id, dict(row))

    def _load_current_closeout_message_ids(self, sprint_id: int) -> None:
        row = self.con.execute(
            "SELECT event_id,event_type,payload,created_at FROM sprint_events "
            "WHERE sprint_id=? AND event_type='sprint.delivery_terminal' "
            "ORDER BY event_id DESC LIMIT 1",
            (sprint_id,),
        ).fetchone()
        if row is None:
            return
        event = dict(row)
        try:
            payload = json.loads(str(row["payload"]))
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
            unreadable = {"kind": "sprint_event", "id": int(row["event_id"])}
            if unreadable not in self.unreadable:
                self.unreadable.append(unreadable)
        event["payload"] = payload
        event["at"] = _parse(str(row["created_at"]))
        self.closeout_event = event
        terminal_count = payload.get("terminal_count")
        if not isinstance(terminal_count, int):
            return
        base_key = f"sprint:{sprint_id}:delivery-terminal:{terminal_count}"
        rows = self.con.execute(
            "SELECT message_id FROM wake_message WHERE sprint_id=? "
            "AND (idempotency_key=? OR idempotency_key LIKE ?) "
            "ORDER BY message_id LIMIT ?",
            (sprint_id, base_key, f"{base_key}:%", MAX_MESSAGE_REFS),
        ).fetchall()
        self.closeout_message_ids.update(int(row["message_id"]) for row in rows)

    def _load_messages(self, sprint_id: int) -> None:
        assert self.sprint is not None
        exact_message_ids = sorted(
            set(self.stage_message_ids.values()) | self.closeout_message_ids
        )
        exact_filter = ""
        if exact_message_ids:
            marks = ",".join("?" for _ in exact_message_ids)
            exact_filter = f" OR m.message_id IN ({marks})"
        rows = self.con.execute(
            "WITH ranked_messages AS ("
            " SELECT m.*,ROW_NUMBER() OVER (PARTITION BY COALESCE(m.work_unit_id,0) "
            " ORDER BY m.message_id DESC) message_rank FROM wake_message m "
            " WHERE m.sprint_id=?) "
            "SELECT m.*,w.wake_id,w.idempotency_key wake_idempotency_key,"
            "w.state wake_state,w.attempt_count,w.created_at wake_created_at,"
            "w.available_at,w.delivered_at wake_delivered_at,w.failed_at,w.claimed_at,"
            "a.attempt_id,a.attempted_at,a.target_conversation_id,a.native_run_ref,a.outcome,"
            "r.run_id,r.state run_state,r.started_at,r.heartbeat_at,r.ended_at,"
            "recovery.recovery_event_id,recovery_event.created_at recovery_created_at,"
            "cm.idempotency_key trigger_idempotency_key,"
            "c.creation_idempotency_key,pc.sprint_participant_id linked_participant_id,"
            "active.chat_id active_chat_id,"
            "active.process_pid,active.process_start_ticks "
            "FROM ranked_messages m "
            "LEFT JOIN sprint_wake_messages wm ON wm.message_id=m.message_id "
            "LEFT JOIN sprint_wake_outbox w ON w.wake_id=wm.wake_id "
            "LEFT JOIN sprint_wake_attempts a ON a.attempt_id=("
            " SELECT latest.attempt_id FROM sprint_wake_attempts latest "
            " WHERE latest.wake_id=w.wake_id ORDER BY latest.attempt_id DESC LIMIT 1) "
            "LEFT JOIN conversation_runs r "
            "ON a.native_run_ref='conversation-run:' || r.run_id "
            "LEFT JOIN sprint_wake_recovery_messages recovery "
            "ON recovery.recovery_event_id=("
            " SELECT latest_recovery.recovery_event_id "
            " FROM sprint_wake_recovery_messages latest_recovery "
            " WHERE latest_recovery.sprint_id=m.sprint_id "
            " AND latest_recovery.replacement_wake_id=w.wake_id "
            " AND latest_recovery.message_id=m.message_id "
            " ORDER BY latest_recovery.recovery_event_id DESC LIMIT 1) "
            "LEFT JOIN sprint_events recovery_event "
            "ON recovery_event.event_id=recovery.recovery_event_id "
            "LEFT JOIN conversation_messages cm ON cm.message_id=r.trigger_message_id "
            "LEFT JOIN conversations c ON c.conversation_id=a.target_conversation_id "
            "LEFT JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=a.target_conversation_id "
            "LEFT JOIN active_shell_chats active ON active.shell_id=m.receiver_shell_id "
            f"WHERE (m.message_rank<=100{exact_filter}) ORDER BY m.message_id DESC",
            (sprint_id, *exact_message_ids),
        ).fetchall()
        generation = str(self.sprint["conversation_generation"])
        for row in rows:
            item = dict(row)
            wake_id = item.get("wake_id")
            item["exact_generation"] = bool(
                wake_id is not None
                and item.get("target_conversation_id")
                and item.get("linked_participant_id") == item.get("to_participant_id")
                and str(item.get("creation_idempotency_key") or "").startswith(
                    f"generation:{generation}:wake:"
                )
                and item.get("trigger_idempotency_key")
                == item.get("wake_idempotency_key")
            )
            item["verified_live"] = bool(
                item["exact_generation"]
                and item.get("run_state") in _LIVE_RUN_STATES
                and item.get("active_chat_id") == item.get("target_conversation_id")
                and item.get("process_pid") is not None
                and item.get("process_start_ticks") is not None
            )
            if item.get("attempt_id") is not None and item.get("outcome") == "delivered":
                native_ref = item.get("native_run_ref")
                well_formed = bool(
                    isinstance(native_ref, str)
                    and native_ref.startswith("conversation-run:")
                    and native_ref.removeprefix("conversation-run:").isdigit()
                )
                if not well_formed or item.get("run_id") is None:
                    signal = {
                        "kind": "native_run",
                        "id": int(item["attempt_id"]),
                        "at": _stamp(
                            _parse(item.get("attempted_at"))
                            or _parse(item.get("wake_delivered_at"))
                            or _parse(item.get("wake_created_at"))
                        ),
                    }
                    self._record_unreadable(signal, item.get("work_unit_id"))
            self.messages.append(item)
            if item.get("work_unit_id") is not None:
                self.messages_by_unit[int(item["work_unit_id"])].append(item)

    def _load_open_replies(self, sprint_id: int) -> None:
        unit_rows = self.con.execute(
            "WITH open_replies AS ("
            " SELECT m.message_id,m.work_unit_id,m.receiver_shell_id,m.created_at,"
            " m.delivered_at,m.read_at FROM wake_message m "
            " WHERE m.sprint_id=? AND m.requires_reply=1 "
            " AND m.reply_to_message_id IS NULL AND m.work_unit_id IS NOT NULL "
            " AND NOT EXISTS (SELECT 1 FROM wake_message reply "
            "  WHERE reply.sprint_id=m.sprint_id "
            "  AND reply.reply_to_message_id=m.message_id)),"
            "ranked_replies AS ("
            " SELECT open_replies.*,ROW_NUMBER() OVER ("
            "  PARTITION BY work_unit_id,(read_at IS NULL) "
            "  ORDER BY julianday(COALESCE(read_at,delivered_at,created_at)),message_id"
            " ) reply_rank FROM open_replies) "
            "SELECT message_id,work_unit_id,receiver_shell_id,created_at,"
            "delivered_at,read_at FROM ranked_replies WHERE reply_rank<=? "
            "ORDER BY work_unit_id,reply_rank",
            (sprint_id, MAX_MESSAGE_REFS),
        ).fetchall()
        for row in unit_rows:
            item = dict(row)
            self.open_replies_by_unit[int(item["work_unit_id"])].append(item)

        assert self.sprint is not None
        sprint_rows = self.con.execute(
            "WITH open_replies AS ("
            " SELECT m.message_id,m.receiver_shell_id,m.created_at,m.delivered_at,"
            " m.read_at,COALESCE(m.read_at,m.delivered_at,m.created_at) trigger_at "
            " FROM wake_message m WHERE m.sprint_id=? AND m.requires_reply=1 "
            " AND m.reply_to_message_id IS NULL AND m.work_unit_id IS NULL "
            " AND NOT EXISTS (SELECT 1 FROM wake_message reply "
            "  WHERE reply.sprint_id=m.sprint_id "
            "  AND reply.reply_to_message_id=m.message_id)),"
            "classified AS ("
            " SELECT open_replies.*,CASE "
            "  WHEN (julianday(?) - MAX(julianday(?),julianday(trigger_at)))"
            "       * 86400 >= ? THEN 3 "
            "  WHEN read_at IS NOT NULL THEN 2 ELSE 1 END condition_rank "
            " FROM open_replies) "
            "SELECT message_id,NULL work_unit_id,receiver_shell_id,created_at,"
            "delivered_at,read_at,condition_rank,COUNT(*) OVER () open_count,"
            "SUM(condition_rank>=2) OVER () root_count,"
            "SUM(condition_rank=3) OVER () attention_count "
            "FROM classified ORDER BY condition_rank DESC,"
            "MAX(julianday(?),julianday(trigger_at)),message_id LIMIT ?",
            (
                sprint_id,
                _stamp(self.now),
                self.sprint["armed_at"],
                int(ATTENTION_AFTER.total_seconds()),
                self.sprint["armed_at"],
                MAX_ROOT_CAUSES,
            ),
        ).fetchall()
        if sprint_rows:
            self.sprint_reply_root_count = int(sprint_rows[0]["root_count"])
            self.sprint_reply_attention_count = int(
                sprint_rows[0]["attention_count"]
            )
        self.open_replies_by_unit[None].extend(dict(row) for row in sprint_rows)

    def _classify_unit(self, unit_id: int) -> Candidate:
        unit = self.units[unit_id]
        disposition = str(unit["disposition"])
        floor = self._stage_floor(unit)
        owner_shell = self._owner_shell(unit)
        owner = self._owner([owner_shell])
        next_code, next_detail = _NEXT_EVENT[disposition]
        capacity = self._capacity(owner_shell)

        if disposition in _TERMINAL_UNITS:
            evidence = self._latest_unit_event(unit_id, _EVENT_STAGE[disposition])
            return Candidate(
                "terminal", "unit_terminal", floor, owner, evidence,
                next_code, next_detail, work_unit_id=unit_id, capacity=capacity,
            )

        incomplete = [
            dependency
            for dependency in self.dependencies[unit_id]
            if str(self.units[dependency]["disposition"]) not in _TERMINAL_UNITS
        ]
        if disposition == "planned" and incomplete:
            evidence = self._latest_dependency_evidence(incomplete)
            since = _max_stamp(floor, evidence.at if evidence else None)
            return Candidate(
                "waiting_dependency", "dependency_wait", since, owner, evidence,
                "prerequisite_advancement", "the incomplete prerequisites advance",
                work_unit_id=unit_id, waiting_on=incomplete, capacity=capacity,
            )

        relevant = self._relevant_messages(unit)
        live_match = self._live_run(relevant, owner_shell, disposition)
        if live_match is not None and disposition != "ready":
            live, live_owner_shell = live_match
            return Candidate(
                "progressing", "run_active", _max_stamp(floor, live.at),
                self._owner([live_owner_shell]),
                live, next_code, next_detail, work_unit_id=unit_id,
                activity="live", capacity=self._capacity(live_owner_shell),
            )

        exhausted = self._pickup_exhausted(unit_id, floor)
        if exhausted is not None:
            return Candidate(
                "infrastructure", "pickup_exhausted", exhausted.at, owner,
                exhausted, next_code, next_detail, work_unit_id=unit_id,
                capacity=capacity,
            )

        failed = self._failed_wake(relevant, floor)
        if failed is not None:
            return Candidate(
                "infrastructure", "wake_failed", failed.at, owner, failed,
                next_code, next_detail, work_unit_id=unit_id, capacity=capacity,
            )

        runtime_failure = self._runtime_failure(disposition, relevant, floor)
        if runtime_failure is not None:
            cause, since, evidence = runtime_failure
            return Candidate(
                "infrastructure", cause, since, owner, evidence,
                next_code, next_detail, work_unit_id=unit_id, capacity=capacity,
            )

        watcher_failure = self._watcher_failure(unit_id, floor)
        if watcher_failure is not None:
            cause, since, evidence = watcher_failure
            return Candidate(
                "infrastructure", cause, since, owner, evidence,
                next_code, next_detail, work_unit_id=unit_id, capacity=capacity,
            )

        wake = self._wake_candidate(relevant, floor)
        if wake is not None:
            condition, cause, since, evidence, refs, wake_owner_shell = wake
            return Candidate(
                condition, cause, since, self._owner([wake_owner_shell]), evidence,
                next_code, next_detail,
                work_unit_id=unit_id, message_refs=refs,
                activity="live" if cause == "pickup_active" else "idle",
                capacity=self._capacity(wake_owner_shell),
            )

        reply = self._reply_candidate(unit_id, floor)
        if reply is not None:
            reply.work_unit_id = unit_id
            if reply.capacity is None:
                reply.capacity = capacity
            return reply

        pr = self.prs.get(unit_id)
        if pr and pr.get("normalized_state") in _ACTIVE_PR_STATES:
            observed = _parse(pr.get("observed_at")) or floor
            evidence = Evidence(
                "pr_transition", int(pr["transition_id"]), observed, 3
            )
            state = str(pr["normalized_state"])
            if state in {"created", "pending"}:
                since = _max_stamp(floor, observed)
                overdue = self.now - since >= CI_STUCK_AFTER
                return Candidate(
                    "attention" if overdue else "waiting_external",
                    "ci_stuck" if overdue else "ci_pending",
                    since, owner, evidence, next_code, next_detail,
                    work_unit_id=unit_id, capacity=capacity,
                )
            since = _max_stamp(
                floor, observed, self._latest_owner_reset(relevant, observed)
            )
            if state == "red":
                overdue = self.now - since >= ATTENTION_AFTER
                return Candidate(
                    "attention" if overdue else "waiting_external",
                    "pr_red_unowned" if overdue else "no_progress_grace",
                    since, owner, evidence, next_code, next_detail,
                    work_unit_id=unit_id, capacity=capacity,
                )
            overdue = self.now - since >= ATTENTION_AFTER
            cause = "merge_idle" if disposition == "merge_ready" else "green_handoff_idle"
            return Candidate(
                "attention" if overdue else "waiting_external",
                cause if overdue else "no_progress_grace",
                since, owner, evidence, next_code, next_detail,
                work_unit_id=unit_id, capacity=capacity,
            )

        if disposition == "blocked":
            anchor = self._latest_reset(unit_id, relevant, floor)
            overdue = self.now - anchor >= ATTENTION_AFTER
            return Candidate(
                "attention" if overdue else "waiting_external",
                "blocked_unowned" if overdue else "blocked_grace",
                anchor, owner, self._latest_evidence(unit_id, relevant),
                next_code, next_detail, work_unit_id=unit_id, capacity=capacity,
            )

        unreadable = self._unit_unreadable(unit_id)
        unreadable_times = [
            value
            for value in (_parse(signal.get("at")) for signal in unreadable)
            if value is not None
        ]
        anchor = (
            _max_stamp(floor, *unreadable_times)
            if unreadable
            else self._latest_reset(unit_id, relevant, floor)
        )
        overdue = self.now - anchor >= ATTENTION_AFTER
        cause = (
            "unreadable_evidence"
            if overdue and unreadable
            else "no_progress_carrier"
            if overdue
            else "no_progress_grace"
        )
        return Candidate(
            "attention" if overdue else "waiting_external", cause, anchor,
            owner, self._latest_evidence(unit_id, relevant), next_code, next_detail,
            work_unit_id=unit_id, unreadable=unreadable, capacity=capacity,
        )

    def _stage_floor(self, unit: sqlite3.Row) -> datetime:
        assert self.sprint is not None
        armed = _parse(self.sprint["armed_at"])
        updated = _parse(unit["updated_at"])
        event = self._latest_unit_event(
            int(unit["work_unit_id"]), _EVENT_STAGE[str(unit["disposition"])]
        )
        return _max_stamp(armed, updated, event.at if event else None)

    def _owner_shell(self, unit: sqlite3.Row) -> int:
        disposition = str(unit["disposition"])
        if disposition == "in_review":
            return int(unit["reviewer_shell_id"])
        if disposition in {"planned", "blocked"}:
            assert self.sprint is not None
            return int(self.sprint["originating_planner_shell_id"])
        return int(unit["assigned_shell_id"])

    def _owner(self, shell_ids: list[int]) -> dict[str, Any]:
        participants = []
        for shell_id in sorted(set(shell_ids)):
            row = self.participants.get(shell_id)
            if row is None:
                continue
            participants.append(
                {
                    "role": str(row["role"]),
                    "shell_id": shell_id,
                    "shortname": str(row["shortname"]),
                }
            )
        return {
            "mode": "single" if len(participants) == 1 else "any",
            "participants": participants,
        }

    def _relevant_messages(self, unit: sqlite3.Row) -> list[dict[str, Any]]:
        assert self.sprint is not None
        unit_id = int(unit["work_unit_id"])
        disposition = str(unit["disposition"])
        owner_shell = self._owner_shell(unit)
        result = []
        recovery_message_ids: set[int] = set()
        if disposition == "blocked":
            floor = self._stage_floor(unit)
            open_reply_ids = {
                int(message["message_id"])
                for message in self._open_reply_candidates(unit_id)
            }
            for message in self.messages_by_unit[unit_id]:
                recovery_at = _parse(message.get("recovery_created_at"))
                is_planner_recovery = (
                    int(message["receiver_shell_id"]) == owner_shell
                    and message.get("recovery_event_id") is not None
                    and recovery_at is not None
                    and recovery_at >= floor
                )
                if (
                    int(message["message_id"]) in open_reply_ids
                    or is_planner_recovery
                ):
                    result.append(message)
            return result
        stage_kind = {
            "ready": "work_assignment",
            "active": "work_assignment",
            "in_review": "review_request",
            "fixing": "notification",
            "merge_ready": "notification",
        }.get(disposition)
        for message in self.messages_by_unit[unit_id]:
            wake_key = str(message.get("wake_idempotency_key") or "")
            is_recovery = wake_key.startswith(
                (
                    f"sprint-recovery:{self.sprint['sprint_id']}:",
                    f"sprint-resume:{self.sprint['sprint_id']}:",
                )
            )
            if is_recovery:
                recovery_message_ids.add(int(message["message_id"]))
            if (
                stage_kind is not None
                and message["message_kind"] != stage_kind
                and not is_recovery
            ):
                continue
            if disposition == "planned":
                continue
            if int(message["receiver_shell_id"]) != owner_shell:
                continue
            result.append(message)
        if stage_kind is None or not result:
            return result
        stage_message_id = self.stage_message_ids.get(unit_id)
        if stage_message_id is not None:
            return [
                message
                for message in result
                if int(message["message_id"]) == stage_message_id
                or int(message["message_id"]) in recovery_message_ids
            ]
        recovery = [
            message
            for message in result
            if int(message["message_id"]) in recovery_message_ids
        ]
        stage = [
            message
            for message in result
            if int(message["message_id"]) not in recovery_message_ids
        ]
        return recovery + (
            [max(stage, key=lambda message: int(message["message_id"]))]
            if stage
            else []
        )

    def _live_run(
        self,
        messages: list[dict[str, Any]],
        owner_shell: int,
        disposition: str,
    ) -> tuple[Evidence, int] | None:
        candidates: list[tuple[Evidence, int]] = []
        for message in messages:
            if not message["verified_live"]:
                continue
            receiver_shell = int(message["receiver_shell_id"])
            if disposition != "blocked" and receiver_shell != owner_shell:
                continue
            if disposition == "in_review" and not (
                message["message_kind"] == "review_request"
                and message["disposition"] == "accepted"
            ):
                continue
            if (
                disposition == "active"
                and message["message_kind"] == "work_assignment"
                and message["disposition"] != "accepted"
            ):
                continue
            at = _parse(message.get("heartbeat_at")) or _parse(message.get("started_at"))
            if at is not None:
                candidates.append(
                    (Evidence("run", int(message["run_id"]), at, 0), receiver_shell)
                )
        return max(
            candidates,
            key=lambda value: (value[0].at, -value[0].rank, value[0].row_id),
            default=None,
        )

    def _runtime_failure(
        self,
        disposition: str,
        messages: list[dict[str, Any]],
        floor: datetime,
    ) -> tuple[str, datetime, Evidence | None] | None:
        applicable = disposition in {"planned", "ready"} or any(
            message.get("wake_state") in {"pending", "delivering"}
            for message in messages
        )
        if any(
            message.get("exact_generation")
            and message.get("run_state") in _PICKUP_RUN_STATES
            for message in messages
        ):
            applicable = False
        state = str(self.runtime["state"])
        if not applicable or state == "live":
            return None
        beat = _parse(self.runtime.get("beat_at"))
        if state == "missing":
            return "runtime_missing", floor, None
        if beat is None:
            raise ValueError("stale runtime has no heartbeat timestamp")
        interval = float(self.runtime["interval_seconds"])
        due = beat + timedelta(seconds=sprint_runtime.RUNTIME_STALE_INTERVALS * interval)
        return "runtime_stale", _max_stamp(floor, due), Evidence("runtime_heartbeat", 0, beat, 5)

    def _watcher_failure(
        self, unit_id: int, floor: datetime
    ) -> tuple[str, datetime, Evidence | None] | None:
        pr = self.prs.get(unit_id)
        if not pr or pr.get("normalized_state") not in _ACTIVE_PR_STATES:
            return None
        state = str(self.watcher["state"])
        if state == "live":
            return None
        beat = _parse(self.watcher.get("beat_at"))
        if state == "never-started":
            return "watcher_missing", floor, None
        if beat is None:
            raise ValueError("stale watcher has no heartbeat timestamp")
        interval = float(self.watcher["interval_seconds"])
        due = beat + timedelta(
            seconds=3 * (interval + sprint_pr_watcher.GITHUB_TIMEOUT_SECONDS)
        )
        return "watcher_stale", _max_stamp(floor, due), Evidence("watcher_heartbeat", 0, beat, 5)

    def _failed_wake(
        self, messages: list[dict[str, Any]], floor: datetime
    ) -> Evidence | None:
        candidates = []
        for message in messages:
            if message.get("wake_state") != "failed":
                continue
            at = _parse(message.get("failed_at")) or _parse(message.get("wake_created_at"))
            if at and at >= floor:
                candidates.append(Evidence("wake", int(message["wake_id"]), at, 1))
        return self._newest(candidates)

    def _pickup_exhausted(self, unit_id: int, floor: datetime) -> Evidence | None:
        for event in self.events_by_unit[unit_id]:
            if event["event_type"] == "wake.pickup_exhausted" and event["at"] >= floor:
                return Evidence("pickup", int(event["event_id"]), event["at"], 1)
        return None

    def _wake_candidate(
        self, messages: list[dict[str, Any]], floor: datetime
    ) -> tuple[str, str, datetime, Evidence, list[int], int] | None:
        rows = []
        for message in messages:
            state = message.get("wake_state")
            if state in {"pending", "delivering"}:
                available = _parse(message.get("available_at"))
                if available is not None and available > self.now:
                    available = None
                at = _max_stamp(
                    floor,
                    _parse(message.get("wake_created_at")),
                    _parse(message.get("claimed_at")),
                    _parse(message.get("attempted_at")),
                    available if state == "pending" else None,
                )
            if state == "pending":
                rows.append((0, "wake_pending", at, message))
            elif state == "delivering":
                rows.append((1, "wake_delivering", at, message))
            elif (
                state == "delivered"
                and message.get("exact_generation")
                and message.get("run_state") in _PICKUP_RUN_STATES
            ):
                at = (
                    _parse(message.get("started_at"))
                    or _parse(message.get("wake_delivered_at"))
                    or floor
                )
                rows.append((2, "pickup_active", at, message))
        if not rows:
            return None
        rows.sort(key=lambda value: (value[0], -value[2].timestamp(), -int(value[3]["message_id"])))
        _, cause, trigger, selected = rows[0]
        since = _max_stamp(floor, trigger)
        wake_id = int(selected["wake_id"])
        refs = [
            int(message["message_id"])
            for message in messages
            if message.get("wake_id") == wake_id
        ]
        evidence = Evidence(
            "pickup" if cause == "pickup_active" else "wake",
            int(selected.get("run_id") or wake_id),
            trigger,
            1,
        )
        condition = (
            "attention"
            if cause in {"wake_pending", "wake_delivering"}
            and self.now - since >= ATTENTION_AFTER
            else "waiting_external"
        )
        return (
            condition,
            cause,
            since,
            evidence,
            refs,
            int(selected["receiver_shell_id"]),
        )

    def _reply_candidate(self, unit_id: int, floor: datetime) -> Candidate | None:
        candidates = self._open_reply_candidates(unit_id)
        if not candidates:
            return None
        unread = [row for row in candidates if row.get("read_at") is None]
        pool = unread or candidates
        trigger_field = "delivered_at" if unread else "read_at"
        selected = min(
            pool,
            key=lambda row: (
                _parse(row.get(trigger_field)) or _parse(row["created_at"]),
                int(row["message_id"]),
            ),
        )
        trigger = _required_stamp(
            selected.get(trigger_field) or selected["created_at"]
        )
        since = _max_stamp(floor, trigger)
        overdue = self.now - since >= ATTENTION_AFTER
        is_unread = selected.get("read_at") is None
        condition = (
            "attention"
            if overdue
            else "waiting_external"
            if is_unread
            else "waiting_decision"
        )
        cause = "reply_unread" if is_unread else "reply_overdue" if overdue else "reply_waiting"
        same = [
            row for row in pool
            if (row.get("read_at") is None) == is_unread
        ]
        same.sort(
            key=lambda row: (
                _parse(row.get(trigger_field)) or _parse(row["created_at"]),
                int(row["message_id"]),
            )
        )
        receiver = int(selected["receiver_shell_id"])
        return Candidate(
            condition, cause, since, self._owner([receiver]),
            Evidence(
                "reply", int(selected["message_id"]), trigger, 2
            ),
            "linked_reply", f"{self.participants[receiver]['shortname']} sends a linked reply",
            message_refs=[
                int(row["message_id"]) for row in same[:MAX_MESSAGE_REFS]
            ],
            capacity=self._capacity(receiver),
        )

    def _open_reply_candidates(self, unit_id: int | None) -> list[dict[str, Any]]:
        return self.open_replies_by_unit[unit_id]

    def _sprint_reply_candidates(self) -> list[Candidate]:
        result = []
        assert self.sprint is not None
        floor = _parse(self.sprint["armed_at"])
        for message in self._open_reply_candidates(None):
            trigger = _required_stamp(
                message.get("read_at")
                or message.get("delivered_at")
                or message["created_at"]
            )
            since = _max_stamp(floor, trigger)
            overdue = self.now - since >= ATTENTION_AFTER
            unread = message.get("read_at") is None
            condition = (
                "attention"
                if overdue
                else "waiting_external"
                if unread
                else "waiting_decision"
            )
            cause = "reply_unread" if unread else "reply_overdue" if overdue else "reply_waiting"
            receiver = int(message["receiver_shell_id"])
            result.append(
                Candidate(
                    condition, cause, since, self._owner([receiver]),
                    Evidence("reply", int(message["message_id"]), trigger, 2),
                    "linked_reply",
                    f"{self.participants[receiver]['shortname']} sends a linked reply",
                    scope="sprint", message_refs=[int(message["message_id"])],
                )
            )
        return result

    def _closeout_candidate(self) -> Candidate:
        assert self.sprint is not None
        floor = _required_stamp(self.sprint["armed_at"])
        event = self.closeout_event
        if event is None:
            anchor = floor
            evidence = None
            event_id = 0
        else:
            anchor = _max_stamp(floor, event["at"])
            evidence = Evidence("lifecycle_event", int(event["event_id"]), event["at"], 5)
            event_id = int(event["event_id"])
        messages = [
            message
            for message in self.messages
            if int(message["message_id"]) in self.closeout_message_ids
        ]
        owners = [int(message["receiver_shell_id"]) for message in messages]
        live = self._newest(
            [
                Evidence(
                    "run",
                    int(message["run_id"]),
                    _required_stamp(
                        message.get("heartbeat_at")
                        or message.get("started_at")
                    ),
                    0,
                )
                for message in messages
                if message.get("verified_live")
            ]
        )
        refs = [int(message["message_id"]) for message in messages]
        if live:
            return Candidate(
                "progressing", "conformance_active", _max_stamp(anchor, live.at),
                self._owner(owners), live, "conformance_recorded",
                "a Reviewer records whole-Sprint conformance", scope="sprint",
                message_refs=refs,
                root_key=event_id,
            )
        failed = self._failed_wake(messages, anchor)
        if failed:
            return Candidate(
                "infrastructure", "wake_failed", failed.at, self._owner(owners),
                failed, "conformance_recorded", "delivery-terminal recovery completes",
                scope="sprint", message_refs=refs,
                root_key=event_id,
            )
        wake = self._wake_candidate(messages, anchor)
        if wake:
            _, _, since, wake_evidence, _, _ = wake
            return Candidate(
                "waiting_external", "conformance_handoff", since,
                self._owner(owners), wake_evidence, "conformance_recorded",
                "a Reviewer begins whole-Sprint conformance", scope="sprint",
                message_refs=refs,
                root_key=event_id,
            )
        resets = [anchor]
        for message in messages:
            for field_name in ("read_at", "ended_at", "wake_delivered_at"):
                value = _parse(message.get(field_name))
                if value:
                    resets.append(value)
        report = self.con.execute(
            "SELECT report_id,created_at FROM sprint_reports WHERE sprint_id=? "
            "AND report_kind='conformance' ORDER BY report_id DESC LIMIT 1",
            (self.sprint["sprint_id"],),
        ).fetchone()
        if report:
            report_at = _required_stamp(report["created_at"])
            resets.append(report_at)
            evidence = Evidence("report", int(report["report_id"]), report_at, 4)
        since = max(resets)
        overdue = self.now - since >= ATTENTION_AFTER
        candidate = Candidate(
            "attention" if overdue else "waiting_external",
            "conformance_idle" if overdue else "conformance_grace",
            since, self._owner(owners), evidence, "conformance_recorded",
            "a Reviewer records whole-Sprint conformance", scope="sprint",
            message_refs=refs,
            root_key=event_id,
        )
        if candidate.last_evidence is None and event_id:
            candidate.last_evidence = Evidence("lifecycle_event", event_id, anchor, 5)
        return candidate

    def _propagate_roots(self, candidates: dict[int, Candidate]) -> None:
        visiting: set[int] = set()
        memo: dict[int, list[int]] = {}

        def roots(unit_id: int) -> list[int]:
            if unit_id in memo:
                return memo[unit_id]
            if unit_id in visiting:
                self.unreadable.append({"kind": "dependency_cycle", "id": unit_id})
                return []
            visiting.add(unit_id)
            candidate = candidates[unit_id]
            if candidate.condition in _ROOT_CONDITIONS and candidate.cause != "dependency_wait":
                value = [unit_id]
            elif candidate.condition == "waiting_dependency":
                value = []
                for upstream in candidate.waiting_on:
                    value.extend(roots(upstream))
                value = sorted(set(value))
            else:
                value = []
            visiting.remove(unit_id)
            memo[unit_id] = value
            candidate.roots = value
            return value

        for unit_id in sorted(candidates):
            roots(unit_id)

    def _public(
        self,
        lifecycle: str,
        condition: str,
        units: dict[int, Candidate],
        sprint_candidates: list[Candidate],
    ) -> dict[str, Any]:
        aggregate = [
            candidate
            for unit_id, candidate in units.items()
            if str(self.units[unit_id]["disposition"]) not in _TERMINAL_UNITS
        ] + sprint_candidates
        winning = [candidate for candidate in aggregate if candidate.condition == condition]
        since = min(
            (candidate.since for candidate in winning if candidate.since is not None),
            default=None,
        )
        unit_roots = [
            candidate
            for unit_id, candidate in units.items()
            if candidate.condition in _ROOT_CONDITIONS
            and candidate.roots == [unit_id]
        ]
        sprint_reply_roots = [
            candidate
            for candidate in sprint_candidates
            if candidate.condition in _ROOT_CONDITIONS
            and candidate.scope == "sprint"
            and candidate.root_key is None
        ]
        sprint_other_roots = [
            candidate
            for candidate in sprint_candidates
            if candidate.condition in _ROOT_CONDITIONS
            and not (candidate.scope == "sprint" and candidate.root_key is None)
        ]
        roots = unit_roots + sprint_reply_roots + sprint_other_roots
        root_public = [candidate.root_public(self.now) for candidate in roots]
        root_public.sort(
            key=lambda row: (
                -_SEVERITY[str(row["condition"])],
                str(row["since"] or ""),
                str(row["scope"]),
                str(row["root_id"]),
            )
        )
        sprint_reply_root_count = (
            self.sprint_reply_root_count if lifecycle == "armed" else 0
        )
        sprint_reply_attention_count = (
            self.sprint_reply_attention_count if lifecycle == "armed" else 0
        )
        root_cause_count = (
            len(unit_roots)
            + sprint_reply_root_count
            + len(sprint_other_roots)
        )
        root_public = root_public[:MAX_ROOT_CAUSES]
        root_ids = sorted(
            {
                int(candidate.work_unit_id)
                for candidate in roots
                if candidate.work_unit_id is not None
            }
        )
        return {
            "health": {
                "condition": condition,
                "since": _stamp(since),
                "age_seconds": _age(self.now, since) if since else None,
                "root_work_unit_ids": root_ids,
                "root_causes": root_public,
                "root_cause_count": root_cause_count,
                "root_causes_truncated": root_cause_count > len(root_public),
                "attention_count": (
                    sum(candidate.condition == "attention" for candidate in unit_roots)
                    + sprint_reply_attention_count
                    + sum(
                        candidate.condition == "attention"
                        for candidate in sprint_other_roots
                    )
                ),
                "unreadable_signals": self._bounded_unreadable(self.unreadable),
                "machinery": {
                    "runtime": self.runtime,
                    "watcher": self.watcher,
                    "applicable": lifecycle == "armed",
                },
            },
            "work_units": {
                unit_id: candidate.unit_public(self.now)
                for unit_id, candidate in units.items()
            },
        }

    def _aggregate_condition(self, candidates: list[Candidate]) -> str:
        if not candidates:
            return "waiting_external"
        return max(candidates, key=lambda candidate: _SEVERITY[candidate.condition]).condition

    def _lifecycle_unit(self, unit: sqlite3.Row, condition: str) -> Candidate:
        disposition = str(unit["disposition"])
        if disposition in _TERMINAL_UNITS:
            condition = "terminal"
        owner_shell = self._owner_shell(unit)
        next_code, next_detail = _NEXT_EVENT[disposition]
        return Candidate(
            condition,
            {
                "staged": "sprint_prepared",
                "paused": "sprint_paused",
                "terminal": (
                    "sprint_terminal"
                    if disposition not in _TERMINAL_UNITS
                    else "unit_terminal"
                ),
            }[condition],
            None,
            self._owner([owner_shell]),
            None,
            next_code,
            next_detail,
            work_unit_id=int(unit["work_unit_id"]),
            capacity=self._capacity(owner_shell),
        )

    def _latest_unit_event(
        self, unit_id: int, event_types: frozenset[str]
    ) -> Evidence | None:
        for event in self.events_by_unit[unit_id]:
            if event["event_type"] in event_types:
                return Evidence("work_unit_event", int(event["event_id"]), event["at"], 4)
        return None

    def _latest_dependency_evidence(self, unit_ids: list[int]) -> Evidence | None:
        candidates = []
        for unit_id in unit_ids:
            unit = self.units[unit_id]
            at = _required_stamp(unit["updated_at"])
            candidates.append(Evidence("dependency", unit_id, at, 4))
        return self._newest(candidates)

    def _latest_owner_reset(
        self, messages: list[dict[str, Any]], after: datetime
    ) -> datetime | None:
        values = []
        for message in messages:
            for field_name in ("wake_created_at", "started_at", "ended_at", "read_at"):
                value = _parse(message.get(field_name))
                if value and value > after:
                    values.append(value)
        return max(values, default=None)

    def _latest_reset(
        self, unit_id: int, messages: list[dict[str, Any]], floor: datetime
    ) -> datetime:
        values = [floor]
        event = self._latest_unit_event(
            unit_id, frozenset().union(*_EVENT_STAGE.values())
        )
        if event:
            values.append(event.at)
        for message in messages:
            for field_name in (
                "created_at", "delivered_at", "read_at", "wake_created_at",
                "attempted_at", "started_at", "ended_at",
            ):
                value = _parse(message.get(field_name))
                if value:
                    values.append(value)
        pr = self.prs.get(unit_id)
        if pr:
            value = _parse(pr.get("observed_at"))
            if value:
                values.append(value)
        return max(values)

    def _latest_evidence(
        self, unit_id: int, messages: list[dict[str, Any]]
    ) -> Evidence | None:
        candidates = []
        for message in messages:
            fields = (
                ("ended_at", "run", int(message.get("run_id") or 0), 0),
                ("started_at", "run", int(message.get("run_id") or 0), 0),
                ("attempted_at", "pickup", int(message.get("attempt_id") or 0), 1),
                ("read_at", "reply", int(message["message_id"]), 2),
                ("delivered_at", "wake", int(message.get("wake_id") or 0), 1),
                ("created_at", "wake", int(message["message_id"]), 1),
            )
            for field_name, kind, row_id, rank in fields:
                value = _parse(message.get(field_name))
                if value:
                    candidates.append(Evidence(kind, row_id, value, rank))
        pr = self.prs.get(unit_id)
        if pr and pr.get("observed_at"):
            candidates.append(
                Evidence(
                    "pr_transition",
                    int(pr["transition_id"]),
                    _required_stamp(pr["observed_at"]),
                    3,
                )
            )
        event = self._latest_unit_event(unit_id, frozenset().union(*_EVENT_STAGE.values()))
        if event:
            candidates.append(event)
        return self._newest(candidates)

    def _unit_unreadable(self, unit_id: int) -> list[dict[str, Any]]:
        relevant_ids = {int(event["event_id"]) for event in self.events_by_unit[unit_id]}
        signals = [
            signal for signal in self.unreadable
            if (
                signal["kind"] == "dependency_cycle" and signal["id"] == unit_id
                or signal["kind"] == "sprint_event" and signal["id"] in relevant_ids
            )
        ]
        signals.extend(self.unreadable_by_unit[unit_id])
        return self._bounded_unreadable(signals)

    def _record_unreadable(
        self,
        signal: dict[str, Any],
        work_unit_id: object = None,
    ) -> None:
        if signal not in self.unreadable:
            self.unreadable.append(signal)
        if work_unit_id is None:
            return
        unit_signals = self.unreadable_by_unit[int(work_unit_id)]
        if signal not in unit_signals:
            unit_signals.append(signal)

    @staticmethod
    def _bounded_unreadable(
        signals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return sorted(
            signals,
            key=lambda signal: (
                str(signal.get("kind") or ""),
                int(signal.get("id") or 0),
                str(signal.get("at") or ""),
            ),
        )[:MAX_UNREADABLE_SIGNALS]

    @staticmethod
    def _newest(candidates: list[Evidence]) -> Evidence | None:
        if not candidates:
            return None
        return max(candidates, key=lambda value: (value.at, -value.rank, value.row_id))

    def _capacity(self, shell_id: int) -> dict[str, Any]:
        participant = self.participants.get(shell_id) or {}
        provider = participant.get("provider")
        result: dict[str, Any]
        if provider in self._quota_cache:
            return self._quota_cache[provider]
        if not provider:
            result = {
                "provider": None,
                "state": "unknown",
                "captured_at": None,
                "age_seconds": None,
                "reset_at": None,
            }
            self._quota_cache[provider] = result
            return result
        rows = self.con.execute(
            "SELECT w.window_pk,w.used_percent,w.used,w.limit_value,w.resets_at,"
            "w.captured_at,w.status FROM harness_quota_window w "
            "JOIN harness_quota_account a ON a.account_pk=w.account_pk "
            "WHERE a.provider=? AND datetime(w.captured_at)=("
            " SELECT MAX(datetime(latest.captured_at)) FROM harness_quota_window latest "
            " JOIN harness_quota_account account ON account.account_pk=latest.account_pk "
            " WHERE account.provider=?) ORDER BY w.window_pk",
            (provider, provider),
        ).fetchall()
        if not rows:
            result = {
                "provider": provider,
                "state": "unknown",
                "captured_at": None,
                "age_seconds": None,
                "reset_at": None,
            }
            self._quota_cache[provider] = result
            return result
        captured = max(_required_stamp(row["captured_at"]) for row in rows)
        fresh = self.now - captured <= QUOTA_FRESH_FOR
        statuses = {str(row["status"]) for row in rows}
        active = [
            row
            for row in rows
            if row["resets_at"] is None
            or _required_stamp(row["resets_at"]) > self.now
        ]
        exhausted = any(
            (row["used_percent"] is not None and float(row["used_percent"]) >= 100)
            or (
                row["used"] is not None and row["limit_value"] is not None
                and int(row["limit_value"]) > 0
                and int(row["used"]) >= int(row["limit_value"])
            )
            for row in active
        )
        state = (
            "unknown"
            if statuses != {"ok"}
            else "stale"
            if not fresh
            else "exhausted"
            if exhausted
            else "available"
        )
        resets = [
            _required_stamp(row["resets_at"])
            for row in active
            if row["resets_at"]
        ]
        result = {
            "provider": provider,
            "state": state,
            "captured_at": _stamp(captured),
            "age_seconds": _age(self.now, captured),
            "reset_at": _stamp(min(resets)) if resets else None,
        }
        self._quota_cache[provider] = result
        return result
