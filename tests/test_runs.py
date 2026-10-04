"""Runs ledger acceptance: real HTTP, processes, outage evidence and broker turns."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job
import run_wakes
import runs
import server
import sprint_runtime
from conversation_launch import ConversationLaunchPreparer
from sprint_message_delivery import SprintMessageStore
from test_conversation_broker import ConversationBrokerCase, FakeAdapter
from test_job import TOKEN, build_db


def wait_for(fn, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("timed out waiting for fixture evidence")


class ApiFixture:
    def start_api(self):
        self.engine = self.root / "engine"
        self.patches = [
            mock.patch.object(server, "DB_PATH", str(self.db_path)),
            mock.patch.object(server, "ENGINE", self.engine),
            mock.patch.object(job, "SC_API_TOKEN", TOKEN),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http_thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.http_thread.start()
        self.addCleanup(self.stop_api)
        base = f"http://127.0.0.1:{self.httpd.server_port}"
        patch = mock.patch.object(job, "SC_API_BASE", base)
        patch.start()
        self.addCleanup(patch.stop)
        self.env = {**os.environ, "SC_API_TOKEN": TOKEN, "SC_API_BASE": base}

    def stop_api(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.http_thread.join(2)

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(ENGINE / "scripts/job.py"), *args],
            cwd=self.root,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def register(self, **data):
        return job._api(
            "POST",
            "/_sc/runs",
            {
                "registration_key": "test",
                "argv": ["sh", "-c", "exit 0"],
                "cwd": str(self.root),
                "label": "suite",
                **data,
            },
        )

    def terminal(self, row, code=0):
        return job._api(
            "POST",
            f"/_sc/runs/{row['run_id']}/terminal",
            {
                "state": "done" if code == 0 else "failed",
                "exit_code": code,
                "finished_at": "2026-10-04T10:00:00Z",
            },
        )

    def start_job(self, script, *args):
        result = self.cli("start", *args, "--", "sh", "-c", script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return job._api("GET", "/_sc/runs")["runs"][0]

    def finished(self, row):
        return wait_for(
            lambda: (
                r
                if (r := job._api("GET", f"/_sc/runs/{row['run_id']}"))["state"]
                in runs.TERMINAL
                else None
            )
        )


class RunLedgerTest(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        build_db(str(self.db_path))
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.execute(
            "INSERT INTO shells(shell_id,display_name,shortname,system_prompt,user_id,api_key) "
            "VALUES(2,'Other','other','x',1,'other-token')"
        )
        self.con.commit()
        self.store = runs.RunStore(self.con, self.root / "engine")
        self.start_api()

    def test_legacy_ids_remain_unambiguous_without_retroactive_wakes(self):
        legacy = self.engine / "run" / "jobs" / "18"
        legacy.mkdir(parents=True)
        job.write_meta(
            legacy, {"job_id": "18", "exit_code": 0, "finished_at": job._now()}
        )
        row = self.register()
        self.assertGreater(row["run_id"], 18)
        with mock.patch.object(job, "JOBS", legacy.parent):
            self.assertEqual(job.cmd_status(type("Args", (), {"id": "18"})()), 0)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM wake_message").fetchone()[0], 0
        )

    def test_registration_concurrent_retry_and_label_never_path(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(
                pool.map(
                    lambda n: self.register(
                        registration_key=str(n), label="../../elsewhere"
                    ),
                    range(12),
                )
            )
        self.assertEqual(len({r["run_id"] for r in rows}), 12)
        self.assertTrue(
            all(Path(r["evidence_path"]).parent.name == str(r["run_id"]) for r in rows)
        )
        self.assertEqual(
            self.register(registration_key="0", label="../../elsewhere")["run_id"],
            rows[0]["run_id"],
        )
        with self.assertRaises(urllib.error.HTTPError):
            self.register(registration_key="0", argv=["different"])

    def test_atomic_terminal_and_duplicate_conflict(self):
        row = self.register()
        payload = {
            "state": "done",
            "exit_code": 0,
            "finished_at": "2026-10-04T10:00:00Z",
        }
        with (
            mock.patch.object(
                SprintMessageStore,
                "send_to_shell_in_transaction",
                side_effect=RuntimeError("fault"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.store.terminal(row["run_id"], 1, payload)
        self.assertEqual(self.store.get(row["run_id"])["state"], "registered")
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM shell_messages").fetchone()[0], 0
        )
        first = self.terminal(row)
        second = self.terminal(row)
        self.assertEqual(first["wake_id"], second["wake_id"])
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM wake_message").fetchone()[0], 1
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.terminal(row, code=9)
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(self.store.get(row["run_id"])["exit_code"], 0)

    def test_owner_and_auth_containment(self):
        row = self.register()
        with mock.patch.object(job, "SC_API_TOKEN", "other-token"):
            self.assertEqual(job._api("GET", "/_sc/runs")["runs"], [])
            for method, suffix in [
                ("GET", ""),
                ("GET", "/tail"),
                ("POST", "/kill"),
                ("POST", "/terminal"),
            ]:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    job._api(
                        method,
                        f"/_sc/runs/{row['run_id']}{suffix}",
                        {} if method == "POST" else None,
                    )
                self.assertEqual(caught.exception.code, 403)
        marker = self.root / "unauth-launched"
        result = self.cli(
            "start", "--", "touch", str(marker), env={**self.env, "SC_API_TOKEN": ""}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        result = self.cli("start", "--timeout", "0", "--", "touch", str(marker))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_real_success_failure_spawn_timeout_and_kill(self):
        for script, code in [("echo exact-output; exit 0", 0), ("exit 23", 23)]:
            row = self.finished(self.start_job(script))
            self.assertEqual(row["exit_code"], code)
            self.assertEqual(row["state"], "done" if code == 0 else "failed")
            self.assertIsNotNone(row["message_id"])
        result = self.cli("start", "--", "/no/such/command")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.finished(job._api("GET", "/_sc/runs")["runs"][0])
        self.assertEqual((row["state"], row["exit_code"]), ("failed", 127))
        row = self.finished(self.start_job("sleep 60", "--timeout", "1"))
        self.assertEqual(row["state"], "timeout")
        row = self.start_job("sleep 60")
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        self.assertEqual(self.finished(row)["state"], "killed")

    def test_killed_launcher_does_not_kill_supervisor(self):
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,time,sys; subprocess.run(sys.argv[1:],check=True); time.sleep(60)",
                sys.executable,
                str(ENGINE / "scripts/job.py"),
                "start",
                "--",
                "sh",
                "-c",
                "sleep 1; echo survived; exit 17",
            ],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            row = wait_for(
                lambda: next(iter(job._api("GET", "/_sc/runs")["runs"]), None)
            )
            wait_for(lambda: self.store.get(row["run_id"])["pid"])
            os.killpg(launcher.pid, signal.SIGKILL)
            launcher.wait(3)
            row = self.finished(row)
            self.assertEqual(row["exit_code"], 17)
            self.assertEqual(Path(row["evidence_path"]).read_text().strip(), "survived")
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(3)

    def test_supervisor_loss_and_terminal_gap_reconcile_without_token(self):
        row = self.start_job("sleep 60")
        meta_path = Path(row["evidence_path"]).with_name("meta.json")
        meta = job.read_meta(meta_path.parent)
        os.kill(meta["supervisor_pid"], signal.SIGKILL)
        wait_for(
            lambda: (
                runs.process_alive(
                    meta["supervisor_pid"],
                    meta["supervisor_start_ticks"],
                    meta["boot_id"],
                )
                is False
            )
        )
        try:
            self.store.reconcile()
            lost = self.store.get(row["run_id"])
            self.assertEqual(lost["state"], "lost")
            self.assertIsNone(lost["exit_code"])
            self.assertIsNotNone(lost["wake_id"])
        finally:
            if runs.process_alive(meta["pid"], meta["start_ticks"], meta["boot_id"]):
                os.killpg(meta["pid"], signal.SIGKILL)
        row = self.register(registration_key="terminal-gap")
        path = Path(row["evidence_path"]).parent
        path.mkdir(parents=True)
        job.write_meta(
            path,
            {
                "run_id": row["run_id"],
                "finished_at": "2026-10-04T10:00:00Z",
                "exit_code": 7,
            },
        )
        self.store.reconcile()
        self.store.reconcile()
        self.assertEqual(self.store.get(row["run_id"])["exit_code"], 7)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM wake_message").fetchone()[0], 2
        )

    def test_recycled_pid_boot_and_deleted_owner(self):
        row = self.register()
        data = {
            "pid": os.getpid(),
            "start_ticks": job._start_ticks(os.getpid()) + 1,
            "supervisor_pid": os.getpid(),
            "supervisor_start_ticks": job._start_ticks(os.getpid()),
            "boot_id": runs.boot_id(),
            "started_at": job._now(),
        }
        self.store.running(row["run_id"], 1, data)
        with mock.patch.object(os, "killpg") as kill:
            with self.assertRaises(ValueError):
                self.store.kill(row["run_id"], 1)
            kill.assert_not_called()
        with self.assertRaisesRegex(ValueError, "conflicting run incarnation"):
            self.store.running(row["run_id"], 1, {**data, "boot_id": "different-boot"})
        with mock.patch.object(runs, "boot_id", return_value="new-boot"):
            self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=1")
            self.con.commit()
            self.store.reconcile()
        lost = self.store.get(row["run_id"])
        self.assertEqual((lost["state"], lost["wake_state"]), ("lost", "blocked"))
        self.assertIn("owner unavailable", lost["last_error"])

    def test_populated_run_and_wake_survive_snapshot_rebuild(self):
        import snapshot
        row = self.terminal(self.register())
        content = snapshot.serialize_instance(self.con)
        rebuilt = self.root / 'rebuilt.db'
        build_db(str(rebuilt))
        con = sqlite3.connect(rebuilt)
        try:
            con.executescript(content)
            restored = runs.RunStore(con, self.engine).get(row['run_id'])
            self.assertEqual(restored, row)
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            con.close()

    def test_migration_repeat_preserves_terminal_wake_and_references(self):
        row = self.terminal(self.register())
        migration = (ENGINE / "migrations/0273_runs_ledger.sql").read_text()
        self.con.executescript(migration)
        self.con.executescript(migration)
        self.assertEqual(self.store.get(row["run_id"]), row)
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE runs SET owner_shell_id=2 WHERE run_id=?", (row["run_id"],)
            )
        self.con.rollback()


class RunContinuationTest(ApiFixture, ConversationBrokerCase):
    def setUp(self):
        super().setUp()
        con = self.connect()
        con.execute("UPDATE shells SET api_key=? WHERE shell_id=1", (TOKEN,))
        con.commit()
        con.close()
        self.start_api()

    def _adapter(self, *, start_job=False):
        case = self

        class TranscriptAdapter(FakeAdapter):
            def start(self, context, message):
                if start_job:
                    case.start_job("sleep .3; echo continued; exit 19")
                return self._turn(super().start(context, message), message)

            def resume(self, session_ref, context, message):
                return self._turn(
                    super().resume(session_ref, context, message), message
                )

            def _turn(self, turn, message):
                path = case.root / f"{turn.run_ref}.jsonl"
                path.write_text(
                    json.dumps({"type": "user", "message": {"content": message}}) + "\n"
                )
                return dataclasses.replace(
                    turn,
                    metadata={"transcript_path": str(path), "transcript_offset": 0},
                )

        return TranscriptAdapter()

    def test_codex_server_transcript_proves_consumption_without_a_local_path(self):
        self.add_conversation(state="idle")
        row = self.terminal(self.register())
        case = self
        from conversation_adapters.base import SessionInspection

        class ServerTranscript(FakeAdapter):
            def start(self, context, message):
                self.prompt = message
                turn = super().start(context, message)
                self.turn_id = turn.run_ref
                return turn

            def inspect(self, session_ref, context):
                return SessionInspection(
                    session_ref,
                    True,
                    "completed",
                    context.worktree,
                    {
                        "turns": [
                            {
                                "id": self.turn_id,
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "content": [
                                            {"type": "inputText", "text": self.prompt}
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                )

        adapter = ServerTranscript()
        broker = self.start_broker(lambda _: adapter)
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once()
        broker.notify()
        wait_for(
            lambda: (
                case.cli("status", str(row["run_id"])).stdout.find(
                    '"wake_state": "consumed"'
                )
                >= 0
            )
        )
        self.assertTrue(broker.wait_idle(3))

    def test_job_from_turn_one_resumes_same_chat_with_exact_outcome(self):
        chat = self.add_conversation()
        first = self.add_message(chat)
        adapter = self._adapter(start_job=True)
        broker = self.start_broker(lambda _: adapter)
        wait_for(lambda: self._message_state(first) == "completed")
        row = job._api("GET", "/_sc/runs")["runs"][0]
        self.finished(row)
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)

        def continued():
            runtime.pulse_once()
            broker.notify()
            return (
                result
                if (result := job._api("GET", f"/_sc/runs/{row['run_id']}"))[
                    "wake_state"
                ]
                == "consumed"
                else None
            )

        result = wait_for(continued)
        self.assertEqual(result["exit_code"], 19)
        self.assertIsNotNone(result["turn_run_id"])
        self.assertEqual(adapter.started, 1)
        self.assertEqual(adapter.resumed, 1)
        con = self.connect()
        body = con.execute(
            "SELECT m.body FROM conversation_runs r JOIN conversation_messages m "
            "ON r.trigger_message_id=m.message_id WHERE r.run_id=?",
            (result["turn_run_id"],),
        ).fetchone()[0]
        self.assertIn("exit=19", body)
        self.assertIn(f"Run {row['run_id']}", body)
        self.assertEqual(
            con.execute("SELECT count(*) FROM wake_message").fetchone()[0], 1
        )
        con.close()
        self.assertTrue(broker.wait_idle(3))

    def _message_state(self, mid):
        con = self.connect()
        try:
            return con.execute(
                "SELECT state FROM conversation_messages WHERE message_id=?", (mid,)
            ).fetchone()[0]
        finally:
            con.close()

    def _busy_setup(self):
        self.add_conversation(state="idle")
        row = self.terminal(self.register())
        held = {"busy": True}
        native_preparer = ConversationLaunchPreparer(
            self.db_path,
            liveness_retries=0,
            liveness=lambda: {
                "supported": True,
                "processes": [
                    {
                        "pid": os.getpid(),
                        "shortname": "sh1",
                        "orphaned": False,
                        "claimed": False,
                    }
                ]
                if held["busy"]
                else [],
            },
        )

        def prepare(run):
            if held["busy"]:
                return native_preparer(
                    run
                )  # real liveness refusal before native dispatch
            return run.context(), None

        adapter = self._adapter()
        broker = self.start_broker(lambda _: adapter, launch_preparer=prepare)
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once()
        broker.notify()
        con = self.connect()
        self.addCleanup(con.close)
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts WHERE message_id=?",
            (row["message_id"],),
        ).fetchone()[0]
        wait_for(lambda mid=mid: self._message_state(mid) == "failed")
        return row, held, adapter, broker, runtime, con, mid

    def test_busy_cli_slot_queues_then_consumes_original_message(self):
        row, held, adapter, broker, _runtime, con, mid = self._busy_setup()
        run_wakes.reconcile(con)
        self.assertEqual(job.ledger_run(str(row["run_id"]))["wake_state"], "pending")
        held["busy"] = False
        run_wakes.reconcile(con, now=datetime.now(timezone.utc) + timedelta(seconds=16))
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts WHERE message_id=?",
            (row["message_id"],),
        ).fetchone()[0]
        broker.notify()
        wait_for(lambda: self._message_state(mid) == "completed")
        run_wakes.reconcile(con)
        result = job.ledger_run(str(row["run_id"]))
        self.assertEqual(result["wake_state"], "consumed")
        self.assertEqual(adapter.started, 1)
        attempts = con.execute(
            "SELECT error_code FROM conversation_runs ORDER BY run_id"
        ).fetchall()
        self.assertEqual([r[0] for r in attempts], ["SHELL_BUSY", None])

    def test_busy_ladder_exhaustion_is_visible_and_does_not_reboot(self):
        row, _held, adapter, broker, runtime, con, mid = self._busy_setup()
        now = datetime.now(timezone.utc)
        for delay in run_wakes.BACKOFF:
            run_wakes.reconcile(con, now=now)
            now += timedelta(seconds=delay + 1)
            run_wakes.reconcile(con, now=now)
            mid = con.execute(
                "SELECT conversation_message_id FROM engine_wake_receipts WHERE message_id=?",
                (row["message_id"],),
            ).fetchone()[0]
            broker.notify()
            wait_for(lambda mid=mid: self._message_state(mid) == "failed")
        run_wakes.reconcile(con, now=now)
        result = job.ledger_run(str(row["run_id"]))
        self.assertEqual(result["wake_state"], "blocked")
        self.assertIn("SHELL_BUSY", result["last_error"])
        self.assertEqual(adapter.started, 0)
        before = con.execute("SELECT count(*) FROM conversation_runs").fetchone()[0]
        runtime.pulse_once(startup=True)
        self.assertEqual(
            con.execute("SELECT count(*) FROM conversation_runs").fetchone()[0], before
        )

    def test_transport_outage_beyond_old_budget_retains_one_intent(self):
        self.add_conversation(state="idle")
        self.terminal(self.register())
        con = self.connect()
        self.addCleanup(con.close)
        from sprint_message_delivery import SprintWakeDeliveryService

        now = datetime.now(timezone.utc)
        service = SprintWakeDeliveryService(con, now=lambda: now)
        for _ in range(7):
            outcome = service.deliver_once(
                "test", lambda *_: (_ for _ in ()).throw(OSError("offline"))
            )
            self.assertEqual(outcome.state, "pending")
            now += timedelta(seconds=301)
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        outcome = service.deliver_once("test", runtime.deliver)
        self.assertEqual(outcome.state, "delivered")
        self.assertEqual(
            con.execute("SELECT count(*) FROM wake_message").fetchone()[0], 1
        )
        self.assertEqual(
            con.execute("SELECT count(*) FROM sprint_wake_outbox").fetchone()[0], 1
        )
        self.assertEqual(
            con.execute("SELECT attempts FROM engine_wake_failures").fetchone()[0], 7
        )
