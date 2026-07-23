#!/usr/bin/env python3
"""Runtime Admin credentials (spec doc #30 req 11, issue #516).

Two halves, one contract:

- `mem_credentials.provision` — the supervised API writes one owner-only
  (0600, dir 0700) artifact per live, keyed Admin shell under
  `.super-coder/run/mem/`, refreshes them every boot (key rotation), and
  sweeps artifacts whose shell is gone, demoted, deleted, or unkeyed.
- `mem.py` discovery — with BOTH SC_API_BASE/SC_API_TOKEN absent, `sc mem`
  adopts the unique Admin artifact and still calls the API; multiple Admins
  refuse until SC_MEM_AS names one; an insecure artifact and a stale
  (rotated) token refuse with the supported action.

Discovery tests stand up the real `server.Handler` against a throwaway engine
DB (same harness as test_mem — the token is the only identity), so a
discovered credential is proved against the actual auth path, not a stub.

Run:
    python3 tests/test_runtime_credentials.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mem  # noqa: E402
import mem_credentials  # noqa: E402
import server  # noqa: E402
from test_mem import TOKEN, PEER_TOKEN, build_engine_db  # noqa: E402


def build_shells_db(path: Path) -> None:
    """Engine-shaped DB with a keyed Admin, a keyed dev, an unkeyed Admin,
    and a deleted Admin — the full provisioning matrix."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    rows = [
        (1, "Adm One", "ADM1", "admin", "k-adm1", 0),
        (2, "Dev One", "DEV1", "dev", "k-dev1", 0),
        (3, "Adm Two", "ADM2", "admin", None, 0),
        (4, "Adm Three", "ADM3", "admin", "k-adm3", 1),
    ]
    con.executemany(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped, flavor, api_key, is_deleted) "
        "VALUES (?, ?, ?, 'm', 'sp', 1, 0, 1, 0, ?, ?, ?)", rows)
    con.commit()
    con.close()


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_shells_db(self.db)
        self.run_dir = self.tmp / "run" / "mem"

    def provision(self, base="http://127.0.0.1:8800"):
        return mem_credentials.provision(str(self.db), base, self.run_dir)

    def test_one_artifact_per_live_keyed_admin(self):
        names = self.provision()
        self.assertEqual(names, ["ADM1"])
        artifact = self.run_dir / "ADM1.json"
        data = json.loads(artifact.read_text())
        self.assertEqual(data["token"], "k-adm1")
        self.assertEqual(data["api_base"], "http://127.0.0.1:8800")
        self.assertEqual(data["shortname"], "ADM1")
        # dev / unkeyed / deleted shells get nothing
        self.assertFalse((self.run_dir / "DEV1.json").exists())
        self.assertFalse((self.run_dir / "ADM2.json").exists())
        self.assertFalse((self.run_dir / "ADM3.json").exists())

    def test_permissions_are_owner_only(self):
        self.provision()
        mode = stat.S_IMODE((self.run_dir / "ADM1.json").stat().st_mode)
        self.assertEqual(mode, 0o600)
        dmode = stat.S_IMODE(self.run_dir.stat().st_mode)
        self.assertEqual(dmode, 0o700)

    def test_reprovision_repairs_weakened_permissions(self):
        self.provision()
        artifact = self.run_dir / "ADM1.json"
        artifact.chmod(0o644)
        self.provision()
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_refresh_picks_up_key_rotation(self):
        self.provision()
        con = sqlite3.connect(self.db)
        con.execute("UPDATE shells SET api_key='k-adm1-rotated' WHERE shell_id=1")
        con.commit()
        con.close()
        self.provision()
        data = json.loads((self.run_dir / "ADM1.json").read_text())
        self.assertEqual(data["token"], "k-adm1-rotated")

    def test_stale_artifacts_are_swept(self):
        self.run_dir.mkdir(parents=True)
        stale = self.run_dir / "GONE.json"
        stale.write_text("{}")
        self.provision()
        self.assertFalse(stale.exists())
        # demote ADM1 → its artifact disappears on the next boot too
        con = sqlite3.connect(self.db)
        con.execute("UPDATE shells SET flavor='dev' WHERE shell_id=1")
        con.commit()
        con.close()
        self.assertEqual(self.provision(), [])
        self.assertFalse((self.run_dir / "ADM1.json").exists())


class DiscoveryTest(unittest.TestCase):
    """mem.py discovery against the real API auth path (no env wiring)."""

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

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.cred_dir = Path(tempfile.mkdtemp())
        self._saved = (mem.SC_API_TOKEN, mem.SC_API_BASE,
                       mem._CRED_DIR, mem._DISCOVERED_FROM)
        # Fully unwired client, discovery pointed at the temp dir.
        mem.SC_API_TOKEN = ""
        mem.SC_API_BASE = ""
        mem._CRED_DIR = self.cred_dir
        mem._DISCOVERED_FROM = None
        self.addCleanup(self._restore)

    def _restore(self):
        (mem.SC_API_TOKEN, mem.SC_API_BASE,
         mem._CRED_DIR, mem._DISCOVERED_FROM) = self._saved

    def write_artifact(self, shortname, token, mode=0o600, base=None):
        p = self.cred_dir / f"{shortname}.json"
        p.write_text(json.dumps({
            "shell_id": 1, "shortname": shortname,
            "api_base": base or f"http://127.0.0.1:{self.port}",
            "token": token,
        }))
        p.chmod(mode)
        return p

    def run_which(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mem.main(["which"])
        return rc, out.getvalue()

    def test_unique_admin_artifact_is_discovered(self):
        artifact = self.write_artifact("TC", TOKEN)
        rc, out = self.run_which()
        self.assertEqual(rc, 0)
        self.assertIn("TC", out)                       # whoami resolved shell 1
        self.assertIn("discovered from runtime artifact", out)
        self.assertEqual(mem._DISCOVERED_FROM, artifact)
        # discovery adopted the artifact's wiring, and it authenticates
        self.assertEqual(mem.SC_API_TOKEN, TOKEN)

    def test_env_wiring_wins_over_artifacts(self):
        self.write_artifact("TC", TOKEN)
        mem.SC_API_TOKEN = PEER_TOKEN
        mem.SC_API_BASE = f"http://127.0.0.1:{self.port}"
        rc, out = self.run_which()
        self.assertEqual(rc, 0)
        self.assertIn("Peer", out)                     # env identity, not TC's
        self.assertIsNone(mem._DISCOVERED_FROM)

    def test_ambiguous_admin_identity_refuses(self):
        self.write_artifact("TC", TOKEN)
        self.write_artifact("PEER", PEER_TOKEN)
        with self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("ambiguous", str(cm.exception))
        self.assertIn("SC_MEM_AS", str(cm.exception))

    def test_sc_mem_as_selects_among_admins(self):
        self.write_artifact("TC", TOKEN)
        self.write_artifact("PEER", PEER_TOKEN)
        with mock.patch.dict(os.environ, {"SC_MEM_AS": "peer"}):
            rc, out = self.run_which()
        self.assertEqual(rc, 0)
        self.assertIn("Peer", out)

    def test_sc_mem_as_unknown_shell_refuses(self):
        self.write_artifact("TC", TOKEN)
        with mock.patch.dict(os.environ, {"SC_MEM_AS": "NOPE"}), \
                self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("NOPE", str(cm.exception))

    def test_insecure_artifact_is_refused(self):
        self.write_artifact("TC", TOKEN, mode=0o644)
        with self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("owner-only", str(cm.exception))
        self.assertIn("restart", str(cm.exception))

    def test_malformed_artifact_is_refused(self):
        p = self.cred_dir / "TC.json"
        p.write_text("not json")
        p.chmod(0o600)
        with self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("malformed", str(cm.exception))

    def test_stale_credential_names_the_refresh_action(self):
        self.write_artifact("TC", "rotated-away-token")
        with self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("stale", str(cm.exception))
        self.assertIn("restart", str(cm.exception))

    def test_no_artifact_keeps_the_original_unwired_death(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_which()
        self.assertIn("isn't API-wired", str(cm.exception))
        self.assertIn("runtime", str(cm.exception))  # …and names the new path


if __name__ == "__main__":
    unittest.main()
