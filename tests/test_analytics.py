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
from token_parsers import opencode as p_opencode  # noqa: E402


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

    def sweep(self, since=lambda ref: None, cache=None):
        return p_claude.sweep(self.repo, since, lambda m: None, cache=cache)

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
        self.assertEqual(rows[0]["native_session_id"], "a")

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

    def test_subagent_fold_into_parent(self):
        # subagent transcripts live at <uuid>/subagents/agent-*.jsonl — their
        # spend folds into the top-level session row; title from parent only
        cwd = str(self.repo)
        parent = self.proj / "f497716c.jsonl"
        parent.write_text(user_line("build the feature", cwd) + "\n"
                          + usage_line("m1", cwd=cwd))
        sub = self.proj / "f497716c/subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text("\n".join([
            user_line("You are an implementer agent…", cwd),
            usage_line("m2", cwd=cwd),
            usage_line("m1", cwd=cwd),  # copy of the parent's id — dedupes
        ]))
        os.utime(parent, (1, 1))  # parent older: it owns m1
        rows = self.sweep()
        self.assertEqual(len(rows), 1)               # ONE row, not two
        r = rows[0]
        self.assertEqual(r["harness_session_ref"], str(parent))
        self.assertEqual(r["input_tokens"], 200)     # m1 + m2, m1 counted once
        self.assertEqual(r["title"], "build the feature")  # never the subagent prompt

    # ── parse cache (CC-145): grown files parse only their tail delta,
    #    unchanged files replay from cache without touching disk ──

    def test_cache_tail_append_never_rereads_head(self):
        cwd = str(self.repo)
        f = self.proj / "a.jsonl"
        f.write_text(usage_line("m1", cwd=cwd) + "\n")
        cache = {}
        self.assertEqual(self.sweep(cache=cache)[0]["input_tokens"], 100)
        # corrupt the head IN PLACE (same length): a full re-parse would lose
        # m1 and flag the garbage as a bad line — a tail parse sees neither
        size = f.stat().st_size
        with open(f, "r+b") as fh:
            fh.write(b"X" * (size - 1))
        with open(f, "ab") as fh:
            fh.write(usage_line("m2", cwd=cwd).encode() + b"\n")
        r = self.sweep(cache=cache)[0]
        self.assertEqual(r["input_tokens"], 200)   # m1 (cached) + m2 (tail)
        self.assertEqual(r["status"], "ok")        # head garbage never read

    def test_cache_unchanged_file_replays_without_read(self):
        cwd = str(self.repo)
        f = self.proj / "a.jsonl"
        f.write_text(usage_line("m1", cwd=cwd) + "\n")
        cache = {}
        self.sweep(cache=cache)
        st = f.stat()
        f.write_bytes(b"#" * st.st_size)                 # junk, same size…
        os.utime(f, (st.st_atime, st.st_mtime))          # …same mtime
        r = self.sweep(cache=cache)[0]                   # dir re-aggregates (since=None)
        self.assertEqual(r["input_tokens"], 100)         # state came from cache
        self.assertEqual(r["status"], "ok")

    def test_cache_shrunk_file_reparses_in_full(self):
        cwd = str(self.repo)
        f = self.proj / "a.jsonl"
        f.write_text(usage_line("m1", cwd=cwd) + "\n" + usage_line("m2", cwd=cwd) + "\n")
        cache = {}
        self.assertEqual(self.sweep(cache=cache)[0]["input_tokens"], 200)
        f.write_text(usage_line("m3", cwd=cwd, inp=70) + "\n")   # rewrite, smaller
        self.assertEqual(self.sweep(cache=cache)[0]["input_tokens"], 70)

    def test_cache_partial_tail_left_for_next_sweep(self):
        cwd = str(self.repo)
        f = self.proj / "a.jsonl"
        m2 = usage_line("m2", cwd=cwd, inp=50)
        f.write_text(usage_line("m1", cwd=cwd) + "\n" + m2[:20])  # live mid-append
        cache = {}
        r = self.sweep(cache=cache)[0]
        self.assertEqual(r["input_tokens"], 100)   # partial tail not counted…
        self.assertEqual(r["status"], "ok")        # …and not a "bad line" either
        with open(f, "ab") as fh:
            fh.write(m2[20:].encode() + b"\n")     # the append completes it
        self.assertEqual(self.sweep(cache=cache)[0]["input_tokens"], 150)

    def test_cache_identity_with_and_without(self):
        # the cache is an accelerator, never a source of truth: cached and
        # cacheless sweeps must return identical rows
        cwd = str(self.repo)
        a, b = self.proj / "a.jsonl", self.proj / "b.jsonl"
        a.write_text(usage_line("m1", cwd=cwd))
        b.write_text(usage_line("m1", cwd=cwd) + "\n" + usage_line("m2", cwd=cwd))
        os.utime(a, (1, 1))
        cache = {}
        self.sweep(cache=cache)                    # warm
        self.assertEqual(self.sweep(cache=cache), self.sweep(cache=None))


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
                        "payload": {"id": "thread-x", "cwd": str(repo),
                                    "model_provider": "openai"}}),
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
        self.assertEqual(r["native_session_id"], "thread-x")

    def test_off_repo_bails_on_meta_line(self):
        # off-repo rollouts never earn a watermark row, so without the bail
        # the since gate re-admits them every sweep — the parse must stop at
        # the session_meta line, not read the (potentially huge) body
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "r"
        repo.mkdir()
        day = tmp / "sessions/2026/07/19"
        day.mkdir(parents=True)
        f = day / "rollout-y.jsonl"
        f.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"cwd": "/elsewhere"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 9, "output_tokens": 9}}}}),
        ]))
        self.assertEqual(p_codex._parse_file(f, lambda m: None, repo),
                         {"off_repo": True})
        orig = p_codex.DATA_DIR
        p_codex.DATA_DIR = tmp / "sessions"
        try:
            self.assertEqual(p_codex.sweep(repo, lambda ref: None, lambda m: None), [])
        finally:
            p_codex.DATA_DIR = orig


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
            "id": "session-1", "title": "swarm run", "workDir": str(repo)}))
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
        self.assertEqual(r["native_session_id"], "session-1")


class OpencodeParserTest(unittest.TestCase):
    def test_reasoning_folds_into_output(self):
        # opencode reports reasoning DISJOINT from output; the row contract is
        # codex-shaped (reasoning ⊆ output), so the parser folds it in
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "r"
        repo.mkdir()
        db = tmp / "opencode.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, "
                    "model TEXT, time_created INTEGER, time_updated INTEGER, "
                    "tokens_input INTEGER NOT NULL DEFAULT 0, "
                    "tokens_output INTEGER NOT NULL DEFAULT 0, "
                    "tokens_reasoning INTEGER NOT NULL DEFAULT 0, "
                    "tokens_cache_read INTEGER NOT NULL DEFAULT 0, "
                    "tokens_cache_write INTEGER NOT NULL DEFAULT 0)")
        con.execute("INSERT INTO session VALUES ('ses_1', ?, 't', "
                    "'{\"id\": \"gpt-5.5\", \"providerID\": \"openai\"}', "
                    "1781566825695, 1781566825695, 100, 80, 20, 500, 0)",
                    (str(repo),))
        con.commit()
        con.close()
        orig = p_opencode.DB
        p_opencode.DB = db
        try:
            rows = p_opencode.sweep(repo, lambda ref: None, lambda m: None)
        finally:
            p_opencode.DB = orig
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["output_tokens"], 100)      # 80 output + 20 reasoning
        self.assertEqual(r["reasoning_tokens"], 20)    # split kept informationally
        self.assertEqual(r["input_tokens"], 100)
        self.assertEqual(r["provider"], "openai")


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

    def test_since_fn_version_pinned(self):
        # rows written by an OLDER parser version don't gate the skip — a
        # version bump must force the full re-parse
        con = self.con()
        r = {"harness": "claude", "harness_session_ref": "/x.jsonl", "model": "m",
             "provider": None, "title": None, "started_at": None, "ended_at": None,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": None,
             "cache_write_tokens": None, "reasoning_tokens": None,
             "status": "ok", "parser_version": "1"}
        analytics._upsert(con, r, "2026-07-19T10:00:00Z")
        con.commit()
        self.assertIsNotNone(analytics._since_fn(con, "claude", "1")("/x.jsonl"))
        self.assertIsNone(analytics._since_fn(con, "claude", "2")("/x.jsonl"))
        self.assertIsNone(analytics._since_fn(con, "kimi", "1")("/x.jsonl"))

    def test_parse_cache_roundtrip_and_version_pin(self):
        # migration 0073: the payload persists per harness, pinned to the
        # parser version — a bump discards it (forcing the full re-parse)
        con = self.con()
        analytics._save_cache(con, "claude", "2", {"files": {"/a.jsonl": {"size": 1}}})
        self.assertEqual(analytics._load_cache(con, "claude", "2"),
                         {"files": {"/a.jsonl": {"size": 1}}})
        self.assertEqual(analytics._load_cache(con, "claude", "3"), {})  # version pin
        self.assertEqual(analytics._load_cache(con, "kimi", "1"), {})    # absent
        analytics._save_cache(con, "claude", "3", {"x": 1})              # upsert in place
        self.assertEqual(analytics._load_cache(con, "claude", "3"), {"x": 1})
        n = con.execute("SELECT COUNT(*) FROM analytics_parse_cache").fetchone()[0]
        self.assertEqual(n, 1)
        analytics._save_cache(con, "claude", "3", {})                    # empty: no write
        self.assertEqual(analytics._load_cache(con, "claude", "3"), {"x": 1})

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
        n, shell_only = analytics._attribute(con, rows, lambda m: None)
        con.commit()
        self.assertEqual(n, 2)
        self.assertEqual(shell_only, 1)  # s3: no kimi window, but cwd → dev1
        got = {r["harness_session_ref"]: (r["archive_id"], r["shell_id"]) for r in
               con.execute("SELECT harness_session_ref, archive_id, shell_id "
                           "FROM session_token_usage")}
        self.assertEqual(got["s1"], (10, 2))
        self.assertEqual(got["s2"], (11, 2))
        self.assertEqual(got["s3"], (None, 2))   # shell-only: flavor rollups see it
        self.assertEqual(got["s4"], (None, None))  # off-repo → nothing, not admin
        # ended_at backfill: archive 10's end = its attributed rows' max ended_at
        con.execute("UPDATE session_token_usage SET ended_at='2026-07-19T10:30:00Z' "
                    "WHERE harness_session_ref='s1'")
        analytics._backfill_ended(con)
        con.commit()
        self.assertEqual(con.execute("SELECT ended_at FROM shell_memory_archives "
                                     "WHERE archive_id=10").fetchone()[0],
                         "2026-07-19T10:30:00Z")

    def test_native_binding_attribution_precedes_fallback_without_duplicate_rows(self):
        con = self.con()
        con.execute(
            "INSERT INTO shell_memory_archives "
            "(archive_id, shell_id, session_id, date, started_at, harness) "
            "VALUES (30, 2, '0009', '2026-07-19', '2026-07-19T11:00:00Z', 'codex')"
        )
        con.execute(
            "INSERT INTO shell_session_bindings "
            "(binding_id, archive_id, shell_id, harness, native_session_id, "
            "state, managed) VALUES (40, 30, 2, 'codex', 'thread-exact', "
            "'dormant', 1)"
        )
        base = {
            "harness": "codex", "harness_session_ref": "/rollout.jsonl",
            "native_session_id": "thread-exact", "provider": "openai",
            "title": None, "started_at": "2020-01-01T00:00:00Z",
            "ended_at": None, "input_tokens": 2, "output_tokens": 3,
            "cache_read_tokens": None, "cache_write_tokens": None,
            "reasoning_tokens": None, "status": "ok", "parser_version": "1",
            "cwd": "/outside/the/repo",
        }
        batch = [{**base, "model": "m1"}, {**base, "model": "m2"}]
        for row in batch:
            analytics._upsert(con, row, "2026-07-19T13:00:00Z")
        con.commit()

        attributed, shell_only = analytics._attribute(con, batch, lambda _m: None)
        con.commit()

        self.assertEqual((2, 0), (attributed, shell_only))
        got = [tuple(row) for row in con.execute(
            "SELECT model, archive_id, shell_id, input_tokens, output_tokens "
            "FROM session_token_usage ORDER BY model"
        )]
        self.assertEqual([
            ("m1", 30, 2, 2, 3), ("m2", 30, 2, 2, 3),
        ], got)
        self.assertEqual(2, con.execute(
            "SELECT COUNT(*) FROM session_token_usage"
        ).fetchone()[0])

    def test_shell_only_attribution(self):
        """Rows predating lifecycle archives still get shell_id from cwd alone;
        a later window match upgrades them to full attribution; ambiguous
        root-cwd (two admin shells) stays NULL."""
        con = self.con()
        wt = str(analytics.REPO_ROOT / ".sc-worktrees/dev1")
        root = str(analytics.REPO_ROOT)
        base = {"model": "m", "provider": None, "title": None, "ended_at": None,
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": None,
                "cache_write_tokens": None, "reasoning_tokens": None,
                "status": "ok", "parser_version": "1"}
        rows = [
            {**base, "harness": "claude", "harness_session_ref": "h1",
             "started_at": "2026-06-01T09:00:00Z", "cwd": wt},    # worktree → dev1
            {**base, "harness": "claude", "harness_session_ref": "h2",
             "started_at": "2026-06-01T09:00:00Z", "cwd": root},  # root → sole admin
            {**base, "harness": "claude", "harness_session_ref": "h3",
             "started_at": None, "cwd": wt},                      # no timestamp: still ok
        ]
        for r in rows:
            analytics._upsert(con, r, "2026-07-19T13:00:00Z")
        con.commit()
        n, shell_only = analytics._attribute(con, rows, lambda m: None)
        con.commit()
        self.assertEqual((n, shell_only), (0, 3))
        got = {r["harness_session_ref"]: (r["archive_id"], r["shell_id"]) for r in
               con.execute("SELECT harness_session_ref, archive_id, shell_id "
                           "FROM session_token_usage")}
        self.assertEqual(got["h1"], (None, 2))
        self.assertEqual(got["h2"], (None, 1))
        self.assertEqual(got["h3"], (None, 2))
        # a lifecycle archive appearing later upgrades the shell-only row
        con.execute("INSERT INTO shell_memory_archives (archive_id, shell_id, "
                    "session_id, date, started_at, harness) VALUES (20, 2, '0001', "
                    "'2026-06-01', '2026-06-01T08:00:00Z', 'claude')")
        con.commit()
        n, shell_only = analytics._attribute(con, [rows[0]], lambda m: None)
        con.commit()
        self.assertEqual((n, shell_only), (1, 0))
        got = con.execute("SELECT archive_id, shell_id FROM session_token_usage "
                          "WHERE harness_session_ref='h1'").fetchone()
        self.assertEqual((got[0], got[1]), (20, 2))
        # two admin shells → root cwd is ambiguous, second sweep leaves h2 as-is
        con.execute("INSERT INTO shells (shell_id, display_name, shortname, mandate, "
                    "system_prompt, user_id, is_shared, has_identity, bootstrapped, "
                    "flavor, api_key) VALUES (3, 'Admin2', 'adm2', 't', 'sp', 1, 0, "
                    "1, 0, 'admin', 'tok-b')")
        con.commit()
        rows[1]["harness_session_ref"] = "h4"
        analytics._upsert(con, rows[1], "2026-07-19T13:00:00Z")
        con.commit()
        n, shell_only = analytics._attribute(con, [rows[1]], lambda m: None)
        con.commit()
        self.assertEqual((n, shell_only), (0, 0))
        self.assertIsNone(con.execute(
            "SELECT shell_id FROM session_token_usage "
            "WHERE harness_session_ref='h4'").fetchone()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
