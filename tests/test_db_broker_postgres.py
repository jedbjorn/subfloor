"""Optional real DB proof; point SUBFLOOR_TEST_POSTGRES_DSN at a disposable DB."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
import dbq


@unittest.skipUnless(os.environ.get("SUBFLOOR_TEST_POSTGRES_DSN"), "disposable PostgreSQL not configured")
class PostgresBrokerTests(unittest.TestCase):
    def setUp(self):
        self.table = "broker_test_" + uuid.uuid4().hex
        self.config = {"dsn_env": "SUBFLOOR_TEST_POSTGRES_DSN",
                       "dsn_env_file": "/nonexistent-subfloor-test-env",
                       "access_mode": "read_write", "row_cap": 3}
        patch = mock.patch.object(dbq, "read", return_value=self.config)
        patch.start()
        self.addCleanup(patch.stop)
        self.query(f"CREATE TABLE {self.table} (id int PRIMARY KEY, value text)")
        self.addCleanup(self.query, f"DROP TABLE IF EXISTS {self.table}")

    def query(self, sql, **kwargs):
        result = dbq.do_query(sql, **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def test_commit_rollback_multiple_results_and_maintenance(self):
        result = self.query(f"INSERT INTO {self.table} VALUES (1, 'before') RETURNING id")
        self.assertEqual(result["affected_rows"], 1)
        failed = dbq.do_query(f"UPDATE {self.table} SET value='bad'; SELECT 1/0")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["sqlstate"], "22012")
        self.assertEqual(self.query(f"SELECT value FROM {self.table}")["rows"], [["before"]])
        self.query(f"BEGIN; UPDATE {self.table} SET value='rollback'; ROLLBACK;")
        self.assertEqual(self.query(f"SELECT value FROM {self.table}")["rows"], [["before"]])
        result = self.query(f"UPDATE {self.table} SET value='after' RETURNING value; SELECT value FROM {self.table}")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["rows"], [["after"]])
        self.query(f"VACUUM {self.table}", autocommit=True)

    def test_sql_literals_diagnostics_and_row_cap(self):
        sql = "WITH x AS (SELECT 'O''Brien -- /* literal */' AS v) SELECT v, NULL, $$a;b$$ FROM x"
        self.assertEqual(self.query(sql)["rows"], [["O'Brien -- /* literal */", None, "a;b"]])
        self.assertTrue(self.query("EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1")["rows"])
        self.assertTrue(self.query("SHOW server_version")["rows"])
        result = self.query("SELECT n FROM generate_series(1,4) n")
        self.assertEqual(result["row_count"], 3)
        self.assertTrue(result["truncated"])

    def test_read_only_default_and_connection_health(self):
        with mock.patch.object(dbq, "read", return_value={**self.config, "access_mode": "read_only", "allow_tables": [self.table]}):
            self.assertFalse(dbq.do_query(f"DELETE FROM {self.table}")["ok"])
            self.assertEqual(dbq.health()["transaction_read_only"], "on")
            self.assertEqual(self.query("SELECT '/* -- literal */' AS value")["rows"], [["/* -- literal */"]])
        self.assertEqual(dbq.health()["transaction_read_only"], "off")


if __name__ == "__main__":
    unittest.main()
