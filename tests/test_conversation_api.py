"""Feature #24 conversation API, idempotency, isolation, and SSE contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))

import conversation_broker
import conversation_events
import conversation_routes


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


def decoded(response):
    status, headers, body = response
    return status, dict(headers), json.loads(body)


class _DisconnectingWriter:
    def __init__(self) -> None:
        self.body = bytearray()
        self.drains = 0
        self.closed = False
        self.head_written = asyncio.Event()

    def write(self, value: bytes) -> None:
        self.body.extend(value)

    async def drain(self) -> None:
        self.drains += 1
        if self.drains == 1:
            self.head_written.set()
        elif b"data:" in self.body:
            raise ConnectionError("test client disconnected")

    def close(self) -> None:
        self.closed = True


class ConversationApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "shell.db"
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute(
            "INSERT INTO users (user_id,username,is_active) "
            "VALUES (1,'operator',1),(2,'other',1)"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,"
            "api_key) VALUES "
            "(1,'Dev','dev','dev','prompt',1,'shell-token'),"
            "(2,'Review','review','dev','prompt',1,'review-token')"
        )
        con.commit()
        con.close()
        (self.root / ".sc-worktrees" / "dev").mkdir(parents=True)
        (self.root / ".sc-worktrees" / "review").mkdir(parents=True)

        patches = (
            mock.patch.object(conversation_routes, "DB_PATH", self.db_path),
            mock.patch.object(conversation_routes.run_mod, "REPO_ROOT", self.root),
            mock.patch.object(
                conversation_routes.conversation_broker,
                "notify_commit",
                return_value=True,
            ),
            mock.patch.object(
                conversation_routes,
                "_wait_for_cli_release",
                return_value=None,
            ),
            mock.patch.object(
                conversation_routes,
                "_live_shell_session",
                return_value=None,
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @staticmethod
    def headers(*, key: str | None = None, extra: dict | None = None) -> str:
        values = {"Host": "localhost:8800", **(extra or {})}
        if key is not None:
            values["Idempotency-Key"] = key
        return "\r\n".join(f"{name}: {value}" for name, value in values.items())

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        key: str | None = None,
        extra_headers: dict | None = None,
    ):
        return decoded(
            conversation_routes.handle(
                method,
                path,
                self.headers(key=key, extra=extra_headers),
                json.dumps(body).encode() if body is not None else b"",
            )
        )

    def create(self, key: str = "create-1", **changes) -> dict:
        body = {"shell_id": 1, "harness": "codex", **changes}
        status, _, obj = self.request("POST", "/api/conversations", body=body, key=key)
        self.assertEqual(status, 201, obj)
        return obj

    def test_create_prepares_never_booted_worktree_on_first_turn(self):
        worktree = self.root / ".sc-worktrees" / "dev"
        worktree.rmdir()
        created = self.create()
        self.assertEqual(
            created["route"]["effort"],
            "high",
            "headless effort must be resolved and immutable at creation",
        )
        self.assertFalse(worktree.exists())
        con = self.connect()
        try:
            row = con.execute(
                "SELECT worktree,effort FROM conversations "
                "WHERE conversation_id=?",
                (created["conversation_id"],),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(Path(row["worktree"]), worktree)
        self.assertEqual(row["effort"], "high")


class ConversationResourceTest(ConversationApiCase):
    def test_write_contention_returns_a_retryable_service_error(self) -> None:
        with mock.patch.object(
            conversation_routes.db_driver,
            "write_transaction",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            status, _, error = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": 1, "harness": "codex"},
                key="busy-create",
            )
        self.assertEqual(status, 503)
        self.assertEqual(error["error"]["code"], "ENGINE_DB_BUSY")
        self.assertEqual(error["error"]["details"]["retry_after"], 2)

    def test_cli_release_drain_runs_before_the_write_transaction(self) -> None:
        def assert_writer_is_unlocked(shell) -> None:
            contender = self.connect()
            try:
                contender.execute("PRAGMA busy_timeout=10")
                contender.execute("BEGIN IMMEDIATE")
                contender.rollback()
            finally:
                contender.close()
            return None

        with mock.patch.object(
            conversation_routes,
            "_wait_for_cli_release",
            side_effect=assert_writer_is_unlocked,
        ):
            created = self.create(key="drain-before-write-lock")
        self.assertEqual(created["state"], "idle")

    def test_create_rechecks_cli_owner_after_the_drain(self) -> None:
        with mock.patch.object(
            conversation_routes,
            "_live_shell_session",
            return_value="busy",
        ):
            status, _, error = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": 1, "harness": "codex"},
                key="cli-raced-after-drain",
            )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "SHELL_BUSY")

    def test_creating_a_chat_refuses_a_live_cli_owner(self) -> None:
        with mock.patch.object(
            conversation_routes,
            "_wait_for_cli_release",
            return_value="busy",
        ):
            status, _, error = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": 1, "harness": "codex"},
                key="cli-owned-shell",
            )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "SHELL_BUSY")
        self.assertIn("live CLI session", error["error"]["message"])

    def test_creating_a_chat_closes_the_previous_idle_chat(self) -> None:
        first = self.create(key="first-chat", title="First")
        second = self.create(key="second-chat", title="Second")
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

        con = self.connect()
        try:
            rows = con.execute(
                "SELECT conversation_id,state,closed_at FROM conversations"
            ).fetchall()
        finally:
            con.close()
        by_id = {row["conversation_id"]: row for row in rows}
        self.assertEqual(by_id[first["conversation_id"]]["state"], "closed")
        self.assertIsNotNone(by_id[first["conversation_id"]]["closed_at"])
        self.assertEqual(by_id[second["conversation_id"]]["state"], "idle")

    def test_each_shell_may_keep_one_browser_chat_open(self) -> None:
        first = self.create(key="first-shell-chat", title="Dev")
        second = self.create(
            key="second-shell-chat",
            shell_id=2,
            title="Review",
        )

        con = self.connect()
        try:
            rows = con.execute(
                "SELECT conversation_id,shell_id,state FROM conversations "
                "ORDER BY shell_id"
            ).fetchall()
        finally:
            con.close()
        by_id = {row["conversation_id"]: row for row in rows}
        self.assertEqual(by_id[first["conversation_id"]]["state"], "idle")
        self.assertEqual(by_id[second["conversation_id"]]["state"], "idle")
        self.assertEqual(by_id[first["conversation_id"]]["shell_id"], 1)
        self.assertEqual(by_id[second["conversation_id"]]["shell_id"], 2)

    def test_creating_a_chat_refuses_while_the_open_turn_is_running(self) -> None:
        first = self.create(key="running-chat")
        con = self.connect()
        try:
            con.execute(
                "UPDATE conversations SET state='queued' "
                "WHERE conversation_id=?",
                (first["conversation_id"],),
            )
            con.commit()
        finally:
            con.close()

        status, _, error = self.request(
            "POST",
            "/api/conversations",
            body={"shell_id": 1, "harness": "codex", "title": "Second"},
            key="blocked-second-chat",
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "BROWSER_CHAT_BUSY")

    def test_close_repairs_running_state_without_a_live_run(self) -> None:
        first = self.create(key="running-close")
        con = self.connect()
        try:
            con.execute(
                "UPDATE conversations SET state='queued' "
                "WHERE conversation_id=?",
                (first["conversation_id"],),
            )
            con.execute(
                "UPDATE conversations SET state='running' "
                "WHERE conversation_id=?",
                (first["conversation_id"],),
            )
            con.commit()
        finally:
            con.close()

        status, _, closed = self.request(
            "PATCH",
            f"/api/conversations/{first['conversation_id']}",
            body={"version": first["version"], "state": "closed"},
        )
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"], "closed")
        self.assertIsNone(closed["close_requested_at"])
        con = self.connect()
        try:
            row = con.execute(
                "SELECT state,closed_at FROM conversations "
                "WHERE conversation_id=?",
                (first["conversation_id"],),
            ).fetchone()
            events = con.execute(
                "SELECT event_type,payload FROM conversation_events "
                "WHERE conversation_id=? ORDER BY sequence",
                (first["conversation_id"],),
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(row["state"], "closed")
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(events[-1]["event_type"], "conversation.closed")
        self.assertEqual(
            json.loads(events[-1]["payload"])["recovered_orphaned_state"],
            "running",
        )

    def test_close_cancels_every_queued_turn_before_releasing_ownership(self) -> None:
        conversation = self.create(key="queued-close")
        message_ids = []
        for number in range(2):
            status, _, accepted = self.request(
                "POST",
                f"/api/conversations/{conversation['conversation_id']}/messages",
                body={"text": f"queued {number}"},
                key=f"queued-close-{number}",
            )
            self.assertEqual(status, 202, accepted)
            message_ids.append(accepted["message"]["message_id"])
        _, _, latest = self.request(
            "GET",
            f"/api/conversations/{conversation['conversation_id']}",
        )

        status, _, closed = self.request(
            "PATCH",
            f"/api/conversations/{conversation['conversation_id']}",
            body={"version": latest["version"], "state": "closed"},
        )

        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"], "closed")
        con = self.connect()
        try:
            messages = con.execute(
                "SELECT message_id,state,completed_at "
                "FROM conversation_messages WHERE conversation_id=? "
                "ORDER BY message_id",
                (conversation["conversation_id"],),
            ).fetchall()
            outbox = con.execute(
                "SELECT message_id,state,run_id FROM conversation_outbox "
                "WHERE conversation_id=? ORDER BY message_id",
                (conversation["conversation_id"],),
            ).fetchall()
            live_runs = con.execute(
                "SELECT COUNT(*) FROM conversation_runs "
                "WHERE conversation_id=? "
                "AND state IN ('leased','starting','running')",
                (conversation["conversation_id"],),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual([row["message_id"] for row in messages], message_ids)
        self.assertEqual([row["state"] for row in messages], ["cancelled", "cancelled"])
        self.assertTrue(all(row["completed_at"] for row in messages))
        self.assertEqual(
            [(row["message_id"], row["state"], row["run_id"]) for row in outbox],
            [(message_ids[0], "cancelled", None), (message_ids[1], "cancelled", None)],
        )
        self.assertEqual(live_runs, 0)

    def test_close_interrupts_active_run_and_closes_after_terminal_proof(self) -> None:
        conversation = self.create(key="active-close")
        message_ids = []
        for number in range(2):
            status, _, accepted = self.request(
                "POST",
                f"/api/conversations/{conversation['conversation_id']}/messages",
                body={"text": f"turn {number}"},
                key=f"active-close-{number}",
            )
            self.assertEqual(status, 202, accepted)
            message_ids.append(accepted["message"]["message_id"])
        store = conversation_broker.BrokerStore(self.db_path)
        run = store.claim_next("test-close")
        self.assertIsNotNone(run)
        _, _, latest = self.request(
            "GET",
            f"/api/conversations/{conversation['conversation_id']}",
        )

        with mock.patch.object(
            conversation_routes.conversation_broker,
            "interrupt_run",
            return_value=True,
        ) as interrupt:
            status, _, closing = self.request(
                "PATCH",
                f"/api/conversations/{conversation['conversation_id']}",
                body={"version": latest["version"], "state": "closed"},
            )

        self.assertEqual(status, 200, closing)
        self.assertEqual(closing["state"], "running")
        self.assertIsNotNone(closing["close_requested_at"])
        interrupt.assert_called_once_with(run.run_id)
        rejected_status, _, rejected = self.request(
            "POST",
            f"/api/conversations/{conversation['conversation_id']}/messages",
            body={"text": "must not queue"},
            key="active-close-rejected",
        )
        self.assertEqual(rejected_status, 409)
        self.assertEqual(rejected["error"]["code"], "CONVERSATION_CLOSING")

        con = self.connect()
        try:
            before_messages = con.execute(
                "SELECT message_id,state FROM conversation_messages "
                "WHERE conversation_id=? AND message_kind='prompt' "
                "ORDER BY message_id",
                (conversation["conversation_id"],),
            ).fetchall()
            before_outbox = con.execute(
                "SELECT message_id,state FROM conversation_outbox "
                "WHERE conversation_id=? ORDER BY message_id",
                (conversation["conversation_id"],),
            ).fetchall()
            request_events = con.execute(
                "SELECT event_type,run_id FROM conversation_events "
                "WHERE conversation_id=? AND event_type IN "
                "('conversation.close.requested','run.interrupt.requested') "
                "ORDER BY sequence",
                (conversation["conversation_id"],),
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(
            [(row["message_id"], row["state"]) for row in before_messages],
            [(message_ids[0], "running"), (message_ids[1], "cancelled")],
        )
        self.assertEqual(
            [(row["message_id"], row["state"]) for row in before_outbox],
            [(message_ids[0], "dispatched"), (message_ids[1], "cancelled")],
        )
        self.assertEqual(
            [(row["event_type"], row["run_id"]) for row in request_events],
            [
                ("conversation.close.requested", run.run_id),
                ("run.interrupt.requested", run.run_id),
            ],
        )

        self.assertTrue(
            store.finish_run(
                run.run_id,
                "cancelled",
                event_type="run.interrupted",
                payload={"outcome": "cancelled"},
            )
        )
        _, _, closed = self.request(
            "GET",
            f"/api/conversations/{conversation['conversation_id']}",
        )
        self.assertEqual(closed["state"], "closed")
        self.assertIsNone(closed["close_requested_at"])
        con = self.connect()
        try:
            final_messages = con.execute(
                "SELECT message_id,state FROM conversation_messages "
                "WHERE conversation_id=? AND message_kind='prompt' "
                "ORDER BY message_id",
                (conversation["conversation_id"],),
            ).fetchall()
            final_events = con.execute(
                "SELECT event_type FROM conversation_events "
                "WHERE conversation_id=? ORDER BY sequence",
                (conversation["conversation_id"],),
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(
            [(row["message_id"], row["state"]) for row in final_messages],
            [(message_ids[0], "cancelled"), (message_ids[1], "cancelled")],
        )
        self.assertEqual(
            [row["event_type"] for row in final_events][-4:],
            [
                "conversation.close.requested",
                "run.interrupt.requested",
                "run.interrupted",
                "conversation.closed",
            ],
        )

    def test_opencode_requires_an_exact_resolved_model(self) -> None:
        with mock.patch.object(
            conversation_routes.run_mod,
            "flavor_defaults",
            return_value={
                "dev": {
                    "default_harness": "opencode",
                    "models": {},
                }
            },
        ):
            status, _, error = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": 1, "harness": "opencode"},
                key="opencode-no-model",
            )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "HARNESS_MODEL_REQUIRED")
        self.assertIn("provider connected in OpenCode",
                      error["error"]["message"])

        status, _, created = self.request(
            "POST",
            "/api/conversations",
            body={
                "shell_id": 1,
                "harness": "opencode",
                "model": "openai/gpt-connected",
            },
            key="opencode-exact-model",
        )
        self.assertEqual(status, 201, created)
        self.assertEqual(created["route"]["model"], "openai/gpt-connected")

    def test_kimi_create_allows_native_default_model_and_resolves_route(
        self,
    ) -> None:
        with mock.patch.object(
            conversation_routes.run_mod,
            "flavor_defaults",
            return_value={
                "dev": {
                    "default_harness": "kimi",
                    "models": {},
                }
            },
        ):
            status, _, created = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": 1, "harness": "kimi"},
                key="kimi-native-default",
            )
        self.assertEqual(status, 201, created)
        self.assertEqual(
            created["route"],
            {
                "harness": "kimi",
                "provider": "kimi",
                "model": None,
                "effort": "high",
            },
        )
        con = self.connect()
        try:
            row = con.execute(
                "SELECT harness,provider,model,effort FROM conversations "
                "WHERE conversation_id=?",
                (created["conversation_id"],),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(
            tuple(row),
            ("kimi", "kimi", None, "high"),
        )

    def test_conductor_shell_rejects_ordinary_browser_chat(self) -> None:
        con = self.connect()
        try:
            con.execute("UPDATE shells SET flavor='conductor' WHERE shell_id=1")
            con.commit()
        finally:
            con.close()
        status, _, error = self.request(
            "POST",
            "/api/conversations",
            body={"shell_id": 1, "harness": "kimi"},
            key="conductor-kimi",
        )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "SPRINT_OWNED_SHELL")
        self.assertIn("armed sprints", error["error"]["message"].lower())
        con = self.connect()
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_create_is_idempotent_and_never_exposes_native_identity(self) -> None:
        first = self.create(title="API")
        second = self.create(title="API")
        self.assertEqual(first, second)
        self.assertNotIn("worktree", first)
        self.assertNotIn("harness_session_ref", json.dumps(first))
        self.assertEqual(first["route"]["harness"], "codex")

        status, _, error = self.request(
            "POST",
            "/api/conversations",
            body={"shell_id": 1, "harness": "codex", "title": "different"},
            key="create-1",
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "CONVERSATION_IDEMPOTENCY_CONFLICT")

        con = self.connect()
        event = con.execute(
            "SELECT sequence,event_type FROM conversation_events"
        ).fetchone()
        count = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        self.assertEqual(tuple(event), (1, "conversation.created"))

    def test_operator_boundary_rejects_tokens_and_cross_site_mutations(self) -> None:
        status, _, obj = self.request(
            "POST",
            "/api/conversations",
            body={"shell_id": 1, "harness": "codex"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(obj["error"]["code"], "IDEMPOTENCY_KEY_REQUIRED")

        status, _, obj = self.request(
            "GET",
            "/api/conversations",
            extra_headers={"Authorization": "Bearer shell-token"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(obj["error"]["code"], "OPERATOR_REQUIRED")

        status, _, obj = self.request(
            "GET",
            "/api/conversations",
            extra_headers={"Authorization": "Bearer stale"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(obj["error"]["code"], "UNAUTHORIZED")

        status, _, obj = self.request(
            "POST",
            "/api/conversations",
            body={"shell_id": 1, "harness": "codex"},
            key="cross-site",
            extra_headers={
                "Origin": "https://hostile.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(obj["error"]["code"], "NOT_SAME_ORIGIN")

    def test_message_outbox_event_commit_atomically_before_broker_wake(self) -> None:
        conversation = self.create()
        conversation_id = conversation["conversation_id"]
        observed = []

        def notified():
            con = self.connect()
            observed.append(
                (
                    con.execute(
                        "SELECT COUNT(*) FROM conversation_messages"
                    ).fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM conversation_outbox").fetchone()[
                        0
                    ],
                    con.execute(
                        "SELECT COUNT(*) FROM conversation_events "
                        "WHERE event_type='message.accepted'"
                    ).fetchone()[0],
                )
            )
            con.close()
            return True

        with mock.patch.object(
            conversation_routes.conversation_broker,
            "notify_commit",
            side_effect=notified,
        ):
            status, _, accepted = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/messages",
                body={"text": "hello"},
                key="message-1",
            )
        self.assertEqual(status, 202, accepted)
        self.assertEqual(observed, [(1, 1, 1)])
        self.assertEqual(accepted["queue_position"], 1)
        self.assertEqual(accepted["message"]["state"], "queued")

        replay_status, _, replay = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "hello"},
            key="message-1",
        )
        self.assertEqual(replay_status, 202)
        self.assertEqual(
            replay["message"]["message_id"],
            accepted["message"]["message_id"],
        )
        con = self.connect()
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0],
            1,
        )
        con.close()

        status, _, conflict = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "not hello"},
            key="message-1",
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "MESSAGE_IDEMPOTENCY_CONFLICT")

    def test_message_stacked_during_run_queues_without_interrupting(self) -> None:
        conversation_id = self.create()["conversation_id"]
        status, _, first = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "keep working"},
            key="message-active",
        )
        self.assertEqual(status, 202, first)
        run = conversation_broker.BrokerStore(self.db_path).claim_next("test")
        self.assertIsNotNone(run)

        status, _, stacked = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "do this next"},
            key="message-stacked",
        )
        self.assertEqual(status, 202, stacked)
        self.assertEqual(stacked["queue_position"], 1)
        self.assertEqual(stacked["message"]["state"], "queued")

        con = self.connect()
        conversation_state = con.execute(
            "SELECT state FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        interrupt_events = con.execute(
            "SELECT COUNT(*) FROM conversation_events "
            "WHERE conversation_id=? AND event_type='run.interrupt.requested'",
            (conversation_id,),
        ).fetchone()[0]
        control_messages = con.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id=? AND message_kind='control'",
            (conversation_id,),
        ).fetchone()[0]
        con.close()
        self.assertEqual(conversation_state, "running")
        self.assertEqual(interrupt_events, 0)
        self.assertEqual(control_messages, 0)

    def test_cursor_pages_have_no_duplicates_and_patch_uses_version(self) -> None:
        created = [
            self.create(key=f"create-{number}")
            for number in range(3)
        ]
        identifiers = {item["conversation_id"] for item in created}
        seen = []
        cursor = None
        while True:
            path = "/api/conversations?limit=1"
            if cursor:
                path += f"&cursor={cursor}"
            status, _, page = self.request("GET", path)
            self.assertEqual(status, 200, page)
            seen.extend(item["conversation_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(set(seen), identifiers)
        self.assertEqual(len(seen), 3)

        conversation_id = created[-1]["conversation_id"]
        status, _, current = self.request(
            "GET",
            f"/api/conversations/{conversation_id}",
        )
        self.assertEqual(status, 200, current)
        version = current["version"]
        status, _, updated = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": version, "title": "Renamed"},
        )
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["title"], "Renamed")
        self.assertEqual(updated["version"], version + 1)
        status, _, conflict = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": version, "title": "Stale"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "CONVERSATION_VERSION_CONFLICT")

    def test_star_round_trip_is_durable_versioned_and_preserves_activity(self) -> None:
        created = self.create(title="Keep my place")
        self.assertFalse(created["starred"])
        conversation_id = created["conversation_id"]
        original_version = created["version"]
        original_activity = created["last_activity_at"]

        status, _, starred = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": original_version, "starred": True},
        )

        self.assertEqual(status, 200, starred)
        self.assertTrue(starred["starred"])
        self.assertEqual(starred["version"], original_version + 1)
        self.assertEqual(starred["title"], "Keep my place")
        self.assertEqual(starred["state"], "idle")
        self.assertEqual(starred["last_activity_at"], original_activity)
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT starred,title,state,last_activity_at,version "
                "FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (1, "Keep my place", "idle", original_activity, original_version + 1),
            )

        conflict_status, _, conflict = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": original_version, "starred": False},
        )
        self.assertEqual(conflict_status, 409, conflict)
        self.assertEqual(
            conflict["error"]["code"],
            "CONVERSATION_VERSION_CONFLICT",
        )
        with closing(self.connect()) as con:
            self.assertEqual(
                con.execute(
                    "SELECT starred FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                1,
            )

    def test_star_requires_boolean_and_closed_history_remains_starrable(self) -> None:
        created = self.create()
        conversation_id = created["conversation_id"]

        status, _, invalid = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": created["version"], "starred": 1},
        )
        self.assertEqual(status, 422, invalid)
        self.assertEqual(invalid["error"]["code"], "VALIDATION_ERROR")
        with closing(self.connect()) as con:
            self.assertEqual(
                tuple(con.execute(
                    "SELECT starred,version FROM conversations "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()),
                (0, created["version"]),
            )

        close_status, _, closed = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": created["version"], "state": "closed"},
        )
        self.assertEqual(close_status, 200, closed)
        star_status, _, starred = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": closed["version"], "starred": True},
        )
        self.assertEqual(star_status, 200, starred)
        self.assertEqual(starred["state"], "closed")
        self.assertTrue(starred["starred"])
        self.assertEqual(starred["version"], closed["version"] + 1)

    def test_closed_conversation_rejects_messages(self) -> None:
        conversation = self.create()
        conversation_id = conversation["conversation_id"]
        status, _, closed = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": 1, "state": "closed"},
        )
        self.assertEqual(status, 200, closed)
        status, _, error = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "too late"},
            key="message-closed",
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "CONVERSATION_CLOSED")

    def test_message_cursor_and_auditable_idempotent_interruption(self) -> None:
        conversation_id = self.create()["conversation_id"]
        message_ids = []
        for number in range(3):
            status, _, accepted = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/messages",
                body={"text": f"message {number}"},
                key=f"message-{number}",
            )
            self.assertEqual(status, 202, accepted)
            message_ids.append(accepted["message"]["message_id"])

        status, _, first = self.request(
            "GET",
            f"/api/conversations/{conversation_id}/messages?limit=2",
        )
        self.assertEqual(status, 200, first)
        self.assertEqual(
            [item["message_id"] for item in first["items"]],
            message_ids[:2],
        )
        status, _, second = self.request(
            "GET",
            f"/api/conversations/{conversation_id}/messages"
            f"?limit=2&cursor={first['next_cursor']}",
        )
        self.assertEqual(status, 200, second)
        self.assertEqual(
            [item["message_id"] for item in second["items"]],
            message_ids[2:],
        )

        run = conversation_broker.BrokerStore(self.db_path).claim_next("test")
        self.assertIsNotNone(run)
        with mock.patch.object(
            conversation_routes.conversation_broker,
            "interrupt_run",
            return_value=True,
        ) as interrupt:
            status, _, receipt = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/interruptions",
                body={"run_id": run.run_id},
                key="interrupt-1",
            )
            self.assertEqual(status, 202, receipt)
            replay_status, _, replay = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/interruptions",
                body={"run_id": run.run_id},
                key="interrupt-1",
            )
        self.assertEqual(replay_status, 202)
        self.assertEqual(
            replay["interruption"]["message_id"],
            receipt["interruption"]["message_id"],
        )
        self.assertEqual(interrupt.call_count, 2)
        self.assertEqual(receipt["interruption"]["message_kind"], "control")
        self.assertEqual(receipt["interruption"]["state"], "completed")

        con = self.connect()
        controls = con.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE message_kind='control'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(controls, 1)

    def test_interruption_intent_survives_a_temporarily_absent_broker(self) -> None:
        conversation_id = self.create()["conversation_id"]
        status, _, _ = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "long turn"},
            key="message-long",
        )
        self.assertEqual(status, 202)
        run = conversation_broker.BrokerStore(self.db_path).claim_next("test")
        self.assertIsNotNone(run)
        unavailable = conversation_broker.BrokerError(
            "CONVERSATION_BROKER_UNAVAILABLE",
            "test broker is restarting",
        )
        with mock.patch.object(
            conversation_routes.conversation_broker,
            "interrupt_run",
            side_effect=unavailable,
        ):
            status, _, receipt = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/interruptions",
                body={"run_id": run.run_id},
                key="interrupt-durable",
            )
        self.assertEqual(status, 202, receipt)
        con = self.connect()
        event = con.execute(
            "SELECT event_type FROM conversation_events "
            "WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(event["event_type"], "run.interrupt.requested")


class ConversationEventStreamTest(ConversationApiCase):
    def test_replay_then_live_sse_redacts_native_session_fields(self) -> None:
        conversation_id = self.create()["conversation_id"]

        async def exercise():
            writer = _DisconnectingWriter()
            task = asyncio.create_task(
                conversation_routes.stream_events(
                    "GET",
                    f"/api/conversations/{conversation_id}/events?after=1",
                    self.headers(),
                    writer,
                )
            )
            await asyncio.wait_for(writer.head_written.wait(), timeout=2)
            con = self.connect()
            con.execute(
                "UPDATE conversations SET harness_session_ref=? "
                "WHERE conversation_id=?",
                ("native-secret", conversation_id),
            )
            con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,payload) "
                "VALUES (?,2,'session.started',?)",
                (
                    conversation_id,
                    json.dumps(
                        {
                            "status": "ready",
                            "session_ref": "native-secret",
                            "detail": "session native-secret failed",
                            "access_token": "credential-secret",
                            "nested": {"threadId": "also-secret", "safe": True},
                        }
                    ),
                ),
            )
            con.commit()
            con.close()
            conversation_events.notify(conversation_id)
            await asyncio.wait_for(task, timeout=2)
            return bytes(writer.body), writer.closed

        body, closed = asyncio.run(exercise())
        self.assertTrue(closed)
        self.assertIn(b"Content-Type: text/event-stream", body)
        self.assertIn(b"id: 2", body)
        self.assertIn(b"event: session.started", body)
        self.assertIn(b'"status":"ready"', body)
        self.assertIn(b'"safe":true', body)
        self.assertIn(b"session [redacted] failed", body)
        self.assertNotIn(b"native-secret", body)
        self.assertNotIn(b"also-secret", body)
        self.assertNotIn(b"credential-secret", body)

    def test_stream_authorization_errors_use_uniform_envelope(self) -> None:
        conversation_id = self.create()["conversation_id"]

        async def exercise():
            writer = _DisconnectingWriter()
            handled = await conversation_routes.stream_events(
                "GET",
                f"/api/conversations/{conversation_id}/events",
                self.headers(extra={"Authorization": "Bearer stale"}),
                writer,
            )
            return handled, bytes(writer.body)

        handled, body = asyncio.run(exercise())
        self.assertTrue(handled)
        payload = body.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(json.loads(payload)["error"]["code"], "UNAUTHORIZED")


if __name__ == "__main__":
    unittest.main()
