"""Mechanical activity ceiling and engine-wake recovery pulse."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

import active_chat_registry
import conversation_events
import db_driver

DEFAULT_CHAT_INACTIVITY_CEILING_SECONDS = 60 * 60
ENGINE_WAKE_RECOVERY_BACKOFF_SECONDS = 180
ENGINE_WAKE_RECOVERY_MAX_AGE_SECONDS = 24 * 60 * 60
_LOG = logging.getLogger("super_coder.activity_monitor")


def _configured_ceiling(env: Mapping[str, str]) -> float:
    raw = env.get("SC_CHAT_INACTIVITY_CEILING", "").strip()
    try:
        value = DEFAULT_CHAT_INACTIVITY_CEILING_SECONDS if not raw else float(raw)
    except ValueError:
        value = 0
    if not math.isfinite(value) or value <= 0:
        _LOG.warning(
            "invalid SC_CHAT_INACTIVITY_CEILING=%r; using default %s seconds",
            raw,
            DEFAULT_CHAT_INACTIVITY_CEILING_SECONDS,
        )
        return DEFAULT_CHAT_INACTIVITY_CEILING_SECONDS
    return value


@dataclass(frozen=True)
class ActivityMonitorConfig:
    inactivity_ceiling_seconds: float = DEFAULT_CHAT_INACTIVITY_CEILING_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ActivityMonitorConfig:
        return cls(_configured_ceiling(os.environ if env is None else env))


@dataclass(frozen=True)
class ActivityMonitorOutcome:
    closed_chat_ids: tuple[str, ...]
    recovered_wake_ids: tuple[int, ...]

    @property
    def changed(self) -> bool:
        return bool(self.closed_chat_ids or self.recovered_wake_ids)


class ActivityMonitor:
    """Run one engine-wide, timer-driven maintenance pulse."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        config: ActivityMonitorConfig | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.config = config or ActivityMonitorConfig.from_env()

    @staticmethod
    def _append_closed_event(
        con: sqlite3.Connection,
        *,
        chat_id: str,
        run_id: int,
        ceiling_seconds: float,
    ) -> None:
        sequence = int(
            con.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
                "WHERE conversation_id=?",
                (chat_id,),
            ).fetchone()[0]
        )
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,run_id) "
            "VALUES (?,?,'conversation.closed',?,?)",
            (
                chat_id,
                sequence,
                json.dumps(
                    {
                        "inactivity_ceiling_seconds": ceiling_seconds,
                        "reason": "chat inactivity ceiling exceeded",
                        "state": "closed",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                run_id,
            ),
        )

    def _close_inactive_chats(self) -> list[str]:
        modifier = f"-{self.config.inactivity_ceiling_seconds:g} seconds"
        rows = self.con.execute(
            "SELECT a.shell_id,a.chat_id,a.process_pid,a.process_start_ticks,"
            "r.run_id FROM active_shell_chats a "
            "JOIN conversations c ON c.conversation_id=a.chat_id "
            "JOIN conversation_runs r ON r.conversation_id=a.chat_id "
            "AND r.process_pid=a.process_pid "
            "AND r.process_start_ticks=a.process_start_ticks "
            "WHERE a.process_pid IS NOT NULL "
            "AND r.state IN ('leased','starting','running') "
            "AND c.last_activity_at<=datetime('now', ?) "
            "ORDER BY a.shell_id",
            (modifier,),
        ).fetchall()
        closed: list[str] = []
        for row in rows:
            active = active_chat_registry.get(self.con, int(row["shell_id"]))
            if active is None or (
                active.chat_id != row["chat_id"]
                or active.process_pid != row["process_pid"]
                or active.process_start_ticks != row["process_start_ticks"]
            ):
                continue
            result = active_chat_registry.close_for_inactivity(
                self.con, int(row["shell_id"])
            )
            if result is None:
                continue
            self._append_closed_event(
                self.con,
                chat_id=result.chat_id,
                run_id=int(row["run_id"]),
                ceiling_seconds=self.config.inactivity_ceiling_seconds,
            )
            closed.append(result.chat_id)
        return closed

    def _recover_engine_wakes(self) -> list[int]:
        age_modifier = f"-{ENGINE_WAKE_RECOVERY_MAX_AGE_SECONDS} seconds"
        rows = self.con.execute(
            "SELECT w.wake_id,w.receiver_shell_id "
            "FROM sprint_wake_outbox w "
            "WHERE w.sprint_id IS NULL AND w.state='failed' "
            "AND w.idempotency_key NOT LIKE 'engine-recovery:%' "
            "AND w.created_at>=datetime('now', ?) "
            "AND EXISTS (SELECT 1 FROM sprint_wake_messages joined "
            "JOIN wake_message message USING (message_id) "
            "WHERE joined.wake_id=w.wake_id AND message.sprint_id IS NULL "
            "AND message.delivered_at IS NULL AND message.read_at IS NULL) "
            "ORDER BY w.wake_id",
            (age_modifier,),
        ).fetchall()
        recovered: list[int] = []
        for row in rows:
            old_wake_id = int(row["wake_id"])
            receiver_shell_id = int(row["receiver_shell_id"])
            pending = self.con.execute(
                "SELECT wake_id FROM sprint_wake_outbox "
                "WHERE receiver_shell_id=? AND state='pending' "
                "ORDER BY wake_id LIMIT 1",
                (receiver_shell_id,),
            ).fetchone()
            if pending is None:
                recovery_key = f"engine-recovery:failed-wake:{old_wake_id}"
                new_wake_id = int(
                    self.con.execute(
                        "INSERT INTO sprint_wake_outbox "
                        "(receiver_shell_id,idempotency_key,available_at) "
                        "VALUES (?,?,datetime('now', ?))",
                        (
                            receiver_shell_id,
                            recovery_key,
                            f"+{ENGINE_WAKE_RECOVERY_BACKOFF_SECONDS} seconds",
                        ),
                    ).lastrowid
                )
            else:
                new_wake_id = int(pending["wake_id"])
            self.con.execute(
                "UPDATE sprint_wake_messages SET wake_id=? "
                "WHERE wake_id=? AND message_id IN ("
                "SELECT message_id FROM wake_message "
                "WHERE sprint_id IS NULL AND delivered_at IS NULL AND read_at IS NULL)",
                (new_wake_id, old_wake_id),
            )
            recovered.append(new_wake_id)
        return recovered

    def tick(self) -> ActivityMonitorOutcome:
        with db_driver.write_transaction(self.con, "activity_monitor.tick"):
            closed = self._close_inactive_chats()
            recovered = self._recover_engine_wakes()
        for chat_id in closed:
            conversation_events.notify(chat_id)
        return ActivityMonitorOutcome(tuple(closed), tuple(recovered))
