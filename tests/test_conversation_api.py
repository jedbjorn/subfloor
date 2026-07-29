"""Feature #24 conversation API, idempotency, isolation, and SSE contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
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
            "api_key) VALUES (1,'Dev','dev','dev','prompt',1,'shell-token')"
        )
        con.commit()
        con.close()
        (self.root / ".sc-worktrees" / "dev").mkdir(parents=True)

        patches = (
            mock.patch.object(conversation_routes, "DB_PATH", self.db_path),
            mock.patch.object(conversation_routes.run_mod, "REPO_ROOT", self.root),
            mock.patch.object(
                conversation_routes.conversation_broker,
                "notify_commit",
                return_value=True,
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


class ConversationResourceTest(ConversationApiCase):
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

    def test_cursor_pages_have_no_duplicates_and_patch_uses_version(self) -> None:
        identifiers = {
            self.create(key=f"create-{number}")["conversation_id"]
            for number in range(3)
        }
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

        conversation_id = seen[0]
        status, _, updated = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": 1, "title": "Renamed"},
        )
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["title"], "Renamed")
        self.assertEqual(updated["version"], 2)
        status, _, conflict = self.request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"version": 1, "title": "Stale"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "CONVERSATION_VERSION_CONFLICT")

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
