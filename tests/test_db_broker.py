#!/usr/bin/env python3
"""DB broker policy, psycopg execution, CLI, and Unix-socket contracts.

The driver is faked at its connection boundary; no live DB or installed driver
is required. Optional real-PostgreSQL coverage lives in test_db_broker_postgres.
"""
from __future__ import annotations

import json
import io
import os
import tempfile
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
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
    "dsn_env_file": "/nonexistent-subfloor-test-env",
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

    def test_comment_markers_and_dollar_strings_do_not_hide_stacked_sql(self):
        for literal in ("'-- /* update */'", "$$delete; --$$", "$tag$drop; /*$tag$"):
            self.assertTrue(self.ok(f"SELECT {literal} FROM models")[0])
            self.assertFalse(self.ok(f"SELECT {literal} FROM models; DELETE FROM models")[0])

    def test_read_only_diagnostics_still_apply_the_allowlist(self):
        self.assertTrue(self.ok("EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM models")[0])
        self.assertTrue(self.ok("SHOW server_version")[0])
        self.assertFalse(self.ok("EXPLAIN SELECT * FROM private_table")[0])
        self.assertFalse(self.ok("EXPLAIN ANALYZE DELETE FROM models")[0])


class DriverError(Exception):
    def __init__(self, message, sqlstate="42501"):
        super().__init__(message)
        self.diag = SimpleNamespace(message_primary=message)
        self.sqlstate = sqlstate


@contextmanager
def database(*, block=None, rows=None, columns=None):
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [SimpleNamespace(name=n) for n in (columns or ["id"])]
    cursor.fetchmany.return_value = rows if rows is not None else [(1,), (2,)]
    cursor.rowcount = len(cursor.fetchmany.return_value)
    cursor.statusmessage = "SELECT 2"
    cursor.nextset.return_value = None
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    driver = SimpleNamespace(
        Error=DriverError, connect=mock.Mock(return_value=conn),
        conninfo=SimpleNamespace(conninfo_to_dict=lambda dsn: {"password": "secret"}),
    )
    with mock.patch.dict(sys.modules, {"psycopg": driver}), \
         mock.patch.object(dbq, "read", return_value=block or BLOCK), \
         mock.patch.dict(os.environ, {"SC_TEST_RO_DSN": "postgresql://ro:secret@h/db"}):
        yield driver, conn, cursor


class QueryVerbTests(unittest.TestCase):
    def test_query_preserves_sql_and_sets_read_only_timeouts(self):
        sql = "SELECT 'please update -- /* literal */' AS label FROM models"
        with database(rows=[("hello",), (None,)], columns=["label"]) as (driver, conn, cur):
            result = dbq.do_query(sql)
        self.assertTrue(result["ok"])
        self.assertEqual(result["columns"], ["label"])
        self.assertEqual(result["rows"], [["hello"], [None]])
        self.assertEqual(result["row_count"], 2)
        cur.execute.assert_called_once_with(sql)
        options = driver.connect.call_args.kwargs
        self.assertIn("default_transaction_read_only=on", options["options"])
        self.assertIn("statement_timeout=5000", options["options"])
        self.assertIn("lock_timeout=5000", options["options"])
        self.assertFalse(options["autocommit"])
        conn.__exit__.assert_called_once_with(None, None, None)

    def test_row_cap_and_string_cell_protocol(self):
        with database(rows=[(1, True, None, b"a", {"x": 1}), (2,), (3,), (4,)]) as (_, _, cur):
            result = dbq.do_query("SELECT * FROM models")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["rows"][0], ["1", "t", None, "\\x61", '{"x": 1}'])
        cur.fetchmany.assert_called_once_with(4)

    def test_validation_failure_never_connects(self):
        with database() as (driver, _, _):
            result = dbq.do_query("DELETE FROM models")
        self.assertFalse(result["ok"])
        driver.connect.assert_not_called()

    def test_invalid_access_mode_fails_closed(self):
        with database(block={**BLOCK, "access_mode": "typo"}) as (driver, _, _):
            result = dbq.do_query("SELECT 1")
        self.assertFalse(result["ok"])
        driver.connect.assert_not_called()

    def test_invalid_policy_is_a_clean_error(self):
        with database(block={**BLOCK, "row_cap": "invalid"}) as (driver, _, _):
            result = dbq.do_query("SELECT 1")
        self.assertFalse(result["ok"])
        driver.connect.assert_not_called()

    def test_missing_dsn_is_a_clean_error(self):
        with database() as (driver, _, _), mock.patch.dict(os.environ, {}, clear=True):
            result = dbq.do_query("SELECT 1")
        self.assertFalse(result["ok"])
        self.assertIn("SC_TEST_RO_DSN", result["error"])
        driver.connect.assert_not_called()

    def test_no_db_block_is_a_clean_error(self):
        with mock.patch.object(dbq, "read", return_value=None):
            result = dbq.do_query("SELECT 1")
        self.assertFalse(result["ok"])
        self.assertIn("db-init", result["error"])

    def test_write_mode_accepts_scripts_without_rewriting(self):
        sql = "UPDATE private_table SET note='a;b'; SELECT $$x;y$$"
        with database(block={**BLOCK, "access_mode": "read_write"}) as (driver, _, cur):
            cur.nextset.side_effect = [True, None]
            result = dbq.do_query(sql)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["affected_rows"], 2)
        cur.execute.assert_called_once_with(sql)
        self.assertIn("default_transaction_read_only=off", driver.connect.call_args.kwargs["options"])

    def test_non_row_statement_and_autocommit(self):
        with database(block={**BLOCK, "access_mode": "read_write"}) as (driver, _, cur):
            cur.description = None
            cur.statusmessage = "VACUUM"
            cur.rowcount = -1
            result = dbq.do_query("VACUUM models", autocommit=True)
        self.assertTrue(result["ok"])
        self.assertTrue(driver.connect.call_args.kwargs["autocommit"])
        self.assertEqual(result["status"], "VACUUM")
        self.assertEqual(result["rows"], [])
        cur.fetchmany.assert_not_called()

    def test_failed_script_exits_transaction_with_error_and_never_retries(self):
        error = DriverError("division by zero", "22012")
        with database(block={**BLOCK, "access_mode": "read_write"}) as (driver, conn, cur):
            cur.execute.side_effect = error
            result = dbq.do_query("UPDATE models SET x=1; SELECT 1/0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["sqlstate"], "22012")
        self.assertIs(conn.__exit__.call_args.args[1], error)
        driver.connect.assert_called_once()
        cur.execute.assert_called_once()

    def test_driver_errors_redact_credentials(self):
        with database() as (driver, _, _):
            driver.connect.side_effect = DriverError("failed postgresql://ro:secret@h/db password secret")
            result = dbq.do_query("SELECT 1")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", result["error"])
        self.assertNotIn("postgresql://", result["error"])

    def test_health_probes_the_database(self):
        with database(rows=[("testdb", "readonly", "on")], columns=["db", "role", "ro"]):
            result = dbq.health()
        self.assertEqual(result, {"ok": True, "service": "db-broker", "database": "testdb",
                                  "role": "readonly", "access_mode": "read_only",
                                  "transaction_read_only": "on"})

    def test_unhealthy_database_reports_failure(self):
        with database() as (driver, _, _):
            driver.connect.side_effect = DriverError("unavailable")
            result = dbq.health()
        self.assertFalse(result["ok"])

    def test_env_file_precedes_inherited_env_without_expansion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broker.env"
            path.write_text("SC_TEST_RO_DSN='postgresql://host/db?application_name=$unchanged'\n")
            with mock.patch.dict(os.environ, {"SC_TEST_RO_DSN": "stale"}):
                dsn, _, error = dbq.resolve_dsn({**BLOCK, "dsn_env_file": str(path)})
            self.assertIsNone(error)
            self.assertEqual(dsn, "postgresql://host/db?application_name=$unchanged")

    def test_cli_uses_socket_and_preserves_file_stdin_and_autocommit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "repair.sql"
            sql = "BEGIN; SELECT 'literal'; ROLLBACK;\n"
            path.write_text(sql)
            with mock.patch.object(dbq, "broker_call", return_value={"ok": True}) as call, redirect_stdout(io.StringIO()):
                self.assertEqual(dbq.main(["query", "--file", str(path)]), 0)
                call.assert_called_with("POST", "/query", {"sql": sql, "autocommit": False})
                with mock.patch.object(sys, "stdin", io.StringIO("VACUUM models")):
                    self.assertEqual(dbq.main(["query", "--autocommit", "-"]), 0)
                call.assert_called_with("POST", "/query", {"sql": "VACUUM models", "autocommit": True})

    def test_cli_returns_failure_and_health_uses_socket(self):
        with mock.patch.object(dbq, "broker_call", return_value={"ok": False}) as call, redirect_stdout(io.StringIO()):
            self.assertEqual(dbq.main(["health"]), 1)
            call.assert_called_once_with("GET", "/health")

    def test_configured_cli_reflects_a_linked_db(self):
        with mock.patch.object(dbq, "read", return_value=BLOCK):
            self.assertEqual(dbq.main(["configured"]), 0)
        with mock.patch.object(dbq, "read", return_value=None):
            self.assertEqual(dbq.main(["configured"]), 1)


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sc-db-test-")
        self.addCleanup(self.tmp.cleanup)
        self.sock = Path(self.tmp.name) / "broker.sock"
        audit = mock.patch.object(dbq, "RUN_DIR", Path(self.tmp.name))
        audit.start()
        self.addCleanup(audit.stop)
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
        with database(rows=[("db", "role", "on")]):
            r = dbq.broker_call("GET", "/health")
        self.assertTrue(r["ok"])
        self.assertEqual(r["database"], "db")

    def test_unknown_route_is_404_shaped(self):
        r = dbq.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_query_round_trips_over_the_socket(self):
        with database():
            r = dbq.broker_call("POST", "/query", {"sql": "SELECT id FROM models"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["columns"], ["id"])
        self.assertEqual(r["row_count"], 2)

    def test_rejected_query_round_trips_the_denial(self):
        with database() as (driver, _, _):
            r = dbq.broker_call("POST", "/query", {"sql": "DROP TABLE models"})
        self.assertFalse(r["ok"])
        driver.connect.assert_not_called()

    def test_non_string_sql_is_400_shaped(self):
        r = dbq.broker_call("POST", "/query", {"sql": {"nope": 1}})
        self.assertFalse(r["ok"])

    def test_non_boolean_autocommit_is_rejected_without_execution(self):
        with mock.patch.object(dbq, "do_query") as query:
            r = dbq.broker_call("POST", "/query", {"sql": "SELECT 1", "autocommit": "false"})
        self.assertFalse(r["ok"])
        query.assert_not_called()

    def test_unhealthy_database_round_trips_failure(self):
        with mock.patch.object(dbq, "health", return_value={"ok": False, "error": "unavailable"}):
            r = dbq.broker_call("GET", "/health")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "unavailable")

    def test_broker_call_raises_when_nothing_listens(self):
        dbq.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            dbq.broker_call("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
