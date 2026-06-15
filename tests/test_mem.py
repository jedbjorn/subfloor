#!/usr/bin/env python3
"""Tests for the `sc mem` engine-DB write surface (scripts/mem.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests. mem.py opens the DB BY PATH (so it can guard which file it is),
so these build a throwaway *file* DB the way the engine ships it (schema.sql +
every migration), seed one shell, then drive mem.py end-to-end via `main(argv)`.

Why this file exists: mem.py's whole reason to exist is to refuse the wrong DB —
a 0-byte stub or a substrate *product* DB whose table names overlap the engine's.
That guard is the load-bearing safety property, so it gets explicit coverage
here: a regression that weakened `assert_engine_db` would otherwise be invisible
until a shell silently wrote its memory into the wrong database.

Run:
    python3 tests/test_mem.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import mem  # noqa: E402


def build_engine_db(path: Path) -> None:
    """A throwaway file DB shaped like the shipped engine, with one shell."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    con.execute(
        "INSERT INTO shells (display_name, shortname, mandate, system_prompt, user_id, "
        "is_shared, has_identity, bootstrapped) "
        "VALUES ('TC', 'tc', 'test', 'sp', 1, 0, 1, 1)")
    con.commit()
    con.close()


class MemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)

    def tearDown(self):
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    # ── the load-bearing guard ────────────────────────────────────────────────
    def test_guard_accepts_engine_db(self):
        mem.assert_engine_db(self.db)  # must not raise

    def test_guard_rejects_zero_byte_stub(self):
        stub = self.tmp / "stub.db"
        stub.touch()
        with self.assertRaises(SystemExit):
            mem.assert_engine_db(stub)

    def test_guard_rejects_missing_file(self):
        with self.assertRaises(SystemExit):
            mem.assert_engine_db(self.tmp / "nope.db")

    def test_guard_rejects_product_db(self):
        """A DB carrying product tables (contacts/emails) and lacking the engine
        sentinels is refused — this is the cross-DB-mutation foot-gun."""
        prod = self.tmp / "app.db"
        con = sqlite3.connect(prod)
        con.executescript("CREATE TABLE contacts(id INTEGER); "
                          "CREATE TABLE emails(id INTEGER); "
                          "CREATE TABLE shells(shell_id INTEGER);")
        con.commit()
        con.close()
        with self.assertRaises(SystemExit):
            mem.assert_engine_db(prod)

    # ── shell resolution ──────────────────────────────────────────────────────
    def test_resolve_single_shell_default(self):
        con = mem.connect(self.db)
        try:
            self.assertEqual(mem.resolve_shell(con, None), 1)
            self.assertEqual(mem.resolve_shell(con, "tc"), 1)
            self.assertEqual(mem.resolve_shell(con, "1"), 1)
        finally:
            con.close()

    def test_resolve_unknown_shell_exits(self):
        con = mem.connect(self.db)
        try:
            with self.assertRaises(SystemExit):
                mem.resolve_shell(con, "ghost")
        finally:
            con.close()

    # ── writes land (driven end-to-end through main, --no-sync) ───────────────
    def _run(self, *argv):
        return mem.main([*argv, "--db", str(self.db), "--no-sync", "--shell", "tc"])

    def test_state_write(self):
        self._run("state", "hello state")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT current_state FROM shells WHERE shell_id=1")
                         .fetchone()[0], "hello state")
        con.close()

    def test_decision_write(self):
        self._run("decision", "do the thing", "--rationale", "because")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT count(*) FROM shell_decisions "
                                     "WHERE decision='do the thing'").fetchone()[0], 1)
        con.close()

    def test_flag_open_then_close(self):
        self._run("flag", "open", "[T] blocker", "--name", "T-1")
        con = sqlite3.connect(self.db)
        fid = con.execute("SELECT flag_id FROM flags WHERE display_name='T-1'").fetchone()[0]
        con.close()
        self._run("flag", "close", str(fid), "--notes", "fixed")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT resolved FROM flags WHERE flag_id=?",
                                     (fid,)).fetchone()[0], 1)
        con.close()

    def test_doc_add_and_freeze(self):
        spec = self.tmp / "spec.md"
        spec.write_text("# Spec\nbody\n")
        self._run("doc", "add", "A Spec", "--body-file", str(spec), "--kind", "spec")
        con = sqlite3.connect(self.db)
        did, frozen = con.execute("SELECT document_id, frozen FROM documents "
                                  "WHERE title='A Spec'").fetchone()
        con.close()
        self.assertEqual(frozen, 0)
        mem.main(["doc", "freeze", str(did), "--db", str(self.db), "--no-sync"])
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT frozen FROM documents WHERE document_id=?",
                                     (did,)).fetchone()[0], 1)
        con.close()

    def test_message_send_check_mark_read(self):
        # second shell to send to
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO shells (display_name, shortname, mandate, system_prompt, "
                    "user_id, is_shared, has_identity, bootstrapped) "
                    "VALUES ('TD', 'td', 'm', 'sp', 1, 0, 1, 1)")
        con.commit(); con.close()
        # tc -> td
        self._run("message", "send", "td", "hello td")
        # unknown recipient rejected
        with self.assertRaises(SystemExit):
            self._run("message", "send", "ghost", "x")
        # td sees it; tc does not
        con = sqlite3.connect(self.db)
        mid = con.execute("SELECT message_id FROM shell_messages WHERE body='hello td'").fetchone()[0]
        self.assertEqual(con.execute("SELECT to_shell_id FROM shell_messages "
                                     "WHERE message_id=?", (mid,)).fetchone()[0], 2)
        con.close()
        # mark-read as td
        mem.main(["message", "mark-read", str(mid), "--db", str(self.db), "--no-sync", "--shell", "td"])
        con = sqlite3.connect(self.db)
        self.assertIsNotNone(con.execute("SELECT read_at FROM shell_messages "
                                         "WHERE message_id=?", (mid,)).fetchone()[0])
        con.close()

    def test_seed_and_retire(self):
        self._run("seed", "a genesis line", "--tag", "tc")
        con = sqlite3.connect(self.db)
        eid = con.execute("SELECT entry_id FROM shell_identity_entries "
                          "WHERE kind='seed' AND body='a genesis line'").fetchone()[0]
        con.close()
        mem.main(["retire", str(eid), "--db", str(self.db), "--no-sync"])
        con = sqlite3.connect(self.db)
        self.assertIsNotNone(con.execute("SELECT retired_at FROM shell_identity_entries "
                                         "WHERE entry_id=?", (eid,)).fetchone()[0])
        con.close()


if __name__ == "__main__":
    unittest.main()
