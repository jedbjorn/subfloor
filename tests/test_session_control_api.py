#!/usr/bin/env python3
"""Session-control API state changes and token scope."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402
import session_dispatcher as dispatcher  # noqa: E402


def build_db(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);"
        "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
        "system_prompt, user_id, api_key, active_archive_id) "
        "VALUES (1, 'Planner 1', 'PLN1', 'planner', 'x', 1, 'token-1', NULL);"
        "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
        "system_prompt, user_id, api_key, active_archive_id) "
        "VALUES (2, 'Planner 2', 'PLN2', 'planner', 'x', 1, 'token-2', NULL);"
        "INSERT INTO shell_memory_archives "
        "(archive_id, shell_id, session_id, date, harness, provider, model) "
        "VALUES (10, 1, '0001', '2026-07-21', 'fake', 'test', 'm');"
        "INSERT INTO shell_memory_archives "
        "(archive_id, shell_id, session_id, date, harness, provider, model) "
        "VALUES (11, 2, '0001', '2026-07-21', 'fake', 'test', 'm');"
        "UPDATE shells SET active_archive_id=10 WHERE shell_id=1;"
        "UPDATE shells SET active_archive_id=11 WHERE shell_id=2;"
        "INSERT INTO shell_session_bindings "
        "(binding_id, archive_id, shell_id, harness, native_session_id, "
        "control_capabilities, state, managed) "
        "VALUES (20, 10, 1, 'fake', 'native-1', "
        "'{\"deliver\":true,\"resume\":true}', 'dormant', 0);"
        "INSERT INTO shell_session_bindings "
        "(binding_id, archive_id, shell_id, harness, native_session_id, "
        "control_capabilities, state, managed) "
        "VALUES (21, 11, 2, 'fake', 'native-2', "
        "'{\"deliver\":true}', 'idle', 0);"
    )
    con.commit()
    return con


def add_message(con: sqlite3.Connection, shell_id: int = 1) -> int:
    mid = con.execute(
        "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind) "
        "VALUES (?, ?, 'wake', 'task')", (shell_id, shell_id)
    ).lastrowid
    con.commit()
    return mid


class ControlMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.addCleanup(self.con.close)

    def test_manage_reconstructs_exact_unread_work_and_is_idempotent(self):
        unread = add_message(self.con)
        read = add_message(self.con)
        self.con.execute(
            "UPDATE shell_messages SET read_at=datetime('now') WHERE message_id=?",
            (read,),
        )
        self.con.commit()

        payload, error = server.manage_session_control(self.con, 1, 20)
        self.assertIsNone(error)
        self.assertEqual((20, 1, "dormant"), (
            payload["binding"]["binding_id"], payload["binding"]["managed"],
            payload["binding"]["state"],
        ))
        self.assertEqual({"queued": 1}, payload["jobs"])
        self.assertEqual(
            [(20, unread, "queued", 0)],
            [tuple(row) for row in self.con.execute(
                "SELECT binding_id, trigger_message_id, state, attempt_count "
                "FROM session_wake_jobs")],
        )
        # Run the actual second call and prove it did not duplicate the ledger.
        again, second_error = server.manage_session_control(self.con, 1, 20)
        self.assertIsNone(second_error)
        self.assertEqual({"queued": 1}, again["jobs"])
        self.assertEqual(1, self.con.execute(
            "SELECT COUNT(*) FROM session_wake_jobs"
        ).fetchone()[0])

    def test_manage_rejects_approval_prompting_posture_without_mutation(self):
        message_id = add_message(self.con)
        self.con.execute(
            "UPDATE shell_session_bindings SET control_capabilities=? "
            "WHERE binding_id=20",
            (json.dumps({
                "deliver": True,
                "resume": True,
                "settings": {
                    "sandbox": "workspace-write",
                    "approval_policy": "on-request",
                },
            }),),
        )
        self.con.commit()

        payload, error = server.manage_session_control(self.con, 1, 20)

        self.assertIsNone(payload)
        self.assertEqual(
            "managed wake requires approval_policy='never' or "
            "sandbox='danger-full-access' so a headless turn cannot request approval",
            error,
        )
        self.assertEqual(
            ("dormant", 0, None),
            tuple(self.con.execute(
                "SELECT state, managed, last_error FROM shell_session_bindings "
                "WHERE binding_id=20"
            ).fetchone()),
        )
        self.assertEqual(
            [],
            [tuple(row) for row in self.con.execute(
                "SELECT binding_id, trigger_message_id FROM session_wake_jobs "
                "WHERE trigger_message_id=?", (message_id,)
            )],
        )

    def test_release_cancels_queue_but_keeps_message_unread(self):
        message_id = add_message(self.con)
        server.manage_session_control(self.con, 1, 20)
        payload, error = server.release_session_control(self.con, 1, 20)
        self.assertIsNone(error)
        self.assertEqual(("released", 0), (
            payload["binding"]["state"], payload["binding"]["managed"]
        ))
        row = self.con.execute(
            "SELECT state, last_error FROM session_wake_jobs"
        ).fetchone()
        self.assertEqual(("cancelled", "binding released"), tuple(row))
        self.assertIsNone(self.con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=?", (message_id,)
        ).fetchone()[0])

    def test_release_refuses_running_dispatch_without_mutation(self):
        self.con.execute(
            "UPDATE shell_session_bindings SET managed=1, state='dispatching' "
            "WHERE binding_id=20"
        )
        self.con.commit()
        payload, error = server.release_session_control(self.con, 1, 20)
        self.assertIsNone(payload)
        self.assertEqual("binding is dispatching; release after the turn exits", error)
        row = self.con.execute(
            "SELECT state, managed FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()
        self.assertEqual(("dispatching", 1), tuple(row))

    def test_retry_resets_failed_unread_jobs_and_dispatches_again(self):
        failed = add_message(self.con)
        done = add_message(self.con)
        self.con.execute(
            "INSERT INTO session_wake_jobs "
            "(binding_id, trigger_message_id, state, attempt_count, last_error) "
            "VALUES (20, ?, 'failed', 4, 'old')", (failed,)
        )
        self.con.execute(
            "INSERT INTO session_wake_jobs "
            "(binding_id, trigger_message_id, state, attempt_count) "
            "VALUES (20, ?, 'done', 1)", (done,)
        )
        self.con.execute(
            "UPDATE shell_session_bindings SET state='error', managed=1, "
            "last_error='old' WHERE binding_id=20"
        )
        self.con.commit()

        payload, error = server.retry_session_control(self.con, 1, 20)
        self.assertIsNone(error)
        self.assertEqual(("starting", None), (
            payload["binding"]["state"], payload["binding"]["last_error"]
        ))
        self.assertEqual(
            [(failed, "queued", 0, None), (done, "done", 1, None)],
            [tuple(row) for row in self.con.execute(
                "SELECT trigger_message_id, state, attempt_count, last_error "
                "FROM session_wake_jobs ORDER BY trigger_message_id")],
        )

        adapter = mock.Mock()
        adapter.status.return_value = "dormant"

        def acknowledge(_binding, _prompt):
            self.con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id=?", (failed,)
            )
            self.con.commit()

        adapter.resume.side_effect = acknowledge
        attempted = dispatcher.poll_once(
            self.con,
            adapter_factory=lambda _binding: adapter,
            api_probe=lambda _binding, _base: True,
            reconcile=lambda _con, _binding_id, **_kwargs: "vacant",
            lease_preflight=lambda _con, _binding_id, **_kwargs: None,
            attempt_log=mock.Mock(),
        )
        self.assertEqual(1, attempted)
        adapter.resume.assert_called_once()
        self.assertEqual(
            [(failed, "done", 1, None), (done, "done", 1, None)],
            [tuple(row) for row in self.con.execute(
                "SELECT trigger_message_id, state, attempt_count, last_error "
                "FROM session_wake_jobs ORDER BY trigger_message_id")],
        )
        self.assertEqual("dormant", self.con.execute(
            "SELECT state FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()[0])

    def test_binding_patch_rejects_remote_endpoint_without_partial_write(self):
        payload, error = server.patch_session_binding(
            self.con, 1, 20,
            {"control_endpoint": "https://user:pw@example.test/control",
             "cli_version": "should-not-land"},
        )
        self.assertIsNone(payload)
        self.assertEqual(
            "control_endpoint must be a local socket or loopback URL", error
        )
        row = self.con.execute(
            "SELECT control_endpoint, cli_version FROM shell_session_bindings "
            "WHERE binding_id=20"
        ).fetchone()
        self.assertEqual((None, None), tuple(row))

    def test_binding_patch_canonicalizes_capabilities_and_cannot_cross_shells(self):
        payload, error = server.patch_session_binding(
            self.con, 1, 20,
            {"control_endpoint": "unix:///tmp/fake.sock",
             "control_capabilities": {"resume": True, "deliver": False},
             "cli_version": "1.2.3", "state": "idle"},
        )
        self.assertIsNone(error)
        self.assertEqual(("idle", "unix:///tmp/fake.sock", "1.2.3"), (
            payload["binding"]["state"], payload["binding"]["control_endpoint"],
            payload["binding"]["cli_version"],
        ))
        self.assertEqual(
            '{"deliver":false,"resume":true}',
            payload["binding"]["control_capabilities"],
        )
        denied, denied_error = server.patch_session_binding(
            self.con, 1, 21, {"cli_version": "stolen"}
        )
        self.assertIsNone(denied)
        self.assertEqual("no such session binding", denied_error)
        self.assertIsNone(self.con.execute(
            "SELECT cli_version FROM shell_session_bindings WHERE binding_id=21"
        ).fetchone()[0])

    def test_binding_patch_cannot_bypass_dispatch_or_release_operations(self):
        for forbidden in ("dispatching", "released"):
            payload, error = server.patch_session_binding(
                self.con, 1, 20, {"state": forbidden}
            )
            self.assertIsNone(payload)
            self.assertEqual(
                "dispatching/released are owned by dispatcher/release operations",
                error,
            )
        row = self.con.execute(
            "SELECT state, managed FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()
        self.assertEqual(("dormant", 0), tuple(row))

        self.con.execute(
            "UPDATE shell_session_bindings SET state='dispatching' "
            "WHERE binding_id=20"
        )
        self.con.commit()
        payload, error = server.patch_session_binding(
            self.con, 1, 20, {"state": "idle"}
        )
        self.assertIsNone(payload)
        self.assertEqual("dispatching is owned by the dispatcher", error)
        self.assertEqual("dispatching", self.con.execute(
            "SELECT state FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()[0])

    def test_channel_registration_uses_fenced_supervisor_and_stays_scoped(self):
        with mock.patch.object(
            server.session_supervisor, "register_active_channel", return_value=88
        ) as register:
            payload, error = server.update_session_channel(
                self.con, 1,
                {"binding_id": 20, "action": "register", "pid": 123},
            )
        self.assertIsNone(error)
        self.assertEqual({"binding_id": 20, "pid": 123, "start_ticks": 88}, payload)
        register.assert_called_once_with(
            self.con, 20, 123, repo_root=server.REPO_ROOT
        )
        denied, denied_error = server.update_session_channel(
            self.con, 1,
            {"binding_id": 21, "action": "register", "pid": 123},
        )
        self.assertIsNone(denied)
        self.assertEqual("no such session binding", denied_error)


class TokenScopeHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "api.db"
        con = build_db(str(self.db_path))
        con.close()
        self.db_patch = mock.patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, *, token: str | None = None,
                body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_auth_is_required_and_token_cannot_mutate_another_binding(self):
        status, payload = self.request("GET", "/_sc/session-control")
        self.assertEqual((401, "Authorization: Bearer <token> required"),
                         (status, payload["error"]))
        status, payload = self.request(
            "POST", "/_sc/session-control/manage", token="token-1",
            body={"binding_id": 21},
        )
        self.assertEqual((409, "no session binding for this shell"),
                         (status, payload["error"]))
        status, payload = self.request(
            "POST", "/_sc/session-control/manage", token="token-1",
            body={"binding_id": 20},
        )
        self.assertEqual((200, 20, 1),
                         (status, payload["binding"]["binding_id"],
                          payload["binding"]["managed"]))
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT binding_id, managed FROM shell_session_bindings "
                "ORDER BY binding_id"
            ).fetchall()
        self.assertEqual([(20, 1), (21, 0)], rows)


if __name__ == "__main__":
    unittest.main()
