"""Authorization contract for Admin-only general engine SQL."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import engine_sql


class EngineSqlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "engine.db"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE shells (api_key TEXT, flavor TEXT, is_deleted INTEGER)"
        )
        con.execute(
            "INSERT INTO shells VALUES ('admin-token','admin',0)"
        )
        con.execute(
            "INSERT INTO shells VALUES ('dev-token','dev',0)"
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self):
        return mock.patch.object(
            engine_sql.instance_state, "active_database_path",
            return_value=self.db,
        )

    def test_api_down_admin_keeps_read_only_diagnosis(self):
        completed = mock.Mock(returncode=0)
        env = {
            "SC_API_TOKEN": "admin-token",
            "SC_API_BASE": "http://127.0.0.1:1",
            "SC_SHELL_FLAVOR": "dev",
            "SC_ROOT": "/attacker/path",
            "SC_ENGINE_DIR": "/attacker/engine",
        }
        with mock.patch.dict(os.environ, env, clear=True), self._path(), \
             mock.patch.object(engine_sql, "_api_flavor", return_value=None), \
             mock.patch.object(engine_sql.shutil, "which", return_value="/bin/sqlite3"), \
             mock.patch.object(engine_sql.subprocess, "run", return_value=completed) as run:
            self.assertEqual(engine_sql.main(["read-only", "SELECT 1;"]), 0)
        self.assertEqual(
            run.call_args.args[0],
            ["/bin/sqlite3", "-readonly", str(self.db), "SELECT 1;"],
        )

    def test_api_down_host_admin_discovers_owner_only_runtime_credential(self):
        credential_dir = Path(self.tmp.name) / "credentials"
        credential_dir.mkdir()
        artifact = credential_dir / "admin.json"
        artifact.write_text(json.dumps({
            "shell_id": 1,
            "shortname": "admin",
            "api_base": "http://127.0.0.1:1",
            "token": "admin-token",
        }))
        artifact.chmod(0o600)
        completed = mock.Mock(returncode=0)
        saved = (
            engine_sql.mem._CRED_DIR,
            engine_sql.mem.SC_API_TOKEN,
            engine_sql.mem.SC_API_BASE,
            engine_sql.mem._DISCOVERED_FROM,
        )
        self.addCleanup(
            setattr, engine_sql.mem, "_CRED_DIR", saved[0]
        )
        self.addCleanup(
            setattr, engine_sql.mem, "SC_API_TOKEN", saved[1]
        )
        self.addCleanup(
            setattr, engine_sql.mem, "SC_API_BASE", saved[2]
        )
        self.addCleanup(
            setattr, engine_sql.mem, "_DISCOVERED_FROM", saved[3]
        )
        engine_sql.mem._CRED_DIR = credential_dir
        engine_sql.mem.SC_API_TOKEN = ""
        engine_sql.mem.SC_API_BASE = ""
        engine_sql.mem._DISCOVERED_FROM = None

        with mock.patch.dict(os.environ, {}, clear=True), self._path(), \
             mock.patch.object(engine_sql, "_api_flavor", return_value=None), \
             mock.patch.object(engine_sql.shutil, "which", return_value="/bin/sqlite3"), \
             mock.patch.object(engine_sql.subprocess, "run", return_value=completed) as run:
            self.assertEqual(engine_sql.main(["read-only", "SELECT 1;"]), 0)

        self.assertEqual(
            run.call_args.args[0],
            ["/bin/sqlite3", "-readonly", str(self.db), "SELECT 1;"],
        )

    def test_non_admin_refuses_before_query_even_when_flavor_is_spoofed(self):
        env = {
            "SC_API_TOKEN": "dev-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_FLAVOR": "admin",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(engine_sql, "_api_flavor", return_value="dev"), \
             mock.patch.object(
                 engine_sql.instance_state,
                 "active_database_path",
                 side_effect=AssertionError("DB path must not be resolved"),
             ), self.assertRaises(SystemExit) as caught:
            engine_sql.main(["read-only", "SELECT secret FROM shells;"])
        self.assertIn(engine_sql.ERROR_CODE, str(caught.exception))
        self.assertNotIn(str(self.db), str(caught.exception))

    def test_api_up_admin_write_fails_closed_before_sqlite(self):
        env = {
            "SC_API_TOKEN": "admin-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
        }
        with mock.patch.dict(os.environ, env, clear=True), self._path(), \
             mock.patch.object(engine_sql, "_api_flavor", return_value="admin"), \
             mock.patch.object(
                 engine_sql.shutil,
                 "which",
                 side_effect=AssertionError("sqlite must not be resolved"),
             ), mock.patch.object(
                 engine_sql.subprocess,
                 "run",
                 side_effect=AssertionError("query must not be executed"),
             ), self.assertRaises(SystemExit) as caught:
            engine_sql.main(["read-write", "THIS IS NOT SQL"])
        self.assertIn(
            engine_sql.MAINTENANCE_ERROR_CODE,
            str(caught.exception),
        )

    def test_api_down_admin_write_fails_closed_before_sqlite(self):
        env = {
            "SC_API_TOKEN": "admin-token",
            "SC_API_BASE": "http://127.0.0.1:1",
        }
        with mock.patch.dict(os.environ, env, clear=True), self._path(), \
             mock.patch.object(engine_sql, "_api_flavor", return_value=None), \
             mock.patch.object(
                 engine_sql.shutil,
                 "which",
                 side_effect=AssertionError("sqlite must not be resolved"),
             ), mock.patch.object(
                 engine_sql.subprocess,
                 "run",
                 side_effect=AssertionError("query must not be executed"),
             ), self.assertRaises(SystemExit) as caught:
            engine_sql.main(["read-write", "THIS IS NOT SQL"])
        self.assertIn(
            engine_sql.MAINTENANCE_ERROR_CODE,
            str(caught.exception),
        )

    def test_api_down_local_non_admin_is_still_refused(self):
        with mock.patch.dict(
            os.environ,
            {"SC_API_TOKEN": "dev-token", "SC_SHELL_FLAVOR": "admin"},
            clear=True,
        ), self._path(), \
             mock.patch.object(engine_sql, "_api_flavor", return_value=None), \
             mock.patch.object(
                 engine_sql.subprocess,
                 "run",
                 side_effect=AssertionError("query must not be executed"),
             ), \
             self.assertRaises(SystemExit) as caught:
            engine_sql.main(["read-write", "DELETE FROM shells;"])
        self.assertIn(engine_sql.ERROR_CODE, str(caught.exception))
        self.assertNotIn(str(self.db), str(caught.exception))

    def test_missing_identity_does_not_adopt_an_admin_credential(self):
        # A live install keeps runtime credential artifacts in the engine's
        # run/mem; mem._CRED_DIR is resolved at import, so point it at an empty
        # dir to isolate the missing-identity refusal.
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"SC_MEM_CRED_DIR": td}, clear=True), \
             mock.patch.object(engine_sql.mem, "_CRED_DIR", Path(td)), \
             self.assertRaises(SystemExit) as caught:
            engine_sql.main(["read-only", "SELECT 1;"])
        self.assertIn(engine_sql.ERROR_CODE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
