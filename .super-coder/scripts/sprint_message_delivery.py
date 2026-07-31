#!/usr/bin/env python3
"""Dedicated Sprint v2 messages and retryable wake delivery.

Sprint messages are durable collaboration facts.  An active message commits
its wake intent in the same SQLite transaction; a later delivery worker claims
that intent, resolves the participant's current conversation, and performs the
external harness enqueue outside the database transaction.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

import db_driver
from sprint_domain import SprintInvariantError, SprintLifecycleStore


FIXED_WAKE_PROMPT = (
    "Check your inbox. If you accept the task(s), mark the message as read "
    "and act on the message using your assigned sprint skill."
)
ACTIONABLE_KINDS = frozenset({"work_assignment", "review_request"})


@dataclass(frozen=True)
class MessageReceipt:
    message_id: int
    wake_id: int | None
    created: bool


@dataclass(frozen=True)
class WakeLease:
    wake_id: int
    sprint_id: int
    participant_id: int
    target_conversation_id: str | None
    idempotency_key: str
    attempt_number: int
    claim_owner: str


@dataclass(frozen=True)
class DeliveryOutcome:
    wake_id: int
    state: str
    attempt_number: int


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SprintMessageStore:
    """Transactional inbox, acceptance, decline, and wake coalescing."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def send(
        self,
        sprint_id: int,
        *,
        to_participant_id: int,
        message_kind: str,
        body: str,
        idempotency_key: str,
        from_participant_id: int | None = None,
        work_unit_id: int | None = None,
        actionable: bool = False,
        active: bool = True,
    ) -> MessageReceipt:
        body = body.strip()
        key = idempotency_key.strip()
        if not body:
            raise ValueError("Sprint message body is empty")
        if not key:
            raise ValueError("Sprint message idempotency key is empty")
        if actionable and message_kind not in ACTIONABLE_KINDS:
            raise SprintInvariantError(
                "only work assignments and review requests are actionable"
            )
        with db_driver.write_transaction(self.con, "sprint.message.send"):
            return self._send(
                sprint_id,
                to_participant_id=to_participant_id,
                message_kind=message_kind,
                body=body,
                idempotency_key=key,
                from_participant_id=from_participant_id,
                work_unit_id=work_unit_id,
                actionable=actionable,
                active=active,
            )

    def inbox(self, sprint_id: int, shell_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT m.* FROM sprint_messages m "
            "JOIN sprint_participants p "
            "ON p.sprint_id=m.sprint_id AND p.participant_id=m.to_participant_id "
            "WHERE m.sprint_id=? AND p.shell_id=? AND m.read_at IS NULL "
            "ORDER BY m.message_id",
            (sprint_id, shell_id),
        ).fetchall()

    def mark_read(self, message_id: int, shell_id: int) -> str | None:
        """Read one message; actionable reads atomically accept it."""
        with db_driver.write_transaction(self.con, "sprint.message.read"):
            message = self._recipient_message(message_id, shell_id)
            if message["actionable"]:
                if message["disposition"] == "declined":
                    return "declined"
                if message["disposition"] == "pending":
                    self.con.execute(
                        "UPDATE sprint_messages SET disposition='accepted',"
                        "read_at=datetime('now') WHERE message_id=?",
                        (message_id,),
                    )
                disposition = "accepted"
            else:
                self.con.execute(
                    "UPDATE sprint_messages SET read_at=COALESCE(read_at,datetime('now')) "
                    "WHERE message_id=?",
                    (message_id,),
                )
                disposition = None
            self._cancel_resolved_wakes(message_id)
            return disposition

    def decline(self, message_id: int, shell_id: int, reason: str) -> int:
        """Resolve an actionable message and actively route the result to Planner."""
        reason = reason.strip()
        if not reason:
            raise ValueError("decline requires a reason")
        with db_driver.write_transaction(self.con, "sprint.message.decline"):
            message = self._recipient_message(message_id, shell_id)
            if not message["actionable"]:
                raise SprintInvariantError("informational messages cannot be declined")
            if message["disposition"] == "accepted":
                raise SprintInvariantError("accepted Sprint messages cannot be declined")
            if message["disposition"] == "pending":
                self.con.execute(
                    "UPDATE sprint_messages SET disposition='declined',"
                    "read_at=datetime('now'),decline_reason=? WHERE message_id=?",
                    (reason, message_id),
                )
                if message["message_kind"] == "work_assignment" and message[
                    "work_unit_id"
                ] is not None:
                    self.con.execute(
                        "UPDATE sprint_work_units SET disposition='planned',"
                        "updated_at=datetime('now') WHERE work_unit_id=?",
                        (message["work_unit_id"],),
                    )
            elif message["decline_reason"] != reason:
                raise SprintInvariantError(
                    "decline was already recorded with a different reason"
                )

            planner = self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND role='planner'",
                (message["sprint_id"],),
            ).fetchone()
            if planner is None:
                raise SprintInvariantError("Sprint has no Planner participant")
            lifecycle = self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (message["sprint_id"],),
            ).fetchone()[0]
            routed = self._send(
                int(message["sprint_id"]),
                to_participant_id=int(planner["participant_id"]),
                message_kind="notification",
                body=(
                    f"Participant declined {message['message_kind']} message "
                    f"#{message_id}: {reason}"
                ),
                idempotency_key=f"decline-result:{message_id}",
                from_participant_id=int(message["to_participant_id"]),
                work_unit_id=(
                    int(message["work_unit_id"])
                    if message["work_unit_id"] is not None
                    else None
                ),
                actionable=False,
                active=lifecycle == "armed",
            )
            self._cancel_resolved_wakes(message_id)
            return routed.message_id

    def _send(
        self,
        sprint_id: int,
        *,
        to_participant_id: int,
        message_kind: str,
        body: str,
        idempotency_key: str,
        from_participant_id: int | None,
        work_unit_id: int | None,
        actionable: bool,
        active: bool,
    ) -> MessageReceipt:
        sprint = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if active and sprint["lifecycle"] != "armed":
            raise SprintInvariantError("active Sprint messages require an armed Sprint")
        recipient = self.con.execute(
            "SELECT 1 FROM sprint_participants "
            "WHERE sprint_id=? AND participant_id=?",
            (sprint_id, to_participant_id),
        ).fetchone()
        if recipient is None:
            raise SprintInvariantError("recipient is not a Sprint participant")

        existing = self.con.execute(
            "SELECT * FROM sprint_messages WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            actual = (
                int(existing["sprint_id"]),
                existing["from_participant_id"],
                int(existing["to_participant_id"]),
                existing["work_unit_id"],
                str(existing["message_kind"]),
                str(existing["body"]),
                bool(existing["actionable"]),
            )
            expected = (
                sprint_id,
                from_participant_id,
                to_participant_id,
                work_unit_id,
                message_kind,
                body,
                actionable,
            )
            wake = self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (existing["message_id"],),
            ).fetchone()
            if actual != expected or (wake is not None) != active:
                raise SprintInvariantError(
                    "Sprint message idempotency key was reused with different input"
                )
            return MessageReceipt(
                int(existing["message_id"]),
                int(wake["wake_id"]) if wake is not None else None,
                False,
            )

        disposition = "pending" if actionable else None
        message_id = int(
            self.con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,from_participant_id,to_participant_id,work_unit_id,"
                "message_kind,body,actionable,disposition,idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sprint_id,
                    from_participant_id,
                    to_participant_id,
                    work_unit_id,
                    message_kind,
                    body,
                    1 if actionable else 0,
                    disposition,
                    idempotency_key,
                ),
            ).lastrowid
        )
        wake_id = (
            self._coalesce_wake(sprint_id, to_participant_id, message_id)
            if active
            else None
        )
        return MessageReceipt(message_id, wake_id, True)

    def _coalesce_wake(
        self, sprint_id: int, participant_id: int, message_id: int
    ) -> int:
        wake = self.con.execute(
            "SELECT wake_id FROM sprint_wake_outbox "
            "WHERE sprint_id=? AND participant_id=? AND state='pending'",
            (sprint_id, participant_id),
        ).fetchone()
        if wake is None:
            wake_id = int(
                self.con.execute(
                    "INSERT INTO sprint_wake_outbox "
                    "(sprint_id,participant_id,idempotency_key) VALUES (?,?,?)",
                    (
                        sprint_id,
                        participant_id,
                        f"sprint:{sprint_id}:participant:{participant_id}:"
                        f"wake-for-message:{message_id}",
                    ),
                ).lastrowid
            )
        else:
            wake_id = int(wake["wake_id"])
        self.con.execute(
            "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
            "VALUES (?,?,?)",
            (sprint_id, wake_id, message_id),
        )
        return wake_id

    def _recipient_message(self, message_id: int, shell_id: int) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT m.* FROM sprint_messages m "
            "JOIN sprint_participants p "
            "ON p.sprint_id=m.sprint_id AND p.participant_id=m.to_participant_id "
            "WHERE m.message_id=? AND p.shell_id=?",
            (message_id, shell_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Sprint message {message_id} is not addressed to shell")
        return row

    def _cancel_resolved_wakes(self, message_id: int) -> None:
        wake_ids = self.con.execute(
            "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
            (message_id,),
        ).fetchall()
        for wake in wake_ids:
            unresolved = self.con.execute(
                "SELECT 1 FROM sprint_wake_messages wm "
                "JOIN sprint_messages m USING (message_id) "
                "WHERE wm.wake_id=? AND m.read_at IS NULL LIMIT 1",
                (wake["wake_id"],),
            ).fetchone()
            if unresolved is None:
                self.con.execute(
                    "UPDATE sprint_wake_outbox SET state='cancelled',"
                    "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL "
                    "WHERE wake_id=? AND state='pending'",
                    (wake["wake_id"],),
                )


class SprintWakeDeliveryService:
    """Lease and deliver committed wake intents with a three-attempt budget."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lifecycle = SprintLifecycleStore(con)

    def requeue_expired(self) -> int:
        now = _stamp(self.now())
        with db_driver.write_transaction(self.con, "sprint.wake.recover"):
            result = self.con.execute(
                "UPDATE sprint_wake_outbox SET state='pending',claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL "
                "WHERE state='delivering' AND lease_expires_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_outbox followup "
                "WHERE followup.sprint_id=sprint_wake_outbox.sprint_id "
                "AND followup.participant_id=sprint_wake_outbox.participant_id "
                "AND followup.state='pending')",
                (now,),
            )
        return int(result.rowcount)

    def claim_next(self, owner: str, *, lease_seconds: int = 60) -> WakeLease | None:
        owner = owner.strip()
        if not owner:
            raise ValueError("wake claim owner is empty")
        if lease_seconds <= 0:
            raise ValueError("wake lease must be positive")
        now_value = self.now()
        now = _stamp(now_value)
        expires = _stamp(now_value + timedelta(seconds=lease_seconds))
        with db_driver.write_transaction(self.con, "sprint.wake.claim"):
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='pending',claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL "
                "WHERE state='delivering' AND lease_expires_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_outbox followup "
                "WHERE followup.sprint_id=sprint_wake_outbox.sprint_id "
                "AND followup.participant_id=sprint_wake_outbox.participant_id "
                "AND followup.state='pending')",
                (now,),
            )
            row = self.con.execute(
                "SELECT w.*,p.current_conversation_id "
                "FROM sprint_wake_outbox w "
                "JOIN sprints s USING (sprint_id) "
                "JOIN sprint_participants p "
                "ON p.sprint_id=w.sprint_id AND p.participant_id=w.participant_id "
                "WHERE ((w.state='delivering' AND w.lease_expires_at<=?) "
                "OR (w.state='pending' AND w.available_at<=?)) "
                "AND s.lifecycle='armed' "
                "AND EXISTS (SELECT 1 FROM sprint_wake_messages wm "
                "JOIN sprint_messages m USING (message_id) "
                "WHERE wm.wake_id=w.wake_id AND m.read_at IS NULL) "
                "ORDER BY w.wake_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "pending":
                result = self.con.execute(
                    "UPDATE sprint_wake_outbox SET state='delivering',claim_owner=?,"
                    "claimed_at=?,lease_expires_at=? "
                    "WHERE wake_id=? AND state='pending'",
                    (owner, now, expires, row["wake_id"]),
                )
            else:
                result = self.con.execute(
                    "UPDATE sprint_wake_outbox SET claim_owner=?,claimed_at=?,"
                    "lease_expires_at=? WHERE wake_id=? AND state='delivering' "
                    "AND lease_expires_at<=?",
                    (owner, now, expires, row["wake_id"], now),
                )
            if result.rowcount != 1:
                return None
            return WakeLease(
                wake_id=int(row["wake_id"]),
                sprint_id=int(row["sprint_id"]),
                participant_id=int(row["participant_id"]),
                target_conversation_id=row["current_conversation_id"],
                idempotency_key=str(row["idempotency_key"]),
                attempt_number=int(row["attempt_count"]) + 1,
                claim_owner=owner,
            )

    def deliver_once(
        self,
        owner: str,
        deliver: Callable[[str, str, str], str | None],
    ) -> DeliveryOutcome | None:
        lease = self.claim_next(owner)
        if lease is None:
            return None
        try:
            if lease.target_conversation_id is None:
                raise RuntimeError("participant has no current Sprint conversation")
            native_run_ref = deliver(
                lease.target_conversation_id,
                FIXED_WAKE_PROMPT,
                lease.idempotency_key,
            )
        except Exception as exc:  # external delivery faults become durable evidence
            attempt = self.lifecycle.record_wake_failure(
                lease.wake_id,
                str(exc) or exc.__class__.__name__,
                target_conversation_id=lease.target_conversation_id,
                expected_claim_owner=owner,
            )
            state = "failed" if attempt == 3 else "pending"
            return DeliveryOutcome(lease.wake_id, state, attempt)

        with db_driver.write_transaction(self.con, "sprint.wake.delivered"):
            row = self.con.execute(
                "SELECT state,claim_owner,attempt_count FROM sprint_wake_outbox "
                "WHERE wake_id=?",
                (lease.wake_id,),
            ).fetchone()
            if row is None or row["state"] != "delivering":
                raise SprintInvariantError("wake lease is no longer deliverable")
            if row["claim_owner"] != owner:
                raise SprintInvariantError("wake lease is owned by another worker")
            attempt = int(row["attempt_count"]) + 1
            self.con.execute(
                "INSERT INTO sprint_wake_attempts "
                "(wake_id,attempt_number,target_conversation_id,native_run_ref,"
                "outcome) VALUES (?,?,?,?,'delivered')",
                (
                    lease.wake_id,
                    attempt,
                    lease.target_conversation_id,
                    native_run_ref,
                ),
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='delivered',attempt_count=?,"
                "delivered_at=datetime('now'),claim_owner=NULL,claimed_at=NULL,"
                "lease_expires_at=NULL,last_error=NULL WHERE wake_id=?",
                (attempt, lease.wake_id),
            )
        return DeliveryOutcome(lease.wake_id, "delivered", attempt)
