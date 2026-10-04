"""One caller-identity check for every Admin gate (spec #267 D1-D3, G5).

Engine SQL, shared-instance serialization and map-setup's owner bridge all
resolve the caller from its bearer token through ``engine_identity``. The
retired self-declared ``SC_ADMIN=1`` flag must have no effect anywhere.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "render"))

import _serialize_guard  # noqa: E402
import engine_identity  # noqa: E402
import engine_sql  # noqa: E402
import map_setup  # noqa: E402
import render  # noqa: E402
import snapshot  # noqa: E402


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "engine.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE shells (api_key TEXT, flavor TEXT, is_deleted INTEGER)")
        con.executemany("INSERT INTO shells VALUES (?,?,0)",
                        [("admin-token", "admin"), ("dev-token", "dev")])
        con.commit()
        con.close()

    def test_no_token_is_the_operator_or_engine_seat(self):
        caller = engine_identity.resolve({"SC_SHELL_FLAVOR": "dev"})
        self.assertFalse(caller.launched_shell)
        self.assertTrue(caller.maintains_instance)

    def test_api_answer_wins_over_the_local_database(self):
        with mock.patch.object(engine_identity, "api_flavor", return_value="dev"):
            caller = engine_identity.resolve(
                {"SC_API_TOKEN": "admin-token", "SC_API_BASE": "http://x"},
                db_path=lambda: self.fail("DB consulted although the API answered"),
            )
        self.assertEqual(caller.flavor, "dev")
        self.assertFalse(caller.maintains_instance)

    def test_api_down_falls_back_to_a_read_only_local_lookup(self):
        with mock.patch.object(engine_identity, "api_flavor", return_value=None):
            admin = engine_identity.resolve({"SC_API_TOKEN": "admin-token"},
                                            db_path=lambda: self.db)
            dev = engine_identity.resolve({"SC_API_TOKEN": "dev-token"},
                                          db_path=lambda: self.db)
            unknown = engine_identity.resolve({"SC_API_TOKEN": "nobody"},
                                              db_path=lambda: self.db)
            missing = engine_identity.resolve(
                {"SC_API_TOKEN": "admin-token"},
                db_path=lambda: Path(self.tmp.name) / "absent.db",
            )
        self.assertTrue(admin.admin and admin.maintains_instance)
        self.assertFalse(dev.maintains_instance)
        self.assertIsNone(unknown.flavor)
        self.assertFalse(unknown.maintains_instance)
        self.assertFalse(missing.maintains_instance)

    def test_engine_internal_env_drops_only_shell_identity(self):
        env = engine_identity.engine_internal_env({
            "SC_API_TOKEN": "t", "SC_API_BASE": "b", "PATH": "/bin",
            "XDG_STATE_HOME": "/state",
        })
        self.assertEqual(env, {"PATH": "/bin", "XDG_STATE_HOME": "/state"})

    def test_every_gate_calls_the_same_resolver(self):
        for module in (_serialize_guard, engine_sql, map_setup):
            source = Path(module.__file__).read_text()
            self.assertIn("engine_identity.", source, module.__name__)
            self.assertNotIn("SC_ADMIN", source, module.__name__)


class SerializationGateTest(unittest.TestCase):
    """G5: a non-Admin token is refused even with SC_ADMIN=1 set."""

    SHELL = {"SC_API_TOKEN": "dev-token", "SC_API_BASE": "http://127.0.0.1:1",
             "SC_ADMIN": "1"}

    def _as(self, flavor, env):
        return (mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(engine_identity, "api_flavor", return_value=flavor))

    def assert_refused(self, caught):
        message = str(caught.exception)
        first_line = message.splitlines()[0]
        self.assertIn("shared instance serialization is an Admin step", first_line)
        self.assertIn("already live in the engine DB", first_line)
        self.assertLess(message.index("collide"), message.index("main checkout"))
        self.assertNotIn("SC_ADMIN", message)

    def test_snapshot_refuses_a_non_admin_token_despite_the_retired_flag(self):
        env, flavor = self._as("dev", self.SHELL)
        with env, flavor, \
             mock.patch.object(snapshot.instance_state, "active_database_path"), \
             mock.patch.object(snapshot, "_snapshot_via_runtime_api",
                               side_effect=AssertionError("serialized")), \
             self.assertRaises(SystemExit) as caught:
            snapshot.main()
        self.assert_refused(caught)

    def test_render_flat_refuses_a_non_admin_token_despite_the_retired_flag(self):
        env, flavor = self._as("dev", self.SHELL)
        with env, flavor, mock.patch.object(
            render, "_open", side_effect=AssertionError("shared DB opened"),
        ), self.assertRaises(SystemExit) as caught:
            render.main(["flat"])
        self.assert_refused(caught)

    def test_the_retired_flag_neither_grants_nor_is_required(self):
        # Admin token without the flag passes; the flag alone adds nothing.
        env, flavor = self._as("admin", {"SC_API_TOKEN": "admin-token"})
        with env, flavor:
            _serialize_guard.require_admin("snapshot")
        for flag in ({}, {"SC_ADMIN": "1"}):
            with self.subTest(flag=flag):
                env, flavor = self._as(
                    "reviewer", {"SC_API_TOKEN": "r", **flag})
                with env, flavor, self.assertRaises(SystemExit):
                    _serialize_guard.require_admin("render flat")

    def test_operator_terminal_without_a_token_may_serialize(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/operator"}, clear=True), \
             mock.patch.object(engine_identity, "api_flavor",
                               side_effect=AssertionError("no token to resolve")):
            _serialize_guard.require_admin("snapshot")


if __name__ == "__main__":
    unittest.main()
