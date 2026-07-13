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

import contextlib
import io
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

    # ── decisions recall: index/library split (#274) ──────────────────────────
    def test_decisions_index_excludes_superseded_and_rationale(self):
        self.run_mem("decision", "use X", "--rationale", "r-old")
        old = self.q("SELECT decision_id FROM shell_decisions WHERE decision='use X'")[0]
        self.run_mem("decision", "use Y instead", "--parent", str(old))
        data = mem._api("GET", "/_sc/mem/decisions")
        ids = [d["decision_id"] for d in data["decisions"]]
        self.assertNotIn(old, ids)                 # superseded → out of the index
        self.assertGreaterEqual(data["superseded"], 1)
        self.assertTrue(all("rationale" not in d for d in data["decisions"]))

        # library half: by-id returns rationale + supersession links
        one = mem._api("GET", f"/_sc/mem/decisions/{old}")["decision"]
        self.assertEqual(one["rationale"], "r-old")
        self.assertIsNotNone(one["superseded_by"])

        # --all: the full log, superseded row present and marked
        alld = mem._api("GET", "/_sc/mem/decisions?all=1")["decisions"]
        row = next(d for d in alld if d["decision_id"] == old)
        self.assertIsNotNone(row["superseded_by"])

    def test_decisions_index_cap_with_loud_footer(self):
        for i in range(server.DECISIONS_INDEX_CAP + 3):
            self.run_mem("decision", f"bulk call {i}")
        data = mem._api("GET", "/_sc/mem/decisions")
        self.assertEqual(len(data["decisions"]), server.DECISIONS_INDEX_CAP)
        self.assertGreater(data["total_active"], server.DECISIONS_INDEX_CAP)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_mem("get", "decisions")
        self.assertIn("older active", buf.getvalue())   # cap is never silent
        self.assertIn("--all", buf.getvalue())

    def test_decisions_get_404_and_id_only_for_decisions(self):
        with self.assertRaises(SystemExit):
            self.run_mem("get", "decisions", "999999")
        with self.assertRaises(SystemExit):
            self.run_mem("get", "flags", "1")          # <id> is decisions-only

    # ── decisions why-audit link: feature_id + document_id (#0047) ─────────────
    def test_decision_feature_and_doc_link(self):
        self.run_mem("roadmap", "add", "feat L")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat L'")[0]
        body = self.tmp / "d.md"
        body.write_text("# spec\n")
        self.run_mem("doc", "add", "spec L", "--body-file", str(body), "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec L'")[0]

        # --feature links the decision to the feature
        self.run_mem("decision", "chose L", "--feature", str(fid))
        row = self.q("SELECT feature_id, document_id FROM shell_decisions "
                     "WHERE decision='chose L'")
        self.assertEqual(row[0], fid)
        self.assertIsNone(row[1])

        # --doc alone derives the feature from the document
        self.run_mem("decision", "shaped by spec L", "--doc", str(did))
        dfid, ddid = self.q("SELECT feature_id, document_id FROM shell_decisions "
                            "WHERE decision='shaped by spec L'")
        self.assertEqual((dfid, ddid), (fid, did))

        # the library view echoes the links + their titles
        one = mem._api("GET", f"/_sc/mem/decisions/"
                       f"{self.q('SELECT decision_id FROM shell_decisions WHERE decision=?', 'shaped by spec L')[0]}"
                       )["decision"]
        self.assertEqual(one["feature_id"], fid)
        self.assertEqual(one["document_id"], did)
        self.assertEqual(one["feature_title"], "feat L")
        self.assertEqual(one["document_title"], "spec L")

    def test_decision_bad_link_ids_404(self):
        with self.assertRaises(SystemExit):
            self.run_mem("decision", "bad feature", "--feature", "999999")
        with self.assertRaises(SystemExit):
            self.run_mem("decision", "bad doc", "--doc", "999999")
        # neither wrote a row
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM shell_decisions "
                   "WHERE decision IN ('bad feature','bad doc')")[0], 0)

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
        # edit: revise title + summary on an existing feature (issue #287)
        self.run_mem("roadmap", "edit", str(a["feature_id"]),
                     "--title", "feat A2", "--summary", "revised summary")
        self.assertEqual(list(self.q("SELECT title, summary FROM roadmap WHERE feature_id=?",
                                     a["feature_id"])), ["feat A2", "revised summary"])
        # edit with no fields → client dies before hitting the API
        with self.assertRaises(SystemExit):
            self.run_mem("roadmap", "edit", str(a["feature_id"]))
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
        row = self.q("SELECT from_shell_id, to_shell_id, body, dedupe_key "
                     "FROM shell_messages WHERE body='ping'")
        self.assertEqual((row["from_shell_id"], row["to_shell_id"]), (1, 1))
        self.assertTrue(row["dedupe_key"])  # every CLI send is stamped (#333)

    # ── messaging: idempotent send — a repeat key never writes a twin (#333) ──
    def test_message_send_dedupe_key(self):
        payload = {"to": "tc", "body": "dedupe me", "kind": "shell",
                   "dedupe_key": "test-dk-1"}
        first = mem._api("POST", "/_sc/mem/messages", payload)
        again = mem._api("POST", "/_sc/mem/messages", payload)
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM shell_messages "
                                "WHERE body='dedupe me'")[0], 1)

    # ── messaging: the sent view — check-before-resend is satisfiable (#333) ──
    def test_message_sent_view(self):
        self.run_mem("message", "send", "tc", "outbound proof")
        sent = mem._api("GET", "/_sc/mem/messages?direction=sent")
        self.assertEqual(sent["direction"], "sent")
        mine = [m for m in sent["messages"] if m["body"] == "outbound proof"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["to_shortname"], "tc")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.run_mem("message", "sent"), 0)
        self.assertIn("outbound proof", buf.getvalue())

    # ── engine DB busy → 503 + Retry-After, and the client retries (#331) ─────
    def test_busy_write_maps_to_503_and_client_retries(self):
        real_db, tripped = server.db, {"armed": True}

        class FlakyCon:
            """First non-auth statement raises 'database is locked'; the token
            lookup must stay live or the request dies at auth, not in the
            handler try-block where contention actually surfaces."""
            def __init__(self):
                self._con = real_db()

            def execute(self, sql, *a):
                if tripped["armed"] and "api_key" not in sql:
                    tripped["armed"] = False
                    raise sqlite3.OperationalError("database is locked")
                return self._con.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._con, name)

        server.db = lambda: FlakyCon()
        try:
            self.run_mem("state", "written through contention")
        finally:
            server.db = real_db
        self.assertFalse(tripped["armed"])  # the busy path actually fired
        self.assertEqual(self.q("SELECT current_state FROM shells WHERE shell_id=1")[0],
                         "written through contention")

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

    def test_doc_add_standalone_no_feature(self):
        # feature_id is optional — standalone docs are contract (the docs +
        # onboard skills and `sc mem doc add [--feature ID]` all say so; the
        # server used to 400 on it, the QAQC-04 regression this pins).
        body = self.tmp / "s.md"
        body.write_text("# standalone\n")
        self.assertEqual(
            self.run_mem("doc", "add", "standalone D", "--kind", "doc",
                         "--body-file", str(body)), 0)
        fid, seq = self.q("SELECT feature_id, seq FROM documents WHERE title='standalone D'")
        self.assertIsNone(fid)
        self.assertEqual(seq, 1)  # NULL feature is its own seq scope
        # a second standalone doc of the same kind seqs within that scope
        self.assertEqual(
            self.run_mem("doc", "add", "standalone E", "--kind", "doc",
                         "--body-file", str(body)), 0)
        self.assertEqual(
            self.q("SELECT seq FROM documents WHERE title='standalone E'")[0], 2)

    # ── narrative + oriented ──────────────────────────────────────────────────
    def test_narrative_and_oriented(self):
        self.run_mem("narrative", "a beat")
        self.assertIn("a beat",
                      self.q("SELECT full_narrative FROM shell_memory_archives WHERE archive_id=1")[0])
        self.run_mem("oriented")
        self.assertEqual(self.q("SELECT bootstrapped FROM shells WHERE shell_id=1")[0], 1)

    # ── reads via the API ─────────────────────────────────────────────────────
    def test_get_surfaces_return_ok(self):
        # `tasks` needs a scope (--doc/--feature); the rest list unscoped.
        for surface in mem.GET_SURFACES:
            if surface == "tasks":
                self.assertEqual(self.run_mem("get", "tasks", "--feature", "0"), 0, surface)
                continue
            self.assertEqual(self.run_mem("get", surface), 0, surface)

    def test_get_tasks_requires_scope(self):
        with self.assertRaises(SystemExit):  # no --doc/--feature → fail loud
            self.run_mem("get", "tasks")

    def test_get_surface_aliases(self):
        # boot docs say "doc", the write surface is `mem doc` — the read
        # surface accepts both short forms as `documents` (#242c).
        for alias in mem.GET_SURFACE_ALIASES:
            self.assertEqual(self.run_mem("get", alias), 0, alias)

    # ── shared planning reads (the docs/spec/review surfaces) ─────────────────
    def test_get_projects_documents_tasks_shells(self):
        self.run_mem("project", "add", "wsr", "Read WS")
        self.run_mem("roadmap", "add", "feat R")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat R'")[0]
        body = self.tmp / "r.md"
        body.write_text("# spec R\nthe body\n")
        self.run_mem("doc", "add", "spec R", "--body-file", str(body), "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec R'")[0]
        self.run_mem("task", "add", "task R", "--feature", str(fid),
                     "--doc", str(did), "--seq", "0")

        # projects roster includes the new work-stream
        projs = mem._api("GET", "/_sc/mem/projects")["projects"]
        self.assertIn("wsr", [p["shortname"] for p in projs])

        # documents list (scoped to the feature) carries task_count
        docs = mem._api("GET", f"/_sc/mem/documents?feature={fid}")["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["task_count"], 1)

        # single document returns the body
        one = mem._api("GET", f"/_sc/mem/documents/{did}")["document"]
        self.assertEqual(one["body"], "# spec R\nthe body\n")

        # task plan by doc
        tasks = mem._api("GET", f"/_sc/mem/tasks?doc={did}")["tasks"]
        self.assertEqual([t["title"] for t in tasks], ["task R"])

        # shells roster resolves shortname (review's display_name→shortname need)
        shells = mem._api("GET", "/_sc/mem/shells")["shells"]
        self.assertEqual(next(s["shortname"] for s in shells
                              if s["display_name"] == "TC"), "tc")

    def test_get_document_404(self):
        with self.assertRaises(SystemExit):
            self.run_mem("get", "documents", "--doc", "999999")


if __name__ == "__main__":
    unittest.main()
