#!/usr/bin/env python3
"""Mutation proofs for L&S self-curation (migration 0100).

This ships a GUARD, and a guard is only worth what it REFUSES. One of this
repo's own L&S entries records `render-check` sitting inert for three cycles
while announcing success with a green checkmark — so nothing here asserts that
a legal write works and calls the cap proven. Every cap is proven by writing
OVER it and demanding the abort; every advisory is proven by driving the
counter across its threshold in both directions.

Three surfaces, one feature:
  • the DB caps       — L&S body 500, current_state 300, BEFORE-triggers, so
                        pre-existing oversized rows stay readable (grandfathering)
  • the write path    — `sc mem lns` requires --supersedes | --new; supersede
                        retires first so it works AT the count cap; a rejected
                        insert must not leave the retirements behind
  • the render        — STATUS says "curation due" at 5 since the stamp, and a
                        sweep that retires NOTHING still clears it

Run:
    python3 tests/test_lns_curation.py
"""
from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "render"))
import compose  # noqa: E402
import mem  # noqa: E402
import server  # noqa: E402
import skill as skill_cmd  # noqa: E402

TOKEN = "lns-token-deadbeef"


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, api_key) "
        "VALUES (1, 'TC', 'tc', 'test', 'sp', 1, 0, 1, 1, ?)", (TOKEN,))
    con.commit()
    con.close()


class LnsCurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        build_engine_db(cls.db)
        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        mem.SC_API_TOKEN = TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        con = self.con()
        con.execute("DELETE FROM shell_identity_entries")
        con.execute("UPDATE shells SET lns_curated_at=NULL, current_state=NULL")
        con.commit()
        con.close()

    # ── helpers ───────────────────────────────────────────────────────────────
    def con(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql, *params):
        c = self.con()
        try:
            return c.execute(sql, params).fetchone()
        finally:
            c.close()

    def run_mem(self, *argv) -> int:
        return mem.main(list(argv))

    def mem_fails(self, *argv) -> str:
        """Drive `sc mem` expecting a refusal; return what it said."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_mem(*argv)
        return (err.getvalue() + str(cm.exception or "")).strip()

    def quiet_mem(self, *argv) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return self.run_mem(*argv)

    def seed_lns(self, n: int, body: str = "rule", at: "str | None" = None) -> list[int]:
        """Insert n active L&S entries straight into the DB (bypasses the CLI's
        triage on purpose — these stand in for a set that already exists).

        `at` is a SQLite datetime modifier for created_at. Both the stamp and
        created_at come from `datetime('now')`, i.e. whole seconds, so a test
        that writes and stamps inside one second cannot tell the two apart —
        the clock is controlled explicitly rather than slept through.
        """
        stamp = f"datetime('now', '{at}')" if at else "datetime('now')"
        c = self.con()
        ids = []
        for i in range(n):
            cur = c.execute(
                "INSERT INTO shell_identity_entries (shell_id, kind, body, created_at) "
                f"VALUES (1, 'lns', ?, {stamp})", (f"{body} {i}",))
            ids.append(cur.lastrowid)
        c.commit()
        c.close()
        return ids

    def counts(self) -> dict:
        c = self.con()
        try:
            return compose.fetch_counts(c, 1)
        finally:
            c.close()

    # ── the caps, proven by writing OVER them ─────────────────────────────────
    def test_lns_501_chars_aborts_and_the_message_routes_the_fix(self):
        msg = self.mem_fails("lns", "x" * 501, "--new")
        self.assertIn("500 chars", msg)
        self.assertIn("narrative", msg)          # the overflow is routed, not just refused
        self.assertIn("400", msg)                # client error, not a 500 server fault
        self.assertIsNone(self.q(
            "SELECT 1 FROM shell_identity_entries WHERE kind='lns'"))

    def test_lns_500_chars_writes(self):
        self.assertEqual(self.quiet_mem("lns", "x" * 500, "--new"), 0)
        self.assertEqual(
            self.q("SELECT LENGTH(body) FROM shell_identity_entries "
                   "WHERE kind='lns'")[0], 500)

    def test_current_state_301_aborts_and_300_writes(self):
        msg = self.mem_fails("state", "y" * 301)
        self.assertIn("300 chars", msg)
        self.assertIn("point at the row", msg)
        self.assertIsNone(self.q("SELECT current_state FROM shells WHERE shell_id=1")[0])
        self.assertEqual(self.quiet_mem("state", "y" * 300), 0)
        self.assertEqual(
            len(self.q("SELECT current_state FROM shells WHERE shell_id=1")[0]), 300)

    def test_grandfathered_rows_stay_readable_and_renderable(self):
        """The legacy corpus predates the caps, so it has to survive them.

        Built the only honest way: schema + every migration BEFORE 0100, the
        oversized rows written while that was still legal, then 0100 laid on
        top. BEFORE-triggers don't touch rows in place — the caps constrain new
        writes and the sweep clears the legacy, two mechanisms over different
        halves.
        """
        legacy = self.tmp / "legacy.db"
        c = sqlite3.connect(legacy)
        c.row_factory = sqlite3.Row
        try:
            c.executescript(SCHEMA.read_text())
            older = [p for p in sorted(MIGRATIONS.glob("*.sql"))
                     if p.name < "0100_"]
            self.assertIn(
                "0097_quota_drop_account_identity.sql",
                {p.name for p in older},
            )
            for p in older:
                c.executescript(p.read_text())
            c.execute("INSERT INTO users (user_id, username) VALUES (1, 'T')")
            c.execute("INSERT INTO shells (shell_id, display_name, shortname, "
                      "system_prompt, current_state, user_id) "
                      "VALUES (1, 'Old', 'old', 'sp', ?, 1)", ("S" * 3087,))
            c.execute("INSERT INTO shell_identity_entries (shell_id, kind, body) "
                      "VALUES (1, 'lns', ?)", ("L" * 2914,))
            c.commit()
            c.executescript((MIGRATIONS / "0100_lns_self_curation.sql").read_text())
            c.commit()

            self.assertEqual(
                len(c.execute("SELECT current_state FROM shells "
                              "WHERE shell_id=1").fetchone()[0]), 3087)
            self.assertIn("L" * 100, compose.render_lns(c, 1))
            self.assertEqual(compose.fetch_counts(c, 1)["lns_chars"], 2914)
            # …and the cap is live on that same DB for the NEXT write.
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO shell_identity_entries "
                          "(shell_id, kind, body) VALUES (1, 'lns', ?)",
                          ("N" * 501,))
        finally:
            c.close()

    # ── write-time triage ─────────────────────────────────────────────────────
    def test_neither_flag_refuses_with_the_triage(self):
        msg = self.mem_fails("lns", "a rule")
        self.assertIn("--supersedes", msg)
        self.assertIn("--new", msg)
        self.assertIsNone(self.q(
            "SELECT 1 FROM shell_identity_entries WHERE kind='lns'"))

    def test_both_flags_refuse(self):
        self.mem_fails("lns", "a rule", "--new", "--supersedes", "1")
        self.assertIsNone(self.q(
            "SELECT 1 FROM shell_identity_entries WHERE kind='lns'"))

    def test_supersedes_retires_the_named_entries(self):
        ids = self.seed_lns(3)
        self.quiet_mem("lns", "the one rule", "--supersedes",
                       f"{ids[0]},{ids[1]}")
        for eid in ids[:2]:
            self.assertIsNotNone(self.q(
                "SELECT retired_at FROM shell_identity_entries WHERE entry_id=?",
                eid)["retired_at"])
        self.assertIsNone(self.q(
            "SELECT retired_at FROM shell_identity_entries WHERE entry_id=?",
            ids[2])["retired_at"])

    def test_supersedes_works_at_the_count_cap(self):
        """The whole point of the verb: at 20/20 a supersede frees the slot it
        uses. Retire-then-insert, or the cap trigger refuses the write that was
        going to fix the set."""
        ids = self.seed_lns(20)
        self.assertEqual(self.quiet_mem("lns", "merged rule", "--supersedes",
                                        f"{ids[0]},{ids[1]},{ids[2]}"), 0)
        self.assertEqual(self.counts()["lns"], 18)

    def test_rejected_insert_leaves_no_orphan_retirements(self):
        """A supersede whose body busts the length cap must roll BOTH halves
        back — otherwise a typo silently deletes rules and adds nothing."""
        ids = self.seed_lns(3)
        self.mem_fails("lns", "x" * 501, "--supersedes", str(ids[0]))
        self.assertIsNone(self.q(
            "SELECT retired_at FROM shell_identity_entries WHERE entry_id=?",
            ids[0])["retired_at"])
        self.assertEqual(self.counts()["lns"], 3)

    def test_supersedes_refuses_a_foreign_or_already_retired_entry(self):
        c = self.con()
        c.execute("INSERT INTO shells (shell_id, display_name, system_prompt, user_id) "
                  "VALUES (3, 'Other', 'sp', 1)")
        foreign = c.execute(
            "INSERT INTO shell_identity_entries (shell_id, kind, body) "
            "VALUES (3, 'lns', 'theirs')", ).lastrowid
        c.commit()
        c.close()
        self.assertIn("not one of your active", self.mem_fails(
            "lns", "mine", "--supersedes", str(foreign)))
        mine = self.seed_lns(1)[0]
        self.quiet_mem("retire", str(mine))
        self.assertIn("not one of your active", self.mem_fails(
            "lns", "mine", "--supersedes", str(mine)))

    # ── the sweep advisory ────────────────────────────────────────────────────
    def test_four_since_curation_is_quiet_five_is_due(self):
        self.seed_lns(4)
        self.assertNotIn("curation due", compose.render_lns_status(self.counts()))
        self.seed_lns(1, body="fifth")
        line = compose.render_lns_status(self.counts())
        self.assertIn("curation due", line)
        self.assertIn("`curate` skill", line)
        self.assertIn("5/20", line)

    def test_stamp_clears_the_counter(self):
        self.seed_lns(6)
        self.assertIn("curation due", compose.render_lns_status(self.counts()))
        self.quiet_mem("curated")
        self.assertNotIn("curation due", compose.render_lns_status(self.counts()))
        self.assertEqual(self.counts()["lns_since_curation"], 0)

    def test_a_sweep_that_retires_nothing_still_goes_quiet(self):
        """THE case that matters — the failure a MAX(retired_at) signal would
        have shipped. An honest sweep over a clean set retires nothing; if that
        left the advisory standing, shells would learn to ignore it."""
        self.seed_lns(7)
        self.quiet_mem("curated")
        before = self.q("SELECT COUNT(*) FROM shell_identity_entries "
                        "WHERE retired_at IS NOT NULL")[0]
        self.assertEqual(before, 0)
        self.assertNotIn("curation due", compose.render_lns_status(self.counts()))

    def test_writes_after_the_stamp_count_again(self):
        self.seed_lns(6)
        self.quiet_mem("curated")
        self.assertEqual(self.counts()["lns_since_curation"], 0)
        self.seed_lns(5, body="post", at="+2 seconds")
        self.assertEqual(self.counts()["lns_since_curation"], 5)
        self.assertIn("curation due", compose.render_lns_status(self.counts()))

    def test_the_sweeps_own_merged_entries_do_not_count_against_it(self):
        """A merge writes a new entry and then stamps. `>` (not `>=`) is what
        keeps the sweep's own output out of the next interval's count — a sweep
        that immediately re-armed its own advisory would never converge."""
        self.seed_lns(5)
        self.quiet_mem("lns", "the one rule", "--new")   # the merged entry
        self.quiet_mem("curated")
        self.assertEqual(self.counts()["lns_since_curation"], 0)

    def test_status_line_reports_chars(self):
        self.seed_lns(2)
        self.assertIn("chars", compose.render_lns_status(self.counts()))


class CurationGovernanceTest(unittest.TestCase):
    """Curation recommends upstream; only admins author durable fork skills."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "shell_db.db"
        build_engine_db(self.db)
        self.saved_db = skill_cmd.DB_PATH
        skill_cmd.DB_PATH = self.db

    def tearDown(self):
        skill_cmd.DB_PATH = self.saved_db
        self.tmp.cleanup()

    def test_skill_help_and_execution_have_no_add_surface(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(skill_cmd.main(["--help"]), 0)
        self.assertNotIn(" add ", f" {out.getvalue()} ")

        with self.assertRaisesRegex(SystemExit, "usage: ./sc skill list"):
            skill_cmd.main(["add", "tc_sweep", "--file", "candidate.md"])
        con = sqlite3.connect(self.db)
        try:
            self.assertIsNone(
                con.execute("SELECT 1 FROM skills WHERE name='tc_sweep'").fetchone()
            )
        finally:
            con.close()

    def test_curate_recommends_and_retains_lns_until_delivery(self):
        text = (ENGINE / "assets" / "skills" / "curate" / "SKILL.md").read_text()
        self.assertIn("gh issue list --repo jedbjorn/subfloor --state all", text)
        self.assertIn("skills: recommend <topic>", text)
        self.assertIn("trigger that makes the procedure useful", text)
        self.assertIn("proposed ownership boundary", text)
        self.assertIn("why existing skills do not cover it", text)
        self.assertIn("until a reviewed upstream\nskill ships **and is granted**", text)
        self.assertIn("create no local skill or asset", text)
        self.assertNotIn("sc skill add", text)

    def test_curate_and_issue_reporting_publish_the_authorized_exception(self):
        curate = (ENGINE / "assets" / "skills" / "curate" / "SKILL.md").read_text()
        reporting = (
            ENGINE / "assets" / "skills" / "issue_reporting" / "SKILL.md"
        ).read_text()
        self.assertIn(
            "authorized exception to the \"enhancement ideas go to the FnB "
            "first\" gate in `issue_reporting`",
            " ".join(curate.split()),
        )
        for text in (curate, reporting):
            self.assertIn("skills: recommend <topic>", text)
            self.assertIn("keep the l&s", text.lower())
            self.assertIn("create no local skill or asset", text)
        self.assertIn("FnB-authorized exception", reporting)
        self.assertIn("Planner-owned workflow in `fork_skill_design`", reporting)

    def test_planner_db_first_skill_workflow_remains_explicit(self):
        text = (
            ENGINE / "assets" / "skills" / "fork_skill_design" / "SKILL.md"
        ).read_text()
        self.assertIn("sc skill put --file", text)
        self.assertIn("Planner-owned draft", text)
        self.assertIn("DB, local snapshot, flat catalogue, and managed skill", text)
        self.assertIn("Creation grants nothing", text)
        self.assertNotIn("file -> seed -> grant -> local snapshot", text)
        self.assertNotIn("SC_ADMIN=1 sc snapshot", text)

    def test_trailing_migration_matches_changed_skill_assets_exactly(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            for name in ("curate", "issue_reporting"):
                expected = skill_cmd.seed_skills.parse_skill(
                    ENGINE / "assets" / "skills" / name / "SKILL.md"
                )
                actual = con.execute(
                    "SELECT description, category, command, common, content "
                    "FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertIsNotNone(actual)
                self.assertEqual(dict(actual), {
                    key: expected[key]
                    for key in ("description", "category", "command", "common", "content")
                })
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
