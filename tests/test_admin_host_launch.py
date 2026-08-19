"""Admin singleton convergence and the host-only launcher contract."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATION = ENGINE / "migrations" / "0186_admin_singleton.sql"
sys.path.insert(0, str(ENGINE / "scripts"))
run = importlib.import_module("run")
compose = importlib.import_module("compose")
shell_factory = importlib.import_module("shell_factory")
init_fork = importlib.import_module("init_fork")


class AdminSingletonMigrationTest(unittest.TestCase):
    def test_dirty_fixture_converges_idempotently_and_refuses_future_duplicates(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE shells ("
            "shell_id INTEGER PRIMARY KEY, flavor TEXT, "
            "is_deleted INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE archives ("
            "archive_id INTEGER PRIMARY KEY, shell_id INTEGER REFERENCES shells);"
            "INSERT INTO shells VALUES (4,'admin',0);"
            "INSERT INTO shells VALUES (7,'dev',0);"
            "INSERT INTO shells VALUES (9,'admin',0);"
            "INSERT INTO shells VALUES (12,'admin',0);"
            "INSERT INTO shells VALUES (15,'admin',1);"
            "INSERT INTO archives VALUES (1,9);"
        )

        for _ in range(2):
            con.executescript(MIGRATION.read_text())

        self.assertEqual(
            con.execute(
                "SELECT shell_id,is_deleted FROM shells WHERE flavor='admin' "
                "ORDER BY shell_id"
            ).fetchall(),
            [(4, 0), (9, 1), (12, 1), (15, 1)],
        )
        self.assertEqual(
            con.execute("SELECT shell_id FROM archives").fetchall(), [(9,)]
        )
        self.assertEqual(
            con.execute(
                "SELECT shell_id FROM shells WHERE flavor='dev' AND is_deleted=0"
            ).fetchall(),
            [(7,)],
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
            con.execute("INSERT INTO shells VALUES (20,'admin',0)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
            con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=7")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
            con.execute("UPDATE shells SET is_deleted=0 WHERE shell_id=9")


class HostAdminSelectionTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute(
            "CREATE TABLE shells ("
            "shell_id INTEGER PRIMARY KEY, display_name TEXT, shortname TEXT, "
            "mandate TEXT, is_shared INTEGER, flavor TEXT, current_state TEXT, "
            "is_deleted INTEGER DEFAULT 0)"
        )
        self.addCleanup(self.con.close)

    def add(self, shell_id: int, shortname: str, flavor: str, deleted: int = 0):
        self.con.execute(
            "INSERT INTO shells VALUES (?, ?, ?, 'm', 0, ?, 'state', ?)",
            (shell_id, shortname, shortname, flavor, deleted),
        )

    def test_resolves_only_the_sole_active_admin(self):
        self.add(2, "DEV1", "dev")
        self.add(5, "ADM1", "admin")
        self.add(8, "ADM2", "admin", 1)

        chosen = run.select_host_admin(self.con)

        self.assertEqual(
            (chosen["shell_id"], chosen["shortname"], chosen["flavor"]),
            (5, "ADM1", "admin"),
        )
        self.assertEqual(run.select_host_admin(self.con, "adm1")["shell_id"], 5)
        with self.assertRaisesRegex(run.LaunchError, "is not the sole active Admin"):
            run.select_host_admin(self.con, "DEV1")

    def test_zero_and_legacy_duplicate_admin_states_fail_without_fallback(self):
        self.add(2, "DEV1", "dev")
        with self.assertRaisesRegex(run.LaunchError, "no active Admin exists"):
            run.select_host_admin(self.con)
        self.add(5, "ADM1", "admin")
        self.add(8, "ADM2", "admin")
        with self.assertRaisesRegex(run.LaunchError, "active shell ids: 5, 8"):
            run.select_host_admin(self.con)

    def test_factory_refuses_a_second_admin_before_insert_or_session_open(self):
        self.add(5, "ADM1", "admin")
        before = self.con.execute("SELECT COUNT(*) FROM shells").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "admin is a singleton"):
            shell_factory.create_shell(
                self.con, flavor="admin", name="Other Admin", shortname="ADM2"
            )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM shells").fetchone()[0], before
        )

    def test_fresh_roster_declares_exactly_one_admin(self):
        self.assertEqual(
            [slot for slot in init_fork.TEAM_ROSTER if slot[0] == "admin"],
            [("admin", "Admin", "ADM1")],
        )


class AdminDispatcherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        scripts = self.root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(ENGINE / "scripts" / "dispatch.sh", scripts / "dispatch.sh")
        (scripts / "artifact_policy.py").write_text(
            "from pathlib import Path\nprint(Path.cwd() / '.sc-state' / 'map.db')\n"
        )
        (scripts / "run.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], "
            "'sandbox_present': 'SC_SANDBOX' in os.environ}))\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text("#!/bin/sh\necho docker-was-called >&2\nexit 99\n")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *args: str, sandbox: bool = False):
        env = {
            **os.environ,
            "SC_CALLER_ROOT": str(self.root),
            "SC_PYTHON": sys.executable,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        if sandbox:
            env["SC_SANDBOX"] = "1"
        return subprocess.run(
            ["sh", str(self.root / ".super-coder" / "scripts" / "dispatch.sh"), *args],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_admin_dispatches_directly_to_host_launch_mode_without_docker(self):
        completed = self.invoke("admin", "--harness", "codex")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "argv": ["--host-admin", "--harness", "codex"],
                "sandbox_present": False,
            },
        )
        self.assertNotIn("docker-was-called", completed.stderr)

    def test_admin_help_is_available_without_running_launcher(self):
        completed = self.invoke("admin", "--help", sandbox=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: ./sc admin", completed.stdout)
        self.assertIn("no Docker or API is required", completed.stdout)
        self.assertNotIn('["--host-admin"', completed.stdout)

    def test_admin_refuses_inside_sandbox_before_running_launcher(self):
        completed = self.invoke("admin", sandbox=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("run make dos-admin from a host terminal", completed.stderr)
        self.assertNotIn('["--host-admin"]', completed.stdout)


class AdminExecutionContextTest(unittest.TestCase):
    def test_container_admin_names_contained_limits_and_host_exit(self):
        context = compose.render_execution_context("admin", "container")
        api_guidance = compose.render_api_unreachable_guidance("admin", "container")

        self.assertIn("inside the sandbox container", context)
        self.assertIn("0.0.0.0:$SC_DEV_PORT", context)
        self.assertIn("running `make dos-admin` from a host terminal", context)
        self.assertNotIn("Host authority covers", context)
        self.assertIn("surface it to the FnB and stop", api_guidance)
        self.assertNotIn("host Admin boot remains valid", api_guidance)

    def test_host_admin_names_authority_role_boundary_and_offline_recovery(self):
        context = compose.render_execution_context("admin", "host")
        api_guidance = compose.render_api_unreachable_guidance("admin", "host")

        self.assertIn("directly on the host", context)
        self.assertIn("bound to `127.0.0.1`", context)
        self.assertIn("engine update, rollback, migration", context)
        self.assertIn("ownership stay with Dev and DevOps", context)
        self.assertIn("operator owns avoiding simultaneous use", context)
        self.assertNotIn("0.0.0.0:$SC_DEV_PORT", context)
        self.assertIn("host Admin boot remains valid", api_guidance)
        self.assertIn("`sc mem` stays unavailable until the API returns", api_guidance)
        self.assertIn("never falls back to raw writes", api_guidance)
        self.assertNotIn("surface it to the FnB", api_guidance)

    def test_unknown_launch_mode_is_rejected_instead_of_misrendered(self):
        with self.assertRaisesRegex(ValueError, "unsupported launch mode: vm"):
            compose.render_execution_context("admin", "vm")


class AdminFocusTest(unittest.TestCase):
    def test_admin_template_has_no_standing_sweep(self):
        admin = json.loads((ENGINE / "templates" / "shells" / "admin.json").read_text())

        self.assertIn("there is no standing every-session sweep", admin["focus"])
        self.assertIn("Recovery work takes precedence", admin["focus"])
        self.assertNotIn("Run your standing maintenance pass", admin["focus"])
        self.assertNotIn("`flag_sweep`", admin["focus"])


if __name__ == "__main__":
    unittest.main()
