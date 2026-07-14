#!/usr/bin/env python3
"""Tests for sc job (specs_sc/job-runner.md): the job lifecycle (start →
supervise → meta), wait's exit-code contract, --timeout group kill, kill's
flag surviving the supervisor's final write, and the completion result row
landing in the starting shell's own inbox through the real API.

Stdlib `unittest`, matching the sibling suites. The supervisor is exercised
synchronously (supervise(jobdir) is a plain function; `notify` is injectable),
so no test depends on detached-process timing. API tests stand up the real
server.Handler on an ephemeral port (the test_mem harness pattern).

Run:
    python3 tests/test_job.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import job  # noqa: E402
import server  # noqa: E402

TOKEN = "test-token-jobrunner"


def build_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
        "VALUES (1, 'Dev One', 'dev1', 'x', 1, ?)", (TOKEN,))
    con.commit()
    con.close()


def start_job(jobs_dir: Path, cmd: list, timeout=None, label=None) -> Path:
    """Author a job dir the way cmd_start does, minus the detach — the
    supervisor is then driven synchronously."""
    job_id = f"1-{label}" if label else "1"
    jobdir = jobs_dir / job_id
    jobdir.mkdir(parents=True)
    (jobdir / "log").touch()
    job.write_meta(jobdir, {
        "job_id": job_id, "label": label, "cmd": cmd, "cwd": str(jobs_dir),
        "timeout": timeout, "started_at": job._now(),
        "log": str(jobdir / "log"),
    })
    return jobdir


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent: list = []
        self.notify = lambda meta: self.sent.append(meta) or True

    def test_success_lifecycle(self):
        jd = start_job(self.tmp, ["sh", "-c", "echo out; echo err >&2; exit 0"])
        rc = job.supervise(jd, notify=self.notify)
        self.assertEqual(0, rc)
        meta = job.read_meta(jd)
        self.assertEqual("done", job.state_of(meta))
        self.assertEqual(0, meta["exit_code"])
        self.assertIsNotNone(meta["finished_at"])
        log = (jd / "log").read_text()
        self.assertIn("out", log)
        self.assertIn("err", log)          # stderr folds into the one log
        self.assertEqual(1, len(self.sent))

    def test_failure_exit_code(self):
        jd = start_job(self.tmp, ["sh", "-c", "exit 3"])
        rc = job.supervise(jd, notify=self.notify)
        self.assertEqual(3, rc)
        self.assertEqual("failed", job.state_of(job.read_meta(jd)))

    def test_spawn_error_is_a_recorded_failure(self):
        jd = start_job(self.tmp, ["/nonexistent/binary"])
        rc = job.supervise(jd, notify=self.notify)
        self.assertEqual(127, rc)
        meta = job.read_meta(jd)
        self.assertEqual("failed", job.state_of(meta))
        self.assertIn("spawn_error", meta)
        self.assertEqual(1, len(self.sent))   # even a spawn failure wakes the shell

    def test_timeout_kills_the_group(self):
        old_grace = job.KILL_GRACE
        job.KILL_GRACE = 1
        try:
            jd = start_job(self.tmp, ["sh", "-c", "sleep 60"], timeout=1)
            t0 = time.monotonic()
            job.supervise(jd, notify=self.notify)
            self.assertLess(time.monotonic() - t0, 30)
            meta = job.read_meta(jd)
            self.assertEqual("timeout", job.state_of(meta))
            self.assertTrue(meta["timed_out"])
        finally:
            job.KILL_GRACE = old_grace

    def test_killed_flag_survives_final_write(self):
        # kill stamps killed=True on disk while the supervisor holds a stale
        # copy — the supervisor's final write must not clobber it.
        jd = start_job(self.tmp, ["sh", "-c", "exit 0"])
        real_read = job.read_meta
        stamped = {"done": False}

        def stamping_read(jobdir):
            meta = real_read(jobdir)
            if meta.get("pid") and not stamped["done"] and not meta.get("finished_at"):
                stamped["done"] = True
                disk = real_read(jobdir)
                disk["killed"] = True
                job.write_meta(jobdir, disk)
                meta = real_read(jobdir)
            return meta

        job.read_meta = stamping_read
        try:
            job.supervise(jd, notify=self.notify)
        finally:
            job.read_meta = real_read
        meta = job.read_meta(jd)
        self.assertTrue(meta.get("killed"))
        self.assertEqual("killed", job.state_of(meta))

    def test_completion_body_shape(self):
        jd = start_job(self.tmp, ["sh", "-c", "exit 0"], label="suite")
        job.supervise(jd, notify=self.notify)
        body = job.completion_body(job.read_meta(jd))
        self.assertIn("1-suite", body)
        self.assertIn("done", body)
        self.assertIn("exit=0", body)
        self.assertIn("sc job status", body)


class StateTest(unittest.TestCase):
    def test_lost_when_supervisor_dead_and_unfinished(self):
        meta = {"supervisor_pid": 2**22 + 1234567, "job_id": "1"}  # not a real pid
        self.assertEqual("lost", job.state_of(meta))

    def test_running_when_supervisor_alive(self):
        import os
        meta = {"supervisor_pid": os.getpid(), "job_id": "1"}
        self.assertEqual("running", job.state_of(meta))

    def test_next_job_id_monotonic(self):
        tmp = Path(tempfile.mkdtemp())
        old = job.JOBS
        job.JOBS = tmp
        try:
            self.assertEqual("1", job.next_job_id(None))
            (tmp / "1").mkdir()
            (tmp / "2-bench").mkdir()
            self.assertEqual("3-suite", job.next_job_id("suite"))
        finally:
            job.JOBS = old


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_jobs, self.old_poll = job.JOBS, job.POLL
        job.JOBS, job.POLL = self.tmp, 0.05

    def tearDown(self):
        job.JOBS, job.POLL = self.old_jobs, self.old_poll

    def _args(self, id, for_seconds=1):
        import argparse
        return argparse.Namespace(id=id, for_seconds=for_seconds)

    def test_wait_finished_is_zero(self):
        jd = start_job(self.tmp, ["sh", "-c", "exit 0"])
        job.supervise(jd, notify=lambda m: True)
        self.assertEqual(0, job.cmd_wait(self._args(jd.name)))

    def test_wait_slice_expiry_is_two(self):
        import os
        jd = start_job(self.tmp, ["sh", "-c", "sleep 60"])
        meta = job.read_meta(jd)
        meta["supervisor_pid"] = os.getpid()   # "supervisor" alive = running
        job.write_meta(jd, meta)
        self.assertEqual(2, job.cmd_wait(self._args(jd.name, for_seconds=1)))

    def test_wait_lost_is_one(self):
        jd = start_job(self.tmp, ["sh", "-c", "exit 0"])
        meta = job.read_meta(jd)
        meta["supervisor_pid"] = 2**22 + 1234567
        job.write_meta(jd, meta)
        self.assertEqual(1, job.cmd_wait(self._args(jd.name)))


class CompletionRowTest(unittest.TestCase):
    """send_completion against the real API server: the result row lands in
    the starting shell's own inbox, and the dedupe_key makes a resend a no-op."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.db = str(Path(cls.tmpdir) / "shell_db.db")
        build_db(cls.db)
        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        job.SC_API_BASE = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        job.SC_API_TOKEN = TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_completion_row_lands_and_dedupes(self):
        meta = {"job_id": "9-suite", "label": "suite", "cmd": ["pytest"],
                "started_at": "2026-07-14T10:00:00Z",
                "finished_at": "2026-07-14T10:05:00Z",
                "exit_code": 0, "log": "/tmp/x/log"}
        self.assertTrue(job.send_completion(meta, retries=1, delay=0))
        self.assertTrue(job.send_completion(meta, retries=1, delay=0))  # retry
        rows = self.q(
            "SELECT from_shell_id, to_shell_id, kind, body FROM shell_messages "
            "WHERE dedupe_key='job-9-suite-completion'")
        self.assertEqual(1, len(rows))          # deduped, not twinned
        frm, to, kind, body = rows[0]
        self.assertEqual((1, 1, "result"), (frm, to, kind))  # own inbox
        self.assertIn("9-suite", body)
        self.assertIn("done", body)

    def test_no_api_env_gives_up_quietly(self):
        old_base, old_tok = job.SC_API_BASE, job.SC_API_TOKEN
        job.SC_API_BASE, job.SC_API_TOKEN = "", ""
        try:
            self.assertFalse(job.send_completion({"job_id": "x"}, retries=1, delay=0))
        finally:
            job.SC_API_BASE, job.SC_API_TOKEN = old_base, old_tok


if __name__ == "__main__":
    unittest.main()
