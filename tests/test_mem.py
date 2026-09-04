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
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
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
PEER_TOKEN = "peer-token-cafebabe"   # second shell — cross-shell read coverage
REVIEW_TOKEN = "review-token-012345"
PLANNER_TOKEN = "planner-token-6789ab"


class MemMessageHelpContractTest(unittest.TestCase):
    def test_authored_send_help_matches_parser_options(self):
        parser = mem.build_parser()
        message = next(
            action for action in parser._actions
            if isinstance(action, mem.argparse._SubParsersAction)
        ).choices["message"]
        send = next(
            action for action in message._actions
            if isinstance(action, mem.argparse._SubParsersAction)
        ).choices["send"]
        options = {
            option
            for action in send._actions
            for option in action.option_strings
        }
        self.assertEqual(options, {"-h", "--help", "--kind"})

        rendered = send.format_help()
        synopsis = (
            './sc mem message send <to-shortname> "<body>" '
            '[--kind shell|task|result]'
        )
        self.assertIn(synopsis, mem.__doc__)
        for retired in ("--assignment", "--result-kind", "--directive"):
            self.assertNotIn(retired, mem.__doc__)
            self.assertNotIn(retired, rendered)


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
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (2, 'Peer', 'peer', 'test', 'sp', 1, 0, 1, 0, ?)", (PEER_TOKEN,))
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, flavor, mandate, "
        "system_prompt, user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (3, 'Reviewer', 'rev1', 'reviewer', 'test', 'sp', "
        "1, 0, 1, 0, ?)",
        (REVIEW_TOKEN,),
    )
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, flavor, mandate, "
        "system_prompt, user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (4, 'Planner', 'pln1', 'planner', 'test', 'sp', "
        "1, 0, 1, 0, ?)",
        (PLANNER_TOKEN,),
    )
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
        # Doc writes trigger a server-side snapshot+render against REPO_ROOT
        # (subfloor#434) — stub it class-wide so tests never touch the real
        # main tree; the hook itself is covered by test_doc_write_serializes.
        # staticmethod: a plain function stored on the class would come back
        # bound via self._real_serialize and eat an unwanted `self`.
        cls._real_serialize = staticmethod(server.serialize_doc_write)
        server.serialize_doc_write = lambda: {"ok": True, "output": "(test stub)"}

    @classmethod
    def tearDownClass(cls):
        server.serialize_doc_write = cls._real_serialize
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

    def write(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    # ── identity comes from the token, not an argument ────────────────────────
    def test_whoami_resolves_token_to_shell(self):
        self.assertEqual(self.run_mem("which"), 0)

    def test_delivery_audit_is_planner_only_and_preserves_dedup(self):
        implemented = self.write(
            "INSERT INTO roadmap "
            "(title,roadmap_status,sort_order,owning_shell,summary) "
            "VALUES ('audit implemented','in_progress',900,4,'x')"
        )
        shipped = self.write(
            "INSERT INTO roadmap "
            "(title,roadmap_status,sort_order,owning_shell,summary) "
            "VALUES ('audit shipped','shipped',901,4,'x')"
        )
        covered = self.write(
            "INSERT INTO roadmap "
            "(title,roadmap_status,sort_order,owning_shell,summary) "
            "VALUES ('audit covered','in_progress',902,4,'x')"
        )
        for feature in (implemented, covered):
            document = self.write(
                "INSERT INTO documents (feature_id,kind,seq,title) "
                "VALUES (?,'spec',1,?)",
                feature,
                f"audit spec {feature}",
            )
            self.write(
                "INSERT INTO spec_tasks "
                "(shell_id,feature_id,document_id,seq,title,status) "
                "VALUES (4,?,?,1,'Verification','done')",
                feature,
                document,
            )
        self.write(
            "INSERT INTO flags "
            "(shell_id,display_name,description,priority,feature_id,resolved) "
            "VALUES (4,'SC-998','[Ship] already handed off','Medium',?,0)",
            covered,
        )
        open_flag = self.write(
            "INSERT INTO flags "
            "(shell_id,display_name,description,priority,feature_id,resolved) "
            "VALUES (4,'SC-999','ordinary blocker','High',?,0)",
            implemented,
        )

        saved = mem.SC_API_TOKEN
        mem.SC_API_TOKEN = PLANNER_TOKEN
        try:
            data = mem._api("GET", "/_sc/mem/delivery-audit")
            self.assertIn(
                implemented,
                [row["feature_id"] for row in data["implemented_but_unshipped"]],
            )
            self.assertNotIn(
                covered,
                [row["feature_id"] for row in data["implemented_but_unshipped"]],
            )
            self.assertIn(
                shipped,
                [row["feature_id"] for row in data["shipped_but_undocumented"]],
            )
            row = next(row for row in data["open_flags"]
                       if row["flag_id"] == open_flag)
            self.assertEqual(row["roadmap_status"], "in_progress")
            self.assertEqual(row["frozen_docs"], 0)
            self.assertIn("SC-999", data["recent_flag_names"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.run_mem("delivery-audit", "--json"), 0)
            self.assertIn('"implemented_but_unshipped"', out.getvalue())
        finally:
            mem.SC_API_TOKEN = saved

        with self.assertRaises(SystemExit) as caught:
            self.run_mem("delivery-audit")
        self.assertIn("planner_only_delivery_audit", str(caught.exception))

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

    def test_decisions_get_404_and_unrelated_surface_rejects_id(self):
        with self.assertRaises(SystemExit):
            self.run_mem("get", "decisions", "999999")
        with self.assertRaises(SystemExit):
            self.run_mem("get", "state", "1")

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

    # ── decisions read fleet-wide; writes stay token-scoped (#318/#340) ───────
    def test_decisions_read_fleet_wide(self):
        saved = mem.SC_API_TOKEN
        mem.SC_API_TOKEN = PEER_TOKEN
        try:
            self.run_mem("decision", "peer design lock", "--rationale", "peer why")
        finally:
            mem.SC_API_TOKEN = saved
        did = self.q("SELECT decision_id FROM shell_decisions "
                     "WHERE decision='peer design lock'")[0]
        # by-id resolves from another seat — a cross-shell citation is live
        one = mem._api("GET", f"/_sc/mem/decisions/{did}")["decision"]
        self.assertEqual(one["rationale"], "peer why")
        self.assertEqual(one["shortname"], "peer")
        # the full log carries it too, attributed to its author
        alld = mem._api("GET", "/_sc/mem/decisions?all=1")["decisions"]
        row = next(d for d in alld if d["decision_id"] == did)
        self.assertEqual(row["shortname"], "peer")
        # the write itself stayed scoped to the author's token
        self.assertEqual(self.q("SELECT shell_id FROM shell_decisions "
                                "WHERE decision_id=?", did)[0], 2)

    # ── flags ─────────────────────────────────────────────────────────────────
    def test_flag_open_then_close(self):
        self.run_mem("flag", "open", "[x] blocked | Blocker for: y", "--name", "SC-1")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-1'")[0]
        self.run_mem("flag", "close", str(fid), "--notes", "fixed")
        self.assertEqual(self.q("SELECT resolved FROM flags WHERE flag_id=?", fid)[0], 1)

    def test_flag_exact_id_reads_open_row_with_complete_human_evidence(self):
        self.run_mem("roadmap", "add", "open evidence feature")
        feature = self.q(
            "SELECT feature_id FROM roadmap WHERE title='open evidence feature'"
        )[0]
        self.run_mem(
            "flag", "open", "[audit] open evidence | Blocker for: review",
            "--name", "SC-922-OPEN-EXACT", "--priority", "Low",
            "--feature", str(feature),
        )
        flag_id = self.q(
            "SELECT flag_id FROM flags WHERE display_name='SC-922-OPEN-EXACT'"
        )[0]
        created = self.q(
            "SELECT created_date FROM flags WHERE flag_id=?", flag_id
        )[0]

        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            self.run_mem("get", "flags", str(flag_id))
        self.assertEqual(
            human.getvalue(),
            f"#{flag_id} [SC-922-OPEN-EXACT] @tc (Low) [open]\n"
            f"  feature: #{feature} — open evidence feature\n"
            f"  opened: {created} · resolved: —\n"
            "  description: [audit] open evidence | Blocker for: review\n"
            "  closure notes: —\n",
        )

    def test_flag_exact_id_reads_resolved_row_with_complete_human_and_json_evidence(self):
        self.run_mem("roadmap", "add", "resolved evidence feature")
        feature = self.q(
            "SELECT feature_id FROM roadmap WHERE title='resolved evidence feature'"
        )[0]
        saved = mem.SC_API_TOKEN
        mem.SC_API_TOKEN = PEER_TOKEN
        try:
            self.run_mem(
                "flag", "open", "[audit] exact closure | Blocker for: review",
                "--name", "SC-922-EXACT", "--priority", "High",
                "--feature", str(feature),
            )
        finally:
            mem.SC_API_TOKEN = saved
        flag_id = self.q(
            "SELECT flag_id FROM flags WHERE display_name='SC-922-EXACT'"
        )[0]
        self.run_mem("flag", "close", str(flag_id), "--notes", "REV2 verified at abc123")
        expected = self.q(
            "SELECT created_date, resolved_date FROM flags WHERE flag_id=?", flag_id
        )

        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            self.run_mem("get", "flags", str(flag_id))
        output = human.getvalue()
        for value in (
            f"#{flag_id}", "SC-922-EXACT", "@peer", "High", "resolved",
            f"#{feature}", "resolved evidence feature", expected["created_date"],
            expected["resolved_date"], "[audit] exact closure",
            "REV2 verified at abc123",
        ):
            self.assertIn(str(value), output)

        raw = io.StringIO()
        with contextlib.redirect_stdout(raw):
            self.run_mem("get", "flags", str(flag_id), "--json")
        row = json.loads(raw.getvalue())["flag"]
        self.assertEqual(
            {
                key: row[key]
                for key in (
                    "flag_id", "display_name", "owner", "feature_id", "priority",
                    "description", "created_date", "resolved_date", "resolution_notes",
                )
            },
            {
                "flag_id": flag_id,
                "display_name": "SC-922-EXACT",
                "owner": "peer",
                "feature_id": feature,
                "priority": "High",
                "description": "[audit] exact closure | Blocker for: review",
                "created_date": expected["created_date"],
                "resolved_date": expected["resolved_date"],
                "resolution_notes": "REV2 verified at abc123",
            },
        )

    def test_flag_resolved_history_is_feature_scoped_and_excludes_open_deleted_and_other(self):
        self.run_mem("roadmap", "add", "flag history A")
        self.run_mem("roadmap", "add", "flag history B")
        feature_a = self.q("SELECT feature_id FROM roadmap WHERE title='flag history A'")[0]
        feature_b = self.q("SELECT feature_id FROM roadmap WHERE title='flag history B'")[0]

        def open_flag(name: str, feature: int) -> int:
            self.run_mem(
                "flag", "open", f"[history] {name} | Blocker for: audit",
                "--name", name, "--feature", str(feature),
            )
            return self.q("SELECT flag_id FROM flags WHERE display_name=?", name)[0]

        wanted = open_flag("SC-922-WANTED", feature_a)
        still_open = open_flag("SC-922-OPEN", feature_a)
        other = open_flag("SC-922-OTHER", feature_b)
        deleted = open_flag("SC-922-DELETED", feature_a)
        self.run_mem("flag", "close", str(wanted), "--notes", "wanted closure")
        self.run_mem("flag", "close", str(other), "--notes", "other closure")
        self.run_mem("flag", "close", str(deleted), "--notes", "deleted closure")
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE flags SET is_deleted=1 WHERE flag_id=?", (deleted,))
            con.commit()

        raw = io.StringIO()
        with contextlib.redirect_stdout(raw):
            self.run_mem(
                "get", "flags", "--feature", str(feature_a), "--resolved", "--json"
            )
        rows = json.loads(raw.getvalue())["flags"]
        self.assertEqual([row["flag_id"] for row in rows], [wanted])
        self.assertEqual(rows[0]["resolution_notes"], "wanted closure")
        self.assertNotIn(still_open, [row["flag_id"] for row in rows])
        self.assertNotIn(other, [row["flag_id"] for row in rows])
        self.assertNotIn(deleted, [row["flag_id"] for row in rows])

        dates = self.q(
            "SELECT created_date, resolved_date FROM flags WHERE flag_id=?", wanted
        )
        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            self.run_mem(
                "get", "flags", "--feature", str(feature_a), "--resolved"
            )
        self.assertEqual(
            human.getvalue(),
            f"#{wanted} [SC-922-WANTED] @tc (Medium) [resolved]\n"
            f"  feature: #{feature_a} — flag history A\n"
            f"  opened: {dates['created_date']} · resolved: {dates['resolved_date']}\n"
            "  description: [history] SC-922-WANTED | Blocker for: audit\n"
            "  closure notes: wanted closure\n",
        )
        with self.assertRaises(SystemExit):
            self.run_mem("get", "flags", str(deleted))

    def test_flag_resolved_history_refuses_unscoped_or_malformed_reads(self):
        with self.assertRaises(SystemExit):
            self.run_mem("get", "flags", "--resolved")
        with self.assertRaises(SystemExit):
            self.run_mem("get", "flags", "--feature", "1")
        with self.assertRaises(SystemExit):
            self.run_mem("get", "flags", "1", "--resolved", "--feature", "1")
        with self.assertRaises(SystemExit):
            self.run_mem("get", "state", "--resolved")
        with self.assertRaises(SystemExit):
            mem._api("GET", "/_sc/mem/flags?resolved=1")
        with self.assertRaises(SystemExit):
            mem._api("GET", "/_sc/mem/flags?feature=1")
        with self.assertRaises(SystemExit):
            mem._api("GET", "/_sc/mem/flags?feature=wat&resolved=1")

    # ── #149: close names the row it is about to resolve, and never twice ─────
    def test_flag_close_names_the_target_before_it_writes(self):
        """flag_id and the SC-### display_name are both small integers from two
        counters that drift through the same range, so a wrong number resolves
        to a different real row rather than failing. The one thing that lets a
        caller SEE it holds the wrong record is the row itself, echoed by the
        command that is about to resolve it."""
        self.run_mem("flag", "open", "[x] the wrong one | Blocker for: nothing",
                     "--name", "SC-149-A", "--priority", "High")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-149-A'")[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_mem("flag", "close", str(fid), "--notes", "done")
        out = buf.getvalue()
        self.assertIn("SC-149-A", out)          # the name, not just the number
        self.assertIn("the wrong one", out)     # and enough body to recognise it
        self.assertIn("High", out)

    def test_flag_close_refuses_to_overwrite_an_existing_resolution(self):
        """A redundant close is not a no-op: it replaces the resolution notes
        of whoever verified the flag with the closer's. The first writer's
        evidence must survive the second close attempt."""
        self.run_mem("flag", "open", "[x] verified | Blocker for: y",
                     "--name", "SC-149-B")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-149-B'")[0]
        self.run_mem("flag", "close", str(fid),
                     "--notes", "REV1 verified at head f7f687c")
        with self.assertRaises(SystemExit):
            self.run_mem("flag", "close", str(fid), "--notes", "closed without evidence")
        row = self.q("SELECT resolution_notes, resolved FROM flags WHERE flag_id=?",
                     fid)
        self.assertEqual(row["resolution_notes"], "REV1 verified at head f7f687c")
        self.assertEqual(row["resolved"], 1)

    # ── #288: a flag opened unnamed can be given a name; --append never eats ──
    def test_flag_edit_sets_a_display_name_a_flag_was_opened_without(self):
        self.run_mem("flag", "open", "[x] unnamed | Blocker for: naming")
        fid = self.q("SELECT flag_id FROM flags WHERE description LIKE '%unnamed%'")[0]
        self.assertIsNone(
            self.q("SELECT display_name FROM flags WHERE flag_id=?", fid)[0])
        self.run_mem("flag", "edit", str(fid), "--name", "SC-288")
        self.assertEqual(
            self.q("SELECT display_name FROM flags WHERE flag_id=?", fid)[0],
            "SC-288")

    def test_flag_edit_append_extends_the_body_instead_of_replacing_it(self):
        self.run_mem("flag", "open", "[x] tracker gate 1 | Blocker for: arc",
                     "--name", "SC-288-B")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-288-B'")[0]
        self.run_mem("flag", "edit", str(fid), "--append", "\n\nGATE 2 CLEARED.")
        desc = self.q("SELECT description FROM flags WHERE flag_id=?", fid)[0]
        self.assertIn("tracker gate 1", desc)      # the original body survives
        self.assertIn("GATE 2 CLEARED.", desc)
        with self.assertRaises(SystemExit):        # replace and append conflict
            self.run_mem("flag", "edit", str(fid), "--description", "new",
                         "--append", "more")
        self.assertIn("tracker gate 1",
                      self.q("SELECT description FROM flags WHERE flag_id=?",
                             fid)[0])

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

    # ── messaging: `cartographer` role alias (#369–#372) ─────────────────────
    # Boot docs address the map-keeper by role; forks mint shortnames like
    # CART1. Ordered a/b/c: the shared class DB walks no-cartographer →
    # flavor-resolved → exact-shortname-precedence.
    def test_message_cart_alias_a_missing_cartographer_is_a_clear_404(self):
        with self.assertRaises(SystemExit):   # _api dies on HTTP 404
            mem._api("POST", "/_sc/mem/messages",
                     {"to": "cartographer", "body": "map gap: x. heal."})

    def test_message_cart_alias_b_resolves_by_flavor(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
            "system_prompt, user_id) VALUES (7, 'Cart', 'CART9', 'cartographer', 'sp', 1)")
        con.commit()
        con.close()
        self.run_mem("message", "send", "cartographer", "map gap: y. heal.")
        row = self.q("SELECT to_shell_id FROM shell_messages WHERE body='map gap: y. heal.'")
        self.assertEqual(row["to_shell_id"], 7)

    def test_message_cart_alias_c_exact_shortname_wins(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, "
            "system_prompt, user_id) VALUES (8, 'Literal', 'cartographer', 'sp', 1)")
        con.commit()
        con.close()
        self.run_mem("message", "send", "cartographer", "map gap: z. heal.")
        row = self.q("SELECT to_shell_id FROM shell_messages WHERE body='map gap: z. heal.'")
        self.assertEqual(row["to_shell_id"], 8)

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

    def test_legacy_mem_qaqc_command_uses_the_sprint_approval_surface(self):
        self.run_mem("roadmap", "add", "feat QAQC")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat QAQC'")[0]
        body = self.tmp / "qaqc.md"
        body.write_text("# exact QAQC body\n")
        self.run_mem(
            "doc",
            "add",
            "spec QAQC",
            "--body-file",
            str(body),
            "--feature",
            str(fid),
        )
        did = self.q("SELECT document_id FROM documents WHERE title='spec QAQC'")[0]
        original_token = mem.SC_API_TOKEN
        mem.SC_API_TOKEN = REVIEW_TOKEN
        try:
            self.assertEqual(
                0,
                self.run_mem(
                    "doc", "qaqc", str(did), "--verdict", "approved"
                ),
            )
        finally:
            mem.SC_API_TOKEN = original_token

        approval = self.q(
            "SELECT reviewer_shell_id,verdict,revision_sha256 "
            "FROM sprint_spec_approvals WHERE document_id=?",
            did,
        )
        self.assertEqual((3, "pass"), tuple(approval[:2]))
        self.assertEqual(64, len(approval["revision_sha256"]))
        self.assertEqual(0, self.run_mem("get", "qaqc", "--doc", str(did)))

    # ── doc writes serialize headlessly (subfloor#434) ───────────────────────
    def test_doc_write_serializes(self):
        # Real hook, fake subprocess pair: assert the wiring, not the scripts.
        server.serialize_doc_write = self._real_serialize
        calls = []
        real_rsr = server.run_snapshot_render
        server.run_snapshot_render = lambda: calls.append(1) or "snapshot+render ok"
        try:
            self.run_mem("roadmap", "add", "feat ser")
            fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat ser'")[0]
            body = self.tmp / "ser.md"
            body.write_text("# ser\nbody\n")
            self.assertEqual(
                self.run_mem("doc", "add", "spec ser", "--body-file", str(body),
                             "--feature", str(fid)), 0)
            did = self.q("SELECT document_id FROM documents WHERE title='spec ser'")[0]
            self.assertEqual(
                self.run_mem("doc", "edit", str(did), "--title", "spec ser v2"), 0)
            self.assertEqual(self.run_mem("doc", "freeze", str(did)), 0)
            self.assertEqual(len(calls), 3)  # add + edit + freeze each serialize once
            # a failed serialize never fails the write — the row commits, the
            # CLI warns and exits nonzero so the drift is visible
            def boom():
                raise RuntimeError("snapshot failed:\nbroken")
            server.run_snapshot_render = boom
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.run_mem("doc", "add", "spec ser2", "--body-file",
                                  str(body), "--feature", str(fid))
            self.assertEqual(rc, 1)
            self.assertIn("WARNING", buf.getvalue())
            self.assertIsNotNone(
                self.q("SELECT document_id FROM documents WHERE title='spec ser2'"))
        finally:
            server.run_snapshot_render = real_rsr
            server.serialize_doc_write = lambda: {"ok": True, "output": "(test stub)"}

    def test_move_spec_to_feature_preserves_identity_and_plan(self):
        self.run_mem("roadmap", "add", "split source")
        self.run_mem("roadmap", "add", "split target")
        source = self.q(
            "SELECT feature_id FROM roadmap WHERE title='split source'"
        )[0]
        target = self.q(
            "SELECT feature_id FROM roadmap WHERE title='split target'"
        )[0]
        self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'spec',1,'existing target spec')",
            target,
        )
        did = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'spec',7,'active v2 spec')",
            source,
        )
        tid = self.write(
            "INSERT INTO spec_tasks "
            "(feature_id,document_id,seq,title,status,shell_id) "
            "VALUES (?,?,0,'Preparation','in_progress',1)",
            source,
            did,
        )
        decision_id = self.write(
            "INSERT INTO shell_decisions "
            "(shell_id,decision_date,decision,rationale,feature_id,document_id) "
            "VALUES (1,'2026-09-01','move with the spec','why',?,?)",
            source,
            did,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                self.run_mem(
                    "doc", "move", str(did), "--feature", str(target)
                ),
                0,
            )
        self.assertIn(f"feature #{source} → #{target}", output.getvalue())
        self.assertIn("as spec seq 2 (1 task(s), 1 decision(s))", output.getvalue())
        self.assertIn("local snapshot + flat render refreshed", output.getvalue())
        document = self.q(
            "SELECT feature_id,seq,title FROM documents WHERE document_id=?", did
        )
        self.assertEqual((target, 2, "active v2 spec"), tuple(document))
        self.assertEqual(
            tuple(
                self.q(
                    "SELECT feature_id,status FROM spec_tasks WHERE task_id=?", tid
                )
            ),
            (target, "in_progress"),
        )
        self.assertEqual(
            self.q(
                "SELECT feature_id FROM shell_decisions WHERE decision_id=?",
                decision_id,
            )[0],
            target,
        )

    def test_move_spec_to_feature_refuses_ineligible_history(self):
        self.run_mem("roadmap", "add", "move refusal source")
        self.run_mem("roadmap", "add", "move refusal target")
        self.run_mem(
            "roadmap", "add", "move terminal target", "--status", "shipped"
        )
        source = self.q(
            "SELECT feature_id FROM roadmap WHERE title='move refusal source'"
        )[0]
        target = self.q(
            "SELECT feature_id FROM roadmap WHERE title='move refusal target'"
        )[0]
        terminal = self.q(
            "SELECT feature_id FROM roadmap WHERE title='move terminal target'"
        )[0]
        frozen = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title,frozen) "
            "VALUES (?,'spec',1,'frozen history',1)",
            source,
        )
        ordinary = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'doc',1,'ordinary doc')",
            source,
        )
        terminal_bound = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'spec',2,'terminal target candidate')",
            source,
        )
        sprint_bound = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'spec',3,'sprint history')",
            source,
        )
        sprint_id = self.write(
            "INSERT INTO sprints (feature_id,originating_planner_shell_id) "
            "VALUES (?,4)",
            source,
        )
        self.write(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256) VALUES (?,?,?)",
            sprint_id,
            sprint_bound,
            "a" * 64,
        )

        refused = (
            (frozen, target, "frozen"),
            (ordinary, target, "only spec"),
            (terminal_bound, terminal, "terminal"),
            (sprint_bound, target, f"Sprint #{sprint_id}"),
            (terminal_bound, source, "already belongs"),
        )
        for did, feature_id, message in refused:
            with self.subTest(document_id=did, message=message):
                with self.assertRaises(SystemExit) as caught:
                    mem._api(
                        "PATCH",
                        f"/_sc/mem/docs/{did}/feature",
                        {"feature_id": feature_id},
                    )
                self.assertIn("409", str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertEqual(
                    self.q(
                        "SELECT feature_id FROM documents WHERE document_id=?", did
                    )[0],
                    source,
                )

    def test_move_spec_to_feature_rolls_back_related_rows(self):
        self.run_mem("roadmap", "add", "atomic move source")
        self.run_mem("roadmap", "add", "atomic move target")
        source = self.q(
            "SELECT feature_id FROM roadmap WHERE title='atomic move source'"
        )[0]
        target = self.q(
            "SELECT feature_id FROM roadmap WHERE title='atomic move target'"
        )[0]
        did = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title) "
            "VALUES (?,'spec',1,'atomic move spec')",
            source,
        )
        tid = self.write(
            "INSERT INTO spec_tasks (feature_id,document_id,seq,title,shell_id) "
            "VALUES (?,?,0,'Preparation',1)",
            source,
            did,
        )
        self.write(
            "CREATE TRIGGER fail_atomic_spec_move "
            "BEFORE UPDATE OF feature_id ON spec_tasks "
            f"WHEN OLD.task_id={tid} BEGIN "
            "SELECT RAISE(ABORT,'forced move failure'); END"
        )
        try:
            with self.assertRaises(SystemExit) as caught:
                mem._api(
                    "PATCH",
                    f"/_sc/mem/docs/{did}/feature",
                    {"feature_id": target},
                )
            self.assertIn("409", str(caught.exception))
            self.assertIn("forced move failure", str(caught.exception))
            self.assertEqual(
                self.q(
                    "SELECT feature_id FROM documents WHERE document_id=?", did
                )[0],
                source,
            )
            self.assertEqual(
                self.q("SELECT feature_id FROM spec_tasks WHERE task_id=?", tid)[0],
                source,
            )
        finally:
            self.write("DROP TRIGGER fail_atomic_spec_move")

    def test_feature_move_guidance_is_seeded_for_shells(self):
        for skill in ("db_map", "docs", "spec"):
            with self.subTest(skill=skill):
                content = self.q(
                    "SELECT content FROM skills WHERE name=? AND is_deleted=0",
                    skill,
                )[0]
                self.assertIn(
                    "sc mem doc move <document_id> --feature <target_feature_id>",
                    content,
                )
        docs = self.q("SELECT content FROM skills WHERE name='docs'")[0]
        self.assertIn("Split an active era from feature history", docs)

    # ── doc write vs local save — ONE shared serialization boundary ───────────
    def test_doc_write_and_snapshot_share_one_lock(self):
        # Doc-write serialize and /api/snapshot both write the same non-atomic
        # local files. Force a snapshot to sit inside its critical section, then
        # prove a concurrent doc write waits for it.
        self.run_mem("roadmap", "add", "feat race")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat race'")[0]
        body = self.tmp / "race.md"
        body.write_text("# race\n")
        self.run_mem("doc", "add", "spec race", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec race'")[0]
        server.serialize_doc_write = self._real_serialize
        real_rsr = server.run_snapshot_render
        in_snapshot = threading.Event()
        release = threading.Event()
        overlap = []

        def fake_rsr():
            if not in_snapshot.is_set():
                in_snapshot.set()
                release.wait(5)
                return "snapshot endpoint (test stub)"
            overlap.append(in_snapshot.is_set() and not release.is_set())
            return "snapshot+render (test stub)"

        server.run_snapshot_render = fake_rsr
        snapshot_rc = []

        def do_snapshot():
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/snapshot", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                snapshot_rc.append(resp.status)

        try:
            t = threading.Thread(target=do_snapshot)
            t.start()
            self.assertTrue(in_snapshot.wait(5), "snapshot never entered its section")
            # hold the snapshot inside its section while the doc write arrives;
            # the timer releases it well after an unshared lock would overlap
            threading.Timer(0.5, release.set).start()
            self.assertEqual(
                self.run_mem("doc", "edit", str(did), "--title", "spec race v2"), 0)
            t.join(10)
            self.assertEqual(snapshot_rc, [200])
            self.assertEqual(overlap, [False])
        finally:
            release.set()
            server.run_snapshot_render = real_rsr
            server.serialize_doc_write = lambda: {"ok": True, "output": "(test stub)"}

    def test_publish_endpoint_is_permanently_retired(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/publish",
            data=b"",
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(failed.exception.code, 410)
        payload = json.loads(failed.exception.read())
        self.assertEqual(payload["error"]["code"], "publish_retired")
        self.assertIn("save locally", payload["error"]["message"])

    # ── SC-013: a slow successful serialize is still a successful write ────────
    def test_doc_write_slow_serializer_succeeds(self):
        # The post-write snapshot+render can legitimately run for minutes; the
        # generic client timeout turned that slow SUCCESS into "API unreachable"
        # and a PATCH retry re-ran the serialize. Doc writes carry their own
        # budget — shrink the generic timeout below the serializer's runtime and
        # prove add/edit/freeze still succeed; freeze is idempotent on retry.
        # A urlopen spy pins the wiring: doc writes must USE the doc budget,
        # everything else the generic one (without it a slow-enough-to-pass
        # stub could let a hardcoded timeout slip through).
        server.serialize_doc_write = self._real_serialize
        real_rsr = server.run_snapshot_render
        server.run_snapshot_render = lambda: (time.sleep(1.0), "slow ok")[1]
        real_timeout = mem._TIMEOUT
        mem._TIMEOUT = 0.5  # any call on the generic budget now times out
        timeouts = []
        real_urlopen = mem.urllib.request.urlopen

        def spy(req, timeout=None):
            timeouts.append(timeout)
            return real_urlopen(req, timeout=timeout)

        mem.urllib.request.urlopen = spy
        try:
            self.run_mem("roadmap", "add", "feat slow")
            fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat slow'")[0]
            body = self.tmp / "slow.md"
            body.write_text("# slow\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(
                    self.run_mem("doc", "add", "spec slow", "--body-file", str(body),
                                 "--feature", str(fid)), 0)
                did = self.q(
                    "SELECT document_id FROM documents WHERE title='spec slow'")[0]
                self.assertEqual(
                    self.run_mem("doc", "edit", str(did), "--title", "spec slow v2"), 0)
                self.assertEqual(self.run_mem("doc", "freeze", str(did)), 0)
                # retry of a committed freeze (the ambiguous-timeout case):
                # success again, flagged already_frozen — not a 409
                self.assertEqual(self.run_mem("doc", "freeze", str(did)), 0)
            out = buf.getvalue()
            self.assertIn("already frozen", out)
            self.assertNotIn("WARNING", out)
            self.assertEqual(
                self.q("SELECT frozen FROM documents WHERE document_id=?", did)[0], 1)
            self.assertEqual(timeouts, [0.5] + [mem._DOC_WRITE_TIMEOUT] * 4)
        finally:
            mem.urllib.request.urlopen = real_urlopen
            mem._TIMEOUT = real_timeout
            server.run_snapshot_render = real_rsr
            server.serialize_doc_write = lambda: {"ok": True, "output": "(test stub)"}

    # ── task cancel: honest terminal state after a feature split (#342) ───────
    def test_task_cancel_with_notes(self):
        self.run_mem("roadmap", "add", "feat split")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat split'")[0]
        body = self.tmp / "sp.md"
        body.write_text("# spec\n")
        self.run_mem("doc", "add", "spec split", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec split'")[0]
        self.run_mem("task", "add", "moved away", "--feature", str(fid),
                     "--doc", str(did), "--seq", "1")
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='moved away'")[0]
        self.run_mem("task", "cancel", str(tid), "--notes", "moved to F999 as task #7")
        row = self.q("SELECT status, resolution_notes, completed_date "
                     "FROM spec_tasks WHERE task_id=?", tid)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["resolution_notes"], "moved to F999 as task #7")
        self.assertIsNone(row["completed_date"])  # cancelled ≠ done
        # the read surface shows the trail
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_mem("get", "tasks", "--doc", str(did))
        self.assertIn("cancelled", buf.getvalue())
        self.assertIn("moved to F999", buf.getvalue())

    # ── task edit: revise title/description in place (SC-010) ────────────────
    def test_task_edit_title_and_desc(self):
        self.run_mem("roadmap", "add", "feat edit")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat edit'")[0]
        body = self.tmp / "se.md"
        body.write_text("# spec\n")
        self.run_mem("doc", "add", "spec edit", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec edit'")[0]
        self.run_mem("task", "add", "vague title", "--feature", str(fid),
                     "--doc", str(did), "--seq", "1")
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='vague title'")[0]
        self.run_mem("task", "edit", str(tid), "--title", "precise title",
                     "--desc", "the QA contract, spelled out")
        row = self.q("SELECT title, description, status FROM spec_tasks WHERE task_id=?", tid)
        self.assertEqual(row["title"], "precise title")
        self.assertEqual(row["description"], "the QA contract, spelled out")
        self.assertEqual(row["status"], "pending")  # edit is not a status change
        # editing one field leaves the other untouched
        self.run_mem("task", "edit", str(tid), "--desc", "revised contract")
        row = self.q("SELECT title, description FROM spec_tasks WHERE task_id=?", tid)
        self.assertEqual(row["title"], "precise title")
        self.assertEqual(row["description"], "revised contract")
        # nothing-to-edit is a client-side refusal, not a silent no-op
        with self.assertRaises(SystemExit):
            self.run_mem("task", "edit", str(tid))

    def test_task_edit_rejects_blank_title(self):
        # SC-011 — the API enforces the task-add invariant on edit too: an
        # empty/whitespace title is a 400, and the existing row is untouched.
        self.run_mem("roadmap", "add", "feat blank")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat blank'")[0]
        body = self.tmp / "blank.md"
        body.write_text("# spec\n")
        self.run_mem("doc", "add", "spec blank", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec blank'")[0]
        self.run_mem("task", "add", "kept title", "--feature", str(fid),
                     "--doc", str(did), "--seq", "1")
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='kept title'")[0]
        for blank in ("", "   "):
            with self.assertRaises(SystemExit):
                self.run_mem("task", "edit", str(tid), "--title", blank)
        row = self.q("SELECT title, description FROM spec_tasks WHERE task_id=?", tid)
        self.assertEqual(row["title"], "kept title")
        self.assertIsNone(row["description"])

    def test_task_bogus_status_is_400_not_500(self):
        self.run_mem("roadmap", "add", "feat vs")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat vs'")[0]
        body = self.tmp / "vs.md"
        body.write_text("# spec\n")
        self.run_mem("doc", "add", "spec vs", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec vs'")[0]
        self.run_mem("task", "add", "victim", "--feature", str(fid),
                     "--doc", str(did), "--seq", "1")
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='victim'")[0]
        try:
            mem._api("PATCH", f"/_sc/mem/tasks/{tid}", {"status": "wontfix"})
            self.fail("bogus status accepted")
        except SystemExit as e:
            self.assertIn("400", str(e.code))   # validated, not a CHECK 500
        self.assertEqual(self.q("SELECT status FROM spec_tasks WHERE task_id=?",
                                tid)["status"], "pending")

    # ── flag edit: tracker flags update via CLI, not raw PATCH probes (#316) ──
    def test_flag_edit(self):
        self.run_mem("flag", "open", "[x] tracker | Blocker for: arc", "--name", "SC-9")
        fid = self.q("SELECT flag_id FROM flags WHERE display_name='SC-9'")[0]
        self.run_mem("flag", "edit", str(fid),
                     "--description", "[x] tracker — gate 1 cleared | Blocker for: arc",
                     "--priority", "High")
        row = self.q("SELECT description, priority, resolved FROM flags "
                     "WHERE flag_id=?", fid)
        self.assertIn("gate 1 cleared", row["description"])
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["resolved"], 0)  # edit never resolves
        with self.assertRaises(SystemExit):   # no fields → dies client-side
            self.run_mem("flag", "edit", str(fid))

    # ── doc edit --render-path is honored, not silently dropped (#312) ────────
    def test_doc_edit_render_path(self):
        body = self.tmp / "rp.md"
        body.write_text("# publishable\n")
        self.run_mem("doc", "add", "pathless doc", "--kind", "doc",
                     "--body-file", str(body))
        did = self.q("SELECT document_id FROM documents WHERE title='pathless doc'")[0]
        self.assertIsNone(self.q("SELECT render_path FROM documents "
                                 "WHERE document_id=?", did)[0])
        self.run_mem("doc", "edit", str(did), "--render-path", "docs_sc/pathless.md")
        self.assertEqual(self.q("SELECT render_path FROM documents "
                                "WHERE document_id=?", did)[0], "docs_sc/pathless.md")

    def test_doc_add_rejects_duplicate_render_path(self):
        body = self.tmp / "duplicate-add.md"
        body.write_text("# duplicate\n")
        self.run_mem("roadmap", "add", "duplicate add feature")
        feature_id = self.q(
            "SELECT feature_id FROM roadmap WHERE title='duplicate add feature'"
        )[0]
        self.assertEqual(
            self.run_mem(
                "doc", "add", "render owner", "--kind", "doc",
                "--body-file", str(body), "--render-path", "docs_sc/owned.md",
                "--feature", str(feature_id),
            ),
            0,
        )
        with self.assertRaises(SystemExit) as caught:
            self.run_mem(
                "doc", "add", "render intruder", "--kind", "doc",
                "--body-file", str(body), "--render-path", "docs_sc//owned.md",
                "--feature", str(feature_id),
            )
        self.assertIn("document IDs", str(caught.exception))
        self.assertIsNone(
            self.q("SELECT document_id FROM documents WHERE title='render intruder'")
        )

    def test_doc_edit_rejects_derived_render_path_collision(self):
        body = self.tmp / "duplicate-edit.md"
        body.write_text("# duplicate\n")
        self.run_mem("roadmap", "add", "duplicate edit feature")
        feature_id = self.q(
            "SELECT feature_id FROM roadmap WHERE title='duplicate edit feature'"
        )[0]
        self.run_mem(
            "doc", "add", "derived owner", "--kind", "doc",
            "--body-file", str(body), "--feature", str(feature_id),
        )
        self.run_mem(
            "doc", "add", "derived candidate", "--kind", "doc",
            "--body-file", str(body), "--feature", str(feature_id),
        )
        candidate = self.q(
            "SELECT document_id FROM documents WHERE title='derived candidate'"
        )[0]

        with self.assertRaises(SystemExit) as caught:
            self.run_mem("doc", "edit", str(candidate), "--title", "derived owner")

        self.assertIn("document IDs", str(caught.exception))
        self.assertEqual(
            self.q("SELECT title FROM documents WHERE document_id=?", candidate)[0],
            "derived candidate",
        )

    def test_frozen_doc_render_path_moves_off_a_collision(self):
        # subfloor#629: a retired frozen duplicate sharing a render_path with
        # its live successor stalls every flat render on the fork. The frozen
        # row's path is the only thing that may change, and changing it must
        # clear the collision.
        body = self.tmp / "frozen-collision.md"
        body.write_text("# sales site\n")
        self.run_mem("roadmap", "add", "frozen collision feature")
        feature_id = self.q(
            "SELECT feature_id FROM roadmap WHERE title='frozen collision feature'"
        )[0]
        self.run_mem(
            "doc", "add", "SC-099 Sales Site", "--kind", "spec",
            "--body-file", str(body), "--render-path", "specs_sc/sc-099-sales-site.md",
            "--feature", str(feature_id),
        )
        live = self.q(
            "SELECT document_id FROM documents WHERE title='SC-099 Sales Site'"
        )[0]
        # Legacy rows predate the write-side duplicate check: seed the
        # collision directly, the way such a row exists on a real fork.
        retired = self.write(
            "INSERT INTO documents (feature_id,kind,seq,title,body,render_path,frozen) "
            "VALUES (?,'spec',2,'RETIRED (dupe)','# sales site\n',"
            "'specs_sc/sc-099-sales-site.md',1)",
            feature_id,
        )
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM documents WHERE render_path=?",
                   "specs_sc/sc-099-sales-site.md")[0],
            2,
        )

        with self.assertRaises(SystemExit) as caught:
            self.run_mem("doc", "edit", str(retired), "--title", "RETIRED")
        self.assertIn("frozen", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.run_mem("doc", "edit", str(retired),
                         "--render-path", "specs_sc/sc-099-sales-site.md")
        self.assertIn("document IDs", str(caught.exception))

        self.assertEqual(
            self.run_mem("doc", "edit", str(retired),
                         "--render-path", "specs_sc/sc-099-sales-site-retired.md"),
            0,
        )
        row = self.q("SELECT title,render_path,frozen FROM documents WHERE document_id=?",
                     retired)
        self.assertEqual(tuple(row),
                         ("RETIRED (dupe)", "specs_sc/sc-099-sales-site-retired.md", 1))
        self.assertEqual(
            self.q("SELECT render_path FROM documents WHERE document_id=?", live)[0],
            "specs_sc/sc-099-sales-site.md",
        )

    def test_concurrent_doc_adds_cannot_claim_one_render_path(self):
        self.run_mem("roadmap", "add", "concurrent render owners")
        feature_id = self.q(
            "SELECT feature_id FROM roadmap WHERE title='concurrent render owners'"
        )[0]
        barrier = threading.Barrier(2)
        outcomes = []

        def add(title):
            barrier.wait()
            try:
                mem._api("POST", "/_sc/mem/docs", {
                    "feature_id": feature_id,
                    "kind": "doc",
                    "title": title,
                    "body": "# concurrent\n",
                    "render_path": "docs_sc/concurrent-owner.md",
                })
                outcomes.append("created")
            except SystemExit as exc:
                outcomes.append(str(exc))

        threads = [
            threading.Thread(target=add, args=("concurrent owner A",)),
            threading.Thread(target=add, args=("concurrent owner B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(sum(outcome == "created" for outcome in outcomes), 1)
        self.assertTrue(any("409" in outcome for outcome in outcomes))
        self.assertEqual(
            self.q(
                "SELECT COUNT(*) FROM documents WHERE render_path=?",
                "docs_sc/concurrent-owner.md",
            )[0],
            1,
        )

    # ── decision guard: a bare sibling verb is a guess, not a decision (#311) ─
    def test_decision_bare_verb_guarded(self):
        before = self.q("SELECT COUNT(*) FROM shell_decisions")[0]
        with self.assertRaises(SystemExit):
            self.run_mem("decision", "add")
        self.assertEqual(self.q("SELECT COUNT(*) FROM shell_decisions")[0], before)
        # --force records it; a real multi-word decision never trips the guard
        self.run_mem("decision", "add", "--force")
        self.assertIsNotNone(self.q("SELECT 1 FROM shell_decisions WHERE decision='add'"))

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
            if surface == "qaqc":
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
