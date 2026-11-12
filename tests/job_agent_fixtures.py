"""Shared fixture for the hermetic `sc job agent` and `sc job watch` tests.

A real API over a temporary ledger, a real git repository for the subagent
worktrees, the real `job.py` verbs and detached supervisor, and stand-in
harness and `gh` binaries on PATH (tests/fixtures/job_agent/stub_*.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job
import runs
from test_job import build_db
from test_runs import ApiFixture, wait_for

STUBS = Path(__file__).resolve().parent / "fixtures" / "job_agent"
HARNESSES = ("claude", "codex", "kimi", "opencode")


class DelegationCase(ApiFixture):
    """Mix into a unittest.TestCase."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.db_path = Path(self.temp.name) / "test.db"
        build_db(str(self.db_path))
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.git("init", "-q", "-b", "main")
        (self.root / ".gitignore").write_text("engine/\n.sc-worktrees/\n")
        (self.root / "tracked.txt").write_text("one\n")
        self.git("add", ".gitignore", "tracked.txt")
        self.git("-c", "user.name=Test", "-c", "user.email=test@subfloor.local",
                 "commit", "-q", "-m", "first")
        self.base = self.git("rev-parse", "HEAD")
        self.start_api()
        self.store = runs.RunStore(self.con, self.engine)

        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        for name in HARNESSES:
            self._stub(bin_dir / name, STUBS / "stub_harness.py", name)
        self._stub(bin_dir / "gh", STUBS / "stub_gh.py")
        self.record = Path(self.temp.name) / "harness-starts.jsonl"
        self.gh_calls = Path(self.temp.name) / "gh-calls.jsonl"
        self.gh_map = Path(self.temp.name) / "gh-map.json"
        self.gh_map.write_text("{}")
        self.env = {
            **{k: v for k, v in self.env.items()
               if k not in ("SC_JOB_AGENT_RUN", "SC_SEAT", "SC_DISABLED_HARNESSES")},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_RECORD": str(self.record),
            "GH_STUB_MAP": str(self.gh_map),
            "GH_STUB_CALLS": str(self.gh_calls),
            "SC_SEAT": "gui",
        }
        self.addCleanup(self.stop_everything)

    @staticmethod
    def _stub(path: Path, script: Path, *leading: str) -> None:
        words = " ".join(f'"{word}"' for word in (sys.executable, str(script), *leading))
        path.write_text(f'#!/bin/sh\nexec {words} "$@"\n')
        path.chmod(0o755)

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(
            ["git", "-C", str(cwd or self.root), *args],
            capture_output=True, text=True, check=True).stdout.strip()

    def stop_everything(self) -> None:
        """No supervisor or stand-in harness outlives its test."""
        for row in self.con.execute(
                "SELECT pid,supervisor_pid FROM runs WHERE state IN ('registered','running')"):
            for pid in (row["supervisor_pid"], row["pid"]):
                if pid:
                    try:
                        os.kill(pid, 9)
                    except (ProcessLookupError, PermissionError):
                        pass

    # ── verbs ────────────────────────────────────────────────────────────────

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(ENGINE / "scripts/job.py"), *args],
            cwd=self.root, env=env or self.env,
            capture_output=True, text=True, timeout=90, check=False)

    def launch(self, brief: str = "RESULT:done", *args, harness: str = "claude",
               model: str = "model-x") -> dict:
        before = self.newest_id()
        done = self.cli("agent", "--harness", harness, "--model", model,
                        "--brief", brief, *args)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        row = job._api("GET", "/_sc/runs")["runs"][0]
        self.assertGreater(row["run_id"], before)
        self.assertIn(f"job: {row['run_id']} started", done.stdout)
        return row

    def continue_run(self, row: dict, brief: str = "RESULT:again", *args) -> dict:
        done = self.cli("continue", str(row["run_id"]), "--brief", brief, *args)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        return job._api("GET", "/_sc/runs")["runs"][0]

    def newest_id(self) -> int:
        return self.con.execute("SELECT COALESCE(MAX(run_id),0) FROM runs").fetchone()[0]

    def api_error(self, method, path, body=None) -> tuple[int, str]:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            job._api(method, path, body)
        return caught.exception.code, json.loads(caught.exception.read())["error"]

    # ── evidence ─────────────────────────────────────────────────────────────

    def starts(self) -> list[dict]:
        """What each stand-in harness process was started with, in order."""
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def wait_started(self, count: int = 1) -> list[dict]:
        return wait_for(lambda: self.starts() if len(self.starts()) >= count else None)

    def wake_bodies(self, row: dict) -> list[str]:
        return [found["body"] for found in self.con.execute(
            "SELECT body FROM shell_messages WHERE dedupe_key=?",
            (f"run-{row['run_id']}-terminal",))]

    def wake(self, row: dict) -> str:
        bodies = self.wake_bodies(row)
        self.assertEqual(len(bodies), 1, bodies)
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM wake_message WHERE message_id=?",
            (row["message_id"],)).fetchone()[0], 1)
        return bodies[0]

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            return False
        return fields[0] != "Z"

    def assert_gone(self, *pids: int) -> None:
        wait_for(lambda: not any(self.alive(pid) for pid in pids))
