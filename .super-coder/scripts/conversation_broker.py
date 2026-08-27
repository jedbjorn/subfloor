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
import queue
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

import active_chat_registry
import conversation_events
import conversation_git_targets
import db_driver
import route_transport
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
DEFAULT_BUSY_RETRY_ATTEMPTS = 6
_STREAM_END = object()


def unexpected_error_code(exc: Exception) -> str:
    """Classify an internal exception without persisting its message."""
    return f"BROKER_RUN_{type(exc).__name__.upper()}"[:255]


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
    route_contract_version: int = 1
    route_binding: Mapping[str, Any] | None = None
    binding_digest: str | None = None
    lifecycle_epoch: int = 1

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
            route_binding=self.route_binding,
            binding_digest=self.binding_digest,
            conversation_id=self.conversation_id,
            lifecycle_epoch=self.lifecycle_epoch,
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
    def _deepseek_usage_tokens(payload: Mapping[str, Any]) -> dict[str, int | None]:
        raw = payload.get("tokens")
        if not isinstance(raw, Mapping):
            return {}
        values: dict[str, int | None] = {}
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            value = raw.get(field)
            values[field] = (
                int(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= 0
                and int(value) == value
                else None
            )
        return values if any(value is not None for value in values.values()) else {}

    @staticmethod
    def _record_deepseek_usage(con, row, payload: Mapping[str, Any], now: str) -> None:
        """Feed normalized browser usage into the existing analytics store."""
        if row["harness"] != "deepseek" or not row["harness_session_ref"]:
            return
        tokens = BrokerStore._deepseek_usage_tokens(payload)
        if not tokens:
            return
        status = (
            "ok"
            if tokens["input_tokens"] is not None
            and tokens["output_tokens"] is not None
            else "partial"
        )
        params = [
            row["shell_id"],
            row["provider"],
            row["title"],
            row["created_at"],
            now,
        ]
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            params.extend((tokens[field], tokens[field]))
        params.extend(
            (
                status,
                now,
                row["harness"],
                row["harness_session_ref"],
                row["model"],
            )
        )
        changed = con.execute(
            "UPDATE session_token_usage SET shell_id=?,provider=?,title=?,"
            "started_at=COALESCE(started_at,?),ended_at=?,"
            + ",".join(
                f"{field}=CASE WHEN ? IS NULL THEN {field} "
                f"ELSE COALESCE({field},0)+? END"
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                )
            )
            + ",status=CASE WHEN status='ok' OR ?='ok' THEN 'ok' "
            "ELSE 'partial' END,parser_version='browser-events-v1',captured_at=? "
            "WHERE harness=? AND harness_session_ref=? AND model IS ?",
            params,
        ).rowcount
        if changed:
            return
        con.execute(
            "INSERT INTO session_token_usage "
            "(shell_id,harness,harness_session_ref,provider,model,title,"
            "started_at,ended_at,input_tokens,output_tokens,cache_read_tokens,"
            "cache_write_tokens,reasoning_tokens,status,parser_version,captured_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'browser-events-v1',?)",
            (
                row["shell_id"],
                row["harness"],
                row["harness_session_ref"],
                row["provider"],
                row["model"],
                row["title"],
                row["created_at"],
                now,
                tokens["input_tokens"],
                tokens["output_tokens"],
                tokens["cache_read_tokens"],
                tokens["cache_write_tokens"],
                tokens["reasoning_tokens"],
                status,
                now,
            ),
        )

    @staticmethod
    def _route_from_row(
        row,
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        contract_version = int(row["route_contract_version"])
        if contract_version == 1:
            return contract_version, None, None
        if contract_version not in {
            route_transport.route_bindings.V2_CONTRACT_VERSION,
            route_transport.route_bindings.LIVE_NATIVE_CONTRACT_VERSION,
        }:
            raise BrokerInvariantError(
                "CONVERSATION_ROUTE_INVALID",
                f"unsupported conversation route contract: {contract_version}",
            )
        try:
            binding = json.loads(row["route_binding"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerInvariantError(
                "CONVERSATION_ROUTE_INVALID",
                "versioned conversation route binding is not valid JSON",
            ) from exc
        if not isinstance(binding, dict):
            raise BrokerInvariantError(
                "CONVERSATION_ROUTE_INVALID",
                "versioned conversation route binding must be an object",
            )
        try:
            route_transport.route_bindings.validate_binding(binding)
        except (
            TypeError,
            route_transport.route_bindings.RouteResolutionError,
        ) as exc:
            raise BrokerInvariantError(
                "CONVERSATION_ROUTE_INVALID",
                "versioned conversation route binding is invalid",
            ) from exc
        return (
            contract_version,
            binding,
            route_transport.route_bindings.digest_json(binding),
        )

    @staticmethod
    def _run_from_row(row) -> BrokerRun:
        contract_version, binding, binding_digest = BrokerStore._route_from_row(row)
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
            route_contract_version=contract_version,
            route_binding=binding,
            binding_digest=binding_digest,
            lifecycle_epoch=int(row["lifecycle_epoch"]),
        )

    @staticmethod
    def _run_select() -> str:
        return (
            "SELECT r.run_id,r.conversation_id,r.trigger_message_id,r.shell_id,"
            "r.harness_session_before,r.harness_session_after,r.runner_ref,"
            "r.state AS run_state,c.harness,c.provider,c.model,c.effort,"
            "c.route_contract_version,c.route_binding,"
            "c.worktree,c.title,m.body,"
            "1+(SELECT COUNT(*) FROM conversation_events reopened "
            " WHERE reopened.conversation_id=r.conversation_id "
            " AND reopened.event_type='conversation.reopened') AS lifecycle_epoch,"
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
                    "c.shell_id,c.harness,c.provider,c.model,c.effort,"
                    "c.route_contract_version,c.route_binding,c.worktree,"
                    "c.title,c.harness_session_ref,m.body,m.state AS message_state,"
                    "1+(SELECT COUNT(*) FROM conversation_events reopened "
                    " WHERE reopened.conversation_id=c.conversation_id "
                    " AND reopened.event_type='conversation.reopened') AS lifecycle_epoch "
                    "FROM conversation_outbox o "
                    "JOIN conversations c "
                    " ON c.conversation_id=o.conversation_id "
                    "JOIN active_shell_chats active "
                    " ON active.shell_id=c.shell_id "
                    " AND active.chat_id=c.conversation_id "
                    "JOIN conversation_messages m ON m.message_id=o.message_id "
                    "WHERE o.state='pending' AND c.state='queued' "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM conversation_events closing "
                    " WHERE closing.conversation_id=c.conversation_id "
                    " AND closing.event_type='conversation.close.requested'"
                    " AND closing.sequence>COALESCE(("
                    "  SELECT MAX(reopened.sequence) "
                    "  FROM conversation_events reopened "
                    "  WHERE reopened.conversation_id=closing.conversation_id "
                    "  AND reopened.event_type='conversation.reopened'),0)"
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
                contract_version, binding, binding_digest = self._route_from_row(row)
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
                    route_contract_version=contract_version,
                    route_binding=binding,
                    binding_digest=binding_digest,
                    lifecycle_epoch=int(row["lifecycle_epoch"]),
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
                    "AND (r.process_pid IS NULL OR EXISTS ("
                    " SELECT 1 FROM active_shell_chats active "
                    " WHERE active.process_pid=r.process_pid "
                    " AND active.process_start_ticks=r.process_start_ticks"
                    ")) "
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
        process = active_chat_registry.process_details(turn.process_ref)
        process_pid = process.pid if process is not None else None
        process_start_ticks = process.start_ticks if process is not None else None
        process_group_id = (
            process.process_group_id if process is not None else None
        )
        con = self.connect()
        now, expires = self._times()
        try:
            with db_driver.write_transaction(
                con,
                "conversation.broker.mark_native_started",
            ):
                row = con.execute(
                    "SELECT r.state,r.conversation_id,r.shell_id,"
                    "c.harness_session_ref,"
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
                    "lease_expires_at=?,process_pid=?,process_start_ticks=?,"
                    "process_group_id=? WHERE run_id=? AND lease_owner=? "
                    "AND state='starting'",
                    (
                        turn.session_ref,
                        turn.run_ref,
                        now,
                        expires,
                        process_pid,
                        process_start_ticks,
                        process_group_id,
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
                active_chat_registry.set_process(
                    con,
                    shell_id=int(row["shell_id"]),
                    chat_id=str(row["conversation_id"]),
                    pid=process_pid,
                    start_ticks=process_start_ticks,
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
                    "SELECT r.conversation_id,r.trigger_message_id,r.state,"
                    "c.shell_id,c.harness,c.provider,c.model,c.title,c.created_at,"
                    "c.harness_session_ref FROM conversation_runs r "
                    "JOIN conversations c ON c.conversation_id=r.conversation_id "
                    "WHERE r.run_id=?",
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
                    if event.type == "usage":
                        self._record_deepseek_usage(con, row, payload, now)
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
                    " AND closing.event_type='conversation.close.requested' "
                    " AND closing.sequence>COALESCE(("
                    "  SELECT MAX(reopened.sequence) "
                    "  FROM conversation_events reopened "
                    "  WHERE reopened.conversation_id=closing.conversation_id "
                    "  AND reopened.event_type='conversation.reopened'),0)) "
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
                already_closed = row["conversation_state"] == "closed"
                if not already_closed:
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
                active_chat_registry.clear_process(
                    con,
                    shell_id=int(row["shell_id"]),
                    chat_id=str(row["conversation_id"]),
                )
                con.execute(
                    "UPDATE conversation_messages SET state=?,completed_at=? "
                    "WHERE message_id=?",
                    (message_state, now, row["trigger_message_id"]),
                )
                if not already_closed:
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
                if close_after and not already_closed:
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
        busy_retry_attempts: int = DEFAULT_BUSY_RETRY_ATTEMPTS,
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
        if event_batch_size <= 0:
            raise ValueError("event_batch_size must be positive")
        if event_backlog_size <= 0:
            raise ValueError("event_backlog_size must be positive")
        if event_flush_seconds <= 0:
            raise ValueError("event_flush_seconds must be positive")
        if busy_retry_attempts <= 0:
            raise ValueError("busy_retry_attempts must be positive")
        self.event_batch_size = event_batch_size
        self.event_backlog_size = event_backlog_size
        self.event_flush_seconds = event_flush_seconds
        self.busy_retry_attempts = busy_retry_attempts
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
        attempts = 0
        while not self._stop_event.is_set():
            attempts += 1
            try:
                return True, action()
            except Exception as exc:
                if not db_driver.is_busy_error(exc):
                    raise
                if not wait or attempts >= self.busy_retry_attempts:
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
        batch = list(pending[: self.event_batch_size])
        persisted, _result = self._retry_busy(
            partial(self.store.append_events, run_id, batch),
            wait=wait,
        )
        if persisted:
            del pending[: len(batch)]
        return persisted

    @staticmethod
    def _produce_stream(
        adapter: ConversationAdapter,
        turn: NativeTurn,
        items: queue.Queue[object],
        stopped: threading.Event,
    ) -> None:
        """Drain one native stream into its bounded per-run queue."""
        try:
            for event in adapter.stream(turn):
                while not stopped.is_set():
                    try:
                        items.put(event, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if stopped.is_set() or event.type in TERMINAL_EVENTS:
                    break
        except Exception as exc:
            while not stopped.is_set():
                try:
                    items.put(exc, timeout=0.1)
                    break
                except queue.Full:
                    continue
        finally:
            while not stopped.is_set():
                try:
                    items.put(_STREAM_END, timeout=0.1)
                    break
                except queue.Full:
                    continue

    def _consume_stream(
        self,
        active: _ActiveRun,
        adapter: ConversationAdapter,
        turn: NativeTurn,
    ) -> bool:
        """Persist one stream with real flush deadlines and bounded batches."""
        items: queue.Queue[object] = queue.Queue(
            maxsize=self.event_backlog_size
        )
        stopped = threading.Event()
        threading.Thread(
            target=self._produce_stream,
            args=(adapter, turn, items, stopped),
            name=f"conversation-stream-{active.run.run_id}",
            daemon=True,
        ).start()
        pending: list[NormalizedEvent] = []
        pending_since: float | None = None
        retry_after = 0.0
        stream_error: Exception | None = None
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                flush_at = None
                if pending_since is not None:
                    flush_at = max(
                        pending_since + self.event_flush_seconds,
                        retry_after,
                    )
                timeout = 0.1
                if flush_at is not None:
                    timeout = min(timeout, max(0.0, flush_at - now))
                if (
                    len(pending) >= self.event_batch_size
                    and now >= retry_after
                ):
                    timeout = 0.0
                try:
                    item = items.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if isinstance(item, NormalizedEvent):
                    if item.type in TERMINAL_EVENTS:
                        while pending:
                            if not self._flush_events(
                                active.run.run_id,
                                pending,
                                wait=True,
                            ):
                                return False
                        return self._finish_adapter_event(active, item)
                    if not pending:
                        pending_since = time.monotonic()
                    pending.append(item)
                elif isinstance(item, Exception):
                    stream_error = item
                    break
                elif item is _STREAM_END:
                    break

                now = time.monotonic()
                deadline_reached = (
                    pending_since is not None
                    and now >= pending_since + self.event_flush_seconds
                )
                meaningful_event = (
                    isinstance(item, NormalizedEvent)
                    and item.type != "assistant.delta"
                )
                batch_ready = len(pending) >= self.event_batch_size
                force_backpressure = (
                    len(pending) >= self.event_backlog_size
                )
                should_flush = (
                    meaningful_event
                    or batch_ready
                    or deadline_reached
                    or force_backpressure
                )
                if not should_flush:
                    continue
                if now < retry_after and not force_backpressure:
                    continue
                if self._flush_events(
                    active.run.run_id,
                    pending,
                    wait=force_backpressure,
                ):
                    pending_since = time.monotonic() if pending else None
                    retry_after = 0.0
                else:
                    retry_after = now + self.recovery_seconds

            while pending:
                if not self._flush_events(
                    active.run.run_id,
                    pending,
                    wait=True,
                ):
                    return False
            if stream_error is not None:
                # Preserve synchronous submission evidence. Reconciling an
                # idle native session here would erase the precise failure.
                raise stream_error
            return False
        finally:
            stopped.set()

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
        detail = event.payload.get("detail")
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
                error_detail=(
                    str(detail)[:16384]
                    if detail is not None
                    else str(error) if error else None
                ),
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
        code = getattr(exc, "code", unexpected_error_code(exc))
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
                persisted, _result = self._retry_busy(
                    partial(
                        self.store.note_reconcile_error,
                        active.run.run_id,
                        self.owner,
                        reconcile_detail,
                    ),
                    wait=True,
                )
                if not persisted:
                    return
                if failures >= self.reconcile_attempts:
                    self._finish_error(active.run.run_id, exc, proven=False)
                    return
                self._stop_event.wait(self.recovery_seconds)
                continue

            if result.outcome == "running" and result.proven:
                failures = 0
                persisted, _result = self._retry_busy(
                    partial(
                        self.store.renew_runs,
                        self.owner,
                        [active.run.run_id],
                    ),
                    wait=True,
                )
                if not persisted:
                    return
                self._stop_event.wait(self.recovery_seconds)
                continue
            outcome = result.outcome if result.proven else "unknown"
            terminal_payload = {
                "reconciled": True,
                "proven": result.proven,
                "detail": result.detail,
                **(
                    {"interrupt_evidence": result.interrupt_evidence}
                    if result.interrupt_evidence
                    else {}
                ),
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

                if self._consume_stream(active, adapter, turn):
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
            recovery = getattr(self.launch_preparer, "recovery", None)
            if run.harness == "deepseek" and not callable(recovery):
                raise BrokerError(
                    "HARNESS_SHELL_IDENTITY_UNAVAILABLE",
                    "DeepSeek recovery requires canonical launch preparation",
                )
            context = recovery(run) if callable(recovery) else run.context()
            self._reconcile_loop(active, context)
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
