"""Armed-only Sprint work dispatch plus engine-wide wake-turn delivery."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import activity_monitor
import conversation_broker
import conversation_events
import db_driver
import sprint_cleanup
from sprint_domain import (
    ArmedServiceSwitch,
    SprintInvariantError,
    SprintLifecycleStore,
    SprintWorkUnitStore,
)
from sprint_message_delivery import SprintWakeDeliveryService

DEFAULT_PULSE_SECONDS = 5.0
RUNTIME_DAEMON_NAME = "sprint-runtime"
# A runtime is stale after three missed recorded pulse intervals.  The
# threshold follows the durable interval rather than assuming the default.
RUNTIME_STALE_INTERVALS = 3


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def runtime_status(
    con: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, str | int | float | None]:
    """Project live/stale/missing state from the durable runtime heartbeat."""
    heartbeat = con.execute(
        "SELECT beat_at,interval_s FROM daemon_heartbeats WHERE name=?",
        (RUNTIME_DAEMON_NAME,),
    ).fetchone()
    if heartbeat is None:
        return {
            "state": "missing",
            "beat_at": None,
            "interval_seconds": int(DEFAULT_PULSE_SECONDS),
        }
    beat_at = str(heartbeat["beat_at"])
    interval = float(heartbeat["interval_s"])
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - _parse_stamp(beat_at)).total_seconds()
    state = "live" if age <= RUNTIME_STALE_INTERVALS * interval else "stale"
    interval_value: int | float = int(interval) if interval.is_integer() else interval
    return {
        "state": state,
        "beat_at": beat_at,
        "interval_seconds": interval_value,
    }


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
        self._ready = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._started_once.wait(timeout)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait until the first complete successful cycle is durable."""
        return self._ready.wait(timeout)

    def pulse_once(self, *, startup: bool = False) -> bool:
        con = db_driver.connect(self.db_path)
        try:
            return self._pulse(con, startup=startup)
        finally:
            con.close()

    def _pulse(self, con: sqlite3.Connection, *, startup: bool) -> bool:
        monitored = activity_monitor.ActivityMonitor(
            con, config=self.activity_config
        ).tick()
        switch = self._switch(con)
        serviced = switch.recover_on_startup() if startup else switch.tick()
        delivered = self._deliver_wakes(con)
        cleanup = sprint_cleanup.SprintCleanupExecutor(
            sprint_cleanup.SprintCleanupTargetStore(con)
        ).run_next(f"{self.owner}:cleanup")
        self._record_heartbeat(con)
        return delivered or serviced or monitored.changed or cleanup.state != "idle"

    def _record_heartbeat(self, con: sqlite3.Connection) -> None:
        prior = con.execute(
            "SELECT beat_at FROM daemon_heartbeats WHERE name=?",
            (RUNTIME_DAEMON_NAME,),
        ).fetchone()
        floor = str(prior[0]) if prior is not None else None
        stamp = str(
            con.execute(
                "SELECT CASE WHEN ? IS NOT NULL "
                "AND ?>=strftime('%Y-%m-%d %H:%M:%f','now') "
                "THEN strftime('%Y-%m-%d %H:%M:%f',?,'+0.001 seconds') "
                "ELSE strftime('%Y-%m-%d %H:%M:%f','now') END",
                (floor, floor, floor),
            ).fetchone()[0]
        )
        with db_driver.write_transaction(con, "sprint.runtime.heartbeat"):
            con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "beat_at=excluded.beat_at,interval_s=excluded.interval_s",
                (RUNTIME_DAEMON_NAME, stamp, self.pulse_seconds),
            )

    def _switch(self, con: sqlite3.Connection) -> ArmedServiceSwitch:
        lifecycle = SprintLifecycleStore(con)
        units = SprintWorkUnitStore(con)

        def recover_pickup(sprint_id: int, trigger: str) -> None:
            lifecycle.reconcile_unread_pickup(sprint_id, trigger=trigger)

        def dispatch(sprint_id: int, _trigger: str) -> None:
            units.dispatch_ready(sprint_id)

        return ArmedServiceSwitch(
            lifecycle,
            (recover_pickup, dispatch),
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
        try:
            con = db_driver.connect(self.db_path)
            try:
                self._started_once.set()
                if self._stop_event.is_set():
                    return
                try:
                    self._pulse(con, startup=True)
                except Exception as exc:  # noqa: BLE001 - startup must fail closed
                    print(f"sprint-runtime: startup error ({exc})", flush=True)
                    return
                self._ready.set()
                while not self._stop_event.wait(self.pulse_seconds):
                    try:
                        self._pulse(con, startup=False)
                    except Exception as exc:  # noqa: BLE001 - keep inspection alive
                        print(f"sprint-runtime: pulse error ({exc})", flush=True)
            except Exception as exc:  # noqa: BLE001 - service faults stay visible
                print(f"sprint-runtime: service error ({exc})", flush=True)
            finally:
                con.close()
        finally:
            self._started_once.set()


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
