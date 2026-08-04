"""Armed-only Sprint work dispatch plus engine-wide wake-turn delivery."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import activity_monitor
import conversation_broker
import conversation_events
import db_driver
from sprint_domain import (
    ArmedServiceSwitch,
    SprintInvariantError,
    SprintLifecycleStore,
    SprintWorkUnitStore,
)
from sprint_liveness import SprintLivenessMonitor
from sprint_message_delivery import SprintWakeDeliveryService

DEFAULT_PULSE_SECONDS = 5.0


def _request_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def enqueue_conversation_turn(
    db_path: str | Path,
    conversation_id: str,
    prompt: str,
    idempotency_key: str,
) -> str:
    """Queue one engine-authored native turn using the wake's stable identity."""
    prompt = prompt.strip()
    idempotency_key = idempotency_key.strip()
    if not prompt or not idempotency_key:
        raise ValueError("Sprint wake prompt and idempotency key are required")
    request_hash = _request_hash(prompt)
    created = False
    con = db_driver.connect(db_path)
    try:
        with db_driver.write_transaction(con, "sprint.wake.enqueue_turn"):
            conversation = con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(f"unknown Sprint conversation: {conversation_id}")
            if conversation["state"] == "closed":
                raise SprintInvariantError(
                    "closed Sprint conversations reject wake turns"
                )
            existing = con.execute(
                "SELECT message_id,sender_kind,sender_ref,message_kind,body,"
                "request_hash FROM conversation_messages "
                "WHERE conversation_id=? AND idempotency_key=?",
                (conversation_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                actual = (
                    existing["sender_kind"],
                    existing["sender_ref"],
                    existing["message_kind"],
                    existing["body"],
                    existing["request_hash"],
                )
                expected = (
                    "engine",
                    "sprint-runtime",
                    "prompt",
                    prompt,
                    request_hash,
                )
                if actual != expected:
                    raise SprintInvariantError(
                        "Sprint wake idempotency key was reused with different input"
                    )
                message_id = int(existing["message_id"])
            else:
                message_id = int(
                    con.execute(
                        "INSERT INTO conversation_messages "
                        "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                        "idempotency_key,request_hash,state) "
                        "VALUES (?,'engine','sprint-runtime','prompt',?,?,?,'queued')",
                        (
                            conversation_id,
                            prompt,
                            idempotency_key,
                            request_hash,
                        ),
                    ).lastrowid
                )
                con.execute(
                    "INSERT INTO conversation_outbox (conversation_id,message_id) "
                    "VALUES (?,?)",
                    (conversation_id, message_id),
                )
                target_state = (
                    conversation["state"]
                    if conversation["state"] in {"queued", "running"}
                    else "queued"
                )
                con.execute(
                    "UPDATE conversations SET state=?,"
                    "last_activity_at=datetime('now'),version=version+1 "
                    "WHERE conversation_id=?",
                    (target_state, conversation_id),
                )
                sequence = int(
                    con.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 "
                        "FROM conversation_events WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                )
                con.execute(
                    "INSERT INTO conversation_events "
                    "(conversation_id,sequence,event_type,payload,message_id) "
                    "VALUES (?,?,'message.accepted',?,?)",
                    (
                        conversation_id,
                        sequence,
                        json.dumps(
                            {
                                "message_id": message_id,
                                "queue_state": "queued",
                                "source": "sprint_wake",
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        message_id,
                    ),
                )
                created = True
    finally:
        con.close()
    if created:
        conversation_events.notify(conversation_id)
        conversation_broker.notify_commit()
    return f"conversation-message:{message_id}"


class SprintRuntimeService(threading.Thread):
    """Five-second armed-service pulse for dispatch and wake delivery."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        deliver: Callable[[str, str, str], str | None] | None = None,
        owner: str | None = None,
        pulse_seconds: float = DEFAULT_PULSE_SECONDS,
        activity_config: activity_monitor.ActivityMonitorConfig | None = None,
    ) -> None:
        super().__init__(name="sprint-runtime", daemon=True)
        if pulse_seconds <= 0:
            raise ValueError("Sprint runtime pulse must be positive")
        self.db_path = str(db_path)
        self.owner = owner or f"sprint-runtime:{os.getpid()}"
        self.pulse_seconds = pulse_seconds
        self.activity_config = activity_config
        self.deliver = deliver or (
            lambda conversation_id, prompt, key: enqueue_conversation_turn(
                self.db_path,
                conversation_id,
                prompt,
                key,
            )
        )
        self._stop_event = threading.Event()
        self._started_once = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._started_once.wait(timeout)

    def pulse_once(self, *, startup: bool = False) -> bool:
        con = db_driver.connect(self.db_path)
        try:
            monitored = activity_monitor.ActivityMonitor(
                con, config=self.activity_config
            ).tick()
            switch = self._switch(con)
            serviced = switch.recover_on_startup() if startup else switch.tick()
            return self._deliver_wakes(con) or serviced or monitored.changed
        finally:
            con.close()

    def _switch(self, con: sqlite3.Connection) -> ArmedServiceSwitch:
        lifecycle = SprintLifecycleStore(con)
        units = SprintWorkUnitStore(con)
        liveness = SprintLivenessMonitor(con)

        def recover_pickup(sprint_id: int, trigger: str) -> None:
            lifecycle.reconcile_unread_pickup(sprint_id, trigger=trigger)

        def dispatch(sprint_id: int, _trigger: str) -> None:
            units.dispatch_ready(sprint_id)

        def evaluate_liveness(sprint_id: int, _trigger: str) -> None:
            liveness.evaluate(sprint_id)

        return ArmedServiceSwitch(
            lifecycle,
            (recover_pickup, dispatch, evaluate_liveness),
        )

    def _deliver_wakes(self, con: sqlite3.Connection) -> bool:
        wakes = SprintWakeDeliveryService(con)
        delivered = False
        while not self._stop_event.is_set():
            outcome = wakes.deliver_once(self.owner, self.deliver)
            if outcome is None:
                return delivered
            if outcome.state != "delivered":
                return True
            delivered = True
        return delivered

    def run(self) -> None:  # pragma: no cover - loop tested through pulse_once
        con = db_driver.connect(self.db_path)
        try:
            switch = self._switch(con)
            monitor = activity_monitor.ActivityMonitor(con, config=self.activity_config)
            try:
                monitor.tick()
                switch.recover_on_startup()
                self._deliver_wakes(con)
            except Exception as exc:  # noqa: BLE001 - service faults stay visible
                print(f"sprint-runtime: startup error ({exc})", flush=True)
            self._started_once.set()
            while not self._stop_event.wait(self.pulse_seconds):
                try:
                    monitor.tick()
                    switch.tick()
                    self._deliver_wakes(con)
                except Exception as exc:  # noqa: BLE001 - keep inspection alive
                    print(f"sprint-runtime: pulse error ({exc})", flush=True)
        finally:
            self._started_once.set()
            con.close()


_SERVICE_LOCK = threading.Lock()
_SERVICE: SprintRuntimeService | None = None


def start_service(
    db_path: str | Path,
    **kwargs: Any,
) -> SprintRuntimeService:
    """Install the process-wide armed Sprint runtime."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE.is_alive():
            return _SERVICE
        _SERVICE = SprintRuntimeService(db_path, **kwargs)
        _SERVICE.start()
        return _SERVICE


def service() -> SprintRuntimeService | None:
    return _SERVICE
