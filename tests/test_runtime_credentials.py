#!/usr/bin/env python3
"""Runtime Admin credentials (spec doc #30 req 11, issue #516).

Two halves, one contract:

- `mem_credentials.provision` — the supervised API writes one owner-only
  (0600, dir 0700) artifact per live, keyed Admin shell under
  `.super-coder/run/mem/`, refreshes them every boot (key rotation), and
  sweeps artifacts whose shell is gone, demoted, deleted, or unkeyed.
- `mem.py` discovery — with BOTH SC_API_BASE/SC_API_TOKEN absent, `sc mem`
  adopts the unique Admin artifact and still calls the API; multiple Admins
  refuse until SC_MEM_AS names one; a symlinked or otherwise insecure
  artifact and a stale (rotated) token refuse with the supported action.

The trust boundary is the real artifact, never a path: neither half may follow
a symlink planted at the artifact name — discovery refuses it, provisioning
replaces it — and `./sc token` gives the two refusal classes distinct nonzero
exit statuses (1 nothing-to-read, 2 unsafe artifact; spec doc #30 req 23).

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
import subprocess
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
import operator_token as sc_token  # noqa: E402
from test_mem import TOKEN, PEER_TOKEN, build_engine_db  # noqa: E402


def refuse(fn):
    """Run a refusing entrypoint; return (exit status, stderr text).

    `mem.die` keeps the historical `sys.exit("<message>")` for status 1 — there
    the message IS the SystemExit payload — and prints + exits with the number
    for the other classes. The process sees stderr plus a status either way, so
    normalise both shapes into that here."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            fn()
        except SystemExit as exc:
            if isinstance(exc.code, str):
                return 1, exc.code
            return exc.code, err.getvalue()
    raise AssertionError("expected a refusal, got a clean return")


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

    def test_symlinked_artifact_is_replaced_never_followed(self):
        """A same-user symlink at the artifact path must not become a write
        channel: provisioning replaces the link with a real 0600 file and the
        link's target keeps its contents (no truncate, no token written)."""
        self.run_dir.mkdir(parents=True)
        target = self.tmp / "victim.txt"
        target.write_text("not-a-credential")
        (self.run_dir / "ADM1.json").symlink_to(target)
        self.provision()
        artifact = self.run_dir / "ADM1.json"
        self.assertFalse(artifact.is_symlink())
        self.assertEqual(target.read_text(), "not-a-credential")
        self.assertEqual(json.loads(artifact.read_text())["token"], "k-adm1")
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        # the write goes through a temp inode — none of it is left behind
        self.assertEqual([p.name for p in self.run_dir.iterdir()], ["ADM1.json"])

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
        code, err = refuse(self.run_which)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("owner-only", err)
        self.assertIn("restart", err)

    def test_symlinked_artifact_is_refused(self):
        """A same-user symlink pointing at a perfectly valid, owner-only file
        must not be adopted: the trust boundary is the real artifact, and a
        link is someone else's choice of what discovery reads."""
        outside = Path(tempfile.mkdtemp()) / "planted.json"
        outside.write_text(json.dumps({
            "shell_id": 1, "shortname": "TC",
            "api_base": f"http://127.0.0.1:{self.port}", "token": TOKEN,
        }))
        outside.chmod(0o600)
        before = outside.read_text()
        (self.cred_dir / "TC.json").symlink_to(outside)
        code, err = refuse(self.run_which)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("symbolic link", err)
        self.assertIsNone(mem._DISCOVERED_FROM)      # nothing adopted
        self.assertEqual(mem.SC_API_TOKEN, "")
        self.assertEqual(outside.read_text(), before)  # target untouched

    def test_dangling_symlink_is_refused_not_reported_missing(self):
        (self.cred_dir / "TC.json").symlink_to(self.cred_dir / "gone.json")
        code, err = refuse(self.run_which)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("symbolic link", err)

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


class TokenCommandTest(unittest.TestCase):
    """`./sc token` (spec doc #30 req 23) — artifact-only read of the Admin
    runtime credential: stdout carries ONLY the token; missing/unreadable/
    insecure artifacts refuse on stderr with the service action; help labels
    the value without printing it. No API needed — the artifact is the
    contract, so these tests run without a server."""

    def setUp(self):
        self.cred_dir = Path(tempfile.mkdtemp())
        self._saved = (mem.SC_API_TOKEN, mem.SC_API_BASE,
                       mem._CRED_DIR, mem._DISCOVERED_FROM, mem._PROG)
        mem.SC_API_TOKEN = ""
        mem.SC_API_BASE = ""
        mem._CRED_DIR = self.cred_dir
        mem._DISCOVERED_FROM = None
        self.addCleanup(self._restore)

    def _restore(self):
        (mem.SC_API_TOKEN, mem.SC_API_BASE,
         mem._CRED_DIR, mem._DISCOVERED_FROM, mem._PROG) = self._saved

    def write_artifact(self, shortname, token, mode=0o600):
        p = self.cred_dir / f"{shortname}.json"
        p.write_text(json.dumps({
            "shell_id": 1, "shortname": shortname,
            "api_base": "http://127.0.0.1:8800",
            "token": token,
        }))
        p.chmod(mode)
        return p

    def run_token(self, argv=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sc_token.main(list(argv))
        return rc, out.getvalue()

    def test_prints_exactly_the_token(self):
        self.write_artifact("TC", TOKEN)
        rc, out = self.run_token()
        self.assertEqual(rc, 0)
        self.assertEqual(out, TOKEN + "\n")   # stdout purity — nothing else

    def test_sc_mem_as_selects_among_admins(self):
        self.write_artifact("TC", TOKEN)
        self.write_artifact("PEER", PEER_TOKEN)
        with mock.patch.dict(os.environ, {"SC_MEM_AS": "peer"}):
            rc, out = self.run_token()
        self.assertEqual(rc, 0)
        self.assertEqual(out, PEER_TOKEN + "\n")

    def test_ambiguity_refuses_without_printing(self):
        self.write_artifact("TC", TOKEN)
        self.write_artifact("PEER", PEER_TOKEN)
        with self.assertRaises(SystemExit) as cm:
            self.run_token()
        self.assertIn("ambiguous", str(cm.exception))
        self.assertIn("SC_MEM_AS", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(PEER_TOKEN, str(cm.exception))

    def test_missing_artifact_names_the_service_action(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_token()
        msg = str(cm.exception)
        self.assertIn("sc token:", msg)            # refusal names this command
        self.assertIn("./sc restart", msg)
        self.assertIn("make dos-r", msg)

    def test_insecure_artifact_is_refused(self):
        self.write_artifact("TC", TOKEN, mode=0o644)
        code, err = refuse(self.run_token)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("owner-only", err)
        self.assertNotIn(TOKEN, err)                # refusal never leaks the value

    def test_symlinked_artifact_is_refused(self):
        outside = Path(tempfile.mkdtemp()) / "planted.json"
        outside.write_text(json.dumps({
            "shell_id": 1, "shortname": "TC",
            "api_base": "http://127.0.0.1:8800", "token": TOKEN,
        }))
        outside.chmod(0o600)
        before = outside.read_text()
        (self.cred_dir / "TC.json").symlink_to(outside)
        code, err = refuse(self.run_token)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("symbolic link", err)
        self.assertNotIn(TOKEN, err)
        self.assertEqual(outside.read_text(), before)

    def test_malformed_artifact_is_refused(self):
        p = self.cred_dir / "TC.json"
        p.write_text("not json")
        p.chmod(0o600)
        with self.assertRaises(SystemExit) as cm:
            self.run_token()
        self.assertIn("malformed", str(cm.exception))

    def test_env_wiring_never_substitutes_for_the_artifact(self):
        # An injected SC_API_TOKEN must not bypass the artifact contract:
        # insecure artifact still refuses even when env is fully wired.
        self.write_artifact("TC", TOKEN, mode=0o644)
        mem.SC_API_TOKEN = "env-token"
        mem.SC_API_BASE = "http://127.0.0.1:8800"
        code, err = refuse(self.run_token)
        self.assertEqual(code, mem.EXIT_UNSAFE)
        self.assertIn("owner-only", err)

    def test_help_labels_without_printing(self):
        self.write_artifact("TC", TOKEN)
        rc, out = self.run_token(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("operator capability", out)
        self.assertIn("./sc token", out)
        self.assertNotIn(TOKEN, out)

    def run_script(self):
        """Real interpreter, real process — the only place the exit *status*
        (as opposed to the SystemExit payload) is observable."""
        env = dict(os.environ, SC_MEM_CRED_DIR=str(self.cred_dir))
        env.pop("SC_MEM_AS", None)
        return subprocess.run(
            [sys.executable, str(ENGINE / "scripts" / "operator_token.py")],
            capture_output=True, text=True, env=env)

    def test_script_end_to_end(self):
        """Real interpreter, real artifact: stdout is the token and only the
        token. (The `token)` line in ./sc is the same one-line exec pattern as
        every other verb; a bash-dispatcher subprocess test would resolve the
        engine at the MAIN worktree root and break in linked dev worktrees.)"""
        self.write_artifact("TC", TOKEN)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, TOKEN + "\n")

    def test_script_exit_status_unavailable(self):
        """Nothing to read (service not running / no artifact) → status 1."""
        proc = self.run_script()
        self.assertEqual(proc.returncode, mem.EXIT_UNAVAILABLE)
        self.assertEqual(proc.stdout, "")
        self.assertIn("./sc restart", proc.stderr)

    def test_script_exit_status_unsafe_artifact(self):
        """An artifact that fails the trust boundary is a DIFFERENT nonzero
        status from 'nothing to read' (spec doc #30 req 23) — a caller can
        branch on it without parsing stderr."""
        self.write_artifact("TC", TOKEN, mode=0o644)
        proc = self.run_script()
        self.assertEqual(proc.returncode, mem.EXIT_UNSAFE)
        self.assertNotEqual(mem.EXIT_UNSAFE, mem.EXIT_UNAVAILABLE)
        self.assertEqual(proc.stdout, "")
        self.assertIn("owner-only", proc.stderr)
        self.assertNotIn(TOKEN, proc.stderr)

    def test_script_symlinked_artifact_refuses_without_printing(self):
        outside = Path(tempfile.mkdtemp()) / "planted.json"
        outside.write_text(json.dumps({
            "shell_id": 1, "shortname": "TC",
            "api_base": "http://127.0.0.1:8800", "token": TOKEN,
        }))
        outside.chmod(0o600)
        before = outside.read_text()
        (self.cred_dir / "TC.json").symlink_to(outside)
        proc = self.run_script()
        self.assertEqual(proc.returncode, mem.EXIT_UNSAFE)
        self.assertEqual(proc.stdout, "")            # the planted token stays unread
        self.assertIn("symbolic link", proc.stderr)
        self.assertNotIn(TOKEN, proc.stderr)
        self.assertEqual(outside.read_text(), before)


if __name__ == "__main__":
    unittest.main()
