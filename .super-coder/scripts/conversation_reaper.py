#!/usr/bin/env python3
"""Heartbeat reaper for unlinked browser-conversation process groups."""

from __future__ import annotations

import json
import os
import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import conversation_events
import db_driver
from conversation_state import require_transition

TERMINAL_MESSAGE_STATES = frozenset({"completed", "failed", "cancelled"})
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_TERM_GRACE_SECONDS = 15.0
DEFAULT_KILL_GRACE_SECONDS = 15.0
DEFAULT_YOUNG_GRACE_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _configured_seconds(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    allow_zero: bool = False,
) -> float:
    raw = env.get(name, "").strip()
    try:
        value = default if not raw else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of seconds") from exc
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return value


@dataclass(frozen=True)
class ReaperConfig:
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS
    young_grace_seconds: float = DEFAULT_YOUNG_GRACE_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ReaperConfig:
        values = os.environ if env is None else env
        return cls(
            heartbeat_seconds=_configured_seconds(
                values,
                "SC_REAPER_HEARTBEAT_SECONDS",
                DEFAULT_HEARTBEAT_SECONDS,
            ),
            term_grace_seconds=_configured_seconds(
                values,
                "SC_REAPER_TERM_GRACE_SECONDS",
                DEFAULT_TERM_GRACE_SECONDS,
                allow_zero=True,
            ),
            kill_grace_seconds=_configured_seconds(
                values,
                "SC_REAPER_KILL_GRACE_SECONDS",
                DEFAULT_KILL_GRACE_SECONDS,
                allow_zero=True,
            ),
            young_grace_seconds=_configured_seconds(
                values,
                "SC_REAPER_YOUNG_GRACE_SECONDS",
                DEFAULT_YOUNG_GRACE_SECONDS,
                allow_zero=True,
            ),
        )


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    start_ticks: int
    process_group_id: int


@dataclass(frozen=True)
class ReaperCandidate:
    run_id: int
    conversation_id: str
    message_id: int
    pid: int
    start_ticks: int
    process_group_id: int
    started_at: datetime
    last_signal: str | None
    signaled_at: datetime | None


def read_process_snapshot(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> ProcessSnapshot | None:
    """Read pid, pgrp, and field-22 start ticks from one procfs snapshot."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
        fields_after_comm = stat.rsplit(")", 1)[1].split()
        process_group_id = int(fields_after_comm[2])
        start_ticks = int(fields_after_comm[19])
    except (IndexError, OSError, ValueError):
        return None
    if pid <= 0 or process_group_id <= 0 or start_ticks < 0:
        return None
    return ProcessSnapshot(pid, start_ticks, process_group_id)


class ReaperStore:
    """Short SQLite reads and writes for the process-ladder state machine."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        connect: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.db_path = str(db_path)
        self._connect = connect
        self.clock = clock

    def connect(self):
        return (
            self._connect()
            if self._connect is not None
            else db_driver.connect(self.db_path)
        )

    @staticmethod
    def _protected_sql() -> str:
        return (
            "EXISTS(SELECT 1 FROM active_shell_chats active "
            "WHERE active.process_pid=r.process_pid "
            "AND active.process_start_ticks=r.process_start_ticks)"
        )

    @staticmethod
    def _sweepable_sql() -> str:
        return (
            "(r.state IN ('starting','running') OR (r.state='unknown' "
            "AND NOT EXISTS(SELECT 1 FROM conversation_events reaped "
            "WHERE reaped.run_id=r.run_id "
            "AND reaped.event_type='run.interrupted')))"
        )

    def candidates(self) -> list[ReaperCandidate]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT r.run_id,r.conversation_id,r.trigger_message_id,"
                "r.process_pid,r.process_start_ticks,r.process_group_id,"
                "r.started_at,r.reaper_last_signal,r.reaper_signaled_at "
                "FROM conversation_runs r "
                f"WHERE {self._sweepable_sql()} "
                "AND r.process_pid IS NOT NULL "
                f"AND NOT {self._protected_sql()} "
                "ORDER BY r.run_id"
            ).fetchall()
        finally:
            con.close()
        return [
            ReaperCandidate(
                run_id=int(row["run_id"]),
                conversation_id=str(row["conversation_id"]),
                message_id=int(row["trigger_message_id"]),
                pid=int(row["process_pid"]),
                start_ticks=int(row["process_start_ticks"]),
                process_group_id=int(row["process_group_id"]),
                started_at=_parse_stamp(str(row["started_at"])),
                last_signal=row["reaper_last_signal"],
                signaled_at=(
                    _parse_stamp(str(row["reaper_signaled_at"]))
                    if row["reaper_signaled_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def heartbeat(self, interval_seconds: float) -> None:
        con = self.connect()
        try:
            with db_driver.write_transaction(con, "conversation.reaper.heartbeat"):
                con.execute(
                    "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                    "VALUES ('conversation-reaper',datetime('now'),?) "
                    "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
                    "interval_s=excluded.interval_s",
                    (interval_seconds,),
                )
        finally:
            con.close()

    def eligible(self, candidate: ReaperCandidate) -> bool:
        con = self.connect()
        try:
            return (
                con.execute(
                    "SELECT 1 FROM conversation_runs r "
                    f"WHERE r.run_id=? AND {self._sweepable_sql()} "
                    "AND r.process_pid=? AND r.process_start_ticks=? "
                    "AND r.process_group_id=? "
                    f"AND NOT {self._protected_sql()}",
                    (
                        candidate.run_id,
                        candidate.pid,
                        candidate.start_ticks,
                        candidate.process_group_id,
                    ),
                ).fetchone()
                is not None
            )
        finally:
            con.close()

    def record_signal(self, candidate: ReaperCandidate, value: str) -> bool:
        con = self.connect()
        now = _stamp(self.clock())
        try:
            with db_driver.write_transaction(con, "conversation.reaper.signal"):
                changed = con.execute(
                    "UPDATE conversation_runs AS r SET reaper_last_signal=?,"
                    "reaper_signaled_at=? WHERE run_id=? "
                    f"AND {self._sweepable_sql()} "
                    "AND process_pid=? AND process_start_ticks=? "
                    "AND process_group_id=? "
                    f"AND NOT {self._protected_sql()}",
                    (
                        value,
                        now,
                        candidate.run_id,
                        candidate.pid,
                        candidate.start_ticks,
                        candidate.process_group_id,
                    ),
                ).rowcount
                return changed == 1
        finally:
            con.close()

    @staticmethod
    def _append_interrupted_event(con, candidate: ReaperCandidate, reason: str) -> None:
        sequence = con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?",
            (candidate.conversation_id,),
        ).fetchone()[0]
        payload = json.dumps(
            {
                "interrupt_evidence": "operator",
                "outcome": "cancelled",
                "reaper": {
                    "process_group_id": candidate.process_group_id,
                    "reason": reason,
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id,run_id) "
            "VALUES (?,?,'run.interrupted',?,?,?)",
            (
                candidate.conversation_id,
                sequence,
                payload,
                candidate.message_id,
                candidate.run_id,
            ),
        )

    def finish_interrupted(self, candidate: ReaperCandidate, reason: str) -> bool:
        """Write the reaper-owned terminal state iff identity stays unprotected."""
        con = self.connect()
        now = _stamp(self.clock())
        try:
            with db_driver.write_transaction(con, "conversation.reaper.finish"):
                row = con.execute(
                    "SELECT r.state,c.state AS conversation_state,m.state AS message_state "
                    "FROM conversation_runs r "
                    "JOIN conversations c ON c.conversation_id=r.conversation_id "
                    "JOIN conversation_messages m "
                    "ON m.message_id=r.trigger_message_id "
                    f"WHERE r.run_id=? AND {self._sweepable_sql()} "
                    "AND r.process_pid=? AND r.process_start_ticks=? "
                    "AND r.process_group_id=? "
                    f"AND NOT {self._protected_sql()}",
                    (
                        candidate.run_id,
                        candidate.pid,
                        candidate.start_ticks,
                        candidate.process_group_id,
                    ),
                ).fetchone()
                if row is None:
                    return False
                run_state = str(row["state"])
                if run_state != "unknown":
                    require_transition("run", run_state, "cancelled")
                    con.execute(
                        "UPDATE conversation_runs SET state='cancelled',ended_at=?,"
                        "error_code='CONVERSATION_RUN_REAPED',error_detail=? "
                        "WHERE run_id=?",
                        (now, reason[:16384], candidate.run_id),
                    )
                    message_state = str(row["message_state"])
                    if message_state not in TERMINAL_MESSAGE_STATES:
                        require_transition("message", message_state, "cancelled")
                        con.execute(
                            "UPDATE conversation_messages SET state='cancelled',"
                            "completed_at=? WHERE message_id=?",
                            (now, candidate.message_id),
                        )
                    conversation_state = str(row["conversation_state"])
                    if conversation_state == "running":
                        pending = (
                            con.execute(
                                "SELECT 1 FROM conversation_outbox "
                                "WHERE conversation_id=? "
                                "AND state IN ('pending','claimed') LIMIT 1",
                                (candidate.conversation_id,),
                            ).fetchone()
                            is not None
                        )
                        target = "queued" if pending else "idle"
                        require_transition("conversation", conversation_state, target)
                        con.execute(
                            "UPDATE conversations SET state=?,last_activity_at=?,"
                            "version=version+1 WHERE conversation_id=?",
                            (target, now, candidate.conversation_id),
                        )
                self._append_interrupted_event(con, candidate, reason)
        finally:
            con.close()
        conversation_events.notify(candidate.conversation_id)
        return True


class ConversationReaper(threading.Thread):
    """Advance each orphan one ladder rung per heartbeat without sleeping."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        store: ReaperStore | None = None,
        config: ReaperConfig | None = None,
        clock: Callable[[], datetime] = _utcnow,
        process_reader: Callable[[int], ProcessSnapshot | None] = read_process_snapshot,
        signal_group: Callable[[int, signal.Signals], None] = os.killpg,
        native_interrupt: Callable[[int], Any] | None = None,
    ) -> None:
        super().__init__(name="conversation-reaper", daemon=True)
        self.clock = clock
        self.store = store or ReaperStore(db_path, clock=clock)
        self.config = config or ReaperConfig.from_env()
        self.process_reader = process_reader
        self.signal_group = signal_group
        self.native_interrupt = native_interrupt
        self._stop_event = threading.Event()
        self._started_once = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._started_once.wait(timeout)

    @staticmethod
    def _matches(candidate: ReaperCandidate, snapshot: ProcessSnapshot | None) -> bool:
        return snapshot is not None and (
            snapshot.pid == candidate.pid
            and snapshot.start_ticks == candidate.start_ticks
            and snapshot.process_group_id == candidate.process_group_id
        )

    def _finish_if_gone(self, candidate: ReaperCandidate, reason: str) -> bool:
        return self.store.finish_interrupted(candidate, reason)

    def _native_step(self, candidate: ReaperCandidate) -> bool:
        if not self.store.eligible(candidate):
            return False
        try:
            if self.native_interrupt is not None:
                self.native_interrupt(candidate.run_id)
        except Exception as exc:  # noqa: BLE001 - adapter boundary stays live
            print(
                f"conversation-reaper: native interrupt failed for run "
                f"{candidate.run_id} ({exc})",
                flush=True,
            )
        return self.store.record_signal(candidate, "interrupt")

    def _signal_step(
        self,
        candidate: ReaperCandidate,
        value: signal.Signals,
        label: str,
    ) -> bool:
        if not self.store.eligible(candidate):
            return False
        snapshot = self.process_reader(candidate.pid)
        if not self._matches(candidate, snapshot):
            return self._finish_if_gone(
                candidate,
                "recorded process identity exited or was recycled before signal",
            )
        try:
            self.signal_group(candidate.process_group_id, value)
        except ProcessLookupError:
            return self._finish_if_gone(
                candidate,
                "recorded process group exited before signal",
            )
        changed = self.store.record_signal(candidate, label)
        if changed and value == signal.SIGKILL:
            return self._finish_if_gone(
                candidate,
                "reaper SIGKILL delivered to unlinked process group",
            )
        return changed

    def sweep_once(self) -> int:
        now = self.clock()
        advanced = 0
        for candidate in self.store.candidates():
            try:
                age = (now - candidate.started_at).total_seconds()
                if age < self.config.young_grace_seconds:
                    continue
                snapshot = self.process_reader(candidate.pid)
                if snapshot is None:
                    advanced += int(
                        self._finish_if_gone(
                            candidate,
                            "recorded process exited before reaper signal",
                        )
                    )
                    continue
                if not self._matches(candidate, snapshot):
                    advanced += int(
                        self._finish_if_gone(
                            candidate,
                            "recorded process identity exited or was recycled",
                        )
                    )
                    continue
                if candidate.last_signal is None:
                    advanced += int(self._native_step(candidate))
                    continue
                elapsed = (
                    (now - candidate.signaled_at).total_seconds()
                    if candidate.signaled_at is not None
                    else float("inf")
                )
                if candidate.last_signal == "interrupt":
                    if elapsed >= self.config.term_grace_seconds:
                        advanced += int(
                            self._signal_step(
                                candidate,
                                signal.SIGTERM,
                                "SIGTERM",
                            )
                        )
                    continue
                if candidate.last_signal == "SIGTERM":
                    if elapsed >= self.config.kill_grace_seconds:
                        advanced += int(
                            self._signal_step(
                                candidate,
                                signal.SIGKILL,
                                "SIGKILL",
                            )
                        )
                    continue
                if candidate.last_signal == "SIGKILL":
                    advanced += int(
                        self._finish_if_gone(
                            candidate,
                            "reaper SIGKILL delivered to unlinked process group",
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one candidate
                print(
                    f"conversation-reaper: run {candidate.run_id} sweep failed ({exc})",
                    flush=True,
                )
        return advanced

    def run(self) -> None:  # pragma: no cover - loop tested through sweep seam
        try:
            self.sweep_once()
            self.store.heartbeat(self.config.heartbeat_seconds)
        except Exception as exc:  # noqa: BLE001 - service must retry next beat
            print(f"conversation-reaper: startup error ({exc})", flush=True)
        self._started_once.set()
        while not self._stop_event.wait(self.config.heartbeat_seconds):
            try:
                self.sweep_once()
                self.store.heartbeat(self.config.heartbeat_seconds)
            except Exception as exc:  # noqa: BLE001 - service must stay live
                print(f"conversation-reaper: heartbeat error ({exc})", flush=True)


_SERVICE_LOCK = threading.Lock()
_SERVICE: ConversationReaper | None = None


def start_service(
    db_path: str | Path,
    **kwargs: Any,
) -> ConversationReaper:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE.is_alive():
            return _SERVICE
        _SERVICE = ConversationReaper(db_path, **kwargs)
        _SERVICE.start()
        return _SERVICE


def stop_service() -> None:
    """Stop and join the process-wide service before its DB can disappear."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            return
        _SERVICE.stop()
        _SERVICE.join()
        _SERVICE = None


def service() -> ConversationReaper | None:
    return _SERVICE
