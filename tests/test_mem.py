#!/usr/bin/env python3
"""Tests for `sc mem` (scripts/mem.py) — the API-only memory surface.

mem.py is a thin HTTP client: every command goes through the engine API
(`/_sc/mem/*`), there is no direct-DB path, and identity comes from the bearer
token (the server resolves token → shell_id). So these are integration tests —
they stand up the real `server.Handler` on an ephemeral port against a throwaway
engine DB, point the client at it, drive `mem.main(argv)` end to end, and assert
the server's effects on the DB. The token is the only identity the client sends.

Run:
    python3 tests/test_mem.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import mem  # noqa: E402
import server  # noqa: E402

TOKEN = "test-token-deadbeef"


def build_engine_db(path: Path) -> None:
    """A throwaway file DB shaped like the shipped engine (schema + every
    migration), with one keyed shell that owns an active session archive."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (1, 'TC', 'tc', 'test', 'sp', 1, 0, 1, 0, ?)", (TOKEN,))
    con.execute(
        "INSERT INTO shell_memory_archives (archive_id, shell_id, session_id, date) "
        "VALUES (1, 1, '0001', '2026-01-01')")
    con.execute("UPDATE shells SET active_archive_id=1 WHERE shell_id=1")
    con.commit()
    con.close()


class ApiMemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        build_engine_db(cls.db)
        server.DB_PATH = cls.db  # db() reads the module global at call time
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # mem reads these into module globals at import — set them directly.
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.port}"
        mem.SC_API_TOKEN = TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()

    def run_mem(self, *argv) -> int:
        return mem.main(list(argv))

    # ── identity comes from the token, not an argument ────────────────────────
    def test_whoami_resolves_token_to_shell(self):
        self.assertEqual(self.run_mem("which"), 0)

    def test_write_lands_on_the_token_shell(self):
        self.run_mem("state", "hello state")
        self.assertEqual(self.q("SELECT current_state FROM shells WHERE shell_id=1")[0],
                         "hello state")

    # ── fail-loud: no API wiring → SystemExit, never a direct-DB write ────────
    def test_no_token_dies(self):
        saved = mem.SC_API_TOKEN
        mem.SC_API_TOKEN = ""
        try:
            with self.assertRaises(SystemExit):
                self.run_mem("state", "should not write")
        finally:
            mem.SC_API_TOKEN = saved

    # ── identity entries + retire ─────────────────────────────────────────────
    def test_seed_then_retire(self):
        self.run_mem("seed", "a seed", "--tag", "cc")
        row = self.q("SELECT entry_id, retired_at FROM shell_identity_entries "
                     "WHERE kind='seed' AND body='a seed'")
        self.assertIsNotNone(row)
        self.assertIsNone(row["retired_at"])
        self.run_mem("retire", str(row["entry_id"]))
        self.assertIsNotNone(
            self.q("SELECT retired_at FROM shell_identity_entries WHERE entry_id=?",
                   row["entry_id"])["retired_at"])

    def test_decision(self):
        self.run_mem("decision", "a call", "--rationale", "why")
        self.assertEqual(self.q("SELECT decision FROM shell_decisions "
                                "WHERE rationale='why'")[0], "a call")

    # ── flags ─────────────────────────────────────────────────────────────────
    def test_flag_open_then_close(self):
        self.run_mem("flag", "open", "[x] blocked | Blocker for: y", "--name", "SC-1")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-1'")[0]
        self.run_mem("flag", "close", str(fid), "--notes", "fixed")
        self.assertEqual(self.q("SELECT resolved FROM flags WHERE flag_id=?", fid)[0], 1)

    # ── roadmap: add / status / work-stream / deps + cycle ────────────────────
    def test_roadmap_lifecycle_and_cycle(self):
        self.run_mem("project", "add", "ws1", "Work Stream 1")
        self.run_mem("roadmap", "add", "feat A", "--status", "next", "--project", "ws1")
        a = self.q("SELECT feature_id, project_id FROM roadmap WHERE title='feat A'")
        self.assertIsNotNone(a["project_id"])  # work-stream assigned on add
        self.run_mem("roadmap", "add", "feat B")
        b = self.q("SELECT feature_id FROM roadmap WHERE title='feat B'")[0]
        self.run_mem("roadmap", "status", str(a["feature_id"]), "shipped")
        self.assertEqual(self.q("SELECT roadmap_status FROM roadmap WHERE feature_id=?",
                                a["feature_id"])[0], "shipped")
        # A depends on B
        self.run_mem("roadmap", "depends", str(a["feature_id"]), "--on", str(b))
        self.assertIsNotNone(self.q("SELECT 1 FROM feature_blockers WHERE feature_id=? "
                                    "AND blocked_by=?", a["feature_id"], b))
        # B depends on A would close a cycle → server refuses, client dies
        with self.assertRaises(SystemExit):
            self.run_mem("roadmap", "depends", str(b), "--on", str(a["feature_id"]))
        self.assertIsNone(self.q("SELECT 1 FROM feature_blockers WHERE feature_id=? "
                                 "AND blocked_by=?", b, a["feature_id"]))

    # ── projects ──────────────────────────────────────────────────────────────
    def test_project_add_standing_status(self):
        self.run_mem("project", "add", "ws2", "Work Stream 2", "--purpose", "p")
        self.assertIsNotNone(self.q("SELECT 1 FROM project_shells ps JOIN projects p "
                                    "ON p.project_id=ps.project_id WHERE p.shortname='ws2' "
                                    "AND ps.shell_id=1"))
        self.run_mem("project", "standing", "ws2", "the standing")
        self.assertEqual(self.q("SELECT standing FROM projects WHERE shortname='ws2'")[0],
                         "the standing")
        self.run_mem("project", "status", "ws2", "paused")
        self.assertEqual(self.q("SELECT status FROM projects WHERE shortname='ws2'")[0],
                         "paused")

    # ── messaging: send by shortname (recipient ≠ identity) ───────────────────
    def test_message_send_by_shortname(self):
        self.run_mem("message", "send", "tc", "ping")
        row = self.q("SELECT from_shell_id, to_shell_id, body FROM shell_messages "
                     "WHERE body='ping'")
        self.assertEqual((row["from_shell_id"], row["to_shell_id"]), (1, 1))

    # ── docs + tasks ──────────────────────────────────────────────────────────
    def test_doc_and_task(self):
        self.run_mem("roadmap", "add", "feat C")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat C'")[0]
        body = self.tmp / "d.md"
        body.write_text("# doc\nbody\n")
        self.run_mem("doc", "add", "spec C", "--body-file", str(body), "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec C'")[0]
        self.run_mem("doc", "edit", str(did), "--title", "spec C v2")
        self.assertEqual(self.q("SELECT title FROM documents WHERE document_id=?", did)[0],
                         "spec C v2")
        self.run_mem("doc", "freeze", str(did))
        self.assertEqual(self.q("SELECT frozen FROM documents WHERE document_id=?", did)[0], 1)
        self.run_mem("task", "add", "task C", "--feature", str(fid), "--doc", str(did), "--seq", "1")
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='task C'")[0]
        self.run_mem("task", "done", str(tid))
        self.assertEqual(self.q("SELECT status FROM spec_tasks WHERE task_id=?", tid)[0], "done")

    # ── narrative + oriented ──────────────────────────────────────────────────
    def test_narrative_and_oriented(self):
        self.run_mem("narrative", "a beat")
        self.assertIn("a beat",
                      self.q("SELECT full_narrative FROM shell_memory_archives WHERE archive_id=1")[0])
        self.run_mem("oriented")
        self.assertEqual(self.q("SELECT bootstrapped FROM shells WHERE shell_id=1")[0], 1)

    # ── reads via the API ─────────────────────────────────────────────────────
    def test_get_surfaces_return_ok(self):
        for surface in mem.GET_SURFACES:
            self.assertEqual(self.run_mem("get", surface), 0, surface)


if __name__ == "__main__":
    unittest.main()
