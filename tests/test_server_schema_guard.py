#!/usr/bin/env python3
"""Startup refusals: a materialized-engine / unmigrated-DB half floor, and a
non-loopback bind (spec #26)."""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
ACK_MIGRATION = "0083_planner_alert_acknowledgement.sql"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402
import transport  # noqa: E402  (main()'s serve call — the line the guard precedes)


def build_pre_acknowledgement_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == ACK_MIGRATION:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration.name,))
        con.commit()
    finally:
        con.close()


class ServerSchemaGuardTest(unittest.TestCase):
    def test_new_code_old_schema_refuses_startup_with_rollback_recovery(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            build_pre_acknowledgement_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError) as raw:
                    con.execute(
                        "SELECT acknowledged_at FROM planner_alerts").fetchall()
            finally:
                con.close()
            self.assertIn("no such column", str(raw.exception))

            with mock.patch.object(
                server, "DB_PATH", db_path
            ), mock.patch.object(
                server.ports_mod, "resolve", return_value={"port": 8800}
            ), mock.patch.object(
                server.backfill_shell_api_keys, "backfill"
            ) as backfill, self.assertRaises(SystemExit) as refused:
                server.main([])

            message = str(refused.exception)
            self.assertIn("installed engine/DB schema mismatch", message)
            self.assertIn(ACK_MIGRATION, message)
            self.assertIn("before first DB use", message)
            self.assertIn("`./sc rollback --engine-only`", message)
            self.assertIn("preserving this unchanged DB", message)
            self.assertNotIn("no such column", message)
            backfill.assert_not_called()

    def test_current_migration_ledger_passes(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(SCHEMA.read_text())
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    con.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (?)",
                        (migration.name,))
                con.commit()
            finally:
                con.close()

            server.require_current_schema(db_path, MIGRATIONS)


class LoopbackBindGuardTest(unittest.TestCase):
    """Spec #26 Failure Modes: a non-loopback bind refuses to start.

    This is the fence behind the automatic browser bootstrap: a session mints
    for any caller that can present an allowed `Host` and a same-origin
    `Origin`, both of which a remote client chooses freely. Unreachability is
    therefore the control, and it has to be enforced rather than assumed.
    """

    def test_host_refuses_a_non_loopback_bind(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SC_SANDBOX", None)
            for bind in ("0.0.0.0", "", "::", "192.168.1.10",
                         "10.0.0.5", "example.com"):
                with self.subTest(bind=bind):
                    with self.assertRaises(SystemExit) as caught:
                        server.require_loopback_bind(bind)
                    self.assertIn("loopback", str(caught.exception))

    def test_host_accepts_loopback_binds(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SC_SANDBOX", None)
            for bind in ("127.0.0.1", "localhost", "LocalHost", "::1",
                         "[::1]", "127.0.0.53"):
                with self.subTest(bind=bind):
                    server.require_loopback_bind(bind)

    def test_sandbox_keeps_the_wide_bind_docker_publishes(self):
        # `./sc launch` sets SC_BIND=0.0.0.0 in the container ON PURPOSE so
        # docker can publish the port; the boundary there is the
        # `-p 127.0.0.1:PORT:PORT` mapping, which is loopback-only on the
        # host whatever the container binds. Refusing here would make the
        # sandbox unlaunchable while removing no exposure — so the guard
        # stands down, and only here.
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
            server.require_loopback_bind("0.0.0.0")


class LoopbackBindStartupWiringTest(unittest.TestCase):
    """The guard only fences anything if main() actually calls it.

    LoopbackBindGuardTest above pins the FUNCTION: delete the single
    `require_loopback_bind(bind)` line from main() and every one of its
    subtests still passes while the unsafe startup it exists to stop comes
    straight back. So these drive `server.main([])` itself, with the
    neighbouring startup steps stubbed, and assert the one thing that is
    load-bearing — an unsafe SC_BIND exits BEFORE the transport ever serves,
    and the sandbox's deliberate wide bind still reaches it.
    """

    def _start(self, bind, *, sandbox=False):
        """Run main([]) to the bind decision. Returns (refusal, served): the
        SystemExit main() raised or None, and the host transport.serve was
        actually called with or None if the listener was never reached."""
        served = []

        async def _fake_serve(host, port, http_handler, ws_handler, log=print):
            served.append(host)
            return port

        env = {"SC_BIND": bind}
        if sandbox:
            env["SC_SANDBOX"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shell_db.db"
            db_path.touch()
            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.dict(os.environ, env, clear=False))
                if not sandbox:
                    os.environ.pop("SC_SANDBOX", None)
                enter(mock.patch.object(server, "DB_PATH", db_path))
                enter(mock.patch.object(server.ports_mod, "resolve",
                                        return_value={"port": 8800}))
                # Everything main() does between the DB check and the bind is
                # someone else's contract — stub it so only the ordering of
                # guard vs. serve is under test here.
                enter(mock.patch.object(server, "require_current_schema"))
                enter(mock.patch.object(server.backfill_shell_api_keys,
                                        "backfill"))
                enter(mock.patch.object(server.mem_credentials, "provision"))
                enter(mock.patch.object(server.db_driver, "connect"))
                enter(mock.patch.object(server.interface_reconcile,
                                        "startup_reconcile", return_value={}))
                enter(mock.patch.object(server.pr_poller, "Poller"))
                enter(mock.patch.object(server, "interface_ws", None))
                enter(mock.patch.object(transport, "serve", _fake_serve))
                enter(contextlib.redirect_stdout(io.StringIO()))
                enter(contextlib.redirect_stderr(io.StringIO()))
                refusal = None
                try:
                    server.main([])
                except SystemExit as exc:
                    refusal = exc
        return refusal, (served[0] if served else None)

    def test_startup_refuses_an_unsafe_bind_before_serving(self):
        for bind in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.subTest(bind=bind):
                refusal, served = self._start(bind)
                self.assertIsNotNone(
                    refusal, "main() accepted a non-loopback bind on a host")
                self.assertIn("loopback", str(refusal))
                self.assertIn("SC_BIND", str(refusal))
                # The refusal is only a fence if the listener never opened —
                # assert that directly rather than inferring it from the exit.
                self.assertIsNone(
                    served, f"transport served {served!r} despite the refusal")

    def test_startup_serves_a_loopback_bind(self):
        self.assertEqual(self._start("127.0.0.1"), (None, "127.0.0.1"))

    def test_startup_serves_the_wide_bind_in_the_sandbox(self):
        # The counterweight: over-refusing here would make `./sc launch`
        # unbootable, so the sandbox exception has to survive the wiring too.
        self.assertEqual(self._start("0.0.0.0", sandbox=True),
                         (None, "0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
