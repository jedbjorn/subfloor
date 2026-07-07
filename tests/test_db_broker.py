#!/usr/bin/env python3
"""Smoke tests for the db broker (api/db_broker.py + scripts/dbq.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests (test_pm2_broker.py, test_vm_broker.py). The broker shells out to
`psql` against a live Postgres, which no CI box has; so we mock at the subprocess
seam (`dbq._run`) and exercise the parts that DO run everywhere: the SELECT-only
+ allowlist validator, the CSV parse + row-cap truncation, the JSON shapes the
`db_query` skill depends on, and the real unix-socket HTTP transport end to end
(a live broker on a temp socket, driven by the same `dbq.broker_call` client).

Run:
    python3 tests/test_db_broker.py
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import dbq  # noqa: E402
import db_broker  # noqa: E402

BLOCK = {
    "dsn_env": "SC_TEST_RO_DSN",
    "allow_tables": ["skill_runs", "tool_call_attempts", "models"],
    "row_cap": 3,
    "statement_timeout_ms": 5000,
}


class ValidationTests(unittest.TestCase):
    """check() is the fail-closed gate — everything but a single, read-only,
    allowlisted SELECT is rejected before psql ever runs."""

    def ok(self, sql):
        return dbq.check(sql, BLOCK)

    def test_plain_select_on_allowlisted_table_passes(self):
        ok, _ = self.ok("SELECT error_class, count(*) FROM tool_call_attempts GROUP BY 1")
        self.assertTrue(ok)

    def test_with_cte_select_passes(self):
        ok, _ = self.ok("WITH r AS (SELECT * FROM skill_runs) SELECT count(*) FROM r")
        self.assertTrue(ok)

    def test_empty_is_rejected(self):
        ok, why = self.ok("   ")
        self.assertFalse(ok)
        self.assertIn("empty", why)

    def test_non_select_is_rejected(self):
        for sql in ("UPDATE models SET x=1",
                    "DELETE FROM skill_runs",
                    "INSERT INTO models VALUES (1)",
                    "DROP TABLE models"):
            ok, why = self.ok(sql)
            self.assertFalse(ok, sql)
            self.assertIn("SELECT", why)

    def test_stacked_statement_is_rejected(self):
        ok, why = self.ok("SELECT 1 FROM models; DROP TABLE models")
        self.assertFalse(ok)
        self.assertIn("single statement", why)

    def test_trailing_semicolon_is_fine(self):
        ok, _ = self.ok("SELECT 1 FROM models;")
        self.assertTrue(ok)

    def test_writing_cte_is_rejected(self):
        # A data-modifying CTE is still a write — the leading token is WITH but
        # the forbidden-keyword scan catches the INSERT inside.
        ok, why = self.ok("WITH x AS (INSERT INTO models VALUES (1) RETURNING id) SELECT * FROM x")
        self.assertFalse(ok)
        self.assertIn("INSERT", why)

    def test_unlisted_table_is_rejected(self):
        ok, why = self.ok("SELECT * FROM contacts")
        self.assertFalse(ok)
        self.assertIn("allowlist", why)
        self.assertIn("contacts", why)

    def test_join_onto_unlisted_table_is_rejected(self):
        ok, why = self.ok("SELECT * FROM skill_runs s JOIN emails e ON e.run_id = s.id")
        self.assertFalse(ok)
        self.assertIn("emails", why)

    def test_schema_qualified_allowlisted_table_passes(self):
        ok, _ = self.ok("SELECT * FROM public.skill_runs")
        self.assertTrue(ok)

    def test_keyword_substring_in_column_is_not_a_false_positive(self):
        # `update_time` / `created_at` contain forbidden substrings but are not
        # the keywords — word boundaries must not trip on them.
        ok, _ = self.ok("SELECT update_time, created_at FROM models")
        self.assertTrue(ok)

    def test_forbidden_word_inside_a_string_literal_is_ignored(self):
        ok, _ = self.ok("SELECT id FROM models WHERE note = 'please update this'")
        self.assertTrue(ok)


class QueryVerbTests(unittest.TestCase):
    """do_query() validates, then shells to psql (mocked) and shapes results."""

    def _psql_csv(self, header, *rows):
        return 0, "\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n", ""

    def test_query_returns_columns_and_rows(self):
        out = self._psql_csv(["error_class", "n"], ["confirm_only", "4"], ["timeout", "1"])
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.dict("os.environ", {"SC_TEST_RO_DSN": "postgresql://ro@h/db"}), \
             mock.patch.object(dbq, "_run", return_value=out) as run:
            r = dbq.do_query("SELECT error_class, count(*) n FROM tool_call_attempts GROUP BY 1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["columns"], ["error_class", "n"])
        self.assertEqual(r["row_count"], 2)
        self.assertFalse(r["truncated"])
        # DSN parsed into PG* env, never onto argv; a row cap wrap + read-only opts applied.
        argv, env = run.call_args[0][0], run.call_args[0][1]
        self.assertNotIn("postgresql://ro@h/db", " ".join(argv))
        self.assertIn("LIMIT 4", " ".join(argv))  # row_cap 3 → cap+1
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        self.assertIn("statement_timeout=5000", env["PGOPTIONS"])
        self.assertEqual(env["PGHOST"], "h")

    def test_row_cap_truncation_is_flagged(self):
        # row_cap is 3; psql returns cap+1 = 4 rows (the wrap fetches one extra).
        out = self._psql_csv(["id"], ["1"], ["2"], ["3"], ["4"])
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.dict("os.environ", {"SC_TEST_RO_DSN": "postgresql://ro@h/db"}), \
             mock.patch.object(dbq, "_run", return_value=out):
            r = dbq.do_query("SELECT id FROM models")
        self.assertTrue(r["ok"])
        self.assertEqual(r["row_count"], 3)      # clipped to cap
        self.assertTrue(r["truncated"])

    def test_validation_failure_never_reaches_psql(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.object(dbq, "_run") as run:
            r = dbq.do_query("DELETE FROM models")
        self.assertFalse(r["ok"])
        run.assert_not_called()

    def test_missing_dsn_is_a_clean_error(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(dbq, "_run") as run:
            r = dbq.do_query("SELECT 1 FROM models")
        self.assertFalse(r["ok"])
        self.assertIn("SC_TEST_RO_DSN", r["error"])
        run.assert_not_called()

    def test_psql_error_surfaces(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.dict("os.environ", {"SC_TEST_RO_DSN": "postgresql://ro@h/db"}), \
             mock.patch.object(dbq, "_run",
                               return_value=(1, "", "permission denied for table models")):
            r = dbq.do_query("SELECT * FROM models")
        self.assertFalse(r["ok"])
        self.assertIn("permission denied", r["error"])

    def test_no_db_block_is_a_clean_error(self):
        with mock.patch.object(dbq, "read", return_value=None):
            r = dbq.do_query("SELECT 1 FROM models")
        self.assertFalse(r["ok"])
        self.assertIn("db-init", r["error"])

    def test_configured_cli_reflects_a_linked_db(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK):
            self.assertEqual(dbq.main(["configured"]), 0)
        with mock.patch.object(dbq, "read", return_value=None):
            self.assertEqual(dbq.main(["configured"]), 1)


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.sock = Path(__file__).resolve().parent / "_test_db_broker.sock"
        self._orig = dbq.SOCKET
        dbq.SOCKET = self.sock  # both server (db_broker path) + client read this
        self.srv = db_broker.UnixHTTPServer(str(self.sock), db_broker.Handler)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        dbq.SOCKET = self._orig
        self.sock.unlink(missing_ok=True)

    def test_health(self):
        r = dbq.broker_call("GET", "/health")
        self.assertEqual(r, {"ok": True, "service": "db-broker"})

    def test_unknown_route_is_404_shaped(self):
        r = dbq.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_query_round_trips_over_the_socket(self):
        out = (0, "id\n1\n2\n", "")
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.dict("os.environ", {"SC_TEST_RO_DSN": "postgresql://ro@h/db"}), \
             mock.patch.object(dbq, "_run", return_value=out):
            r = dbq.broker_call("POST", "/query", {"sql": "SELECT id FROM models"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["columns"], ["id"])
        self.assertEqual(r["row_count"], 2)

    def test_rejected_query_round_trips_the_denial(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK), \
             mock.patch.object(dbq, "_run") as run:
            r = dbq.broker_call("POST", "/query", {"sql": "DROP TABLE models"})
        self.assertFalse(r["ok"])
        run.assert_not_called()

    def test_non_string_sql_is_400_shaped(self):
        r = dbq.broker_call("POST", "/query", {"sql": {"nope": 1}})
        self.assertFalse(r["ok"])

    def test_broker_call_raises_when_nothing_listens(self):
        dbq.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            dbq.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
