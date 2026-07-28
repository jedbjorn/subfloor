#!/usr/bin/env python3
"""Chat-hosted Interface state and the isolated local chat database.

This module owns no HTTP or WebSocket surface.  It is the durable core shared
by the Stage-2 driver and the later chat/toggle units: one SQLite transaction
serializes every session action, and all message/tool content stays in
``.sc-state/local/interface_chat.db`` rather than the engine memory database.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
CHAT_DB_PATH = REPO_ROOT / ".sc-state" / "local" / "interface_chat.db"
CHAT_MIGRATIONS = ENGINE / "chat_migrations"
MAX_HEALTH_COUNT = 2_147_483_647


class ChatMigrationError(RuntimeError):
    """The private chat schema could not be brought to the current version."""


class ChatStoreError(RuntimeError):
    """A chat-store request violated a durable state or data invariant."""


class ChatUnavailable(RuntimeError):
    """Chat hosting is disabled; terminal hosting remains independent."""


def default_chat_db_path(engine_db_path: str | Path) -> Path:
    """Production uses .sc-state/local; throwaway engine DBs stay throwaway."""
    engine_path = Path(engine_db_path)
    if engine_path.parent.name == ".super-coder":
        return engine_path.parent.parent / ".sc-state" / "local" / "interface_chat.db"
    return engine_path.parent / "interface_chat.db"


@dataclasses.dataclass(frozen=True)
class ActionResult:
    status: str
    state: str
    turn_id: str | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def month_key(timestamp: str) -> str:
    return timestamp[:7]


def event_key(session_id: str, turn_id: str, kind: str, ordinal: int) -> str:
    identity = f"{session_id}\0{turn_id}\0{kind}\0{ordinal}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"ev1:{digest}"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _migration_files(directory: Path) -> list[Path]:
    paths = sorted(directory.glob("*.sql"))
    bad = [
        path.name
        for path in paths
        if re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", path.name) is None
    ]
    if bad:
        raise ChatMigrationError(
            f"invalid chat migration filename(s): {', '.join(bad)}"
        )
    if not paths or not paths[0].name.startswith("0001_"):
        raise ChatMigrationError("chat migrations must begin at 0001")
    numbers = [int(path.name[:4]) for path in paths]
    expected = list(range(1, len(paths) + 1))
    if numbers != expected:
        raise ChatMigrationError(
            "chat migration numbers must be contiguous: "
            f"found {numbers}, expected {expected}"
        )
    return paths


def connect_chat(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def run_chat_migrations(
    db_path: str | Path,
    migrations_dir: str | Path = CHAT_MIGRATIONS,
) -> list[str]:
    """Apply the dedicated forward-only chat namespace transactionally."""
    path = Path(db_path)
    directory = Path(migrations_dir)
    files = _migration_files(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_chat(path)
    applied_now: list[str] = []
    try:
        ledger_exists = con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='chat_schema_migrations'"
        ).fetchone()
        applied: dict[str, str] = {}
        if ledger_exists:
            applied = {
                row["migration_id"]: row["checksum_sha256"]
                for row in con.execute(
                    "SELECT migration_id, checksum_sha256 "
                    "FROM chat_schema_migrations"
                )
            }
        known = {path.stem for path in files}
        unexpected = sorted(set(applied) - known)
        if unexpected:
            raise ChatMigrationError(
                "database contains unknown chat migration(s): "
                + ", ".join(unexpected)
            )
        for migration in files:
            migration_id = migration.stem
            body = migration.read_text()
            checksum = hashlib.sha256(body.encode()).hexdigest()
            prior = applied.get(migration_id)
            if prior is not None:
                if prior != checksum:
                    raise ChatMigrationError(
                        f"chat migration {migration_id} checksum changed"
                    )
                continue
            applied_at = utc_now()
            script = (
                "BEGIN IMMEDIATE;\n"
                + body
                + "\nINSERT INTO chat_schema_migrations "
                "(migration_id, checksum_sha256, applied_at) VALUES ("
                + ", ".join(
                    _sql_string(value)
                    for value in (migration_id, checksum, applied_at)
                )
                + ");\nCOMMIT;\n"
            )
            try:
                con.executescript(script)
            except sqlite3.Error as exc:
                if con.in_transaction:
                    con.rollback()
                raise ChatMigrationError(
                    f"chat migration {migration_id} failed: {exc}"
                ) from exc
            applied_now.append(migration_id)
        return applied_now
    finally:
        con.close()


def _json_text(value: Any, *, label: str) -> str:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChatStoreError(f"{label} is not valid JSON") from exc
    return text


def _raw_json_text(value: str, *, label: str) -> str:
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise ChatStoreError(f"{label} is not valid JSON") from exc
    return value


def _health_key(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", value).strip("_") or "unknown"
    if len(cleaned) <= 96:
        return cleaned
    digest = hashlib.sha256(cleaned.encode()).hexdigest()[:16]
    return f"{cleaned[:79]}:{digest}"


class ChatStore:
    def __init__(
        self,
        db_path: str | Path = CHAT_DB_PATH,
        migrations_dir: str | Path = CHAT_MIGRATIONS,
    ):
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)

    def migrate(self) -> list[str]:
        return run_chat_migrations(self.db_path, self.migrations_dir)

    def connect(self) -> sqlite3.Connection:
        return connect_chat(self.db_path)

    def create_session(
        self,
        session_id: str,
        *,
        shell_id: int,
        harness: str,
        cwd: str,
        provider_session_id: str | None = None,
    ) -> None:
        now = utc_now()
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO chat_sessions "
                "(session_id, shell_id, harness, cwd, provider_session_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    shell_id,
                    harness,
                    cwd,
                    provider_session_id,
                    now,
                    now,
                ),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ChatStoreError(f"cannot create chat session {session_id}") from exc
        finally:
            con.close()

    def session(self, session_id: str) -> dict[str, Any]:
        con = self.connect()
        try:
            row = con.execute(
                "SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise ChatStoreError(f"chat session {session_id} not found")
            return dict(row)
        finally:
            con.close()

    def bind_provider_session(
        self,
        session_id: str,
        provider_session_id: str,
        *,
        transcript_locator: str | None = None,
    ) -> None:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT provider_session_id FROM chat_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ChatStoreError(f"chat session {session_id} not found")
            if row["provider_session_id"] not in (None, provider_session_id):
                raise ChatStoreError("provider session id changed")
            con.execute(
                "UPDATE chat_sessions SET provider_session_id=?, "
                "transcript_locator=COALESCE(?, transcript_locator), updated_at=? "
                "WHERE session_id=?",
                (provider_session_id, transcript_locator, utc_now(), session_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def request_action(
        self,
        session_id: str,
        action: str,
        *,
        prompt: str | None = None,
        anchor: dict[str, Any] | None = None,
        turn_id: str | None = None,
        attempt_of: str | None = None,
    ) -> ActionResult:
        if action not in {"composer", "wake", "toggle"}:
            raise ChatStoreError(f"unsupported chat action: {action}")
        if action in {"composer", "wake"}:
            if not isinstance(prompt, str) or not prompt:
                raise ChatStoreError(f"{action} requires a non-empty prompt")
            if not isinstance(anchor, dict):
                raise ChatStoreError(f"{action} requires a pre-turn anchor")
            anchor_json = _json_text(anchor, label="pre-turn anchor")
        else:
            anchor_json = ""

        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            session = con.execute(
                "SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise ChatStoreError(f"chat session {session_id} not found")
            state = session["host_mode"]

            if action == "toggle":
                if state == "hosted_terminal":
                    con.commit()
                    return ActionResult("hosted_terminal", state)
                con.execute(
                    "UPDATE chat_sessions SET toggle_pending=1, updated_at=? "
                    "WHERE session_id=?",
                    (utc_now(), session_id),
                )
                con.commit()
                return ActionResult("toggle_queued", state)

            if state == "running_headless":
                if action == "wake":
                    con.execute(
                        "UPDATE chat_sessions SET wake_pending=1, updated_at=? "
                        "WHERE session_id=?",
                        (utc_now(), session_id),
                    )
                    con.commit()
                    return ActionResult("wake_queued", state)
                con.commit()
                return ActionResult("turn_busy", state)
            if state == "hosted_terminal":
                con.commit()
                return ActionResult("hosted_terminal", state)
            if state != "idle_chat":
                raise ChatStoreError(f"unknown chat host mode: {state}")

            new_turn_id = turn_id
            if not new_turn_id:
                raise ChatStoreError("accepted chat action requires a turn id")
            source = action
            if attempt_of is not None:
                original = con.execute(
                    "SELECT session_id, retry_safe FROM chat_turns WHERE turn_id=?",
                    (attempt_of,),
                ).fetchone()
                if (
                    original is None
                    or original["session_id"] != session_id
                    or original["retry_safe"] != 1
                ):
                    raise ChatStoreError("attempt_of is not a retry-safe turn")
                source = "retry"
            now = utc_now()
            con.execute(
                "INSERT INTO chat_turns "
                "(turn_id, session_id, source, state, attempt_of, "
                "pre_turn_anchor_json, started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    new_turn_id,
                    session_id,
                    source,
                    "running",
                    attempt_of,
                    anchor_json,
                    now,
                ),
            )
            self._insert_cursor(
                con, session_id, new_turn_id, anchor or {}, created_at=now
            )
            self._insert_events(
                con,
                session_id,
                new_turn_id,
                [
                    {
                        "kind": "user_message",
                        "role": "user",
                        "payload": {"text": prompt, "source": source},
                    },
                    {
                        "kind": "turn_started",
                        "role": "system",
                        "payload": {"source": source},
                    },
                ],
                created_at=now,
            )
            con.execute(
                "UPDATE chat_sessions SET host_mode='running_headless', "
                "updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            con.commit()
            return ActionResult("accepted", "running_headless", new_turn_id)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _insert_cursor(
        con: sqlite3.Connection,
        session_id: str,
        turn_id: str,
        anchor: dict[str, Any],
        *,
        created_at: str,
    ) -> None:
        status = anchor.get("status", "missing")
        if status not in {"ready", "missing"}:
            status = "missing"
        con.execute(
            "INSERT INTO chat_transcript_cursors "
            "(session_id, turn_id, transcript_path, source_offset, next_offset, "
            "line_sha256, file_size, resolution_status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                turn_id,
                anchor.get("path"),
                anchor.get("offset"),
                anchor.get("next_offset"),
                anchor.get("line_sha256"),
                anchor.get("file_size"),
                status,
                created_at,
            ),
        )

    def update_anchor_resolution(
        self,
        turn_id: str,
        resolution: dict[str, Any],
    ) -> None:
        status = resolution.get("status")
        if status not in {"exact", "relocated", "gap"}:
            raise ChatStoreError("invalid transcript anchor resolution")
        con = self.connect()
        try:
            cur = con.execute(
                "UPDATE chat_transcript_cursors SET resolution_status=?, "
                "resolved_offset=?, resolved_at=? WHERE turn_id=?",
                (
                    status,
                    resolution.get("next_offset"),
                    utc_now(),
                    turn_id,
                ),
            )
            if cur.rowcount != 1:
                raise ChatStoreError(f"turn {turn_id} has no transcript cursor")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def append_events(
        self,
        turn_id: str,
        events: Iterable[dict[str, Any]],
        *,
        transcript_anchor: dict[str, Any] | None = None,
    ) -> int:
        prepared = self._prepare_events(events, transcript_anchor)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            turn = con.execute(
                "SELECT session_id FROM chat_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise ChatStoreError(f"chat turn {turn_id} not found")
            inserted = self._insert_prepared(
                con, turn["session_id"], turn_id, prepared, created_at=utc_now()
            )
            con.commit()
            return inserted
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def append_event_json(
        self,
        turn_id: str,
        *,
        kind: str,
        role: str | None,
        payload_json: str,
    ) -> int:
        payload = json.loads(
            _raw_json_text(payload_json, label="event payload")
        )
        return self.append_events(
            turn_id, [{"kind": kind, "role": role, "payload": payload}]
        )

    def complete_turn(
        self,
        turn_id: str,
        events: Iterable[dict[str, Any]],
        *,
        exit_code: int,
    ) -> int:
        prepared = self._prepare_events(events, None)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            turn = self._running_turn(con, turn_id)
            now = utc_now()
            inserted = self._insert_prepared(
                con, turn["session_id"], turn_id, prepared, created_at=now
            )
            inserted += self._insert_events(
                con,
                turn["session_id"],
                turn_id,
                [
                    {
                        "kind": "turn_completed",
                        "role": "system",
                        "payload": {"exit_code": exit_code},
                    }
                ],
                created_at=now,
            )
            con.execute(
                "UPDATE chat_turns SET state='completed', ended_at=?, "
                "exit_code=?, retry_safe=0 WHERE turn_id=?",
                (now, exit_code, turn_id),
            )
            con.execute(
                "UPDATE chat_sessions SET host_mode='idle_chat', updated_at=? "
                "WHERE session_id=?",
                (now, turn["session_id"]),
            )
            con.commit()
            return inserted
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def fail_turn(
        self,
        turn_id: str,
        events: Iterable[dict[str, Any]],
        *,
        exit_code: int | None,
        failure_code: str,
        diagnostic: str,
        retry_safe: bool,
        aborted: bool = False,
    ) -> int:
        prepared = self._prepare_events(events, None)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            turn = self._running_turn(con, turn_id)
            now = utc_now()
            inserted = self._insert_prepared(
                con, turn["session_id"], turn_id, prepared, created_at=now
            )
            inserted += self._insert_events(
                con,
                turn["session_id"],
                turn_id,
                [
                    {
                        "kind": "turn_failed",
                        "role": "system",
                        "payload": {
                            "code": failure_code,
                            "retry_safe": bool(retry_safe),
                            "aborted": bool(aborted),
                        },
                    }
                ],
                created_at=now,
            )
            con.execute(
                "UPDATE chat_turns SET state=?, ended_at=?, exit_code=?, "
                "failure_code=?, failure_diagnostic=?, retry_safe=? "
                "WHERE turn_id=?",
                (
                    "aborted" if aborted else "failed",
                    now,
                    exit_code,
                    failure_code,
                    diagnostic,
                    int(retry_safe),
                    turn_id,
                ),
            )
            con.execute(
                "UPDATE chat_sessions SET host_mode='idle_chat', updated_at=? "
                "WHERE session_id=?",
                (now, turn["session_id"]),
            )
            con.commit()
            return inserted
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _running_turn(con: sqlite3.Connection, turn_id: str) -> sqlite3.Row:
        turn = con.execute(
            "SELECT session_id, state FROM chat_turns WHERE turn_id=?", (turn_id,)
        ).fetchone()
        if turn is None:
            raise ChatStoreError(f"chat turn {turn_id} not found")
        if turn["state"] != "running":
            raise ChatStoreError(f"chat turn {turn_id} is not running")
        return turn

    def _prepare_events(
        self,
        events: Iterable[dict[str, Any]],
        transcript_anchor: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        anchor_json = (
            _json_text(transcript_anchor, label="event transcript anchor")
            if transcript_anchor is not None
            else None
        )
        prepared = []
        for event in events:
            if not isinstance(event, dict):
                raise ChatStoreError("normalized event must be an object")
            prepared.append(
                {
                    "kind": event.get("kind"),
                    "role": event.get("role"),
                    "payload_json": _json_text(
                        event.get("payload"), label="event payload"
                    ),
                    "transcript_anchor_json": anchor_json,
                }
            )
        return prepared

    def _insert_events(
        self,
        con: sqlite3.Connection,
        session_id: str,
        turn_id: str,
        events: Iterable[dict[str, Any]],
        *,
        created_at: str,
    ) -> int:
        prepared = self._prepare_events(events, None)
        return self._insert_prepared(
            con, session_id, turn_id, prepared, created_at=created_at
        )

    @staticmethod
    def _insert_prepared(
        con: sqlite3.Connection,
        session_id: str,
        turn_id: str,
        events: list[dict[str, Any]],
        *,
        created_at: str,
    ) -> int:
        next_seq = con.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM chat_events "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        ordinals: dict[str, int] = {}
        inserted = 0
        for event in events:
            kind = event["kind"]
            ordinals[kind] = ordinals.get(kind, 0) + 1
            next_seq += 1
            cur = con.execute(
                "INSERT INTO chat_events "
                "(event_key, session_id, turn_id, event_seq, month_key, kind, "
                "role, payload_json, transcript_anchor_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (
                    event_key(session_id, turn_id, kind, ordinals[kind]),
                    session_id,
                    turn_id,
                    next_seq,
                    month_key(created_at),
                    kind,
                    event["role"],
                    event["payload_json"],
                    event["transcript_anchor_json"],
                    created_at,
                ),
            )
            inserted += cur.rowcount
        return inserted

    def increment_health(self, harness: str, counter_keys: Iterable[str]) -> None:
        keys = [_health_key(key) for key in counter_keys]
        if not keys:
            return
        now = utc_now()
        key_month = month_key(now)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            for key in keys:
                con.execute(
                    "INSERT INTO chat_health "
                    "(harness, counter_key, month_key, count, updated_at) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(harness, counter_key, month_key) DO UPDATE SET "
                    "count=MIN(chat_health.count + 1, ?), "
                    "updated_at=excluded.updated_at",
                    (harness, key, key_month, 1, now, MAX_HEALTH_COUNT),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def retry_prompt(self, turn_id: str) -> str:
        con = self.connect()
        try:
            turn = con.execute(
                "SELECT source, retry_safe FROM chat_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if (
                turn is None
                or turn["source"] == "wake"
                or turn["retry_safe"] != 1
            ):
                raise ChatStoreError("turn is not eligible for composer retry")
            row = con.execute(
                "SELECT payload_json FROM chat_events "
                "WHERE turn_id=? AND kind='user_message' ORDER BY event_seq LIMIT 1",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise ChatStoreError("retry-safe turn has no submitted prompt")
            value = json.loads(row["payload_json"])
            return value["text"]
        finally:
            con.close()

    def turn_context(self, turn_id: str) -> dict[str, Any]:
        con = self.connect()
        try:
            row = con.execute(
                "SELECT t.*, s.provider_session_id, s.cwd, s.harness, "
                "e.payload_json AS submitted_payload "
                "FROM chat_turns t "
                "JOIN chat_sessions s ON s.session_id=t.session_id "
                "LEFT JOIN chat_events e ON e.turn_id=t.turn_id "
                "AND e.kind='user_message' "
                "WHERE t.turn_id=? ORDER BY e.event_seq LIMIT 1",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise ChatStoreError(f"chat turn {turn_id} not found")
            value = dict(row)
            payload = json.loads(value.pop("submitted_payload"))
            value["submitted_prompt"] = payload["text"]
            return value
        finally:
            con.close()

    def consume_boundary_request(self, session_id: str) -> str | None:
        """Consume at most one coalesced request; toggle wins the boundary."""
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT host_mode, toggle_pending, wake_pending "
                "FROM chat_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ChatStoreError(f"chat session {session_id} not found")
            if row["host_mode"] != "idle_chat":
                con.commit()
                return None
            if row["toggle_pending"]:
                action = "toggle"
                column = "toggle_pending"
            elif row["wake_pending"]:
                action = "wake"
                column = "wake_pending"
            else:
                con.commit()
                return None
            con.execute(
                f"UPDATE chat_sessions SET {column}=0, updated_at=? "
                "WHERE session_id=?",
                (utc_now(), session_id),
            )
            con.commit()
            return action
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


class ChatRuntime:
    """Availability gate: migration failure disables chat, never terminal tmux."""

    def __init__(
        self,
        db_path: str | Path = CHAT_DB_PATH,
        migrations_dir: str | Path = CHAT_MIGRATIONS,
    ):
        self.store = ChatStore(db_path, migrations_dir)
        self.available = False
        self.unavailable_reason = "start() not called"

    def start(self) -> None:
        try:
            self.store.migrate()
        except Exception as exc:  # noqa: BLE001 - every migration failure gates chat
            self.available = False
            self.unavailable_reason = f"chat migration failed: {exc}"[:512]
            return
        self.available = True
        self.unavailable_reason = ""

    def require_available(self) -> ChatStore:
        if not self.available:
            raise ChatUnavailable(
                f"chat hosting unavailable: {self.unavailable_reason}"
            )
        return self.store
