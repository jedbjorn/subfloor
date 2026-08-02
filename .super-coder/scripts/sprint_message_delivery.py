#!/usr/bin/env python3
"""Engine-wide wake messages and retryable delivery.

Every message commits its wake intent in the same SQLite transaction.  A later
worker claims the coalesced receiver wake, resolves New versus Re-enter from the
live registry, and performs the native enqueue outside database transactions.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import active_chat_registry
import conversation_events
import db_driver
import sprint_participant_chats
from sprint_domain import SprintInvariantError, SprintLifecycleStore

wake_prompt = sprint_participant_chats.wake_prompt

ACTIONABLE_KINDS = frozenset({"work_assignment", "review_request", "notification"})
ACTIONABLE_KIND_ERROR = (
    "only work assignments, review requests, and notifications are actionable"
)


@dataclass(frozen=True)
class MessageReceipt:
    message_id: int
    wake_id: int | None
    created: bool


@dataclass(frozen=True)
class ParticipantRelayReceipt:
    message_id: int
    wake_id: int
    message_created: bool
    wake_state: str
    conversation_id: str | None


@dataclass(frozen=True)
class WakeLease:
    wake_id: int
    sprint_id: int | None
    participant_id: int | None
    participant_role: str | None
    receiver_shell_id: int
    message_ids: tuple[int, ...]
    declared_types: tuple[str, ...]
    prompt: str
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
    """One transactional producer plus Sprint acceptance conveniences."""

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
        declared_type: str = "re-enter",
    ) -> MessageReceipt:
        body = body.strip()
        key = idempotency_key.strip()
        if not body:
            raise ValueError("Sprint message body is empty")
        if not key:
            raise ValueError("Sprint message idempotency key is empty")
        if actionable and message_kind not in ACTIONABLE_KINDS:
            raise SprintInvariantError(ACTIONABLE_KIND_ERROR)
        with db_driver.write_transaction(self.con, "sprint.message.send"):
            return self.send_in_transaction(
                sprint_id,
                to_participant_id=to_participant_id,
                message_kind=message_kind,
                body=body,
                idempotency_key=key,
                from_participant_id=from_participant_id,
                work_unit_id=work_unit_id,
                actionable=actionable,
                declared_type=declared_type,
            )

    def send_to_shell(
        self,
        receiver_shell_id: int,
        *,
        message_kind: str,
        body: str,
        idempotency_key: str,
        sender_shell_id: int | None = None,
        declared_type: str = "re-enter",
    ) -> MessageReceipt:
        """Send an engine-wide wake message with no Sprint scope."""
        body = body.strip()
        key = idempotency_key.strip()
        if not body:
            raise ValueError("wake message body is empty")
        if not key:
            raise ValueError("wake message idempotency key is empty")
        if declared_type not in {"new", "re-enter"}:
            raise ValueError("wake message type must be new or re-enter")
        with db_driver.write_transaction(self.con, "wake.message.send"):
            receiver = self.con.execute(
                "SELECT 1 FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
                (receiver_shell_id,),
            ).fetchone()
            if receiver is None:
                raise KeyError(f"unknown wake receiver shell: {receiver_shell_id}")
            existing = self.con.execute(
                "SELECT * FROM wake_message WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                actual = (
                    existing["sprint_id"],
                    existing["sender_shell_id"],
                    int(existing["receiver_shell_id"]),
                    str(existing["message_kind"]),
                    str(existing["body"]),
                    str(existing["declared_type"]),
                )
                expected = (
                    None,
                    sender_shell_id,
                    receiver_shell_id,
                    message_kind,
                    body,
                    declared_type,
                )
                if actual != expected:
                    raise SprintInvariantError(
                        "wake message idempotency key was reused with different input"
                    )
                wake = self.con.execute(
                    "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                    (existing["message_id"],),
                ).fetchone()
                if wake is None:
                    raise SprintInvariantError("wake message has no delivery intent")
                return MessageReceipt(
                    int(existing["message_id"]), int(wake["wake_id"]), False
                )
            message_id = int(
                self.con.execute(
                    "INSERT INTO wake_message "
                    "(sender_shell_id,receiver_shell_id,message_kind,body,"
                    "declared_type,actionable,idempotency_key) "
                    "VALUES (?,?,?,?,?,0,?)",
                    (
                        sender_shell_id,
                        receiver_shell_id,
                        message_kind,
                        body,
                        declared_type,
                        key,
                    ),
                ).lastrowid
            )
            wake_id = self._coalesce_wake(
                None, None, receiver_shell_id, message_id
            )
            return MessageReceipt(message_id, wake_id, True)

    def relay(
        self,
        sprint_id: int,
        *,
        from_shell_id: int,
        to_shortname: str,
        body: str,
        idempotency_key: str,
    ) -> ParticipantRelayReceipt:
        if not isinstance(body, str):
            raise ValueError("Sprint message body must be a string")
        if not isinstance(to_shortname, str):
            raise ValueError("Sprint recipient shortname must be a string")
        if not isinstance(idempotency_key, str):
            raise ValueError("Sprint message idempotency key must be a string")
        body = body.strip()
        to_shortname = to_shortname.strip()
        idempotency_key = idempotency_key.strip()
        if not body:
            raise ValueError("Sprint message body is empty")
        if len(body) > 8000:
            raise ValueError(
                f"Sprint message body is {len(body)} characters; maximum is 8000"
            )
        if not to_shortname:
            raise ValueError("Sprint recipient shortname is empty")
        if not idempotency_key:
            raise ValueError("Sprint message idempotency key is empty")
        with db_driver.write_transaction(self.con, "sprint.message.relay"):
            sender = self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=?",
                (sprint_id, from_shell_id),
            ).fetchone()
            if sender is None:
                raise SprintInvariantError("sender is not a Sprint participant")
            recipient = self.con.execute(
                "SELECT p.participant_id FROM sprint_participants p "
                "JOIN shells sh ON sh.shell_id=p.shell_id "
                "WHERE p.sprint_id=? AND lower(sh.shortname)=lower(?)",
                (sprint_id, to_shortname),
            ).fetchone()
            if recipient is None:
                raise SprintInvariantError("recipient is not a Sprint participant")
            if int(recipient["participant_id"]) == int(sender["participant_id"]):
                raise SprintInvariantError("Sprint participants cannot relay to self")
            receipt = self._send(
                sprint_id,
                to_participant_id=int(recipient["participant_id"]),
                message_kind="notification",
                body=body,
                idempotency_key=idempotency_key,
                from_participant_id=int(sender["participant_id"]),
                work_unit_id=None,
                actionable=False,
                declared_type="re-enter",
            )
            wake = self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (receipt.wake_id,),
            ).fetchone()
            route = self.con.execute(
                "SELECT chat_id FROM active_shell_chats WHERE shell_id=("
                "SELECT shell_id FROM sprint_participants WHERE participant_id=?)",
                (recipient["participant_id"],),
            ).fetchone()
            if wake is None:
                raise SprintInvariantError("Sprint relay did not produce a deliverable wake")
            return ParticipantRelayReceipt(
                message_id=receipt.message_id,
                wake_id=int(receipt.wake_id),
                message_created=receipt.created,
                wake_state=str(wake["state"]),
                conversation_id=(str(route["chat_id"]) if route is not None else None),
            )

    def send_in_transaction(
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
        declared_type: str = "re-enter",
    ) -> MessageReceipt:
        """Commit through a caller-owned transaction.

        Deterministic producers use this seam when their evidence row and the
        resulting message+wake must either all commit or all roll back.  The
        caller must already hold the short database-only transaction.
        """
        if not self.con.in_transaction:
            raise RuntimeError("send_in_transaction requires an active transaction")
        body = body.strip()
        key = idempotency_key.strip()
        if not body:
            raise ValueError("Sprint message body is empty")
        if not key:
            raise ValueError("Sprint message idempotency key is empty")
        if actionable and message_kind not in ACTIONABLE_KINDS:
            raise SprintInvariantError(ACTIONABLE_KIND_ERROR)
        return self._send(
            sprint_id,
            to_participant_id=to_participant_id,
            message_kind=message_kind,
            body=body,
            idempotency_key=key,
            from_participant_id=from_participant_id,
            work_unit_id=work_unit_id,
            actionable=actionable,
            declared_type=declared_type,
        )

    def inbox(self, sprint_id: int, shell_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT m.* FROM wake_message m "
            "WHERE m.sprint_id=? AND m.receiver_shell_id=? AND m.read_at IS NULL "
            "ORDER BY m.message_id",
            (sprint_id, shell_id),
        ).fetchall()

    def mark_read(
        self,
        message_id: int,
        shell_id: int,
        *,
        sprint_id: int | None = None,
    ) -> str | None:
        """Read one message; actionable reads atomically accept it."""
        with db_driver.write_transaction(self.con, "sprint.message.read"):
            message = self._recipient_message(message_id, shell_id, sprint_id)
            accepted_now = False
            if message["actionable"]:
                if message["disposition"] == "declined":
                    return "declined"
                if message["disposition"] == "pending":
                    self.con.execute(
                        "UPDATE wake_message SET disposition='accepted',"
                        "read_at=datetime('now') WHERE message_id=?",
                        (message_id,),
                    )
                    accepted_now = True
                disposition = "accepted"
            else:
                self.con.execute(
                    "UPDATE wake_message SET read_at=COALESCE(read_at,datetime('now')) "
                    "WHERE message_id=?",
                    (message_id,),
                )
                disposition = None
            if (
                accepted_now
                and message["message_kind"] == "work_assignment"
                and message["work_unit_id"] is not None
            ):
                changed = self.con.execute(
                    "UPDATE sprint_work_units SET disposition='active',"
                    "updated_at=datetime('now') WHERE sprint_id=? "
                    "AND work_unit_id=? AND assigned_shell_id=? "
                    "AND disposition='ready'",
                    (
                        message["sprint_id"],
                        message["work_unit_id"],
                        shell_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise SprintInvariantError(
                        "work assignment no longer owns a ready editing lane"
                    )
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                    "VALUES (?,'work_unit.accepted','participant',?,?)",
                    (
                        message["sprint_id"],
                        shell_id,
                        json.dumps(
                            {
                                "message_id": message_id,
                                "work_unit_id": int(message["work_unit_id"]),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            self._cancel_resolved_wakes(message_id)
            return disposition

    def decline(
        self,
        message_id: int,
        shell_id: int,
        reason: str,
        *,
        sprint_id: int | None = None,
    ) -> int:
        """Resolve an actionable message and actively route the result to Planner."""
        reason = reason.strip()
        if not reason:
            raise ValueError("decline requires a reason")
        with db_driver.write_transaction(self.con, "sprint.message.decline"):
            message = self._recipient_message(message_id, shell_id, sprint_id)
            if not message["actionable"]:
                raise SprintInvariantError("informational messages cannot be declined")
            if message["disposition"] == "accepted":
                raise SprintInvariantError("accepted Sprint messages cannot be declined")
            if message["disposition"] == "pending":
                self.con.execute(
                    "UPDATE wake_message SET disposition='declined',"
                    "read_at=datetime('now'),decline_reason=? WHERE message_id=?",
                    (reason, message_id),
                )
                if message["message_kind"] == "work_assignment" and message[
                    "work_unit_id"
                ] is not None:
                    self.con.execute(
                        "UPDATE sprint_work_units SET disposition='planned',"
                        "updated_at=datetime('now') WHERE work_unit_id=? "
                        "AND disposition='ready'",
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
                declared_type="re-enter",
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
        declared_type: str,
    ) -> MessageReceipt:
        if declared_type not in {"new", "re-enter"}:
            raise ValueError("wake message type must be new or re-enter")
        sprint = self.con.execute(
            "SELECT sprint_id FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        recipient = self.con.execute(
            "SELECT shell_id FROM sprint_participants "
            "WHERE sprint_id=? AND participant_id=?",
            (sprint_id, to_participant_id),
        ).fetchone()
        if recipient is None:
            raise SprintInvariantError("recipient is not a Sprint participant")
        sender_shell_id = None
        if from_participant_id is not None:
            sender = self.con.execute(
                "SELECT shell_id FROM sprint_participants "
                "WHERE sprint_id=? AND participant_id=?",
                (sprint_id, from_participant_id),
            ).fetchone()
            if sender is None:
                raise SprintInvariantError("sender is not a Sprint participant")
            sender_shell_id = int(sender["shell_id"])
        receiver_shell_id = int(recipient["shell_id"])

        existing = self.con.execute(
            "SELECT * FROM wake_message WHERE idempotency_key=?",
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
                str(existing["declared_type"]),
            )
            expected = (
                sprint_id,
                from_participant_id,
                to_participant_id,
                work_unit_id,
                message_kind,
                body,
                actionable,
                declared_type,
            )
            wake = self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (existing["message_id"],),
            ).fetchone()
            if actual != expected or wake is None:
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
                "INSERT INTO wake_message "
                "(sprint_id,sender_shell_id,receiver_shell_id,from_participant_id,"
                "to_participant_id,work_unit_id,message_kind,body,declared_type,"
                "actionable,disposition,idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sprint_id,
                    sender_shell_id,
                    receiver_shell_id,
                    from_participant_id,
                    to_participant_id,
                    work_unit_id,
                    message_kind,
                    body,
                    declared_type,
                    1 if actionable else 0,
                    disposition,
                    idempotency_key,
                ),
            ).lastrowid
        )
        wake_id = self._coalesce_wake(
            sprint_id,
            to_participant_id,
            receiver_shell_id,
            message_id,
        )
        return MessageReceipt(message_id, wake_id, True)

    def _coalesce_wake(
        self,
        sprint_id: int | None,
        participant_id: int | None,
        receiver_shell_id: int,
        message_id: int,
    ) -> int:
        wake = self.con.execute(
            "SELECT wake_id FROM sprint_wake_outbox "
            "WHERE receiver_shell_id=? AND state='pending'",
            (receiver_shell_id,),
        ).fetchone()
        if wake is None:
            wake_id = int(
                self.con.execute(
                    "INSERT INTO sprint_wake_outbox "
                    "(sprint_id,participant_id,receiver_shell_id,idempotency_key) "
                    "VALUES (?,?,?,?)",
                    (
                        sprint_id,
                        participant_id,
                        receiver_shell_id,
                        f"receiver:{receiver_shell_id}:wake-for-message:{message_id}",
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

    def _recipient_message(
        self,
        message_id: int,
        shell_id: int,
        sprint_id: int | None,
    ) -> sqlite3.Row:
        scope = " AND m.sprint_id=?" if sprint_id is not None else ""
        params = (
            (message_id, shell_id, sprint_id)
            if sprint_id is not None
            else (message_id, shell_id)
        )
        row = self.con.execute(
            "SELECT m.* FROM wake_message m "
            "WHERE m.message_id=? AND m.receiver_shell_id=?" + scope,
            params,
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
                "JOIN wake_message m USING (message_id) "
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
    """Lease wake intents and resolve their chat placement at delivery time."""

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
                "WHERE followup.receiver_shell_id="
                "sprint_wake_outbox.receiver_shell_id "
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
                "WHERE followup.receiver_shell_id="
                "sprint_wake_outbox.receiver_shell_id "
                "AND followup.state='pending')",
                (now,),
            )
            row = self.con.execute(
                "SELECT w.*,p.role "
                "FROM sprint_wake_outbox w "
                "LEFT JOIN sprints s ON s.sprint_id=w.sprint_id "
                "LEFT JOIN sprint_participants p "
                "ON p.sprint_id=w.sprint_id AND p.participant_id=w.participant_id "
                "WHERE ((w.state='delivering' AND w.lease_expires_at<=?) "
                "OR (w.state='pending' AND w.available_at<=?)) "
                "AND (w.sprint_id IS NULL OR s.lifecycle='armed') "
                "AND EXISTS (SELECT 1 FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) "
                "WHERE wm.wake_id=w.wake_id AND m.delivered_at IS NULL) "
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

            messages = self.con.execute(
                "SELECT message_id,sprint_id,declared_type,body "
                "FROM wake_message WHERE receiver_shell_id=? "
                "AND delivered_at IS NULL ORDER BY message_id",
                (row["receiver_shell_id"],),
            ).fetchall()
            if not messages:
                raise SprintInvariantError("claimed wake has no undelivered messages")
            message_ids = tuple(int(message["message_id"]) for message in messages)
            marks = ",".join("?" for _ in message_ids)
            self.con.execute(
                "UPDATE sprint_wake_messages SET wake_id=? "
                "WHERE message_id IN (" + marks + ")",
                (row["wake_id"], *message_ids),
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='cancelled',claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL "
                "WHERE receiver_shell_id=? AND state='pending' AND wake_id<>? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_messages joined "
                "WHERE joined.wake_id=sprint_wake_outbox.wake_id)",
                (row["receiver_shell_id"], row["wake_id"]),
            )
            return WakeLease(
                wake_id=int(row["wake_id"]),
                sprint_id=(
                    int(row["sprint_id"]) if row["sprint_id"] is not None else None
                ),
                participant_id=(
                    int(row["participant_id"])
                    if row["participant_id"] is not None
                    else None
                ),
                participant_role=(str(row["role"]) if row["role"] else None),
                receiver_shell_id=int(row["receiver_shell_id"]),
                message_ids=message_ids,
                declared_types=tuple(
                    str(message["declared_type"]) for message in messages
                ),
                prompt=self._delivery_prompt(row, messages),
                idempotency_key=str(row["idempotency_key"]),
                attempt_number=int(row["attempt_count"]) + 1,
                claim_owner=owner,
            )

    @staticmethod
    def _delivery_prompt(row: sqlite3.Row, messages: list[sqlite3.Row]) -> str:
        sprint_id = row["sprint_id"]
        role = row["role"]
        if sprint_id is not None and role is not None:
            lead = wake_prompt(int(sprint_id), str(role))
        else:
            lead = "Wake-message delivery. Act on every message below."
        rendered = []
        for message in messages:
            declared = str(message["declared_type"]).title()
            rendered.append(
                f"## wake_message #{int(message['message_id'])} "
                f"(declared {declared})\n\n{message['body']}"
            )
        return lead + "\n\n" + "\n\n".join(rendered)

    def _resolve_conversation(self, lease: WakeLease) -> str:
        active = active_chat_registry.get(self.con, lease.receiver_shell_id)
        if active is not None and active_chat_registry.has_live_process(active):
            return active.chat_id
        wants_new = "new" in lease.declared_types
        if active is not None and not wants_new:
            return active.chat_id

        closed_id = None
        if active is not None:
            with db_driver.write_transaction(self.con, "wake.route.close_active"):
                closed = active_chat_registry.close_for_wake(
                    self.con, lease.receiver_shell_id
                )
                if closed is not None:
                    closed_id = closed.chat_id
                    sprint_participant_chats._append_event(
                        self.con,
                        closed.chat_id,
                        "conversation.closed",
                        {
                            "reason": "New wake_message delivery",
                            "state": "closed",
                            "wake_id": lease.wake_id,
                        },
                    )
        if closed_id is not None:
            conversation_events.notify(closed_id)
        if lease.sprint_id is None or lease.participant_id is None:
            raise SprintInvariantError(
                "New wake delivery without an active chat needs routing context"
            )
        with db_driver.write_transaction(self.con, "wake.route.create"):
            return sprint_participant_chats.create_wake_conversation(
                self.con,
                wake_id=lease.wake_id,
                sprint_id=lease.sprint_id,
                participant_id=lease.participant_id,
            )

    def deliver_once(
        self,
        owner: str,
        deliver: Callable[[str, str, str], str | None],
    ) -> DeliveryOutcome | None:
        lease = self.claim_next(owner)
        if lease is None:
            return None
        target_conversation_id = None
        try:
            target_conversation_id = self._resolve_conversation(lease)
            native_run_ref = deliver(
                target_conversation_id,
                lease.prompt,
                lease.idempotency_key,
            )
        except Exception as exc:  # external delivery faults become durable evidence
            if lease.sprint_id is None:
                raise
            attempt = self.lifecycle.record_wake_failure(
                lease.wake_id,
                str(exc) or exc.__class__.__name__,
                target_conversation_id=target_conversation_id,
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
                    target_conversation_id,
                    native_run_ref,
                ),
            )
            marks = ",".join("?" for _ in lease.message_ids)
            self.con.execute(
                "UPDATE wake_message SET delivered_at=COALESCE(delivered_at,"
                "datetime('now')) WHERE message_id IN (" + marks + ")",
                lease.message_ids,
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='delivered',attempt_count=?,"
                "delivered_at=datetime('now'),claim_owner=NULL,claimed_at=NULL,"
                "lease_expires_at=NULL,last_error=NULL WHERE wake_id=?",
                (attempt, lease.wake_id),
            )
        return DeliveryOutcome(lease.wake_id, "delivered", attempt)
