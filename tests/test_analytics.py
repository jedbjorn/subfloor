#!/usr/bin/env python3
"""Tests for token & session analytics (doc #11) — parsers, collector, API.

Covers the load-bearing hazards the spec calls out by name:
  • claude usage duplication — the same usage object repeats per content-block
    line, and resume/fork copies lines into new files: dedupe by message.id
    must hold in-file AND cross-file.
  • upsert idempotency — (harness, ref, model) is the natural key, and model
    is NULL on no_usage rows, where SQLite UNIQUE treats NULLs as distinct:
    a re-sweep must refresh in place, never duplicate.
  • codex subset rules — input ⊇ cached_input (fresh = difference), reasoning
    is inside output (informational, never added).
  • attribution — worktree cwd → shell, archive time-windows, ambiguity and
    unknown cwds stay NULL.

Run:
    python3 tests/test_analytics.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import analytics  # noqa: E402
from token_parsers import claude as p_claude  # noqa: E402
from token_parsers import codex as p_codex  # noqa: E402
from token_parsers import kimi as p_kimi  # noqa: E402


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, flavor, api_key) "
        "VALUES (1, 'Admin', 'adm', 'test', 'sp', 1, 0, 1, 0, 'admin', 'tok-a')")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, flavor, api_key) "
        "VALUES (2, 'Dev', 'dev1', 'test', 'sp', 1, 0, 1, 0, 'dev', 'tok-d')")
    con.commit()
    con.close()


def usage_line(mid: str, model: str = "claude-fable-5", cwd: str = "/repo",
               ts: str = "2026-07-19T10:00:00Z", inp: int = 100) -> str:
    return json.dumps({
        "type": "assistant", "cwd": cwd, "timestamp": ts,
        "message": {"id": mid, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": 10,
                              "cache_read_input_tokens": 20,
                              "cache_creation_input_tokens": 5}}})


def user_line(text: str, cwd: str = "/repo", ts: str = "2026-07-19T09:59:00Z") -> str:
    return json.dumps({"type": "user", "cwd": cwd, "timestamp": ts,
                       "message": {"role": "user", "content": text}})


class ClaudeParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "myrepo"
        self.repo.mkdir()
        self.data = self.tmp / "claude-data"
        self.proj = self.data / p_claude._encode(str(self.repo))
        self.proj.mkdir(parents=True)
        self._orig = p_claude.DATA_DIR
        p_claude.DATA_DIR = self.data

    def tearDown(self):
        p_claude.DATA_DIR = self._orig

    def sweep(self, since=lambda ref: None):
        return p_claude.sweep(self.repo, since, lambda m: None)

    def test_in_file_dedupe_and_title(self):
        # the same message.id on 3 content-block lines counts ONCE
        cwd = str(self.repo)
        (self.proj / "a.jsonl").write_text("\n".join(
            [user_line("fix the bug", cwd)] + [usage_line("m1", cwd=cwd)] * 3))
        rows = self.sweep()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)
        self.assertEqual(rows[0]["cache_write_tokens"], 5)
        self.assertEqual(rows[0]["title"], "fix the bug")
        self.assertEqual(rows[0]["provider"], "anthropic")

    def test_cross_file_dedupe(self):
        # fork copies m1's line into b.jsonl; only b's fresh m2 may count there
        cwd = str(self.repo)
        a, b = self.proj / "a.jsonl", self.proj / "b.jsonl"
        a.write_text(usage_line("m1", cwd=cwd))
        b.write_text(usage_line("m1", cwd=cwd) + "\n" + usage_line("m2", cwd=cwd))
        os.utime(a, (1, 1))  # a is older — mtime order decides who owns m1
        by_ref = {r["harness_session_ref"]: r for r in self.sweep()}
        self.assertEqual(by_ref[str(a)]["input_tokens"], 100)
        self.assertEqual(by_ref[str(b)]["input_tokens"], 100)   # m2 only, not m1+m2

    def test_cwd_filter_and_no_usage(self):
        (self.proj / "other.jsonl").write_text(usage_line("mx", cwd="/elsewhere"))
        (self.proj / "empty.jsonl").write_text(user_line("hi", str(self.repo)))
        rows = self.sweep()
        self.assertEqual(len(rows), 1)  # /elsewhere filtered out
        self.assertEqual(rows[0]["status"], "no_usage")
        self.assertIsNone(rows[0]["model"])

    def test_dir_scoped_incremental_skip(self):
        (self.proj / "a.jsonl").write_text(usage_line("m1", cwd=str(self.repo)))
        self.assertEqual(len(self.sweep()), 1)
        far_future = 4102444800.0  # 2100-01-01 — newer than any file mtime
        self.assertEqual(self.sweep(since=lambda ref: far_future), [])


class CodexParserTest(unittest.TestCase):
    def test_subset_normalization(self):
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "r"
        repo.mkdir()
        day = tmp / "sessions/2026/07/19"
        day.mkdir(parents=True)
        f = day / "rollout-x.jsonl"
        lines = [
            json.dumps({"type": "session_meta", "timestamp": "2026-07-19T10:00:00Z",
                        "payload": {"cwd": str(repo), "model_provider": "openai"}}),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.5"}}),
            # cumulative totals — the LAST event wins, never the sum
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 500, "cached_input_tokens": 100,
                                      "output_tokens": 60, "reasoning_output_tokens": 15}}}}),
            json.dumps({"type": "event_msg", "timestamp": "2026-07-19T10:05:00Z",
                        "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                      "output_tokens": 80, "reasoning_output_tokens": 20}}}}),
        ]
        f.write_text("\n".join(lines))
        orig = p_codex.DATA_DIR
        p_codex.DATA_DIR = tmp / "sessions"
        try:
            rows = p_codex.sweep(repo, lambda ref: None, lambda m: None)
        finally:
            p_codex.DATA_DIR = orig
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["input_tokens"], 600)       # 1000 − 400 cached
        self.assertEqual(r["cache_read_tokens"], 400)
        self.assertEqual(r["output_tokens"], 80)       # includes reasoning
        self.assertEqual(r["reasoning_tokens"], 20)    # informational
        self.assertEqual(r["model"], "gpt-5.5")


class KimiParserTest(unittest.TestCase):
    def test_multi_agent_sum_per_model(self):
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "r"
        repo.mkdir()
        sess = tmp / "sessions/wd_x/session_1"
        for agent in ("main", "agent-0"):
            (sess / "agents" / agent).mkdir(parents=True)
        (sess / "state.json").write_text(json.dumps({
            "createdAt": "2026-07-19T10:00:00Z", "updatedAt": "2026-07-19T10:30:00Z",
            "title": "swarm run", "workDir": str(repo)}))
        rec = {"type": "usage.record", "model": "kimi-code/k3",
               "usage": {"inputOther": 10, "output": 5, "inputCacheRead": 7,
                         "inputCacheCreation": 0}}
        (sess / "agents/main/wire.jsonl").write_text("\n".join([
            json.dumps({"type": "llm.request", "provider": "kimi", "model": "k3"}),
            json.dumps(rec)]))
        (sess / "agents/agent-0/wire.jsonl").write_text(json.dumps(rec))
        orig = p_kimi.DATA_DIR
        p_kimi.DATA_DIR = tmp / "sessions"
        try:
            rows = p_kimi.sweep(repo, lambda ref: None, lambda m: None)
        finally:
            p_kimi.DATA_DIR = orig
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["input_tokens"], 20)        # both agents summed
        self.assertEqual(r["cache_read_tokens"], 14)
        self.assertEqual(r["cache_write_tokens"], 0)   # measured zero, not NULL
        self.assertEqual(r["provider"], "kimi")
        self.assertEqual(r["title"], "swarm run")


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self._db, self._root = analytics.DB_PATH, analytics.REPO_ROOT
        analytics.DB_PATH = self.db
        analytics.REPO_ROOT = self.tmp / "repo"
        (analytics.REPO_ROOT / ".sc-worktrees/dev1").mkdir(parents=True)

    def tearDown(self):
        analytics.DB_PATH, analytics.REPO_ROOT = self._db, self._root

    def con(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def test_upsert_null_model_idempotent(self):
        con = self.con()
        r = {"harness": "claude", "harness_session_ref": "/x.jsonl", "model": None,
             "provider": "anthropic", "title": None, "started_at": None,
             "ended_at": None, "input_tokens": None, "output_tokens": None,
             "cache_read_tokens": None, "cache_write_tokens": None,
             "reasoning_tokens": None, "status": "no_usage", "parser_version": "1"}
        self.assertEqual(analytics._upsert(con, r, "2026-07-19T10:00:00Z"), "insert")
        self.assertEqual(analytics._upsert(con, r, "2026-07-19T11:00:00Z"), "update")
        n, cap = con.execute("SELECT COUNT(*), MAX(captured_at) FROM session_token_usage").fetchone()
        self.assertEqual(n, 1)
        self.assertEqual(cap, "2026-07-19T11:00:00Z")

    def test_attribution_windows(self):
        con = self.con()
        wt = str(analytics.REPO_ROOT / ".sc-worktrees/dev1")
        # dev1 booted twice with claude: window 1 [09:00, 11:00), window 2 [11:00, ∞)
        con.execute("INSERT INTO shell_memory_archives (archive_id, shell_id, session_id, "
                    "date, started_at, harness) VALUES (10, 2, '0001', '2026-07-19', "
                    "'2026-07-19T09:00:00Z', 'claude')")
        con.execute("INSERT INTO shell_memory_archives (archive_id, shell_id, session_id, "
                    "date, started_at, harness) VALUES (11, 2, '0002', '2026-07-19', "
                    "'2026-07-19T11:00:00Z', 'claude')")
        base = {"model": "m", "provider": None, "title": None, "ended_at": None,
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": None,
                "cache_write_tokens": None, "reasoning_tokens": None,
                "status": "ok", "parser_version": "1"}
        rows = [
            {**base, "harness": "claude", "harness_session_ref": "s1",
             "started_at": "2026-07-19T09:30:00Z", "cwd": wt},          # → archive 10
            {**base, "harness": "claude", "harness_session_ref": "s2",
             "started_at": "2026-07-19T12:00:00Z", "cwd": wt},          # → archive 11 (open)
            {**base, "harness": "kimi", "harness_session_ref": "s3",
             "started_at": "2026-07-19T09:30:00Z", "cwd": wt},          # harness mismatch → NULL
            {**base, "harness": "claude", "harness_session_ref": "s4",
             "started_at": "2026-07-19T09:30:00Z", "cwd": "/somewhere/else"},  # cwd off-repo → NULL
        ]
        for r in rows:
            analytics._upsert(con, r, "2026-07-19T13:00:00Z")
        con.commit()
        n = analytics._attribute(con, rows, lambda m: None)
        con.commit()
        self.assertEqual(n, 2)
        got = {r["harness_session_ref"]: r["archive_id"] for r in
               con.execute("SELECT harness_session_ref, archive_id FROM session_token_usage")}
        self.assertEqual(got["s1"], 10)
        self.assertEqual(got["s2"], 11)
        self.assertIsNone(got["s3"])
        self.assertIsNone(got["s4"])
        # ended_at backfill: archive 10's end = its attributed rows' max ended_at
        con.execute("UPDATE session_token_usage SET ended_at='2026-07-19T10:30:00Z' "
                    "WHERE harness_session_ref='s1'")
        analytics._backfill_ended(con)
        con.commit()
        self.assertEqual(con.execute("SELECT ended_at FROM shell_memory_archives "
                                     "WHERE archive_id=10").fetchone()[0],
                         "2026-07-19T10:30:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
