#!/usr/bin/env python3
"""Engine-wide wake messages and retryable delivery.

Every message commits its wake intent in the same SQLite transaction.  A later
worker claims the coalesced receiver wake, resolves New versus Re-enter from the
live registry, and performs the native enqueue outside database transactions.
"""
from __future__ import annotations

import json
import os
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
DECLARED_TYPES = frozenset({"force-new", "new", "re-enter"})
DECLARED_TYPE_ERROR = "wake message type must be force-new, new, or re-enter"
MESSAGE_INTENTS = frozenset(
    {"information", "handoff", "question", "blocker", "decision"}
)
REPLY_REQUIRED_INTENTS = frozenset({"question", "blocker", "decision"})


class ForceNewDeferred(RuntimeError):
    """A force-new lease lost a live-chat boundary race without an attempt."""


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


def _parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


class SprintMessageStore:
    """One transactional producer plus Sprint acceptance conveniences."""

    def __init__(
        self,
        con: sqlite3.Connection,
    ) -> None:
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
        with db_driver.write_transaction(self.con, "wake.message.send"):
            return self.send_to_shell_in_transaction(
                receiver_shell_id,
                message_kind=message_kind,
                body=body,
                idempotency_key=idempotency_key,
                sender_shell_id=sender_shell_id,
                declared_type=declared_type,
            )

    def send_to_shell_in_transaction(
        self,
        receiver_shell_id: int,
        *,
        message_kind: str,
        body: str,
        idempotency_key: str,
        sender_shell_id: int | None = None,
        declared_type: str = "re-enter",
    ) -> MessageReceipt:
        """Commit an engine-wide wake through a caller-owned transaction."""
        if not self.con.in_transaction:
            raise RuntimeError(
                "send_to_shell_in_transaction requires an active transaction"
            )
        body = body.strip()
        key = idempotency_key.strip()
        if not body:
            raise ValueError("wake message body is empty")
        if not key:
            raise ValueError("wake message idempotency key is empty")
        if declared_type not in DECLARED_TYPES:
            raise ValueError(DECLARED_TYPE_ERROR)
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
            None,
            None,
            receiver_shell_id,
            message_id,
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
        intent: str = "information",
        requires_reply: bool = False,
        work_unit_id: int | None = None,
        sprint_level: bool = False,
        reply_to_message_id: int | None = None,
    ) -> ParticipantRelayReceipt:
        if not isinstance(body, str):
            raise TypeError("Sprint message body must be a string")
        if not isinstance(to_shortname, str):
            raise TypeError("Sprint recipient shortname must be a string")
        if not isinstance(idempotency_key, str):
            raise TypeError("Sprint message idempotency key must be a string")
        if not isinstance(intent, str):
            raise TypeError("Sprint message intent must be a string")
        if not isinstance(requires_reply, bool):
            raise TypeError("requires_reply must be a boolean")
        if not isinstance(sprint_level, bool):
            raise TypeError("sprint_level must be a boolean")
        body = body.strip()
        to_shortname = to_shortname.strip()
        idempotency_key = idempotency_key.strip()
        intent = intent.strip()
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
        if intent not in MESSAGE_INTENTS:
            raise ValueError(
                "Sprint message intent must be information, handoff, question, "
                "blocker, or decision"
            )
        if requires_reply and intent not in REPLY_REQUIRED_INTENTS:
            raise SprintInvariantError(
                "reply-requiring messages must use question, blocker, or decision intent"
            )
        if reply_to_message_id is not None and (work_unit_id is not None or sprint_level):
            raise SprintInvariantError(
                "replies inherit scope; do not supply work_unit_id or sprint_level"
            )
        if (
            reply_to_message_id is None
            and requires_reply
            and (work_unit_id is None) == (not sprint_level)
        ):
            raise SprintInvariantError(
                "reply-requiring messages need exactly one work-unit or Sprint-level scope"
            )
        with db_driver.write_transaction(self.con, "sprint.message.relay"):
            sender = self.con.execute(
                "SELECT participant_id,role FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=?",
                (sprint_id, from_shell_id),
            ).fetchone()
            if sender is None:
                raise SprintInvariantError("sender is not a Sprint participant")
            recipient = self.con.execute(
                "SELECT p.participant_id,p.role,p.shell_id FROM sprint_participants p "
                "JOIN shells sh ON sh.shell_id=p.shell_id "
                "WHERE p.sprint_id=? AND lower(sh.shortname)=lower(?)",
                (sprint_id, to_shortname),
            ).fetchone()
            if recipient is None:
                raise SprintInvariantError("recipient is not a Sprint participant")
            if int(recipient["participant_id"]) == int(sender["participant_id"]):
                raise SprintInvariantError("Sprint participants cannot relay to self")
            if reply_to_message_id is not None:
                original = self._reply_target(
                    sprint_id,
                    reply_to_message_id,
                    sender_participant_id=int(sender["participant_id"]),
                    recipient_participant_id=int(recipient["participant_id"]),
                )
                work_unit_id = (
                    int(original["work_unit_id"])
                    if original["work_unit_id"] is not None
                    else None
                )
            if work_unit_id is not None and reply_to_message_id is None:
                self._validate_unit_scope(
                    sprint_id,
                    work_unit_id,
                    sender_shell_id=from_shell_id,
                    sender_role=str(sender["role"]),
                    recipient_shell_id=int(recipient["shell_id"]),
                    recipient_role=str(recipient["role"]),
                )
            receipt = self._send(
                sprint_id,
                to_participant_id=int(recipient["participant_id"]),
                message_kind="notification",
                body=body,
                idempotency_key=idempotency_key,
                from_participant_id=int(sender["participant_id"]),
                work_unit_id=work_unit_id,
                actionable=False,
                declared_type="re-enter",
                intent=intent,
                requires_reply=requires_reply,
                reply_to_message_id=reply_to_message_id,
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

    def _reply_target(
        self,
        sprint_id: int,
        message_id: int,
        *,
        sender_participant_id: int,
        recipient_participant_id: int,
    ) -> sqlite3.Row:
        original = self.con.execute(
            "SELECT * FROM wake_message WHERE sprint_id=? AND message_id=?",
            (sprint_id, message_id),
        ).fetchone()
        if original is None:
            raise SprintInvariantError("reply target is not an earlier message in this Sprint")
        if not original["requires_reply"]:
            raise SprintInvariantError("reply target does not require a reply")
        if (
            int(original["to_participant_id"]) != sender_participant_id
            or int(original["from_participant_id"]) != recipient_participant_id
        ):
            raise SprintInvariantError(
                "reply sender and recipient must reverse the original message endpoints"
            )
        return original

    def _validate_unit_scope(
        self,
        sprint_id: int,
        work_unit_id: int,
        *,
        sender_shell_id: int,
        sender_role: str,
        recipient_shell_id: int,
        recipient_role: str,
    ) -> None:
        unit = self.con.execute(
            "SELECT assigned_shell_id,reviewer_shell_id FROM sprint_work_units "
            "WHERE sprint_id=? AND work_unit_id=?",
            (sprint_id, work_unit_id),
        ).fetchone()
        if unit is None:
            raise SprintInvariantError("work unit does not belong to this Sprint")
        allowed = {int(unit["assigned_shell_id"]), int(unit["reviewer_shell_id"])}
        for shell_id, role in (
            (sender_shell_id, sender_role),
            (recipient_shell_id, recipient_role),
        ):
            if role != "planner" and shell_id not in allowed:
                raise SprintInvariantError(
                    "unit-scoped message endpoint does not own this work unit"
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
        # An undelivered force-new message stays invisible: its wake must
        # rotate the chat and launch a fresh run, so an older turn polling
        # the inbox ahead of delivery must not see (and then resolve) it.
        # Other declared types may be read early by a live turn — that is the
        # coalescing design their wakes exist to make unnecessary.
        return self.con.execute(
            "SELECT m.* FROM wake_message m "
            "WHERE m.sprint_id=? AND m.receiver_shell_id=? AND m.read_at IS NULL "
            "AND (m.delivered_at IS NOT NULL OR m.declared_type<>'force-new') "
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
        intent: str = "information",
        requires_reply: bool = False,
        reply_to_message_id: int | None = None,
    ) -> MessageReceipt:
        if declared_type not in DECLARED_TYPES:
            raise ValueError(DECLARED_TYPE_ERROR)
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
                str(existing["intent"]),
                bool(existing["requires_reply"]),
                existing["reply_to_message_id"],
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
                intent,
                requires_reply,
                reply_to_message_id,
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
                "actionable,disposition,idempotency_key,intent,requires_reply,"
                "reply_to_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    intent,
                    1 if requires_reply else 0,
                    reply_to_message_id,
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
        if row["delivered_at"] is None and row["declared_type"] == "force-new":
            # Resolving a force-new message ahead of delivery would cancel
            # its pending wake and swallow the rotation it exists to force;
            # the recipient acts on it only once its wake has delivered.
            raise SprintInvariantError(
                f"Sprint message {message_id} has not been delivered yet"
            )
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
                    "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL,"
                    "quiet_since=NULL "
                    "WHERE wake_id=? AND state='pending'",
                    (wake["wake_id"],),
                )

    def resolve_in_transaction(self, message_id: int) -> None:
        """Resolve one informational message and its delivery intent.

        Deterministic producers use this when later evidence makes an
        undelivered message obsolete.  Keeping the wake cancellation here
        prevents callers from reimplementing message-store invariants.
        """
        if not self.con.in_transaction:
            raise RuntimeError("message resolution requires an active transaction")
        changed = self.con.execute(
            "UPDATE wake_message SET read_at=COALESCE(read_at,datetime('now')) "
            "WHERE message_id=?",
            (message_id,),
        ).rowcount
        if changed != 1:
            raise KeyError(f"unknown wake message: {message_id}")
        self._cancel_resolved_wakes(message_id)


class SprintWakeDeliveryService:
    """Lease wake intents and resolve their chat placement at delivery time."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        now: Callable[[], datetime] | None = None,
        force_new_quiet_seconds: int | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lifecycle = SprintLifecycleStore(con)
        if force_new_quiet_seconds is None:
            raw_quiet_seconds: str | int = os.environ.get(
                "SC_SPRINT_FORCE_NEW_QUIET_SECONDS", "10"
            )
        else:
            raw_quiet_seconds = force_new_quiet_seconds
        try:
            quiet_seconds = int(raw_quiet_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "SC_SPRINT_FORCE_NEW_QUIET_SECONDS must be a non-negative "
                "whole second value"
            ) from exc
        if quiet_seconds < 0:
            raise ValueError(
                "SC_SPRINT_FORCE_NEW_QUIET_SECONDS must be a non-negative "
                "whole second value"
            )
        self.force_new_quiet_seconds = quiet_seconds

    def requeue_expired(self) -> int:
        now = _stamp(self.now())
        with db_driver.write_transaction(self.con, "sprint.wake.recover"):
            result = self.con.execute(
                "UPDATE sprint_wake_outbox SET state='pending',available_at=?,"
                "claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL "
                "WHERE state='delivering' AND lease_expires_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_outbox followup "
                "WHERE followup.receiver_shell_id="
                "sprint_wake_outbox.receiver_shell_id "
                "AND followup.state='pending')",
                (now, now),
            )
        return int(result.rowcount)

    def _active_belongs_to_wake(
        self,
        active: active_chat_registry.ActiveChat,
        wake_id: int,
    ) -> bool:
        row = self.con.execute(
            "SELECT creation_idempotency_key FROM conversations "
            "WHERE conversation_id=?",
            (active.chat_id,),
        ).fetchone()
        if row is None or row["creation_idempotency_key"] is None:
            return False
        return str(row["creation_idempotency_key"]).endswith(f":wake:{wake_id}")

    def _receiver_has_force_new(self, receiver_shell_id: int) -> bool:
        return (
            self.con.execute(
                "SELECT 1 FROM wake_message "
                "WHERE receiver_shell_id=? AND delivered_at IS NULL "
                "AND declared_type='force-new' LIMIT 1",
                (receiver_shell_id,),
            ).fetchone()
            is not None
        )

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
                "UPDATE sprint_wake_outbox SET state='pending',available_at=?,"
                "claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL "
                "WHERE state='delivering' AND lease_expires_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_outbox followup "
                "WHERE followup.receiver_shell_id="
                "sprint_wake_outbox.receiver_shell_id "
                "AND followup.state='pending')",
                (now, now),
            )
            candidates = self.con.execute(
                "SELECT w.* FROM sprint_wake_outbox w "
                "WHERE ((w.state='delivering' AND w.lease_expires_at<=?) "
                "OR (w.state='pending' AND w.available_at<=?)) "
                "AND EXISTS (SELECT 1 FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) "
                "LEFT JOIN sprints message_sprint "
                "ON message_sprint.sprint_id=m.sprint_id "
                "WHERE wm.wake_id=w.wake_id AND m.delivered_at IS NULL "
                "AND (m.sprint_id IS NULL OR message_sprint.lifecycle='armed')) "
                "ORDER BY w.wake_id",
                (now, now),
            ).fetchall()
            row = None
            for candidate in candidates:
                receiver_shell_id = int(candidate["receiver_shell_id"])
                if not self._receiver_has_force_new(receiver_shell_id):
                    row = candidate
                    break
                active = active_chat_registry.get(self.con, receiver_shell_id)
                if active is not None and self._active_belongs_to_wake(
                    active, int(candidate["wake_id"])
                ):
                    row = candidate
                    break
                if active is not None and active_chat_registry.has_live_process(active):
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET quiet_since=NULL "
                        "WHERE wake_id=? AND quiet_since IS NOT NULL",
                        (candidate["wake_id"],),
                    )
                    continue
                quiet_since = candidate["quiet_since"]
                if quiet_since is None:
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET quiet_since=? WHERE wake_id=?",
                        (now, candidate["wake_id"]),
                    )
                    if self.force_new_quiet_seconds > 0:
                        continue
                elif (
                    now_value - _parse_stamp(str(quiet_since))
                ).total_seconds() < self.force_new_quiet_seconds:
                    continue
                row = self.con.execute(
                    "SELECT * FROM sprint_wake_outbox WHERE wake_id=?",
                    (candidate["wake_id"],),
                ).fetchone()
                break
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
                "SELECT m.message_id,m.sprint_id,m.to_participant_id,"
                "CASE WHEN m.declared_type='re-enter' AND EXISTS ("
                "SELECT 1 FROM sprints coordinate_sprint "
                "WHERE coordinate_sprint.lifecycle='armed' "
                "AND coordinate_sprint.coordinate_mode=1 "
                "AND coordinate_sprint.originating_planner_shell_id="
                "m.receiver_shell_id) "
                "THEN 'new' ELSE m.declared_type END AS declared_type,"
                "m.body,s.lifecycle,p.role "
                "FROM wake_message m LEFT JOIN sprints s "
                "ON s.sprint_id=m.sprint_id LEFT JOIN sprint_participants p "
                "ON p.sprint_id=m.sprint_id "
                "AND p.participant_id=m.to_participant_id "
                "WHERE m.receiver_shell_id=? AND m.delivered_at IS NULL "
                "ORDER BY m.message_id",
                (row["receiver_shell_id"],),
            ).fetchall()
            if not messages:
                raise SprintInvariantError("claimed wake has no undelivered messages")
            route = next(
                (
                    message
                    for message in messages
                    if message["sprint_id"] is not None
                    and message["lifecycle"] == "armed"
                ),
                None,
            )
            if route is None:
                route = next(
                    (message for message in messages if message["sprint_id"] is None),
                    None,
                )
            if route is None:
                raise SprintInvariantError(
                    "claimed wake has no message-scoped routing identity"
                )
            route_sprint_id = (
                int(route["sprint_id"]) if route["sprint_id"] is not None else None
            )
            route_participant_id = (
                int(route["to_participant_id"])
                if route["to_participant_id"] is not None
                else None
            )
            route_role = str(route["role"]) if route["role"] is not None else None
            if route_sprint_id is not None and (
                route_participant_id is None or route_role is None
            ):
                raise SprintInvariantError(
                    "Sprint wake message has no participant routing identity"
                )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET sprint_id=?,participant_id=? "
                "WHERE wake_id=? AND state='delivering' AND claim_owner=?",
                (
                    route_sprint_id,
                    route_participant_id,
                    row["wake_id"],
                    owner,
                ),
            )
            message_ids = tuple(int(message["message_id"]) for message in messages)
            marks = ",".join("?" for _ in message_ids)
            self.con.execute(
                "UPDATE sprint_wake_messages SET wake_id=? "
                "WHERE message_id IN (" + marks + ")",
                (row["wake_id"], *message_ids),
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='cancelled',claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL,quiet_since=NULL "
                "WHERE receiver_shell_id=? AND state='pending' AND wake_id<>? "
                "AND NOT EXISTS (SELECT 1 FROM sprint_wake_messages joined "
                "WHERE joined.wake_id=sprint_wake_outbox.wake_id)",
                (row["receiver_shell_id"], row["wake_id"]),
            )
            return WakeLease(
                wake_id=int(row["wake_id"]),
                sprint_id=route_sprint_id,
                participant_id=route_participant_id,
                participant_role=route_role,
                receiver_shell_id=int(row["receiver_shell_id"]),
                message_ids=message_ids,
                declared_types=tuple(
                    str(message["declared_type"]) for message in messages
                ),
                prompt=self._delivery_prompt(route_sprint_id, route_role, messages),
                idempotency_key=str(row["idempotency_key"]),
                attempt_number=int(row["attempt_count"]) + 1,
                claim_owner=owner,
            )

    @staticmethod
    def _delivery_prompt(
        sprint_id: int | None,
        role: str | None,
        messages: list[sqlite3.Row],
    ) -> str:
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
        wants_force_new = "force-new" in lease.declared_types
        if active is not None and self._active_belongs_to_wake(
            active, lease.wake_id
        ):
            return active.chat_id
        if active is not None and active_chat_registry.has_live_process(active):
            if wants_force_new:
                raise ForceNewDeferred("a live chat appeared after force-new claim")
            return active.chat_id
        wants_new = wants_force_new or "new" in lease.declared_types
        if active is not None and not wants_new:
            return active.chat_id

        shell_route = None
        sprint_route = None
        if lease.sprint_id is None or lease.participant_id is None:
            shell_route = sprint_participant_chats.prepare_shell_wake_conversation(
                self.con, lease.receiver_shell_id
            )
        else:
            sprint_route = sprint_participant_chats.prepare_wake_conversation(
                self.con,
                sprint_id=lease.sprint_id,
                participant_id=lease.participant_id,
            )

        if wants_force_new:
            current = active_chat_registry.get(self.con, lease.receiver_shell_id)
            if current is not None and self._active_belongs_to_wake(
                current, lease.wake_id
            ):
                return current.chat_id
            if current is not None and active_chat_registry.has_live_process(current):
                raise ForceNewDeferred(
                    "a live chat appeared before force-new rotation"
                )
            if current is not None and (
                active is None or active.chat_id != current.chat_id
            ):
                raise ForceNewDeferred(
                    "the active chat changed before force-new rotation"
                )
            active = current

        closed_id = None
        if active is not None:
            try:
                with db_driver.write_transaction(self.con, "wake.route.close_active"):
                    closed = active_chat_registry.close_for_wake(
                        self.con,
                        lease.receiver_shell_id,
                        expected_chat_id=(active.chat_id if wants_force_new else None),
                    )
                    if closed is not None:
                        closed_id = closed.chat_id
                        sprint_participant_chats._append_event(
                            self.con,
                            closed.chat_id,
                            "conversation.closed",
                            {
                                "reason": (
                                    "force-new wake delivery"
                                    if wants_force_new
                                    else "New wake_message delivery"
                                ),
                                "state": "closed",
                                "wake_id": lease.wake_id,
                            },
                        )
            except active_chat_registry.ActiveChatBusy:
                if wants_force_new:
                    raise ForceNewDeferred(
                        "the active chat changed at force-new close"
                    )
                raced = active_chat_registry.get(self.con, lease.receiver_shell_id)
                if raced is not None:
                    return raced.chat_id
                raise
        if closed_id is not None:
            conversation_events.notify(closed_id)
        try:
            with db_driver.write_transaction(self.con, "wake.route.create"):
                if shell_route is not None:
                    return sprint_participant_chats.create_shell_wake_conversation(
                        self.con,
                        wake_id=lease.wake_id,
                        route=shell_route,
                    )
                if sprint_route is None:
                    raise SprintInvariantError("Sprint wake route was not prepared")
                return sprint_participant_chats.create_prepared_wake_conversation(
                    self.con,
                    wake_id=lease.wake_id,
                    route=sprint_route,
                )
        except sprint_participant_chats.WakeConversationBusy:
            if wants_force_new:
                raise ForceNewDeferred(
                    "another chat won the force-new creation boundary"
                )
            raced = active_chat_registry.get(self.con, lease.receiver_shell_id)
            if raced is not None:
                return raced.chat_id
            raise

    def _defer_force_new(self, lease: WakeLease) -> None:
        with db_driver.write_transaction(self.con, "wake.force_new.defer"):
            row = self.con.execute(
                "SELECT state,claim_owner FROM sprint_wake_outbox WHERE wake_id=?",
                (lease.wake_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "delivering"
                or row["claim_owner"] != lease.claim_owner
            ):
                raise SprintInvariantError("force-new wake lease is not owned")
            followups = self.con.execute(
                "SELECT wake_id FROM sprint_wake_outbox "
                "WHERE receiver_shell_id=? AND state='pending' AND wake_id<>? "
                "ORDER BY wake_id",
                (lease.receiver_shell_id, lease.wake_id),
            ).fetchall()
            for followup in followups:
                self.con.execute(
                    "UPDATE sprint_wake_messages SET wake_id=? WHERE wake_id=?",
                    (lease.wake_id, followup["wake_id"]),
                )
                self.con.execute(
                    "UPDATE sprint_wake_outbox SET state='cancelled',"
                    "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL,"
                    "quiet_since=NULL WHERE wake_id=? AND state='pending'",
                    (followup["wake_id"],),
                )
            changed = self.con.execute(
                "UPDATE sprint_wake_outbox SET state='pending',claim_owner=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL,quiet_since=NULL "
                "WHERE wake_id=? AND state='delivering' AND claim_owner=?",
                (lease.wake_id, lease.claim_owner),
            ).rowcount
            if changed != 1:
                raise SprintInvariantError("force-new wake deferral lost its lease")

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
        except ForceNewDeferred:
            self._defer_force_new(lease)
            return DeliveryOutcome(
                lease.wake_id,
                "pending",
                lease.attempt_number - 1,
            )
        except Exception as exc:  # external delivery faults become durable evidence
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
                "lease_expires_at=NULL,last_error=NULL,quiet_since=NULL "
                "WHERE wake_id=?",
                (attempt, lease.wake_id),
            )
        return DeliveryOutcome(lease.wake_id, "delivered", attempt)
