"""Runs ledger acceptance: real HTTP, processes, outage evidence and broker turns."""

from __future__ import annotations

import dataclasses
import io
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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job
import run_wakes
import runs
import server
import sprint_runtime
import test_sprint_work_dispatch as sprint_fixture
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

    def test_oversized_spawn_error_submits_bounded_terminal_payload(self):
        result = self.cli("start", "--label", "long-argv", "--", "x" * 5000)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.finished(job._api("GET", "/_sc/runs")["runs"][0])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit_code"], 127)
        self.assertLess(len(row["terminal_json"].encode()), 4096)
        self.assertIsNotNone(row["message_id"])
        meta = job.read_meta(Path(row["evidence_path"]).parent)
        self.assertGreater(len(meta["spawn_error"]), 4096)
        meta["spawn_error"] = "😀" * 5000
        self.assertLess(len(json.dumps(runs.terminal_payload(meta)).encode()), 4096)

    def test_local_reads_during_api_outage_for_ledger_and_decimal_legacy(self):
        row = self.register()
        directory = Path(row["evidence_path"]).parent
        legacy = self.engine / "run" / "jobs" / "42"
        for path in (directory, legacy):
            path.mkdir(parents=True)
            (path / "log").write_text("persisted command output\n")
            job.write_meta(
                path,
                {
                    "job_id": path.name,
                    "run_id": row["run_id"] if path == directory else None,
                    "cmd": ["echo", "proof"],
                    "exit_code": 19,
                    "finished_at": job._now(),
                    "log": str(path / "log"),
                    "submission_error": "URLError: submission unavailable",
                    "submission_attempted_at": "2026-10-05T10:00:00Z",
                    "submission_attempt": 2,
                },
            )
        self.stop_api()  # exercise the actual refused connection, not a mock
        with (
            mock.patch.object(job, "RUNS", directory.parent),
            mock.patch.object(job, "JOBS", legacy.parent),
        ):
            for path in (directory, legacy):
                args = SimpleNamespace(id=path.name, n=10, for_seconds=1)
                for command in (job.cmd_status, job.cmd_tail, job.cmd_wait):
                    with self.subTest(path=path.name, command=command.__name__):
                        out, err = io.StringIO(), io.StringIO()
                        with redirect_stdout(out), redirect_stderr(err):
                            self.assertEqual(command(args), 0)
                        self.assertIn("API unreachable", err.getvalue())
                        self.assertIn("unauthoritative", err.getvalue())
                        if command == job.cmd_status:
                            self.assertIn("exit_code: 19", out.getvalue())
                            self.assertIn("submission_error: URLError", out.getvalue())
                            self.assertIn("2026-10-05T10:00:00Z", out.getvalue())
                        if command == job.cmd_tail:
                            self.assertIn("persisted command output", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(job.cmd_list(SimpleNamespace(all=True)), 0)
            self.assertEqual(
                out.getvalue().count("local evidence (unauthoritative)"), 2
            )
            self.assertIn("submission_attempted_at", out.getvalue())
            with self.assertRaisesRegex(SystemExit, "cancellation requires the API"):
                job.cmd_kill(SimpleNamespace(id=directory.name))

    def test_forbidden_api_read_never_falls_back_to_local_evidence(self):
        row = self.register()
        with (
            mock.patch.object(job, "SC_API_TOKEN", "other-token"),
            mock.patch.object(job, "job_dir") as local,
        ):
            with self.assertRaisesRegex(SystemExit, "HTTP 403"):
                job.cmd_status(SimpleNamespace(id=str(row["run_id"])))
            local.assert_not_called()

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

    def test_job_client_and_supervisor_do_not_resolve_private_database(self):
        guard = self.root / "restricted-client"
        guard.mkdir()
        activations = guard / "active-pids"
        denied = guard / "denied"
        # Enforce the managed Developer boundary in each fresh interpreter,
        # including the detached supervisor. The API server keeps its own
        # database authority in this test process.
        (guard / "sitecustomize.py").write_text(
            "import os\nfrom pathlib import Path\nimport instance_state\n"
            f"with open({str(activations)!r}, 'a') as f: f.write(str(os.getpid()) + '\\n')\n"
            "def deny_private_resolution(*args, **kwargs):\n"
            f"    Path({str(denied)!r}).touch()\n"
            "    raise PermissionError(13, 'private owner metadata denied')\n"
            "instance_state.active_database_path = deny_private_resolution\n"
        )
        env = {
            **self.env,
            "PYTHONPATH": os.pathsep.join((str(guard), str(ENGINE / "scripts"))),
        }
        result = self.cli(
            "start",
            "--label",
            "restricted-client",
            "--",
            "sh",
            "-c",
            "echo restricted-job-completed; exit 17",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = self.finished(job._api("GET", "/_sc/runs")["runs"][0])
        self.assertEqual((row["state"], row["exit_code"]), ("failed", 17))
        self.assertIsNotNone(row["message_id"])
        self.assertIsNotNone(row["wake_id"])
        for action in ("status", "tail"):
            result = self.cli(action, str(row["run_id"]), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restricted-job-completed", result.stdout)
        self.assertGreaterEqual(len(set(activations.read_text().splitlines())), 4)
        self.assertFalse(denied.exists())

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
        rebuilt = self.root / "rebuilt.db"
        build_db(str(rebuilt))
        con = sqlite3.connect(rebuilt)
        try:
            con.executescript(content)
            restored = runs.RunStore(con, self.engine).get(row["run_id"])
            self.assertEqual(restored, row)
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            con.close()

    def test_migration_repeat_preserves_terminal_wake_and_references(self):
        row = self.terminal(self.register())
        migration = (ENGINE / "migrations/0275_runs_ledger.sql").read_text()
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
        self._codex_consumption(non_run=False)

    def test_codex_non_run_receipt_settles_without_a_local_transcript(self):
        self._codex_consumption(non_run=True)

    def _codex_consumption(self, *, non_run):
        self.add_conversation(state="idle")
        con = self.connect()
        self.addCleanup(con.close)
        if non_run:
            receipt = SprintMessageStore(con).send_to_shell(
                1,
                message_kind="notification",
                body="PR checks passed",
                idempotency_key="non-run-codex",
                declared_type="re-enter",
            )
            mid = receipt.message_id
        else:
            row = self.terminal(self.register())
            mid = row["message_id"]
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
                (
                    receipt := con.execute(
                        "SELECT settled_at FROM engine_wake_receipts WHERE message_id=?",
                        (mid,),
                    ).fetchone()
                )
                and receipt[0]
            )
        )
        if not non_run:
            self.assertEqual(
                job.ledger_run(str(row["run_id"]))["wake_state"], "consumed"
            )
        self.assertTrue(broker.wait_idle(3))
        with mock.patch.object(run_wakes, "_contains_prompt") as read:
            run_wakes.reconcile(con)
            read.assert_not_called()

    def test_non_run_receipt_settles_and_is_never_read_again(self):
        self.add_conversation(state="idle")
        con = self.connect()
        self.addCleanup(con.close)
        receipt = SprintMessageStore(con).send_to_shell(
            1,
            message_kind="notification",
            body="PR checks passed",
            idempotency_key="non-run-pr-wake",
            declared_type="re-enter",
        )
        broker = self.start_broker(lambda _: self._adapter())
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once()
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts WHERE message_id=?",
            (receipt.message_id,),
        ).fetchone()[0]
        broker.notify()
        wait_for(lambda: self._message_state(mid) == "completed")
        run_wakes.reconcile(con)
        self.assertIsNotNone(
            con.execute(
                "SELECT settled_at FROM engine_wake_receipts WHERE message_id=?",
                (receipt.message_id,),
            ).fetchone()[0]
        )
        with mock.patch.object(run_wakes, "_contains_prompt") as read:
            for _ in range(5):
                run_wakes.reconcile(con)
            read.assert_not_called()
        self.assertEqual(con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_cancelled_before_dispatch_receipt_settles_without_a_run(self):
        self.add_conversation(state="idle")
        con = self.connect()
        self.addCleanup(con.close)
        SprintMessageStore(con).send_to_shell(
            1,
            message_kind="notification",
            body="cancelled wake",
            idempotency_key="cancelled-before-dispatch",
            declared_type="re-enter",
        )
        sprint_runtime.SprintRuntimeService(self.db_path).pulse_once()
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts"
        ).fetchone()[0]
        con.execute(
            "UPDATE conversation_messages SET state='cancelled',completed_at=datetime('now') WHERE message_id=?",
            (mid,),
        )
        con.commit()
        run_wakes.reconcile(con)
        receipt = con.execute(
            "SELECT settled_at,blocked_reason FROM engine_wake_receipts"
        ).fetchone()
        self.assertIsNotNone(receipt["settled_at"])
        self.assertIn("without transcript", receipt["blocked_reason"])
        self.assertEqual(
            con.execute("SELECT count(*) FROM conversation_runs").fetchone()[0], 0
        )
        with mock.patch.object(run_wakes, "_contains_prompt") as read:
            run_wakes.reconcile(con)
            read.assert_not_called()

    def test_completed_turn_without_evidence_settles_as_blocked(self):
        self.add_conversation(state="idle")
        row = self.terminal(self.register())
        broker = self.start_broker(lambda _: FakeAdapter())
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once()
        con = self.connect()
        self.addCleanup(con.close)
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts"
        ).fetchone()[0]
        broker.notify()
        wait_for(lambda: self._message_state(mid) == "completed")
        run_wakes.reconcile(con)
        self.assertEqual(job.ledger_run(str(row["run_id"]))["wake_state"], "blocked")
        receipt = con.execute("SELECT * FROM engine_wake_receipts").fetchone()
        self.assertIsNotNone(receipt["settled_at"])
        self.assertIn("without transcript", receipt["last_error"])
        with mock.patch.object(run_wakes, "_contains_prompt") as read:
            runtime.pulse_once(startup=True)
            read.assert_not_called()

    def test_bad_receipt_is_isolated_from_healthy_receipt_and_heartbeat(self):
        self.add_conversation(state="idle")
        bad = self.terminal(self.register(registration_key="bad"))
        good = self.terminal(self.register(registration_key="good"))
        broker = self.start_broker(lambda _: self._adapter())
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once()
        con = self.connect()
        self.addCleanup(con.close)
        mids = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts"
        ).fetchall()
        broker.notify()
        wait_for(
            lambda: all(self._message_state(mid[0]) == "completed" for mid in mids)
        )
        contains = run_wakes._contains_prompt

        def invalid_one(path, offset, marker, body):
            if marker == f"wake_message #{bad['message_id']} ":
                raise ValueError("bad transcript evidence " + "x" * 5000)
            return contains(path, offset, marker, body)

        with mock.patch.object(run_wakes, "_contains_prompt", side_effect=invalid_one):
            runtime.pulse_once(startup=True)
        bad_receipt = con.execute(
            "SELECT * FROM engine_wake_receipts WHERE message_id=?",
            (bad["message_id"],),
        ).fetchone()
        self.assertIsNotNone(bad_receipt["settled_at"])
        self.assertLess(len(bad_receipt["last_error"]), 600)
        self.assertEqual(job.ledger_run(str(bad["run_id"]))["wake_state"], "blocked")
        self.assertEqual(job.ledger_run(str(good["run_id"]))["wake_state"], "consumed")
        self.assertEqual(
            con.execute(
                "SELECT count(*) FROM daemon_heartbeats WHERE name='sprint-runtime'"
            ).fetchone()[0],
            1,
        )
        with mock.patch.object(run_wakes, "_contains_prompt") as read:
            runtime.pulse_once()
            read.assert_not_called()

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

    def test_busy_retry_enqueue_outage_retains_retry_and_does_not_halt_pulse(self):
        row, held, _adapter, broker, runtime, con, _mid = self._busy_setup()
        now = datetime.now(timezone.utc)
        run_wakes.reconcile(con, now=now)
        held["busy"] = False
        with mock.patch.object(
            sprint_runtime, "enqueue_conversation_turn", side_effect=OSError("offline")
        ):
            run_wakes.reconcile(con, now=now + timedelta(seconds=16))
        receipt = con.execute("SELECT * FROM engine_wake_receipts").fetchone()
        self.assertIsNone(receipt["settled_at"])
        self.assertIn("enqueue unavailable", receipt["last_error"])
        runtime.pulse_once(startup=True)
        run_wakes.reconcile(con, now=now + timedelta(seconds=317))
        mid = con.execute(
            "SELECT conversation_message_id FROM engine_wake_receipts"
        ).fetchone()[0]
        broker.notify()
        wait_for(lambda: self._message_state(mid) == "completed")
        run_wakes.reconcile(con)
        self.assertEqual(job.ledger_run(str(row["run_id"]))["wake_state"], "consumed")

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


class RunPulseIsolationTest(sprint_fixture.SprintWorkDispatchCase):
    def test_invalid_run_does_not_stop_healthy_run_sprint_wake_or_heartbeat(self):
        self.create_unit(developer=1)
        self.lifecycle.arm(self.sprint_id, 3, conformance_reviewer_shell_id=2)
        store = runs.RunStore(self.con, self.db_path.parent / "engine")
        bad_rows = []
        for key, evidence in (
            ("bad-terminal", {"finished_at": "invalid", "exit_code": 0}),
            ("bad-incarnation", {"supervisor_pid": -1, "boot_id": "fixture"}),
            ("bad-json-shape", []),
            ("healthy", {"finished_at": job._now(), "exit_code": 0}),
        ):
            row = store.register(
                4,
                {
                    "registration_key": key,
                    "label": key,
                    "argv": ["true"],
                    "cwd": str(self.db_path.parent),
                },
            )
            path = Path(row["evidence_path"]).with_name("meta.json")
            path.parent.mkdir(parents=True)
            if isinstance(evidence, dict):
                evidence["run_id"] = row["run_id"]
            path.write_text(json.dumps(evidence))
            if key != "healthy":
                bad_rows.append(row)
            else:
                good = row
        runtime = sprint_runtime.SprintRuntimeService(self.db_path)
        runtime.pulse_once(startup=True)
        for row in bad_rows:
            result = store.get(row["run_id"])
            self.assertEqual(result["wake_state"], "blocked")
            self.assertIn("reconciliation blocked", result["last_error"])
        self.assertEqual(store.get(good["run_id"])["state"], "done")
        self.assertGreater(
            self.con.execute(
                "SELECT count(*) FROM wake_message WHERE sprint_id=? AND delivered_at IS NOT NULL",
                (self.sprint_id,),
            ).fetchone()[0],
            0,
        )
        first = self.con.execute(
            "SELECT beat_at FROM daemon_heartbeats WHERE name='sprint-runtime'"
        ).fetchone()[0]
        runtime.pulse_once()
        second = self.con.execute(
            "SELECT beat_at FROM daemon_heartbeats WHERE name='sprint-runtime'"
        ).fetchone()[0]
        self.assertGreater(second, first)
