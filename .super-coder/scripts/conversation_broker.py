#!/usr/bin/env python3
"""Durable, event-driven browser conversation broker.

The API commits messages and transactional outbox intents; this service is
notified only after that commit.  It leases the oldest eligible turn, holds the
database-enforced per-shell mutation lock, delegates one native turn to the
harness adapter, and commits normalized events in conversation order.

The recovery rule is intentionally conservative: a leased run has not crossed
the native-dispatch boundary and is safe to start, while a starting/running run
is inspected by exact native session/run reference.  An outcome that cannot be
proved is recorded as unknown and is never replayed.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

import conversation_events
import conversation_git_targets
import db_driver
from conversation_adapters import (
    ConversationAdapter,
    ConversationContext,
    NativeTurn,
    NormalizedEvent,
    adapter_for,
)
from conversation_adapters.base import TERMINAL_EVENTS, terminal_outcome
from conversation_state import require_transition

LIVE_RUN_STATES = frozenset({"leased", "starting", "running"})
TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
DEFAULT_LEASE_SECONDS = 30
DEFAULT_HEARTBEAT_SECONDS = 10
DEFAULT_RECOVERY_SECONDS = 5
DEFAULT_RECONCILE_ATTEMPTS = 3
DEFAULT_MAX_WORKERS = 8
DEFAULT_EVENT_BATCH_SIZE = 64
DEFAULT_EVENT_BACKLOG_SIZE = 1024
DEFAULT_EVENT_FLUSH_SECONDS = 0.1


class BrokerError(RuntimeError):
    """Stable broker failure for the API boundary."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class BrokerInvariantError(BrokerError):
    """Durable state contradicts the exact native identity contract."""


@dataclass(frozen=True)
class BrokerRun:
    run_id: int
    conversation_id: str
    message_id: int
    shell_id: int
    harness: str
    provider: str | None
    model: str | None
    effort: str | None
    worktree: Path
    title: str | None
    body: str
    session_before: str | None
    session_after: str | None
    runner_ref: str | None
    state: str
    interrupt_requested: bool = False

    def context(self) -> ConversationContext:
        # Browser conversations run with worktree command access.
        # Each adapter chooses its native mechanism; there is no shared
        # requirement for a literal --sandbox flag.
        return ConversationContext(
            worktree=self.worktree,
            provider=self.provider,
            model=self.model,
            effort=self.effort,
            permission_mode="unrestricted",
            title=self.title,
        )


@dataclass
class _ActiveRun:
    run: BrokerRun
    adapter: ConversationAdapter | None = None
    turn: NativeTurn | None = None
    interrupt_requested: bool = False
    interrupt_sent: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class BrokerStore:
    """Short SQLite transactions for the broker state machine."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        connect: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.db_path = str(db_path)
        self._connect = connect
        self.clock = clock
        self.lease_seconds = lease_seconds

    def connect(self):
        return (
            self._connect()
            if self._connect is not None
            else db_driver.connect(self.db_path)
        )

    def _times(self) -> tuple[str, str]:
        now = self.clock()
        return _stamp(now), _stamp(now + timedelta(seconds=self.lease_seconds))

    @staticmethod
    def _payload(payload: Mapping[str, Any] | None) -> str:
        encoded = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode()) > 262144:
            raise BrokerError(
                "CONVERSATION_EVENT_TOO_LARGE",
                "normalized event payload exceeds 262144 bytes",
            )
        return encoded

    @staticmethod
    def _append_event(
        con,
        *,
        conversation_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        message_id: int | None,
        run_id: int | None,
    ) -> int:
        sequence = con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 "
            "FROM conversation_events WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id,run_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                conversation_id,
                sequence,
                event_type,
                BrokerStore._payload(payload),
                message_id,
                run_id,
            ),
        )
        return int(sequence)

    @staticmethod
    def _run_from_row(row) -> BrokerRun:
        return BrokerRun(
            run_id=int(row["run_id"]),
            conversation_id=row["conversation_id"],
            message_id=int(row["trigger_message_id"]),
            shell_id=int(row["shell_id"]),
            harness=row["harness"],
            provider=row["provider"],
            model=row["model"],
            effort=row["effort"],
            worktree=Path(row["worktree"]),
            title=row["title"],
            body=row["body"],
            session_before=row["harness_session_before"],
            session_after=row["harness_session_after"],
            runner_ref=row["runner_ref"],
            state=row["run_state"],
            interrupt_requested=bool(row["interrupt_requested"]),
        )

    @staticmethod
    def _run_select() -> str:
        return (
            "SELECT r.run_id,r.conversation_id,r.trigger_message_id,r.shell_id,"
            "r.harness_session_before,r.harness_session_after,r.runner_ref,"
            "r.state AS run_state,c.harness,c.provider,c.model,c.effort,"
            "c.worktree,c.title,m.body,"
            "EXISTS(SELECT 1 FROM conversation_events ie "
            " WHERE ie.run_id=r.run_id "
            " AND ie.event_type='run.interrupt.requested') "
            "AS interrupt_requested "
            "FROM conversation_runs r "
            "JOIN conversations c ON c.conversation_id=r.conversation_id "
            "JOIN conversation_messages m "
            " ON m.message_id=r.trigger_message_id "
        )

    def claim_next(self, owner: str) -> BrokerRun | None:
        """Atomically turn the oldest eligible outbox intent into a leased run.

        The earlier-message predicate provides per-conversation ordering.  The
        live-run indexes are the final shell/conversation mutation fences.
        """
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(con, "conversation.broker.claim"):
                row = con.execute(
                    "SELECT o.outbox_id,o.conversation_id,o.message_id,o.attempts,"
                    "c.shell_id,c.harness,c.provider,c.model,c.effort,c.worktree,"
                    "c.title,c.harness_session_ref,m.body,m.state AS message_state "
                    "FROM conversation_outbox o "
                    "JOIN conversations c "
                    " ON c.conversation_id=o.conversation_id "
                    "JOIN conversation_messages m ON m.message_id=o.message_id "
                    "WHERE o.state='pending' AND c.state='queued' "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM conversation_events closing "
                    " WHERE closing.conversation_id=c.conversation_id "
                    " AND closing.event_type='conversation.close.requested'"
                    ") "
                    "AND m.state IN ('accepted','queued') "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM conversation_messages earlier "
                    " WHERE earlier.conversation_id=m.conversation_id "
                    " AND earlier.message_id<m.message_id "
                    " AND earlier.state IN ('accepted','queued','running')) "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM conversation_runs live "
                    " WHERE live.shell_id=c.shell_id "
                    " AND live.state IN ('leased','starting','running')) "
                    "ORDER BY o.outbox_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None

                updated = con.execute(
                    "UPDATE conversation_outbox SET state='claimed',claim_owner=?,"
                    "claimed_at=?,lease_expires_at=?,attempts=attempts+1 "
                    "WHERE outbox_id=? AND state='pending'",
                    (owner, now, expires, row["outbox_id"]),
                ).rowcount
                if updated != 1:
                    return None

                attempt = int(row["attempts"]) + 1
                if row["message_state"] == "accepted":
                    require_transition("message", "accepted", "queued")
                    con.execute(
                        "UPDATE conversation_messages SET state='queued' "
                        "WHERE message_id=?",
                        (row["message_id"],),
                    )
                require_transition("message", "queued", "running")
                con.execute(
                    "UPDATE conversation_messages SET state='running' "
                    "WHERE message_id=?",
                    (row["message_id"],),
                )

                cur = con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,attempt,"
                    "harness_session_before,state,lease_owner,lease_expires_at,"
                    "heartbeat_at) VALUES (?,?,?,?,?,'leased',?,?,?)",
                    (
                        row["conversation_id"],
                        row["shell_id"],
                        row["message_id"],
                        attempt,
                        row["harness_session_ref"],
                        owner,
                        expires,
                        now,
                    ),
                )
                run_id = int(cur.lastrowid)
                require_transition("outbox", "claimed", "dispatched")
                con.execute(
                    "UPDATE conversation_outbox SET state='dispatched',run_id=?,"
                    "dispatched_at=? WHERE outbox_id=?",
                    (run_id, now, row["outbox_id"]),
                )
                require_transition("conversation", "queued", "running")
                con.execute(
                    "UPDATE conversations SET state='running',"
                    "last_activity_at=?,version=version+1 "
                    "WHERE conversation_id=?",
                    (now, row["conversation_id"]),
                )
                return BrokerRun(
                    run_id=run_id,
                    conversation_id=row["conversation_id"],
                    message_id=int(row["message_id"]),
                    shell_id=int(row["shell_id"]),
                    harness=row["harness"],
                    provider=row["provider"],
                    model=row["model"],
                    effort=row["effort"],
                    worktree=Path(row["worktree"]),
                    title=row["title"],
                    body=row["body"],
                    session_before=row["harness_session_ref"],
                    session_after=None,
                    runner_ref=None,
                    state="leased",
                )
        except db_driver.IntegrityError as exc:
            # A second broker can race the eligibility read. BEGIN IMMEDIATE
            # serializes normal SQLite writers; the unique live-run indexes
            # remain the final fail-closed fence.
            detail = str(exc)
            if (
                "conversation_runs.shell_id" in detail
                or "conversation_runs.conversation_id" in detail
            ):
                return None
            raise
        finally:
            con.close()

    def adopt_recoverable(
        self,
        owner: str,
        *,
        startup: bool,
        limit: int,
    ) -> list[BrokerRun]:
        """Adopt bounded live runs from an expired broker lease.

        ``startup`` names the reconciliation trigger for callers and tests; it
        deliberately does not weaken the lease predicate. A second API process
        can reach startup before losing the listener bind race, so process
        startup alone is not proof that the current lease owner is dead.
        """
        if limit <= 0:
            return []
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(con, "conversation.broker.adopt"):
                selected = con.execute(
                    self._run_select()
                    + "WHERE r.state IN ('leased','starting','running') "
                    "AND r.lease_expires_at<=? ORDER BY r.run_id LIMIT ?",
                    (now, limit),
                ).fetchall()
                adopted: list[BrokerRun] = []
                for row in selected:
                    changed = con.execute(
                        "UPDATE conversation_runs SET lease_owner=?,"
                        "lease_expires_at=?,heartbeat_at=? "
                        "WHERE run_id=? AND state IN "
                        "('leased','starting','running')",
                        (owner, expires, now, row["run_id"]),
                    ).rowcount
                    if changed:
                        adopted.append(self._run_from_row(row))
                return adopted
        finally:
            con.close()

    def requeue_expired_claims(self) -> int:
        """Repair only pre-run claims; dispatched runs recover separately."""
        con = self.connect()
        now, _ = self._times()
        try:
            with db_driver.write_transaction(con, "conversation.broker.requeue"):
                count = con.execute(
                    "UPDATE conversation_outbox SET state='pending',"
                    "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL,"
                    "last_error='expired before durable run creation' "
                    "WHERE state='claimed' AND lease_expires_at<=? "
                    "AND run_id IS NULL",
                    (now,),
                ).rowcount
                return int(count)
        finally:
            con.close()

    def mark_starting(self, run_id: int, owner: str) -> None:
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.mark_starting",
            ):
                row = con.execute(
                    "SELECT state,lease_owner FROM conversation_runs "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise BrokerError("CONVERSATION_RUN_NOT_FOUND", str(run_id))
                require_transition("run", row["state"], "starting")
                changed = con.execute(
                    "UPDATE conversation_runs SET state='starting',started_at=?,"
                    "heartbeat_at=?,lease_expires_at=? "
                    "WHERE run_id=? AND lease_owner=? AND state='leased'",
                    (now, now, expires, run_id, owner),
                ).rowcount
                if changed != 1:
                    raise BrokerError(
                        "CONVERSATION_RUN_LEASE_LOST",
                        f"run {run_id} is not leased by this broker",
                    )
        finally:
            con.close()

    def bind_archive(self, run_id: int, owner: str, archive_id: int) -> None:
        """Attach the canonical shell session archive before native dispatch."""
        con = self.connect()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.bind_archive",
            ):
                changed = con.execute(
                    "UPDATE conversation_runs SET archive_id=? "
                    "WHERE run_id=? AND lease_owner=? AND state='leased' "
                    "AND archive_id IS NULL",
                    (archive_id, run_id, owner),
                ).rowcount
                if changed != 1:
                    raise BrokerError(
                        "CONVERSATION_RUN_LEASE_LOST",
                        f"run {run_id} is not an unbound lease owned by this broker",
                    )
        finally:
            con.close()

    def mark_native_started(
        self,
        run_id: int,
        owner: str,
        turn: NativeTurn,
    ) -> None:
        """Capture exact native identity before consuming the event stream."""
        # Filesystem normalization is external preparation, never writer-lock
        # work. Conversation creation stores an already-resolved absolute path.
        actual_worktree = Path(turn.worktree).resolve()
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.mark_native_started",
            ):
                row = con.execute(
                    "SELECT r.state,r.conversation_id,c.harness_session_ref,"
                    "c.harness,c.worktree "
                    "FROM conversation_runs r JOIN conversations c "
                    "ON c.conversation_id=r.conversation_id WHERE r.run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise BrokerError("CONVERSATION_RUN_NOT_FOUND", str(run_id))
                if turn.harness != row["harness"]:
                    raise BrokerInvariantError(
                        "HARNESS_IDENTITY_MISMATCH",
                        f"conversation uses {row['harness']}, "
                        f"adapter returned {turn.harness}",
                    )
                if not turn.session_ref or not turn.run_ref:
                    raise BrokerInvariantError(
                        "HARNESS_IDENTITY_MISSING",
                        "adapter returned an empty native session or run reference",
                    )
                expected_worktree = Path(row["worktree"])
                if actual_worktree != expected_worktree:
                    raise BrokerInvariantError(
                        "HARNESS_WORKTREE_MISMATCH",
                        f"conversation is bound to {expected_worktree}, "
                        f"adapter returned {actual_worktree}",
                    )
                current_session = row["harness_session_ref"]
                if (
                    current_session is not None
                    and current_session != turn.session_ref
                ):
                    raise BrokerInvariantError(
                        "HARNESS_SESSION_MISMATCH",
                        f"conversation is bound to {current_session}, "
                        f"adapter returned {turn.session_ref}",
                    )
                require_transition("run", row["state"], "running")
                changed = con.execute(
                    "UPDATE conversation_runs SET state='running',"
                    "harness_session_after=?,runner_ref=?,heartbeat_at=?,"
                    "lease_expires_at=? WHERE run_id=? AND lease_owner=? "
                    "AND state='starting'",
                    (
                        turn.session_ref,
                        turn.run_ref,
                        now,
                        expires,
                        run_id,
                        owner,
                    ),
                ).rowcount
                if changed != 1:
                    raise BrokerError(
                        "CONVERSATION_RUN_LEASE_LOST",
                        f"run {run_id} lost its starting lease",
                    )
                con.execute(
                    "UPDATE conversations SET harness_session_ref=?,"
                    "last_activity_at=?,version=version+1 "
                    "WHERE conversation_id=?",
                    (turn.session_ref, now, row["conversation_id"]),
                )
        finally:
            con.close()

    def append_event(
        self,
        run_id: int,
        event: NormalizedEvent,
    ) -> int:
        return self.append_events(run_id, [event])[0]

    def append_events(
        self,
        run_id: int,
        events: list[NormalizedEvent],
    ) -> list[int]:
        """Commit one ordered batch of non-terminal native events."""
        if not events:
            return []
        for event in events:
            if event.type in TERMINAL_EVENTS:
                raise BrokerError(
                    "CONVERSATION_TERMINAL_EVENT_REQUIRES_FINISH",
                    event.type,
                )
        con = self.connect()
        now, _ = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.append_events",
            ):
                row = con.execute(
                    "SELECT conversation_id,trigger_message_id,state "
                    "FROM conversation_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise BrokerError("CONVERSATION_RUN_NOT_FOUND", str(run_id))
                if row["state"] not in LIVE_RUN_STATES:
                    raise BrokerError(
                        "CONVERSATION_RUN_TERMINAL",
                        f"run {run_id} is already {row['state']}",
                    )
                sequences = []
                for event in events:
                    payload = dict(event.payload)
                    if event.native_type:
                        payload["native_type"] = event.native_type
                    if event.interrupt_evidence:
                        payload["interrupt_evidence"] = event.interrupt_evidence
                    sequences.append(
                        self._append_event(
                            con,
                            conversation_id=row["conversation_id"],
                            event_type=event.type,
                            payload=payload,
                            message_id=row["trigger_message_id"],
                            run_id=run_id,
                        )
                    )
                con.execute(
                    "UPDATE conversations SET last_activity_at=?,"
                    "version=version+1 WHERE conversation_id=?",
                    (now, row["conversation_id"]),
                )
                conversation_id = row["conversation_id"]
        finally:
            con.close()
        conversation_events.notify(conversation_id)
        return sequences

    def finish_run(
        self,
        run_id: int,
        outcome: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        exit_code: int | None = None,
    ) -> bool:
        """Commit terminal event and release every durable lock atomically."""
        if outcome not in TERMINAL_RUN_STATES:
            raise BrokerError(
                "CONVERSATION_OUTCOME_INVALID",
                f"unsupported terminal outcome: {outcome}",
            )
        con = self.connect()
        now, _ = self._times()
        notify_ids: set[str] = set()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.finish_run",
            ):
                row = con.execute(
                    "SELECT r.state,r.conversation_id,r.trigger_message_id,"
                    "c.state AS conversation_state,c.shell_id,"
                    "EXISTS(SELECT 1 FROM conversation_events closing "
                    " WHERE closing.conversation_id=r.conversation_id "
                    " AND closing.event_type='conversation.close.requested') "
                    "AS close_requested "
                    "FROM conversation_runs r JOIN conversations c "
                    "ON c.conversation_id=r.conversation_id "
                    "WHERE r.run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise BrokerError("CONVERSATION_RUN_NOT_FOUND", str(run_id))
                if row["state"] in TERMINAL_RUN_STATES:
                    return False
                require_transition("run", row["state"], outcome)
                message_state = {
                    "succeeded": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "unknown": "failed",
                }[outcome]
                message_current = con.execute(
                    "SELECT state FROM conversation_messages WHERE message_id=?",
                    (row["trigger_message_id"],),
                ).fetchone()[0]
                require_transition("message", message_current, message_state)
                close_after = bool(row["close_requested"])
                if close_after:
                    queued_ids = [
                        int(item["message_id"])
                        for item in con.execute(
                            "SELECT DISTINCT message_id FROM conversation_outbox "
                            "WHERE conversation_id=? "
                            "AND state IN ('pending','claimed')",
                            (row["conversation_id"],),
                        ).fetchall()
                    ]
                    if queued_ids:
                        marks = ",".join("?" for _ in queued_ids)
                        con.execute(
                            "UPDATE conversation_messages SET state='cancelled',"
                            "completed_at=? "
                            f"WHERE message_id IN ({marks}) "
                            "AND state IN ('accepted','queued','running')",
                            (now, *queued_ids),
                        )
                        con.execute(
                            "UPDATE conversation_outbox SET state='cancelled',"
                            "claim_owner=NULL,claimed_at=NULL,"
                            "lease_expires_at=NULL "
                            f"WHERE message_id IN ({marks}) "
                            "AND state IN ('pending','claimed')",
                            queued_ids,
                        )
                pending = (
                    con.execute(
                        "SELECT 1 FROM conversation_outbox "
                        "WHERE conversation_id=? "
                        "AND state IN ('pending','claimed') LIMIT 1",
                        (row["conversation_id"],),
                    ).fetchone()
                    is not None
                )
                conversation_target = (
                    "error"
                    if outcome in {"failed", "unknown"}
                    else "queued"
                    if pending
                    else "idle"
                )
                require_transition(
                    "conversation",
                    row["conversation_state"],
                    conversation_target,
                )
                con.execute(
                    "UPDATE conversation_runs SET state=?,ended_at=?,"
                    "exit_code=?,error_code=?,error_detail=? WHERE run_id=?",
                    (
                        outcome,
                        now,
                        exit_code,
                        error_code,
                        (error_detail or "")[:16384] or None,
                        run_id,
                    ),
                )
                con.execute(
                    "UPDATE conversation_messages SET state=?,completed_at=? "
                    "WHERE message_id=?",
                    (message_state, now, row["trigger_message_id"]),
                )
                con.execute(
                    "UPDATE conversations SET state=?,last_activity_at=?,"
                    "version=version+1 WHERE conversation_id=?",
                    (conversation_target, now, row["conversation_id"]),
                )
                terminal_payload = dict(payload or {})
                terminal_payload.setdefault("outcome", outcome)
                self._append_event(
                    con,
                    conversation_id=row["conversation_id"],
                    event_type=event_type,
                    payload=terminal_payload,
                    message_id=row["trigger_message_id"],
                    run_id=run_id,
                )
                if close_after:
                    require_transition(
                        "conversation",
                        conversation_target,
                        "closed",
                    )
                    con.execute(
                        "UPDATE conversations SET state='closed',closed_at=?,"
                        "last_activity_at=?,version=version+1 "
                        "WHERE conversation_id=?",
                        (now, now, row["conversation_id"]),
                    )
                    self._append_event(
                        con,
                        conversation_id=row["conversation_id"],
                        event_type="conversation.closed",
                        payload={"state": "closed", "recovered": True},
                        message_id=None,
                        run_id=run_id,
                    )
                notify_ids.add(str(row["conversation_id"]))
                conversation_id = row["conversation_id"]
        finally:
            con.close()
        for notify_id in notify_ids or {str(conversation_id)}:
            conversation_events.notify(notify_id)
        conversation_git_targets.safely_observe_and_persist(
            self.db_path,
            str(conversation_id),
        )
        return True

    def renew_runs(self, owner: str, run_ids: list[int]) -> int:
        if not run_ids:
            return 0
        con = self.connect()
        now, expires = self._times()
        marks = ",".join("?" for _ in run_ids)
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.renew_runs",
            ):
                changed = con.execute(
                    "UPDATE conversation_runs SET heartbeat_at=?,"
                    "lease_expires_at=? WHERE lease_owner=? "
                    f"AND state IN ('leased','starting','running') "
                    f"AND run_id IN ({marks})",
                    (now, expires, owner, *run_ids),
                ).rowcount
                return int(changed)
        finally:
            con.close()

    def note_reconcile_error(
        self,
        run_id: int,
        owner: str,
        detail: str,
    ) -> None:
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.note_reconcile_error",
            ):
                con.execute(
                    "UPDATE conversation_runs SET heartbeat_at=?,"
                    "lease_expires_at=?,error_detail=? "
                    "WHERE run_id=? AND lease_owner=? "
                    "AND state IN ('starting','running')",
                    (now, expires, detail[:16384], run_id, owner),
                )
        finally:
            con.close()

    def request_interrupt(self, run_id: int) -> bool:
        """Persist interrupt intent once; the active/recovery worker delivers it."""
        con = self.connect()
        now, _ = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.request_interrupt",
            ):
                created, conversation_id = self.request_interrupt_in_transaction(
                    con,
                    run_id,
                    requested_at=now,
                )
        finally:
            con.close()
        if created:
            conversation_events.notify(conversation_id)
        return created

    @staticmethod
    def request_interrupt_in_transaction(
        con,
        run_id: int,
        *,
        requested_at: str,
    ) -> tuple[bool, str]:
        """Persist one interrupt intent inside an existing owner transaction."""
        if not con.in_transaction:
            raise RuntimeError("interrupt intent requires an active transaction")
        row = con.execute(
            "SELECT conversation_id,trigger_message_id,state "
            "FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise BrokerError("CONVERSATION_RUN_NOT_FOUND", str(run_id))
        if row["state"] not in LIVE_RUN_STATES:
            raise BrokerError(
                "CONVERSATION_RUN_NOT_ACTIVE",
                f"run {run_id} is {row['state']}",
            )
        exists = con.execute(
            "SELECT 1 FROM conversation_events "
            "WHERE run_id=? AND event_type='run.interrupt.requested'",
            (run_id,),
        ).fetchone()
        if exists is None:
            BrokerStore._append_event(
                con,
                conversation_id=row["conversation_id"],
                event_type="run.interrupt.requested",
                payload={"requested_at": requested_at},
                message_id=row["trigger_message_id"],
                run_id=run_id,
            )
            con.execute(
                "UPDATE conversations SET last_activity_at=?,"
                "version=version+1 WHERE conversation_id=?",
                (requested_at, row["conversation_id"]),
            )
        return exists is None, str(row["conversation_id"])

    def heartbeat_service(self, interval_seconds: int) -> None:
        con = self.connect()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.heartbeat",
            ):
                con.execute(
                    "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                    "VALUES ('conversation-broker',datetime('now'),?) "
                    "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
                    "interval_s=excluded.interval_s",
                    (interval_seconds,),
                )
        finally:
            con.close()


class ConversationBroker(threading.Thread):
    """Commit-woken dispatcher plus bounded lease reconciliation."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        store: BrokerStore | None = None,
        adapter_factory: Callable[[str], ConversationAdapter] = adapter_for,
        launch_preparer: (
            Callable[[BrokerRun], tuple[ConversationContext, int]] | None
        ) = None,
        owner: str | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        recovery_seconds: int = DEFAULT_RECOVERY_SECONDS,
        reconcile_attempts: int = DEFAULT_RECONCILE_ATTEMPTS,
        event_batch_size: int = DEFAULT_EVENT_BATCH_SIZE,
        event_backlog_size: int = DEFAULT_EVENT_BACKLOG_SIZE,
        event_flush_seconds: float = DEFAULT_EVENT_FLUSH_SECONDS,
    ) -> None:
        super().__init__(name="conversation-broker", daemon=True)
        self.store = store or BrokerStore(db_path)
        self.adapter_factory = adapter_factory
        self.launch_preparer = launch_preparer
        self.owner = owner or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self.max_workers = max_workers
        self.heartbeat_seconds = heartbeat_seconds
        self.recovery_seconds = recovery_seconds
        self.reconcile_attempts = reconcile_attempts
        self.event_batch_size = event_batch_size
        self.event_backlog_size = event_backlog_size
        self.event_flush_seconds = event_flush_seconds
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._active: dict[int, _ActiveRun] = {}
        self._active_lock = threading.Lock()
        self._started_once = threading.Event()

    def notify(self) -> None:
        """Commit notification: call only after message/outbox commit."""
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def active_run_ids(self) -> list[int]:
        with self._active_lock:
            return sorted(self._active)

    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._started_once.wait(timeout)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.active_run_ids():
                return True
            time.sleep(0.01)
        return not self.active_run_ids()

    def interrupt(self, run_id: int) -> bool:
        """Persist and, when possible, immediately deliver an interrupt."""
        self.store.request_interrupt(run_id)
        with self._active_lock:
            active = self._active.get(run_id)
            if active is not None:
                active.interrupt_requested = True
        if active is not None:
            self._deliver_interrupt(active)
        self.notify()
        return True

    def _capacity(self) -> int:
        with self._active_lock:
            return max(0, self.max_workers - len(self._active))

    def _activate(self, run: BrokerRun) -> bool:
        with self._active_lock:
            if run.run_id in self._active or len(self._active) >= self.max_workers:
                return False
            active = _ActiveRun(
                run=run,
                interrupt_requested=run.interrupt_requested,
            )
            self._active[run.run_id] = active
        threading.Thread(
            target=self._execute,
            args=(active,),
            name=f"conversation-run-{run.run_id}",
            daemon=True,
        ).start()
        return True

    def _deactivate(self, run_id: int) -> None:
        with self._active_lock:
            self._active.pop(run_id, None)
        # A terminal commit may have moved this conversation back to queued.
        self.notify()

    def _dispatch(self) -> int:
        count = 0
        while self._capacity() > 0 and not self._stop_event.is_set():
            run = self.store.claim_next(self.owner)
            if run is None:
                break
            if not self._activate(run):
                break
            count += 1
        return count

    def _recover(self, *, startup: bool) -> int:
        capacity = self._capacity()
        if capacity <= 0:
            return 0
        runs = self.store.adopt_recoverable(
            self.owner,
            startup=startup,
            limit=capacity,
        )
        return sum(1 for run in runs if self._activate(run))

    @staticmethod
    def _close_adapter(adapter: ConversationAdapter | None) -> None:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _deliver_interrupt(self, active: _ActiveRun) -> None:
        with self._active_lock:
            if active.interrupt_sent or not active.interrupt_requested:
                return
            adapter, turn = active.adapter, active.turn
            if adapter is None or turn is None:
                return
            active.interrupt_sent = True
        try:
            result = adapter.interrupt(turn)
        except Exception:
            # Durable intent remains for reconciliation; interruption failure
            # never asserts a terminal state.
            with self._active_lock:
                active.interrupt_sent = False
            return
        if not result.acknowledged:
            with self._active_lock:
                active.interrupt_sent = False

    @staticmethod
    def _terminal_event(outcome: str) -> str:
        return {
            "succeeded": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.interrupted",
            "unknown": "run.unknown",
        }[outcome]

    def _retry_busy(
        self,
        action: Callable[[], Any],
        *,
        wait: bool,
    ) -> tuple[bool, Any]:
        """Retry only exhausted SQLite writer acquisition, never native work."""
        while not self._stop_event.is_set():
            try:
                return True, action()
            except Exception as exc:
                if not db_driver.is_busy_error(exc):
                    raise
                if not wait:
                    return False, None
                self._stop_event.wait(min(1.0, self.recovery_seconds))
        return False, None

    def _flush_events(
        self,
        run_id: int,
        pending: list[NormalizedEvent],
        *,
        wait: bool,
    ) -> bool:
        if not pending:
            return True
        batch = list(pending)
        persisted, _result = self._retry_busy(
            partial(self.store.append_events, run_id, batch),
            wait=wait,
        )
        if persisted:
            del pending[: len(batch)]
        return persisted

    def _finish_adapter_event(
        self,
        active: _ActiveRun,
        event: NormalizedEvent,
    ) -> bool:
        if event.type == "run.failed" and active.interrupt_requested:
            event = NormalizedEvent(
                "run.interrupted",
                {
                    **dict(event.payload),
                    "status": "cancelled",
                    "adapter_terminal": "run.failed",
                },
                event.native_type,
                "operator",
            )
        outcome = terminal_outcome(event.type)
        error = event.payload.get("error")
        exit_code = event.payload.get("exit_code")
        persisted, _result = self._retry_busy(
            partial(
                self.store.finish_run,
                active.run.run_id,
                outcome,
                event_type=event.type,
                payload={
                    **dict(event.payload),
                    **(
                        {"native_type": event.native_type}
                        if event.native_type
                        else {}
                    ),
                    **(
                        {"interrupt_evidence": event.interrupt_evidence}
                        if event.interrupt_evidence
                        else {}
                    ),
                },
                error_code=str(error)[:255] if error else None,
                error_detail=str(error) if error else None,
                exit_code=exit_code if isinstance(exit_code, int) else None,
            ),
            wait=True,
        )
        return persisted

    def _finish_error(
        self,
        run_id: int,
        exc: Exception,
        *,
        proven: bool,
    ) -> None:
        code = getattr(exc, "code", "BROKER_RUN_ERROR")
        detail = getattr(exc, "detail", str(exc))
        outcome = "failed" if proven else "unknown"
        self._retry_busy(
            partial(
                self.store.finish_run,
                run_id,
                outcome,
                event_type=self._terminal_event(outcome),
                payload={"error": str(code), "detail": str(detail)},
                error_code=str(code)[:255],
                error_detail=str(detail),
            ),
            wait=True,
        )

    def _reconcile_loop(
        self,
        active: _ActiveRun,
        context: ConversationContext,
    ) -> None:
        adapter = active.adapter
        turn = active.turn
        if adapter is None or turn is None:
            self._finish_error(
                active.run.run_id,
                BrokerError(
                    "HARNESS_IDENTITY_NOT_CAPTURED",
                    "native dispatch crossed the starting boundary without "
                    "a durable exact session/run reference",
                ),
                proven=False,
            )
            return

        failures = 0
        while not self._stop_event.is_set():
            self._deliver_interrupt(active)
            try:
                result = adapter.reconcile(turn, context)
            except Exception as exc:
                failures += 1
                reconcile_detail = f"reconcile attempt {failures}: {exc}"
                self._retry_busy(
                    partial(
                        self.store.note_reconcile_error,
                        active.run.run_id,
                        self.owner,
                        reconcile_detail,
                    ),
                    wait=True,
                )
                if failures >= self.reconcile_attempts:
                    self._finish_error(active.run.run_id, exc, proven=False)
                    return
                self._stop_event.wait(self.recovery_seconds)
                continue

            if result.outcome == "running" and result.proven:
                failures = 0
                self._retry_busy(
                    partial(
                        self.store.renew_runs,
                        self.owner,
                        [active.run.run_id],
                    ),
                    wait=True,
                )
                self._stop_event.wait(self.recovery_seconds)
                continue
            outcome = result.outcome if result.proven else "unknown"
            terminal_payload = {
                "reconciled": True,
                "proven": result.proven,
                "detail": result.detail,
            }
            error_code = (
                "HARNESS_OUTCOME_UNKNOWN" if outcome == "unknown" else None
            )
            error_detail = (
                result.detail if outcome in {"failed", "unknown"} else None
            )
            self._retry_busy(
                partial(
                    self.store.finish_run,
                    active.run.run_id,
                    outcome,
                    event_type=self._terminal_event(outcome),
                    payload=terminal_payload,
                    error_code=error_code,
                    error_detail=error_detail,
                ),
                wait=True,
            )
            return

    def _execute(self, active: _ActiveRun) -> None:
        run = active.run
        adapter: ConversationAdapter | None = None
        try:
            context = run.context()
            if run.state == "leased":
                try:
                    # Validate failures here are proven pre-dispatch.
                    if self.launch_preparer is not None:
                        context, archive_id = self.launch_preparer(run)
                        self.store.bind_archive(
                            run.run_id,
                            self.owner,
                            archive_id,
                        )
                    context.checked_worktree()
                    adapter = self.adapter_factory(run.harness)
                    active.adapter = adapter
                except Exception as exc:
                    self._finish_error(run.run_id, exc, proven=True)
                    return

                self.store.mark_starting(run.run_id, self.owner)
                try:
                    turn = (
                        adapter.resume(run.session_before, context, run.body)
                        if run.session_before
                        else adapter.start(context, run.body)
                    )
                except Exception as exc:
                    # The adapter may have sent the prompt before its transport
                    # failed. Starting is the explicit uncertain crash window.
                    self._finish_error(run.run_id, exc, proven=False)
                    return
                active.turn = turn
                self.store.mark_native_started(run.run_id, self.owner, turn)
                self._deliver_interrupt(active)

                terminal = False
                pending: list[NormalizedEvent] = []
                last_flush = time.monotonic()
                retry_after = 0.0
                try:
                    for event in adapter.stream(turn):
                        if event.type in TERMINAL_EVENTS:
                            if not self._flush_events(
                                run.run_id,
                                pending,
                                wait=True,
                            ):
                                return
                            terminal = self._finish_adapter_event(active, event)
                            break
                        pending.append(event)
                        now = time.monotonic()
                        should_flush = (
                            event.type != "assistant.delta"
                            or len(pending) >= self.event_batch_size
                            or now - last_flush >= self.event_flush_seconds
                        )
                        force_backpressure = (
                            len(pending) >= self.event_backlog_size
                        )
                        if not should_flush and not force_backpressure:
                            continue
                        if now < retry_after and not force_backpressure:
                            continue
                        if self._flush_events(
                            run.run_id,
                            pending,
                            wait=force_backpressure,
                        ):
                            last_flush = time.monotonic()
                            retry_after = 0.0
                        else:
                            retry_after = now + self.recovery_seconds
                except Exception:
                    # Exact identities are durable; inspect them rather than
                    # replaying a message after a broken stream.
                    pass
                if terminal:
                    return
                if not self._flush_events(
                    run.run_id,
                    pending,
                    wait=True,
                ):
                    return
                self._reconcile_loop(active, context)
                return

            # Startup/expiry recovery for a native-dispatch window.
            adapter = self.adapter_factory(run.harness)
            active.adapter = adapter
            session_ref = run.session_after or run.session_before
            if run.state == "starting" or not session_ref or not run.runner_ref:
                self._finish_error(
                    run.run_id,
                    BrokerError(
                        "HARNESS_IDENTITY_NOT_CAPTURED",
                        "no exact native session/run reference survived the "
                        "dispatch crash window",
                    ),
                    proven=False,
                )
                return
            active.turn = NativeTurn(
                harness=run.harness,
                session_ref=session_ref,
                run_ref=run.runner_ref,
                worktree=run.worktree.resolve(),
                metadata={"recovered": True},
            )
            self._deliver_interrupt(active)
            self._reconcile_loop(active, run.context())
        except Exception as exc:
            # Store failures are loud in service stderr, but try to close the
            # durable run if its state still permits a conservative terminal.
            try:
                self._finish_error(run.run_id, exc, proven=False)
            except Exception as finish_exc:
                print(
                    f"conversation-broker: run {run.run_id} failed: {exc}; "
                    f"terminal commit also failed: {finish_exc}",
                    flush=True,
                )
        finally:
            self._close_adapter(adapter)
            self._deactivate(run.run_id)

    def run(self) -> None:  # pragma: no cover - loop behavior tested through seams
        try:
            repaired = self.store.requeue_expired_claims()
            recovered = self._recover(startup=True)
            self.store.heartbeat_service(self.heartbeat_seconds)
            self._dispatch()
            if repaired or recovered:
                print(
                    "conversation-broker: startup "
                    f"requeued={repaired} recovered={recovered}",
                    flush=True,
                )
        except Exception as exc:
            # The API remains available for inspection/retry; the daemon keeps
            # trying instead of taking the localhost server down.
            print(f"conversation-broker: startup error ({exc})", flush=True)
            self._wake.set()
        self._started_once.set()

        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        next_recovery = time.monotonic() + self.recovery_seconds
        while not self._stop_event.is_set():
            due = min(next_heartbeat, next_recovery)
            signalled = self._wake.wait(max(0.0, due - time.monotonic()))
            self._wake.clear()
            now = time.monotonic()
            try:
                if now >= next_heartbeat:
                    self.store.renew_runs(self.owner, self.active_run_ids())
                    self.store.heartbeat_service(self.heartbeat_seconds)
                    next_heartbeat = now + self.heartbeat_seconds
                if now >= next_recovery:
                    self.store.requeue_expired_claims()
                    self._recover(startup=False)
                    next_recovery = now + self.recovery_seconds
                # Routine dispatch only follows startup, a post-commit notify,
                # or a worker's terminal notify. Timer maintenance is not the
                # ordinary queue transport.
                if signalled:
                    self._dispatch()
            except Exception as exc:
                print(f"conversation-broker: cycle error ({exc})", flush=True)
                # Preserve the commit wake across transient DB contention.
                # Back off first so a persistent fault cannot busy-loop.
                self._stop_event.wait(min(1.0, self.recovery_seconds))
                self._wake.set()


_SERVICE_LOCK = threading.Lock()
_SERVICE: ConversationBroker | None = None


def start_service(
    db_path: str | Path,
    **kwargs: Any,
) -> ConversationBroker:
    """Install the process-wide service used by API commit notifications."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE.is_alive():
            return _SERVICE
        _SERVICE = ConversationBroker(db_path, **kwargs)
        _SERVICE.start()
        return _SERVICE


def service() -> ConversationBroker | None:
    return _SERVICE


def notify_commit() -> bool:
    broker = service()
    if broker is None or not broker.is_alive():
        return False
    broker.notify()
    return True


def interrupt_run(run_id: int) -> bool:
    broker = service()
    if broker is None or not broker.is_alive():
        raise BrokerError(
            "CONVERSATION_BROKER_UNAVAILABLE",
            "conversation broker is not running",
        )
    return broker.interrupt(run_id)
