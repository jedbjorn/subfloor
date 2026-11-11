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
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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



class TimeoutReceiptTest(unittest.TestCase):
    """A timed-out write is a lost receipt, not an unreachable API (#1507, #1379)."""

    def setUp(self):
        self._pause = mem._RETRY_PAUSE
        mem._RETRY_PAUSE = 0.0

    def tearDown(self):
        mem._RETRY_PAUSE = self._pause

    def _timeout_message(self, method, path, **kwargs):
        stderr = io.StringIO()
        with mock.patch.object(
            mem.urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            mem._api(method, path, {"x": 1} if method == "POST" else None, **kwargs)
        return str(caught.exception)

    def test_non_idempotent_timeout_names_the_write_and_budget(self):
        message = self._timeout_message("POST", "/_sc/skills/put", timeout=420)
        self.assertIn("POST /_sc/skills/put", message)
        self.assertIn("420s", message)
        self.assertIn("MAY have landed", message)
        self.assertNotIn("unreachable", message)

    def test_idempotent_timeout_still_reports_unreachable(self):
        message = self._timeout_message("GET", "/_sc/mem/whoami")
        self.assertIn("API unreachable", message)
        self.assertNotIn("MAY have landed", message)

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
        self.assertEqual(
            options, {"-h", "--help", "--kind", "--body-file", "--json"})

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

    def run_out(self, *argv, stdin: "str | None" = None):
        """(exit status, stdout) of one `sc mem` call, optionally fed stdin."""
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(buf))
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
            rc = self.run_mem(*argv)
        return rc, buf.getvalue()

    def run_json(self, *argv, stdin: "str | None" = None):
        """A write under --json: stdout must be exactly one JSON object."""
        rc, out = self.run_out(*argv, "--json", stdin=stdin)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        return rc, json.loads(out)

    def refusal(self, *argv, stdin: "str | None" = None) -> str:
        """The stderr text of a call that must refuse with status 1."""
        with self.assertRaises(SystemExit) as caught:
            self.run_out(*argv, stdin=stdin)
        self.assertIsInstance(caught.exception.code, str)
        return caught.exception.code

    def server_today(self) -> str:
        """The date the SERVER stamps (SQLite `date('now')`, UTC). The local
        `date.today()` differs from it for every hour the two calendars
        disagree, so a stamped date is compared with this one."""
        return self.q("SELECT date('now')")[0]

    def fails(self, *argv, stdin: "str | None" = None):
        """(exit status, stdout, stderr prose) of a call that must refuse."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
            with self.assertRaises(SystemExit) as caught:
                self.run_mem(*argv)
        code = caught.exception.code
        if isinstance(code, str):      # sys.exit(str): stderr text + status 1
            return 1, out.getvalue(), code
        return code, out.getvalue(), err.getvalue()

    def fails_json(self, *argv, stdin: "str | None" = None):
        """A refusal under --json: nonzero status, prose on stderr, and
        exactly one `{"ok": false, "error": …}` object on stdout."""
        rc, out, err = self.fails(*argv, "--json", stdin=stdin)
        self.assertNotEqual(rc, 0)
        self.assertTrue(err.strip(), "the stderr prose must survive --json")
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        payload = json.loads(out)
        self.assertIs(payload["ok"], False)
        self.assertEqual(set(payload["error"]), {"code", "message", "http_status"})
        return rc, payload

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
        self.assertIn("flag_sweep's close and open rules are Planner-owned; "
                      "delivery audit is available only to Planner", str(caught.exception))

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

    def test_mem_doc_qaqc_records_pass_fail_on_the_sprint_approval_surface(self):
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
                self.run_mem("doc", "qaqc", str(did), "--verdict", "pass"),
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
            # a failed serialize never fails the write — the row commits and
            # the CLI warns but exits 0: a nonzero exit reads as "retry", and
            # a retried `doc add` writes a second document
            def boom():
                raise RuntimeError("snapshot failed:\nbroken")
            server.run_snapshot_render = boom
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.run_mem("doc", "add", "spec ser2", "--body-file",
                                  str(body), "--feature", str(fid))
            self.assertEqual(rc, 0)
            self.assertIn("WARNING", buf.getvalue())
            self.assertIsNotNone(
                self.q("SELECT document_id FROM documents WHERE title='spec ser2'"))
            # under --json the same outcome is a `warning` field on the one
            # object, still exit 0, still carrying the created id
            rc, out = self.run_json("doc", "add", "spec ser3", "--body-file",
                                    str(body), "--feature", str(fid))
            self.assertEqual(rc, 0)
            self.assertTrue(out["ok"])
            self.assertIn("snapshot/render failed", out["warning"])
            self.assertIn("broken", out["warning_detail"])
            self.assertEqual(
                out["document_id"],
                self.q("SELECT document_id FROM documents WHERE title='spec ser3'")[0])
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

    def test_feature_move_guidance_is_rendered_for_shells(self):
        # The move verb lives in the `spec_authoring` (Planner) and
        # `spec_execution` (Developer) skills; each flavor body names its skill.
        for flavor, skill in (("planner", "spec_authoring"), ("dev", "spec_execution")):
            with self.subTest(flavor=flavor):
                body = (ENGINE / "templates" / "shells" / f"{flavor}.md").read_text()
                self.assertIn(f"`{skill}`", body)
                detail = ENGINE / "assets" / "skills" / skill / "SKILL.md"
                self.assertIn(
                    "sc mem doc move <document_id> --feature <target>",
                    detail.read_text(),
                )
        planner = (ENGINE / "templates" / "shells" / "planner.md").read_text()
        self.assertIn("## REVISE, FREEZE, DOCUMENT", planner)
        self.assertIn("sc mem doc move", (ENGINE / "scripts" / "mem.py").read_text())

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

    def test_doc_invalid_writes_preserve_existing_document(self):
        did = self.write(
            "INSERT INTO documents (kind,seq,title,body,render_path) "
            "VALUES ('doc',1,'validation original','original body','docs_sc/validation.md')"
        )
        for payload in ({"body": ""}, {"body": " \n\t"},
                        {"body": None}, {"render_path": "README.md"},
                        {"render_path": "/tmp/doc.md"}, {"render_path": "docs_sc"},
                        {"render_path": "docs_sc/../README.md"}):
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit):
                    mem._api("PATCH", f"/_sc/mem/docs/{did}",
                             {"title": "must not commit", **payload})
                self.assertEqual(tuple(self.q(
                    "SELECT title,body,render_path FROM documents WHERE document_id=?", did
                )), ("validation original", "original body", "docs_sc/validation.md"))

    def test_doc_invalid_adds_leave_no_row(self):
        for payload in ({"body": ""}, {"body": " \n"}, {"body": None},
                        {"body": "content", "render_path": "README.md"},
                        {"body": "content", "render_path": "specs_sc/wrong.md"}):
            with self.subTest(payload=payload):
                before = self.q("SELECT COUNT(*) FROM documents")[0]
                with self.assertRaises(SystemExit):
                    mem._api("POST", "/_sc/mem/docs", {
                        "kind": "doc", "title": "invalid add", **payload,
                    })
                self.assertEqual(self.q("SELECT COUNT(*) FROM documents")[0], before)

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

    # ── agent ergonomics: --json writes ───────────────────────────────────────
    def test_every_leaf_verb_accepts_json(self):
        def leaves(parser, trail=()):
            subs = [a for a in parser._actions
                    if isinstance(a, mem.argparse._SubParsersAction)]
            if not subs:
                yield trail, parser
            for action in subs:
                for name, child in action.choices.items():
                    yield from leaves(child, trail + (name,))
        seen = 0
        for trail, parser in leaves(mem.build_parser()):
            seen += 1
            self.assertIn("--json", parser._option_string_actions, " ".join(trail))
        self.assertGreater(seen, 30)

    def test_json_writes_return_one_object_with_created_ids(self):
        rc, out = self.run_json("roadmap", "add", "feat json", "--status", "next")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat json'")[0]
        self.assertEqual((rc, out["ok"], out["feature_id"]), (0, True, fid))
        self.assertIn(f"#{fid}", out["message"])
        self.assertNotIn("via engine API", json.dumps(out))

        body = self.tmp / "json.md"
        body.write_text("# json\nbody\n")
        rc, out = self.run_json("doc", "add", "spec json", "--body-file", str(body),
                                "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec json'")[0]
        self.assertEqual((rc, out["document_id"], out["kind"]), (0, did, "spec"))
        self.assertNotIn("warning", out)

        rc, out = self.run_json("task", "add", "task json", "--doc", str(did))
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='task json'")[0]
        self.assertEqual(
            (out["task_id"], out["feature_id"], out["document_id"], out["seq"]),
            (tid, fid, did, 1))

        rc, out = self.run_json("decision", "json decision", "--feature", str(fid))
        self.assertEqual(
            out["decision_id"],
            self.q("SELECT decision_id FROM shell_decisions "
                   "WHERE decision='json decision'")[0])
        self.assertEqual(out["feature_id"], fid)

        rc, out = self.run_json("flag", "open", "[x] json flag | Blocker for: y",
                                "--name", "SC-JSON")
        flag_id = self.q("SELECT flag_id FROM flags WHERE display_name='SC-JSON'")[0]
        self.assertEqual((out["flag_id"], out["display_name"]), (flag_id, "SC-JSON"))
        # close prints the row before it writes — under --json that prose is
        # gone and the row identity rides in the object instead
        rc, out = self.run_json("flag", "close", str(flag_id), "--notes", "done")
        # … uniformly the plural list, with the full identifying row
        self.assertEqual(out["flag_ids"], [flag_id])
        self.assertNotIn("flag_id", out)
        self.assertEqual(out["resolution_notes"], "done")
        self.assertEqual(out["flags"], [{
            "flag_id": flag_id, "display_name": "SC-JSON", "priority": "Medium",
            "owner": "tc", "created_date": self.server_today(),
            "feature_id": None, "feature_title": None,
            "description": "[x] json flag | Blocker for: y"}])

        rc, out = self.run_json("message", "send", "peer", "json hello")
        self.assertEqual(
            out["message_id"],
            self.q("SELECT message_id FROM shell_messages WHERE body='json hello'")[0])
        self.assertEqual((out["to"], out["kind"], out["duplicate"]),
                         ("peer", "shell", False))

        rc, out = self.run_json("seed", "json seed moment")
        self.assertEqual(
            out["entry_id"],
            self.q("SELECT entry_id FROM shell_identity_entries "
                   "WHERE body='json seed moment'")[0])
        rc, out = self.run_json("retire", str(out["entry_id"]))
        self.assertTrue(out["retired"])
        for argv in (("state", "json state"), ("narrative", "json beat"),
                     ("curated",), ("which",)):
            rc, out = self.run_json(*argv)
            self.assertEqual((rc, out["ok"]), (0, True), argv)

    def test_default_write_output_is_unchanged_prose(self):
        rc, out = self.run_out("state", "prose state")
        self.assertEqual(out, "mem: current_state updated (11 chars)\n"
                              "  (via engine API — live in the shared engine DB)\n")

    def test_help_documents_the_exit_status_table(self):
        rendered = mem.build_parser().format_help()
        self.assertIn("exit status:", rendered)
        for status in ("  0  success", "  1  refused or failed", "  2  usage error"):
            self.assertIn(status, rendered)
        self.assertIn("Exit status", mem.__doc__)

    def test_json_failure_is_one_object_on_stdout_with_the_same_status(self):
        # the API answered an error
        rc, payload = self.fails_json("retire", "999999")
        self.assertEqual(rc, 1)
        self.assertEqual((payload["error"]["code"], payload["error"]["http_status"]),
                         ("api_error", 404))
        # the client refused the arguments
        rc, payload = self.fails_json("lns", "a rule with no triage")
        self.assertEqual((rc, payload["error"]["code"]), (1, "refused"))
        self.assertIn("--supersedes", payload["error"]["message"])
        # argparse itself refused the command line
        rc, payload = self.fails_json("flag", "edit", "not-a-number")
        self.assertEqual((rc, payload["error"]["code"], payload["error"]["http_status"]),
                         (2, "usage", None))
        # a read under --json follows the same contract
        rc, payload = self.fails_json("get", "flags", "999999")
        self.assertEqual((rc, payload["error"]["http_status"]), (1, 404))
        # without --json a failure prints nothing on stdout
        rc, out, err = self.fails("retire", "999999")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("HTTP 404", err)

    def test_json_keys_are_stable_null_not_omitted(self):
        rc, out = self.run_json("flag", "open", "[x] unnamed json | Blocker for: y")
        self.assertIn("display_name", out)
        self.assertIsNone(out["display_name"])
        rc, out = self.run_json("decision", "json decision without a feature")
        self.assertIn("feature_id", out)
        self.assertIsNone(out["feature_id"])
        rc, out = self.run_json("flag", "close", str(
            self.q("SELECT flag_id FROM flags WHERE description="
                   "'[x] unnamed json | Blocker for: y'")[0]))
        self.assertIsNone(out["resolution_notes"])
        self.assertIsNone(out["flags"][0]["display_name"])

    def test_missing_body_is_a_usage_error_like_argparse(self):
        for argv in (("state",), ("narrative",), ("seed",), ("lns", "--new"),
                     ("decision",), ("flag", "open"), ("message", "send", "peer")):
            rc, out, err = self.fails(*argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("is required", err, argv)
            self.assertIn("--body-file PATH|-", err, argv)
        rc, payload = self.fails_json("state")
        self.assertEqual((rc, payload["error"]["code"]), (2, "usage"))

    # ── agent ergonomics: one read grammar ────────────────────────────────────
    def test_get_documents_positional_id_equals_doc_flag(self):
        self.run_mem("roadmap", "add", "feat grammar")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat grammar'")[0]
        body = self.tmp / "grammar.md"
        body.write_text("# grammar\nGRAMMAR-BODY\n")
        self.run_mem("doc", "add", "spec grammar", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec grammar'")[0]
        _, by_flag = self.run_out("get", "documents", "--doc", str(did))
        for form in (("documents", str(did)), ("document", str(did)),
                     ("doc", str(did)), ("documents", str(did), "--doc", str(did))):
            rc, out = self.run_out("get", *form)
            self.assertEqual((rc, out), (0, by_flag), form)
        self.assertIn("GRAMMAR-BODY", by_flag)
        self.assertIn("name two documents",
                      self.refusal("get", "documents", str(did), "--doc", str(did + 1)))

    def test_get_roadmap_single_feature_reads_retired_with_deps_and_docs(self):
        self.run_mem("project", "add", "wsone", "One WS")
        self.run_mem("roadmap", "add", "feat base")
        self.run_mem("roadmap", "add", "feat single", "--summary", "the summary",
                     "--project", "wsone")
        base = self.q("SELECT feature_id FROM roadmap WHERE title='feat base'")[0]
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat single'")[0]
        self.run_mem("roadmap", "depends", str(fid), "--on", str(base))
        body = self.tmp / "single.md"
        body.write_text("# single\nbody\n")
        self.run_mem("doc", "add", "spec single", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec single'")[0]
        self.run_mem("roadmap", "status", str(fid), "retired")

        rc, out = self.run_out("get", "roadmap", str(fid), "--json")
        feature = json.loads(out)["feature"]
        self.assertEqual(
            (feature["feature_id"], feature["title"], feature["roadmap_status"],
             feature["project"], feature["blocked_by"]),
            (fid, "feat single", "retired", "wsone", [base]))
        self.assertEqual([d["document_id"] for d in feature["documents"]], [did])
        rc, out = self.run_out("get", "feature", str(fid))
        self.assertEqual(rc, 0)
        self.assertIn(f"#{fid} [retired] feat single", out)
        self.assertIn("work-stream: wsone", out)
        self.assertIn(f"depends on: #{base}", out)
        self.assertIn(f"spec #{did}", out)
        # the board itself is unchanged: retired stays hidden from the list
        _, board = self.run_out("get", "roadmap")
        self.assertNotIn("feat single", board)
        self.assertIn("no such feature", self.refusal("get", "roadmap", "999999"))

    def test_get_flags_feature_alone_lists_that_features_open_flags(self):
        self.run_mem("roadmap", "add", "feat flags A")
        self.run_mem("roadmap", "add", "feat flags B")
        a = self.q("SELECT feature_id FROM roadmap WHERE title='feat flags A'")[0]
        b = self.q("SELECT feature_id FROM roadmap WHERE title='feat flags B'")[0]
        self.run_mem("flag", "open", "[x] a-open | Blocker for: a", "--name",
                     "SC-FA-OPEN", "--feature", str(a))
        self.run_mem("flag", "open", "[x] a-done | Blocker for: a", "--name",
                     "SC-FA-DONE", "--feature", str(a))
        self.run_mem("flag", "open", "[x] b-open | Blocker for: b", "--name",
                     "SC-FB-OPEN", "--feature", str(b))
        done = self.q("SELECT flag_id FROM flags WHERE display_name='SC-FA-DONE'")[0]
        self.run_mem("flag", "close", str(done), "--notes", "cleared")

        rc, out = self.run_out("get", "flags", "--feature", str(a), "--json")
        self.assertEqual([f["display_name"] for f in json.loads(out)["flags"]],
                         ["SC-FA-OPEN"])
        rc, out = self.run_out("get", "flag", "--feature", str(a))
        self.assertIn("SC-FA-OPEN", out)
        self.assertNotIn("SC-FB-OPEN", out)
        self.assertNotIn("SC-FA-DONE", out)
        # history keeps its own explicit form
        rc, out = self.run_out("get", "flags", "--feature", str(a), "--resolved",
                               "--json")
        self.assertEqual([f["display_name"] for f in json.loads(out)["flags"]],
                         ["SC-FA-DONE"])
        rc, out = self.run_out("get", "flags", "--feature", "999999")
        self.assertIn("no matching flags", out)

    def test_get_id_on_every_surface_reads_one_row_or_names_the_form(self):
        self.run_mem("roadmap", "add", "feat ids")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat ids'")[0]
        body = self.tmp / "ids.md"
        body.write_text("# ids\nbody\n")
        self.run_mem("doc", "add", "spec ids", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec ids'")[0]
        self.run_mem("task", "add", "task ids one", "--doc", str(did))
        self.run_mem("task", "add", "task ids two", "--doc", str(did))
        tid = self.q("SELECT task_id FROM spec_tasks WHERE title='task ids two'")[0]
        rc, out = self.run_out("get", "task", str(tid))
        self.assertIn(f"#{tid} seq 2 [pending] task ids two", out)
        self.assertIn(f"feature #{fid} · doc #{did}", out)
        self.assertNotIn("task ids one", out)
        rc, out = self.run_out("get", "tasks", str(tid), "--json")
        self.assertEqual(json.loads(out)["task"]["document_id"], did)
        self.assertIn("no such task", self.refusal("get", "tasks", "999999"))

        rc, out = self.run_out("get", "shells", "2")
        self.assertEqual(out.strip(), "#2 peer — Peer")
        rc, out = self.run_out("get", "shell", "3", "--json")
        self.assertEqual([x["shortname"] for x in json.loads(out)["shells"]], ["rev1"])
        self.run_mem("seed", "pickable seed")
        entry = self.q("SELECT entry_id FROM shell_identity_entries "
                       "WHERE body='pickable seed'")[0]
        rc, out = self.run_out("get", "seed", str(entry))
        self.assertIn("pickable seed", out)
        self.assertIn("no shells row #999", self.refusal("get", "shells", "999"))

        # single-value surfaces refuse an id and say what to run instead
        message = self.refusal("get", "state", "4")
        self.assertIn("one value", message)
        self.assertIn("`get state`", message)
        # a scope-less task read names every accepted form
        message = self.refusal("get", "tasks")
        for form in ("get tasks <task_id>", "--doc <document_id>",
                     "--feature <feature_id>"):
            self.assertIn(form, message)
        self.assertIn("get qaqc <spec_document_id>", self.refusal("get", "qaqc"))
        rc, _ = self.run_out("get", "qaqc", str(did))
        self.assertEqual(rc, 0)

    # ── agent ergonomics: bodies from a file or stdin ─────────────────────────
    def test_body_file_and_stdin_feed_every_text_verb(self):
        note = self.tmp / "note.txt"
        note.write_text("line one\n\"quoted\" $HOME `tick`\n\n")
        want = "line one\n\"quoted\" $HOME `tick`"

        self.run_mem("message", "send", "peer", "--body-file", str(note))
        self.assertIsNotNone(
            self.q("SELECT 1 FROM shell_messages WHERE body=? AND to_shell_id=2", want))
        self.run_out("message", "send", "peer", "--body-file", "-",
                     stdin="from stdin\n")
        self.assertIsNotNone(
            self.q("SELECT 1 FROM shell_messages WHERE body='from stdin'"))

        self.run_out("narrative", "--body-file", "-", stdin="stdin beat\n")
        self.assertIn("stdin beat", self.q(
            "SELECT full_narrative FROM shell_memory_archives WHERE archive_id=1")[0])

        rationale = self.tmp / "why.txt"
        rationale.write_text("because of the file\n")
        self.run_out("decision", "--body-file", "-", "--rationale-file",
                     str(rationale), stdin="decide from stdin\n")
        row = self.q("SELECT rationale FROM shell_decisions "
                     "WHERE decision='decide from stdin'")
        self.assertEqual(row[0], "because of the file")

        self.run_out("lns", "--new", "--body-file", "-", stdin="Rule from stdin.\n")
        self.assertIsNotNone(self.q(
            "SELECT 1 FROM shell_identity_entries WHERE body='Rule from stdin.'"))

        self.run_mem("flag", "open", "--body-file", str(note), "--name", "SC-BF")
        flag_id = self.q("SELECT flag_id FROM flags WHERE display_name='SC-BF'")[0]
        self.assertEqual(
            self.q("SELECT description FROM flags WHERE flag_id=?", flag_id)[0], want)
        self.run_out("flag", "edit", str(flag_id), "--append-file", "-",
                     stdin="APPENDED\n")
        self.assertIn("APPENDED", self.q(
            "SELECT description FROM flags WHERE flag_id=?", flag_id)[0])
        self.run_out("flag", "close", str(flag_id), "--notes-file", "-",
                     stdin="closed from stdin\n")
        self.assertEqual(
            self.q("SELECT resolution_notes FROM flags WHERE flag_id=?", flag_id)[0],
            "closed from stdin")

        self.run_mem("roadmap", "add", "feat stdin doc")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat stdin doc'")[0]
        self.run_out("doc", "add", "spec stdin", "--body-file", "-", "--feature",
                     str(fid), stdin="# stdin spec\nSTDIN-BODY\n")
        did, body = self.q(
            "SELECT document_id, body FROM documents WHERE title='spec stdin'")
        self.assertIn("STDIN-BODY", body)
        self.run_out("doc", "edit", str(did), "--body-file", "-",
                     stdin="# stdin spec\nEDITED-BODY\n")
        self.assertIn("EDITED-BODY", self.q(
            "SELECT body FROM documents WHERE document_id=?", did)[0])

    def test_body_file_refusals_name_the_fix_and_write_nothing(self):
        note = self.tmp / "both.txt"
        note.write_text("from file\n")
        rc, _, err = self.fails(
            "message", "send", "peer", "inline", "--body-file", str(note))
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)
        self.assertIn("cannot read", self.refusal(
            "narrative", "--body-file", str(self.tmp / "absent.txt")))
        self.assertIn("is empty", self.refusal(
            "narrative", "--body-file", "-", stdin="\n\n"))
        self.assertIn("only one argument", self.refusal(
            "decision", "--body-file", "-", "--rationale-file", "-", stdin="x\n"))
        self.assertIsNone(self.q(
            "SELECT 1 FROM shell_messages WHERE body IN ('inline', 'from file')"))

    # ── agent ergonomics: fewer round-trips ───────────────────────────────────
    def test_inbox_names_the_sender_by_shortname(self):
        with mock.patch.object(mem, "SC_API_TOKEN", PEER_TOKEN):
            self.run_mem("message", "send", "tc", "who sent this")
        mid = self.q("SELECT message_id FROM shell_messages "
                     "WHERE body='who sent this'")[0]
        rc, out = self.run_out("message", "check")
        self.assertIn(f"[#{mid}] from shell #2 (peer) · ", out)
        rc, out = self.run_out("get", "messages")
        self.assertIn(f"[#{mid}] from shell #2 (peer) · ", out)
        rc, out = self.run_json("message", "check")
        row = next(m for m in out["messages"] if m["message_id"] == mid)
        self.assertEqual(row["from_shortname"], "peer")
        self.run_mem("message", "mark-read", str(mid))

    def test_message_check_limit_bounds_the_listing_not_the_count(self):
        with mock.patch.object(mem, "SC_API_TOKEN", REVIEW_TOKEN):
            for m in mem._api("GET", "/_sc/mem/messages")["messages"]:
                mem._api("PATCH", f"/_sc/mem/messages/{m['message_id']}/read")
        for n in range(3):
            self.run_mem("message", "send", "rev1", f"limit probe {n}")
        with mock.patch.object(mem, "SC_API_TOKEN", REVIEW_TOKEN):
            rc, out = self.run_out("message", "check", "2")
            self.assertIn("mem: 3 unread (showing the newest 2):", out)
            self.assertEqual(out.count("limit probe"), 2)
            rc, out = self.run_out("message", "check")
            self.assertIn("mem: 3 unread:", out)
            self.assertEqual(out.count("limit probe"), 3)
            rc, out = self.run_json("message", "check", "1")
            self.assertEqual((out["unread"], len(out["messages"])), (3, 1))
            # 0 / negative N is a usage error (status 2), not a silent
            # "everything" and not a status-1 refusal
            for bad in ("0", "-3"):
                rc, out, err = self.fails("message", "check", bad)
                self.assertEqual((rc, out), (2, ""), bad)
                self.assertIn("usage:", err)
            rc, payload = self.fails_json("message", "check", "0")
            self.assertEqual((rc, payload["error"]["code"]), (2, "usage"))
            self.assertIn("positive count", payload["error"]["message"])
            # several ids in one call
            ids = [m["message_id"] for m in
                   mem._api("GET", "/_sc/mem/messages")["messages"]]
            rc, out = self.run_out("message", "mark-read", *map(str, ids))
            self.assertIn("mem: messages " + ", ".join(f"#{i}" for i in ids)
                          + " marked read", out)
            rc, out = self.run_out("message", "check")
            self.assertEqual(out, "mem: inbox empty\n")

    def test_mark_read_batch_failure_reports_applied_and_not_applied(self):
        ids = []
        for n in range(2):
            with mock.patch.object(mem, "SC_API_TOKEN", PEER_TOKEN):
                self.run_mem("message", "send", "tc", f"batch probe {n}")
            ids.append(self.q("SELECT message_id FROM shell_messages "
                              "WHERE body=?", f"batch probe {n}")[0])
        first, second = ids
        rc, payload = self.fails_json(
            "message", "mark-read", str(first), "999999", str(second))
        self.assertEqual(rc, 1)
        self.assertEqual(payload["error"]["http_status"], 404)
        self.assertEqual(payload["applied_ids"], [first])
        self.assertEqual(payload["not_applied_ids"], [999999, second])
        self.assertIn(f"applied: #{first}; not applied: #999999, #{second}",
                      payload["error"]["message"])
        self.assertIsNotNone(self.q(
            "SELECT read_at FROM shell_messages WHERE message_id=?", first)[0])
        self.assertIsNone(self.q(
            "SELECT read_at FROM shell_messages WHERE message_id=?", second)[0])
        # the prose form names the same split on stderr
        rc, out, err = self.fails("message", "mark-read", "999999", str(second))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn(f"applied: none; not applied: #999999, #{second}", err)
        # success: always the plural list, never a lone message_id
        rc, out = self.run_json("message", "mark-read", str(second))
        self.assertEqual(out["message_ids"], [second])
        self.assertNotIn("message_id", out)

    def test_inbox_newest_first_is_stable_within_one_second(self):
        with mock.patch.object(mem, "SC_API_TOKEN", REVIEW_TOKEN):
            for m in mem._api("GET", "/_sc/mem/messages")["messages"]:
                mem._api("PATCH", f"/_sc/mem/messages/{m['message_id']}/read")
        stamp = "2031-01-01 00:00:00"      # three rows inside one second
        ids = [self.write(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, kind, body, "
            "created_at) VALUES (1, 3, 'shell', ?, ?)", f"tie {n}", stamp)
            for n in range(3)]
        with mock.patch.object(mem, "SC_API_TOKEN", REVIEW_TOKEN):
            rc, out = self.run_json("message", "check", "1")
            self.assertEqual([m["message_id"] for m in out["messages"]], [ids[-1]])
            rc, out = self.run_json("message", "check")
            self.assertEqual([m["message_id"] for m in out["messages"]], ids[::-1])
            self.run_mem("message", "mark-read", *map(str, ids))

    def test_flag_append_file_starts_on_a_new_line(self):
        self.run_mem("flag", "open", "a\nb", "--name", "SC-APPEND-NL")
        flag_id = self.q(
            "SELECT flag_id FROM flags WHERE display_name='SC-APPEND-NL'")[0]
        note = self.tmp / "append.txt"
        note.write_text("appended\n")
        self.run_mem("flag", "edit", str(flag_id), "--append-file", str(note))
        self.run_out("flag", "edit", str(flag_id), "--body-file", "-",
                     stdin="again\n")
        self.assertEqual(
            self.q("SELECT description FROM flags WHERE flag_id=?", flag_id)[0],
            "a\nb\nappended\nagain")
        # inline --append stays byte-exact, as on main
        self.run_mem("flag", "edit", str(flag_id), "--append", " glued")
        self.assertTrue(self.q(
            "SELECT description FROM flags WHERE flag_id=?", flag_id)[0]
            .endswith("again glued"))

    def test_flag_close_missing_id_prints_no_closing_line_and_closes_nothing(self):
        self.run_mem("flag", "open", "[x] precheck | Blocker for: z",
                     "--name", "SC-PRECHECK")
        flag_id = self.q(
            "SELECT flag_id FROM flags WHERE display_name='SC-PRECHECK'")[0]
        rc, out, err = self.fails("flag", "close", str(flag_id), "999999")
        self.assertEqual((rc, out), (1, ""))      # no "closing flag …" line
        self.assertIn("no such flag #999999 — nothing was closed", err)
        self.assertEqual(
            self.q("SELECT resolved FROM flags WHERE flag_id=?", flag_id)[0], 0)
        rc, payload = self.fails_json("flag", "close", str(flag_id), "999999")
        self.assertEqual(payload["error"]["http_status"], 404)
        self.assertEqual(payload["applied_ids"], [])
        self.assertEqual(payload["not_applied_ids"], [flag_id, 999999])
        self.run_mem("flag", "close", str(flag_id))

    def test_task_add_refuses_feature_mismatch_and_duplicate_seq_cleanly(self):
        self.run_mem("roadmap", "add", "feat own")
        self.run_mem("roadmap", "add", "feat other")
        own = self.q("SELECT feature_id FROM roadmap WHERE title='feat own'")[0]
        other = self.q("SELECT feature_id FROM roadmap WHERE title='feat other'")[0]
        body = self.tmp / "own.md"
        body.write_text("# own\nbody\n")
        self.run_mem("doc", "add", "spec own", "--body-file", str(body),
                     "--feature", str(own))
        did = self.q("SELECT document_id FROM documents WHERE title='spec own'")[0]
        rc, payload = self.fails_json("task", "add", "wrong feature", "--doc",
                                      str(did), "--feature", str(other))
        self.assertEqual(payload["error"]["http_status"], 409)
        self.assertIn(f"belongs to feature #{own}, not #{other}",
                      payload["error"]["message"])
        self.run_mem("task", "add", "seq one", "--doc", str(did), "--seq", "1")
        rc, payload = self.fails_json("task", "add", "seq clash", "--doc",
                                      str(did), "--seq", "1")
        self.assertEqual(payload["error"]["http_status"], 409)
        self.assertIn("seq 1 is already taken", payload["error"]["message"])
        self.assertIn("next free slot (2)", payload["error"]["message"])
        self.assertNotIn("UNIQUE", payload["error"]["message"])
        self.assertIsNone(self.q("SELECT 1 FROM spec_tasks WHERE title IN "
                                 "('wrong feature', 'seq clash')"))
        # a frozen spec's plan is still worked: inference is unchanged by freeze
        self.run_mem("doc", "freeze", str(did))
        rc, out = self.run_json("task", "add", "after freeze", "--doc", str(did))
        self.assertEqual((out["feature_id"], out["seq"]), (own, 2))

    def test_flag_close_several_ids_checks_all_before_closing_any(self):
        ids = []
        for name in ("SC-MULTI-A", "SC-MULTI-B", "SC-MULTI-C"):
            self.run_mem("flag", "open", f"[x] {name} | Blocker for: z", "--name", name)
            ids.append(self.q("SELECT flag_id FROM flags WHERE display_name=?", name)[0])
        a, b, c = ids
        self.run_mem("flag", "close", str(c), "--notes", "first closer")
        # one already-closed id refuses the whole call — nothing is written
        message = self.refusal("flag", "close", str(a), str(c), str(b), "--notes", "x")
        self.assertIn("already closed", message)
        self.assertIn("none of the flags named in this call were closed", message)
        for flag_id in (a, b):
            self.assertEqual(
                self.q("SELECT resolved FROM flags WHERE flag_id=?", flag_id)[0], 0)
        self.assertEqual(
            self.q("SELECT resolution_notes FROM flags WHERE flag_id=?", c)[0],
            "first closer")
        rc, out = self.run_out("flag", "close", str(a), str(b), str(a),
                               "--notes", "swept")
        self.assertEqual(rc, 0)
        self.assertIn(f"mem: closing flag #{a} (SC-MULTI-A)", out)
        self.assertIn(f"mem: 2 flags closed: #{a} (SC-MULTI-A), #{b} (SC-MULTI-B)", out)
        for flag_id in (a, b):
            self.assertEqual(
                tuple(self.q("SELECT resolved, resolution_notes FROM flags "
                             "WHERE flag_id=?", flag_id)), (1, "swept"))

    def test_task_add_infers_feature_and_advances_seq(self):
        self.run_mem("roadmap", "add", "feat infer")
        fid = self.q("SELECT feature_id FROM roadmap WHERE title='feat infer'")[0]
        body = self.tmp / "infer.md"
        body.write_text("# infer\nbody\n")
        self.run_mem("doc", "add", "spec infer", "--body-file", str(body),
                     "--feature", str(fid))
        did = self.q("SELECT document_id FROM documents WHERE title='spec infer'")[0]
        rc, out = self.run_out("task", "add", "infer one", "--doc", str(did))
        self.assertIn("added (seq 1, 'infer one')", out)
        # the explicit form still works exactly as before
        self.run_mem("task", "add", "infer five", "--feature", str(fid),
                     "--doc", str(did), "--seq", "5")
        self.run_mem("task", "add", "infer next", "--doc", str(did))
        got = {r["title"]: (r["feature_id"], r["seq"]) for r in (
            self.q("SELECT title, feature_id, seq FROM spec_tasks WHERE title=?", t)
            for t in ("infer one", "infer five", "infer next"))}
        self.assertEqual(got, {"infer one": (fid, 1), "infer five": (fid, 5),
                               "infer next": (fid, 6)})
        self.assertIn("no such document",
                      self.refusal("task", "add", "orphan", "--doc", "999999"))
        self.run_mem("doc", "add", "standalone infer", "--body-file", str(body),
                     "--kind", "doc")
        loose = self.q(
            "SELECT document_id FROM documents WHERE title='standalone infer'")[0]
        self.assertIn("has no feature",
                      self.refusal("task", "add", "loose", "--doc", str(loose)))
        self.assertIsNone(
            self.q("SELECT 1 FROM spec_tasks WHERE title IN ('orphan', 'loose')"))

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
        for alias, surface in mem.GET_SURFACE_ALIASES.items():
            self.assertIn(surface, mem.GET_SURFACES, alias)
            if surface == "tasks":      # needs a scope, like the plural form
                self.assertEqual(
                    self.run_mem("get", alias, "--feature", "0"), 0, alias)
                continue
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

    # ── document retirement + supersession (spec #251) ────────────────────────
    # One row of the spec's Behavior table per assertion block.

    def _retirement_feature(self):
        """One shared feature for retirement fixtures.

        These rows must not land in the NULL-feature seq scope: the class DB is
        shared, and `doc add` without --feature auto-advances seq within it.
        """
        row = self.q("SELECT feature_id FROM roadmap "
                     "WHERE title='retirement fixtures'")
        if row is not None:
            return row[0]
        return self.write(
            "INSERT INTO roadmap (title,roadmap_status,sort_order,owning_shell) "
            "VALUES ('retirement fixtures','in_progress',950,4)")

    def _doc(self, title, **columns):
        """A throwaway document row; every column not named takes its default."""
        columns.setdefault("kind", "doc")
        columns.setdefault("feature_id", self._retirement_feature())
        columns.setdefault("body", f"# {title}\n")
        columns.setdefault("seq", self.q(
            "SELECT COALESCE(MAX(seq),0)+1 FROM documents "
            "WHERE feature_id=? AND kind=?",
            columns["feature_id"], columns["kind"])[0])
        names = ["title", *columns]
        sql = (f"INSERT INTO documents ({','.join(names)}) "
               f"VALUES ({','.join('?' for _ in names)})")
        return self.write(sql, title, *columns.values())

    def _retire(self, document_id, **payload):
        return mem._api("PATCH", f"/_sc/mem/docs/{document_id}/retire", payload)

    def _row(self, document_id):
        return self.q(
            "SELECT title,body,frozen,frozen_date,retired,retired_date,"
            "superseded_by FROM documents WHERE document_id=?", document_id)

    def test_retire_targets_any_document_and_records_the_date(self):
        successor = self._doc("retire successor A")
        target = self._doc("retire target A")
        result = self._retire(target, superseded_by=successor)
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_retired"])
        row = self._row(target)
        self.assertEqual(row["retired"], 1)
        self.assertEqual(row["superseded_by"], successor)
        self.assertEqual(row["retired_date"], self.server_today())

    def test_bare_retire_is_legal_and_leaves_no_successor(self):
        target = self._doc("retire bare B")
        self.assertTrue(self._retire(target)["ok"])
        row = self._row(target)
        self.assertEqual(row["retired"], 1)
        self.assertIsNone(row["superseded_by"])

    def test_retire_never_touches_title_body_or_freeze_state(self):
        # A3: retirement is metadata ABOUT a frozen document, never an edit —
        # on the first retire, on a re-point, and on an undo alike.
        successor = self._doc("retire successor C")
        other = self._doc("retire successor C2")
        frozen = self._doc(
            "retire frozen C", frozen=1, frozen_date="2026-01-02",
            body="# frozen C\nimmutable body\n")
        before = tuple(self._row(frozen))[:4]

        self._retire(frozen, superseded_by=successor)
        self.assertEqual(tuple(self._row(frozen))[:4], before)
        self.assertEqual(self._row(frozen)["retired"], 1)

        self._retire(frozen, superseded_by=other)
        self.assertEqual(tuple(self._row(frozen))[:4], before)
        self.assertEqual(self._row(frozen)["superseded_by"], other)

        self._retire(frozen, undo=True)
        self.assertEqual(tuple(self._row(frozen))[:4], before)
        self.assertEqual(self._row(frozen)["retired"], 0)

    def test_a_replayed_bare_retire_never_drops_the_successor(self):
        # The lost-receipt path: `sc mem doc retire 204` runs, the response is
        # lost, the operator re-runs it. Naming no successor must read as the
        # no-op it is, never as "clear the pointer the first call wrote".
        successor = self._doc("retire replay successor O")
        target = self._doc("retire replay target O")
        self._retire(target, superseded_by=successor)

        replay = self._retire(target)
        self.assertTrue(replay["already_retired"])
        self.assertNotIn("repointed", replay)
        self.assertEqual(replay["superseded_by"], successor)
        self.assertEqual(self._row(target)["superseded_by"], successor)

        # Same for a body with no keys at all — a malformed or empty payload
        # is not an instruction to clear anything.
        empty = self._retire(target)
        self.assertTrue(empty["already_retired"])
        self.assertEqual(self._row(target)["superseded_by"], successor)

        # The CLI sends the key only when --superseded-by was given.
        self.assertEqual(self.run_mem("doc", "retire", str(target)), 0)
        self.assertEqual(self._row(target)["superseded_by"], successor)

        # An EXPLICIT null is still a request to clear it.
        cleared = self._retire(target, superseded_by=None)
        self.assertTrue(cleared["repointed"])
        self.assertIsNone(cleared["superseded_by"])
        self.assertIsNone(self._row(target)["superseded_by"])
        self.assertEqual(self._row(target)["retired"], 1)

    def test_undo_with_a_successor_key_is_refused_not_ignored(self):
        target = self._doc("retire undo conflict P")
        successor = self._doc("retire undo conflict successor P")
        self._retire(target, superseded_by=successor)
        for payload in ({"undo": True, "superseded_by": successor},
                        {"undo": True, "superseded_by": None}):
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit) as caught:
                    self._retire(target, **payload)
                self.assertIn("400", str(caught.exception))
                self.assertIn("it cannot also set a successor",
                              str(caught.exception))
        self.assertEqual(self._row(target)["retired"], 1)
        self.assertEqual(self._row(target)["superseded_by"], successor)

    def test_a_retired_document_is_read_only_for_content_everywhere(self):
        # Planner ruling on spec #251: a reader following an old link must find
        # what was retired, not a quietly revised version of it. render_path is
        # a location, not content, so it still moves — the frozen rule's shape.
        successor = self._doc("retire readonly successor Q")
        target = self._doc("retire readonly target Q",
                           render_path="docs_sc/retire-readonly-q.md")
        self._retire(target, superseded_by=successor)
        body = self.tmp / "retired-edit.md"
        body.write_text("# rewritten\n")

        for payload in ({"title": "rewritten"}, {"body": "# rewritten\n"}):
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit) as caught:
                    mem._api("PATCH", f"/_sc/mem/docs/{target}", payload)
                self.assertIn("409", str(caught.exception))
                self.assertIn("document is retired", str(caught.exception))
        with self.assertRaises(SystemExit):
            self.run_mem("doc", "edit", str(target), "--body-file", str(body))
        row = self._row(target)
        self.assertEqual(row["title"], "retire readonly target Q")
        self.assertEqual(row["body"], "# retire readonly target Q\n")

        # render_path still moves, and the edit works again after an undo.
        self.assertEqual(
            self.run_mem("doc", "edit", str(target),
                         "--render-path", "docs_sc/retire-readonly-q2.md"), 0)
        self.assertEqual(self.run_mem("doc", "retire", str(target), "--undo"), 0)
        self.assertEqual(
            self.run_mem("doc", "edit", str(target), "--body-file", str(body)), 0)
        self.assertEqual(self._row(target)["body"], "# rewritten\n")

    def test_doc_edit_help_names_the_retired_rule(self):
        self.assertIn("frozen or retired: --render-path only", mem.__doc__)
        parser = mem.build_parser()
        doc = next(
            action for action in parser._actions
            if isinstance(action, mem.argparse._SubParsersAction)
        ).choices["doc"]
        self.assertIn("frozen or retired: render-path only",
                      " ".join(doc.format_help().split()))

    def test_retire_refuses_an_unfrozen_spec_with_an_open_plan(self):
        self.run_mem("roadmap", "add", "retire plan feature")
        feature = self.q(
            "SELECT feature_id FROM roadmap WHERE title='retire plan feature'")[0]
        spec = self._doc("retire open plan D", kind="spec", feature_id=feature)
        self.write(
            "INSERT INTO spec_tasks (shell_id,feature_id,document_id,seq,title,status) "
            "VALUES (1,?,?,1,'unfinished','in_progress')", feature, spec)
        with self.assertRaises(SystemExit) as caught:
            self._retire(spec)
        self.assertIn("409", str(caught.exception))
        self.assertIn("unfinished task", str(caught.exception))
        self.assertEqual(self._row(spec)["retired"], 0)

        # A plan that is entirely terminal is no longer open.
        self.write("UPDATE spec_tasks SET status='cancelled' WHERE document_id=?",
                   spec)
        self.assertTrue(self._retire(spec)["ok"])

    def test_retire_refuses_a_spec_bound_to_a_sprint(self):
        self.run_mem("roadmap", "add", "retire bound feature")
        feature = self.q(
            "SELECT feature_id FROM roadmap WHERE title='retire bound feature'")[0]
        spec = self._doc("retire bound spec E", kind="spec", feature_id=feature)
        sprint_id = self.write(
            "INSERT INTO sprints (feature_id,originating_planner_shell_id) "
            "VALUES (?,4)", feature)
        self.write(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256) VALUES (?,?,?)",
            sprint_id, spec, "b" * 64)
        with self.assertRaises(SystemExit) as caught:
            self._retire(spec)
        self.assertIn("409", str(caught.exception))
        self.assertIn(f"Sprint #{sprint_id}", str(caught.exception))
        self.assertEqual(self._row(spec)["retired"], 0)

    def test_retire_refuses_a_missing_self_or_retired_successor(self):
        target = self._doc("retire successor checks F")
        already = self._doc("retire retired successor F")
        self._retire(already)
        refusals = (
            ({"superseded_by": 999999}, "no such successor"),
            ({"superseded_by": target}, "cannot supersede itself"),
            ({"superseded_by": already}, "is itself retired"),
        )
        for payload, message in refusals:
            with self.subTest(message=message):
                with self.assertRaises(SystemExit) as caught:
                    self._retire(target, **payload)
                self.assertIn("409", str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertEqual(self._row(target)["retired"], 0)

    def test_retire_accepts_a_successor_under_another_feature(self):
        self.run_mem("roadmap", "add", "retire era old")
        self.run_mem("roadmap", "add", "retire era new")
        old = self.q("SELECT feature_id FROM roadmap WHERE title='retire era old'")[0]
        new = self.q("SELECT feature_id FROM roadmap WHERE title='retire era new'")[0]
        successor = self._doc("retire cross successor G", feature_id=new)
        target = self._doc("retire cross target G", feature_id=old)
        self.assertTrue(self._retire(target, superseded_by=successor)["ok"])
        self.assertEqual(self._row(target)["superseded_by"], successor)

    def test_retire_is_idempotent_and_repoints_without_moving_the_date(self):
        first = self._doc("retire successor H1")
        second = self._doc("retire successor H2")
        target = self._doc("retire target H")
        self._retire(target, superseded_by=first)
        self.write("UPDATE documents SET retired_date='2020-01-01' "
                   "WHERE document_id=?", target)

        repeat = self._retire(target, superseded_by=first)
        self.assertTrue(repeat["already_retired"])
        self.assertNotIn("repointed", repeat)
        self.assertEqual(self._row(target)["retired_date"], "2020-01-01")

        moved = self._retire(target, superseded_by=second)
        self.assertTrue(moved["already_retired"])
        self.assertTrue(moved["repointed"])
        row = self._row(target)
        self.assertEqual(row["superseded_by"], second)
        self.assertEqual(row["retired_date"], "2020-01-01")

    def test_undo_clears_every_column_and_is_a_noop_when_current(self):
        successor = self._doc("retire successor I")
        target = self._doc("retire target I")
        self._retire(target, superseded_by=successor)
        undone = self._retire(target, undo=True)
        self.assertFalse(undone["already_current"])
        row = self._row(target)
        self.assertEqual(row["retired"], 0)
        self.assertIsNone(row["retired_date"])
        self.assertIsNone(row["superseded_by"])
        # Dependency-free by design: the successor is still retirable, and a
        # second undo is a no-op rather than a refusal.
        self.assertTrue(self._retire(target, undo=True)["already_current"])

    def test_a_retired_successor_resolves_through_the_chain(self):
        head = self._doc("retire chain head J")
        middle = self._doc("retire chain middle J")
        tail = self._doc("retire chain tail J")
        # Tail points at middle while middle is still current; middle is then
        # retired in turn. Readers follow the chain to the head.
        self._retire(tail, superseded_by=middle)
        self._retire(middle, superseded_by=head)
        one = mem._api("GET", f"/_sc/mem/documents/{tail}")["document"]
        self.assertEqual(one["superseded_by"], middle)
        self.assertEqual(one["current_document_id"], head)
        self.assertEqual(one["current_title"], "retire chain head J")

    def test_reads_expose_frozen_date_and_every_retirement_field(self):
        successor = self._doc("retire read successor K")
        target = self._doc("retire read target K", frozen=1,
                           frozen_date="2026-03-04")
        self._retire(target, superseded_by=successor)
        listed = {d["document_id"]: d for d in
                  mem._api("GET", "/_sc/mem/documents")["documents"]}[target]
        one = mem._api("GET", f"/_sc/mem/documents/{target}")["document"]
        for payload in (listed, one):
            self.assertEqual(payload["frozen_date"], "2026-03-04")
            self.assertEqual(payload["retired"], 1)
            self.assertEqual(payload["retired_date"], self.server_today())
            self.assertEqual(payload["superseded_by"], successor)
            self.assertEqual(payload["superseded_by_title"],
                             "retire read successor K")

    def test_get_documents_hides_retired_rows_until_asked(self):
        successor = self._doc("retire listing successor L")
        pointed = self._doc("retire listing pointed L")
        bare = self._doc("retire listing bare L")
        self._retire(pointed, superseded_by=successor)
        self._retire(bare)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_mem("get", "documents"), 0)
        hidden = out.getvalue()
        self.assertNotIn("retire listing pointed L", hidden)
        self.assertNotIn("retire listing bare L", hidden)
        self.assertIn("retired hidden — --retired to show", hidden)
        self.assertIn("retire listing successor L", hidden)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_mem("get", "documents", "--retired"), 0)
        shown = out.getvalue()
        self.assertIn(f"[retired → #{successor}] — retire listing pointed L", shown)
        self.assertIn("[retired] — retire listing bare L", shown)
        self.assertNotIn("retired hidden", shown)

    def test_one_document_read_always_works_and_names_the_successor(self):
        successor = self._doc("retire single successor M")
        target = self._doc("retire single target M", body="# target M\nbody\n")
        self.run_mem("doc", "retire", str(target),
                     "--superseded-by", str(successor))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_mem("get", "documents", "--doc",
                                          str(target)), 0)
        text = out.getvalue()
        self.assertIn(f"[retired → #{successor}]", text)
        self.assertIn(f"retired {self.server_today()} — superseded by "
                      f"#{successor} retire single successor M", text)
        self.assertIn("# target M", text)   # the body still reads

    def test_retire_cli_undo_and_flag_conflict(self):
        successor = self._doc("retire cli successor N")
        target = self._doc("retire cli target N")
        self.assertEqual(
            self.run_mem("doc", "retire", str(target),
                         "--superseded-by", str(successor)), 0)
        self.assertEqual(self._row(target)["retired"], 1)
        self.assertEqual(self.run_mem("doc", "retire", str(target), "--undo"), 0)
        self.assertEqual(self._row(target)["retired"], 0)
        with self.assertRaises(SystemExit) as caught:
            self.run_mem("doc", "retire", str(target), "--undo",
                         "--superseded-by", str(successor))
        self.assertIn("--undo", str(caught.exception))

    def test_retire_unknown_document_is_404(self):
        with self.assertRaises(SystemExit) as caught:
            self._retire(999999)
        self.assertIn("404", str(caught.exception))

    def test_get_retired_flag_is_documents_only(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_mem("get", "flags", "--retired")
        self.assertIn("--retired is only valid with get documents",
                      str(caught.exception))

    def test_retirement_guidance_is_rendered_for_the_planner(self):
        planner = (ENGINE / "templates" / "shells" / "planner.md").read_text()
        self.assertIn("## REVISE, FREEZE, DOCUMENT", planner)
        revise = planner.split("## REVISE, FREEZE, DOCUMENT", 1)[1]
        self.assertIn("Retiring a document is metadata about it", revise)
        self.assertIn("`spec_authoring`", revise)
        detail = ENGINE / "assets" / "skills" / "spec_authoring" / "SKILL.md"
        self.assertIn(
            "sc mem doc retire <id> [--superseded-by <id>]", detail.read_text()
        )
        self.assertIn("sc mem doc retire", mem.__doc__)


if __name__ == "__main__":
    unittest.main()
