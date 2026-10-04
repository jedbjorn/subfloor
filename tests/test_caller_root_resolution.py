#!/usr/bin/env python3
"""Caller-root resolution for project-subject verbs (spec #267 U3, decision #428).

Every checkout of an install dispatches the LIVE engine floor at the main
checkout, and the engine's scripts used to compute their project as
``ENGINE.parent`` — the main checkout — from every shell worktree. These cases
drive the real tracked ``sc`` bootstrap from a real LINKED worktree of a real
git repository whose main checkout holds the engine (as a fork does: the
engine is gitignored, so the worktree has none and any pass must come from
caller-root resolution, not from a local engine copy). The two checkouts carry
different markers for each verb; every case asserts the worktree marker is the
one acted on and the main-checkout marker never is.

One test per verb, named after it, so an issue can cite
``tests/test_caller_root_resolution.py::<Class>::test_<verb>``.

The dr_* catalogue is the deliberate exception (decision #429): it is
live-instance state whose subject is the install's main line, so ``map``,
``map-setup`` and ``map finalize`` from a worktree map the LIVE root and say so
(``CatalogueSubjectTest``); only an extractor candidate path is caller-resolved.

The engine under test is copied from this checkout; the fixture never reads a
live ``instance.json`` and runs with the ambient engine environment scrubbed
and a private XDG state home.

Run:  python3 -m pytest tests/test_caller_root_resolution.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import project_root
import sc_wrapper

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "shell_db.db*", "backups", "node_modules",
    "logs", "run", "instance.json",
)
HOOKS = ("deps", "test", "lint", "typecheck")
SCRUB = (
    "SC_API_BASE", "SC_API_TOKEN", "SC_ROOT", "SC_ENGINE_DIR", "SC_SHELL_FLAVOR",
    "SC_SHELL_WORKTREE", "SC_SHELL_ID", "SC_SHELL_SHORTNAME", "SC_SHELL_NAME",
    "SC_CALLER_ROOT", "SC_DISPATCH", "SC_SANDBOX", "SC_ADMIN", "SC_MEM_CREDENTIAL_FILE",
    "SC_DEVKIT_OUTPUT", *project_root.VARIABLES,
)
# A global core.hooksPath (or any global/system git config) must never fire
# inside the fixture's git operations.
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True,
        text=True, env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)
    return path


class _RecordingApi(BaseHTTPRequestHandler):
    """Authenticated stand-in for the engine API; records every request."""

    requests: list[tuple[str, str, object]] = []
    context: dict = {}

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).requests.append(("GET", self.path, None))
        if urlparse(self.path).path == "/_sc/context":
            return self._reply(type(self).context)
        return self._reply({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw) if raw else None
        type(self).requests.append(("POST", self.path, payload))
        return self._reply({"ok": True, "document_id": 7})

    do_PATCH = do_POST

    def log_message(self, *_args):
        pass


class CallerRootFixture(unittest.TestCase):
    """A fork-shaped main checkout holding the engine plus linked worktrees.

    ``main`` is on branch main with MAIN markers; ``wt`` is a linked worktree
    on ``feature`` whose commit swaps every marker to WT; ``dev`` is a second
    linked worktree on main (a different shell); ``detached`` is a detached
    linked worktree at the feature commit.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="sc_caller_root_")
        cls.addClassCleanup(shutil.rmtree, cls._tmp, ignore_errors=True)
        tmp = Path(cls._tmp).resolve()
        cls.state = tmp / "state"
        cls.home = tmp / "home"
        cls.state.mkdir()
        cls.home.mkdir()
        cls.main = tmp / "main"
        cls.main.mkdir()
        shutil.copytree(ENGINE, cls.main / ".super-coder", ignore=IGNORE)
        shutil.copy2(REPO / "sc", cls.main / "sc")
        (cls.main / "sc").chmod(0o755)
        write(cls.main / ".gitignore", ".super-coder/\n.sc-state/local/\n")
        cls._markers(cls.main, "MAIN")
        git(cls.main, "init", "-q", "-b", "main")
        git(cls.main, "add", "-A")
        git(cls.main, "commit", "-qm", "main markers")

        cls.wt = tmp / "linked-worktree"
        git(cls.main, "worktree", "add", "-q", "-b", "feature", str(cls.wt))
        for path in cls.main.rglob("main_only.txt"):
            (cls.wt / path.relative_to(cls.main)).unlink()
        cls._markers(cls.wt, "WT")
        git(cls.wt, "add", "-A")
        git(cls.wt, "commit", "-qm", "worktree markers")
        cls.dev = tmp / "dev-worktree"
        git(cls.main, "worktree", "add", "-q", "-b", "dev", str(cls.dev), "main")
        cls.detached = tmp / "detached-worktree"
        git(cls.main, "worktree", "add", "-q", "--detach", str(cls.detached), "feature")

        cls.bin = tmp / "bin"
        write(cls.bin / "sc", sc_wrapper.WRAPPER_TEXT, 0o755)

    @staticmethod
    def _markers(root: Path, side: str) -> None:
        hook = (
            "#!/bin/sh\n"
            f"echo \"hook=$1 side={side} devkit_root=$SC_DEVKIT_ROOT "
            "project_env=${SC_PROJECT_ROOT:-unset}\"\n"
        )
        write(root / ".subfloor" / "hook", hook, 0o755)
        write(root / ".subfloor" / "dev-kit.json", json.dumps({
            "version": 1,
            "hooks": {name: {"argv": ["./.subfloor/hook", name]} for name in HOOKS},
        }))
        write(root / "sub" / "note.md", f"{side}-SUBDIR-BODY\n")
        write(root / "note.md", f"{side}-ROOT-BODY\n")
        if side == "MAIN":
            write(root / "main_only.txt", "main\n")
        else:
            write(root / "wt_only.txt", "worktree\n")
            write(root / ".sc-state" / "visual-qa.json",
                  json.dumps({"serve": "true", "routes": ["/"]}))

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env.update(XDG_STATE_HOME=str(self.state), HOME=str(self.home),
                   SC_PYTHON=sys.executable, **GIT_ENV)
        env.update(extra)
        return env

    def sc(self, checkout: Path, *args: str, cwd: Path | None = None,
           **extra: str) -> subprocess.CompletedProcess:
        """The tracked bootstrap of ``checkout``, typed from ``cwd``."""
        return subprocess.run(
            [str(checkout / "sc"), *args], cwd=str(cwd or checkout),
            env=self.env(**extra), capture_output=True, text=True,
            timeout=600, check=False,
        )

    def bare_sc(self, *args: str, cwd: Path, **extra: str) -> subprocess.CompletedProcess:
        """Bare ``sc`` through the managed checkout-selecting wrapper on PATH."""
        env = self.env(**extra)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            ["sc", *args], cwd=str(cwd), env=env, capture_output=True,
            text=True, timeout=600, check=False,
        )

    def assert_ok(self, done: subprocess.CompletedProcess) -> None:
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def map_repo_row(self) -> tuple[str, set[str]]:
        db = self.main / ".sc-state" / "local" / "map" / "map.db"
        con = sqlite3.connect(db)
        try:
            root = con.execute("SELECT root FROM dr_repo WHERE repo_id=1").fetchone()[0]
            paths = {row[0] for row in con.execute("SELECT path FROM dr_filepath")}
        finally:
            con.close()
        return root, paths

    def start_api(self, context: dict | None = None) -> str:
        _RecordingApi.requests = []
        _RecordingApi.context = context or {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingApi)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"


class DevKitHookTest(CallerRootFixture):
    """deps/test/lint/typecheck run the INVOKING checkout's declaration."""

    def run_hook(self, hook: str, **kw) -> subprocess.CompletedProcess:
        done = self.sc(self.wt, hook, cwd=self.wt / "sub", SC_DEVKIT_OUTPUT="full", **kw)
        self.assert_ok(done)
        self.assertIn(f"hook={hook} side=WT devkit_root={self.wt}", done.stdout)
        self.assertNotIn("side=MAIN", done.stdout)
        # The hook is fork code: it never inherits the dispatcher's identity.
        self.assertIn("project_env=unset", done.stdout)
        return done

    def test_deps(self):
        self.run_hook("deps")

    def test_test(self):
        self.run_hook("test")

    def test_lint(self):
        self.run_hook("lint")
        # Compact mode: the log receipt is relative to, and lands in, the
        # reported checkout — never the live checkout's .sc-state.
        done = self.sc(self.wt, "lint", cwd=self.wt / "sub")
        self.assert_ok(done)
        self.assertIn(f"dev-kit checkout: {self.wt}", done.stderr)
        log = re.search(r"^dev-kit log: (.+)$", done.stderr, re.MULTILINE).group(1)
        self.assertIn("side=WT", (self.wt / log).read_text())
        self.assertFalse((self.main / ".sc-state" / "local" / "devkit-logs").exists())

    def test_typecheck(self):
        self.run_hook("typecheck")

    def test_bare_sc_wrapper(self):
        """Bare ``sc`` (the managed wrapper) typed in a worktree subdirectory."""
        done = self.bare_sc("deps", cwd=self.wt / "sub", SC_DEVKIT_OUTPUT="full")
        self.assert_ok(done)
        self.assertIn(f"hook=deps side=WT devkit_root={self.wt}", done.stdout)
        self.assertNotIn("side=MAIN", done.stdout)

    def test_detached_worktree(self):
        done = self.bare_sc("test", cwd=self.detached, SC_DEVKIT_OUTPUT="full")
        self.assert_ok(done)
        self.assertIn(f"hook=test side=WT devkit_root={self.detached}", done.stdout)
        self.assertNotIn("side=MAIN", done.stdout)


class ProjectVerbTest(CallerRootFixture):
    """Every other project-subject verb resolves from the caller checkout."""

    def test_visual_qa(self):
        done = self.sc(self.wt, "visual-qa", "setup-ci", cwd=self.wt / "sub")
        self.assert_ok(done)
        workflows = ".github/workflows/subfloor-visual-qa-capture.yml"
        self.assertTrue((self.wt / workflows).is_file())
        self.assertFalse((self.main / workflows).exists())

    def test_context(self):
        base = self.start_api({"resources": {"dev_hooks": {"state": "absent", "hooks": []}}})
        done = self.sc(self.wt, "context", "--task", "5", "--json", cwd=self.wt / "sub",
                       SC_API_BASE=base, SC_API_TOKEN="fixture-shell-token")
        self.assert_ok(done)
        hooks = json.loads(done.stdout)["resources"]["dev_hooks"]
        self.assertEqual(hooks["state"], "declared")
        self.assertEqual(hooks["hooks"], list(HOOKS))
        self.assertEqual(hooks["checkout"], str(self.wt))

    def sprint_body(self, *args: str) -> object:
        base = self.start_api()
        done = self.sc(self.wt, "sprint", *args, cwd=self.wt / "sub",
                       SC_API_BASE=base, SC_API_TOKEN="fixture-shell-token")
        self.assert_ok(done)
        posts = [p for method, _path, p in _RecordingApi.requests if method == "POST"]
        self.assertEqual(len(posts), 1, _RecordingApi.requests)
        return posts[0]

    def test_sprint_send_body_file(self):
        posted = self.sprint_body("send", "--sprint", "1", "--to", "PLN1",
                                  "--body-file", "note.md", "--key", "k1")
        self.assertEqual(posted["body"], "WT-SUBDIR-BODY")

    def test_sprint_complete_unit_result_file(self):
        posted = self.sprint_body("complete-unit", "--sprint", "1", "--work-unit", "2",
                                  "--result-file", "note.md")
        self.assertIn("WT-SUBDIR-BODY", json.dumps(posted))
        self.assertNotIn("MAIN", json.dumps(posted))

    def test_sprint_request_review_readiness_file(self):
        posted = self.sprint_body("request-review", "--sprint", "1", "--registered-pr", "3",
                                  "--readiness-file", "../note.md", "--key", "k2")
        # `../note.md` typed in wt/sub names the worktree root's file.
        self.assertEqual(posted["readiness"], "WT-ROOT-BODY")
        self.assertNotIn("MAIN", json.dumps(posted))

    def test_mem_doc_body_file(self):
        base = self.start_api()
        done = self.sc(self.wt, "mem", "doc", "add", "Fixture doc", "--body-file", "note.md",
                       cwd=self.wt / "sub", SC_API_BASE=base,
                       SC_API_TOKEN="fixture-shell-token")
        self.assert_ok(done)
        posts = [p for method, _path, p in _RecordingApi.requests if method == "POST"]
        self.assertEqual(posts[-1]["body"], "WT-SUBDIR-BODY\n")

    def test_job(self):
        start = self.sc(self.wt, "job", "start", "--label", "cwdprobe", "--",
                        "sh", "-c", "pwd -P; echo project_env=${SC_PROJECT_ROOT:-unset}",
                        cwd=self.wt / "sub")
        self.assert_ok(start)
        job_id = re.search(r"job: (\S+) started", start.stdout).group(1)
        log = self.main / ".super-coder" / "run" / "jobs" / job_id / "log"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "project_env=" not in log.read_text():
            time.sleep(0.1)
        self.assertEqual(log.read_text().split(), [str(self.wt / "sub"), "project_env=unset"])

    def test_skill_put_file(self):
        """A relative draft path is read from the subdirectory it was typed in."""
        db = self.main / ".super-coder" / "shell_db.db"
        con = sqlite3.connect(db)
        con.executescript((self.main / ".super-coder" / "schema.sql").read_text())
        if "api_key" not in {row[1] for row in con.execute("PRAGMA table_info(shells)")}:
            con.execute("ALTER TABLE shells ADD COLUMN api_key TEXT")
        con.execute("INSERT INTO shells (display_name, shortname, flavor, system_prompt, api_key) "
                    "VALUES ('Planner', 'PLN1', 'planner', '', 'fixture-planner-token')")
        con.commit()
        con.close()
        self.addCleanup(db.unlink, missing_ok=True)
        write(self.wt / "sub" / "draft.md", "WT draft without frontmatter\n")
        self.addCleanup((self.wt / "sub" / "draft.md").unlink, missing_ok=True)
        done = self.sc(self.wt, "skill", "put", "--file", "draft.md", cwd=self.wt / "sub",
                       SC_API_TOKEN="fixture-planner-token")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f"sc skill: draft {self.wt / 'sub' / 'draft.md'}:", done.stderr)
        self.assertNotIn("cannot read draft", done.stderr)

    def command_file_case(self, *verb: str) -> None:
        write(self.wt / "sub" / "cmd.sh", "echo WT\n")
        self.addCleanup((self.wt / "sub" / "cmd.sh").unlink, missing_ok=True)
        found = self.sc(self.wt, *verb, "--command-file", "cmd.sh", "--json",
                        cwd=self.wt / "sub")
        missing = self.sc(self.wt, *verb, "--command-file", "absent.sh", "--json",
                          cwd=self.wt / "sub")
        unreadable = "the command file could not be read as UTF-8"
        self.assertIn(unreadable, missing.stdout + missing.stderr)
        self.assertNotIn(unreadable, found.stdout + found.stderr)

    def test_vm_command_file(self):
        self.command_file_case("vm", "exec")

    def test_remote_command_file(self):
        self.command_file_case("remote", "exec", "fixture-box")

    def test_actions_artifacts(self):
        done = self.sc(self.wt, "actions-artifacts", "setup-ci", cwd=self.wt / "sub")
        self.assert_ok(done)
        workflow = ".github/workflows/subfloor-artifact-cleanup.yml"
        self.assertTrue((self.wt / workflow).is_file())
        self.assertFalse((self.main / workflow).exists())

    def test_forged_identity_is_ignored(self):
        """An inherited project identity naming main never redirects a worktree
        command: the dispatcher clears it and re-derives the caller."""
        done = self.sc(self.wt, "sprint", "send", "--sprint", "1", "--to", "PLN1",
                       "--body-file", "note.md", "--key", "forged",
                       cwd=self.wt / "sub", SC_API_BASE=self.start_api(),
                       SC_API_TOKEN="fixture-shell-token",
                       SC_PROJECT_ROOT=str(self.main),
                       SC_INVOCATION_CWD=str(self.main / "sub"),
                       SC_PROJECT_ENGINE=str(self.main / ".super-coder"))
        self.assert_ok(done)
        posts = [p for method, _path, p in _RecordingApi.requests if method == "POST"]
        self.assertEqual(posts[-1]["body"], "WT-SUBDIR-BODY")
        hooks = self.sc(self.wt, "deps", cwd=self.wt / "sub", SC_DEVKIT_OUTPUT="full",
                        SC_PROJECT_ROOT=str(self.main),
                        SC_PROJECT_ENGINE=str(self.main / ".super-coder"))
        self.assert_ok(hooks)
        self.assertIn("side=WT", hooks.stdout)
        self.assertIn("project_env=unset", hooks.stdout)

    def test_admin_seat_unchanged(self):
        """From the main checkout, project verbs act on the main checkout, and a
        relative file still resolves from the subdirectory typed in."""
        deps = self.sc(self.main, "deps", cwd=self.main / "sub", SC_DEVKIT_OUTPUT="full")
        self.assert_ok(deps)
        self.assertIn(f"hook=deps side=MAIN devkit_root={self.main}", deps.stdout)
        posted = None
        done = self.sc(self.main, "sprint", "send", "--sprint", "1", "--to", "PLN1",
                       "--body-file", "note.md", "--key", "admin", cwd=self.main / "sub",
                       SC_API_BASE=self.start_api(), SC_API_TOKEN="fixture-shell-token")
        self.assert_ok(done)
        posted = [p for method, _path, p in _RecordingApi.requests if method == "POST"][-1]
        self.assertEqual(posted["body"], "MAIN-SUBDIR-BODY")


class CatalogueSubjectTest(CallerRootFixture):
    """The dr_* catalogue maps the install's main line, never the caller
    (decision #429): a worktree run maps the live root and names that first."""

    def notice(self, verb: str) -> str:
        return (f"sc {verb}: the catalogue is one shared index of the main checkout, "
                f"not of this worktree; mapping {self.main} (main@")

    def mapped(self) -> tuple[str, set[str], str]:
        db = self.main / ".sc-state" / "local" / "map" / "map.db"
        con = sqlite3.connect(db)
        try:
            root, at = con.execute(
                "SELECT root, mapped_at FROM dr_repo WHERE repo_id=1").fetchone()
            paths = {row[0] for row in con.execute("SELECT path FROM dr_filepath")}
        finally:
            con.close()
        return root, paths, at

    def describe(self, path: str, desc: str) -> None:
        con = sqlite3.connect(self.main / ".sc-state" / "local" / "map" / "map.db")
        try:
            con.execute("UPDATE dr_filepath SET desc=? WHERE path=?", (desc, path))
            con.commit()
        finally:
            con.close()

    def desc_of(self, path: str) -> str | None:
        con = sqlite3.connect(self.main / ".sc-state" / "local" / "map" / "map.db")
        try:
            row = con.execute("SELECT desc FROM dr_filepath WHERE path=?", (path,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def setUp(self):
        self.assert_ok(self.sc(self.main, "map"))

    def test_map(self):
        """#1354: from a worktree the live root is mapped and the confusion named."""
        done = self.sc(self.wt, "map", cwd=self.wt / "sub")
        self.assert_ok(done)
        first = done.stderr.splitlines()[0]
        self.assertTrue(first.startswith(self.notice("map")), done.stderr)
        self.assertTrue(first.endswith(f") instead of {self.wt}."), first)
        self.assertNotIn("behind", first)
        root, paths, _ = self.mapped()
        self.assertEqual(root, str(self.main))
        self.assertIn("main_only.txt", paths)
        self.assertNotIn("wt_only.txt", paths)

    def test_map_notice_names_staleness(self):
        """The live root's staleness comes from its existing refs (no fetch)."""
        tree = git(self.main, "rev-parse", "HEAD^{tree}")
        ahead = git(self.main, "commit-tree", tree, "-p", "HEAD", "-m", "upstream")
        git(self.main, "branch", "fixture-upstream", ahead)
        git(self.main, "branch", "--set-upstream-to=fixture-upstream", "main")
        self.addCleanup(git, self.main, "branch", "-D", "fixture-upstream")
        self.addCleanup(git, self.main, "branch", "--unset-upstream", "main")
        done = self.sc(self.wt, "map")
        self.assert_ok(done)
        self.assertIn(", behind fixture-upstream by 1) instead of", done.stderr)

    def test_map_auto(self):
        _, _, before = self.mapped()
        silent = self.sc(self.wt, "map", "--auto")
        self.assert_ok(silent)
        self.assertEqual((silent.stdout, silent.stderr), ("", ""))
        self.assertEqual(self.mapped()[2], before)
        time.sleep(1.1)  # mapped_at has one-second resolution
        refreshed = self.sc(self.main, "map", "--auto")
        self.assert_ok(refreshed)
        self.assertIn("map_repo:", refreshed.stdout)
        self.assertEqual(refreshed.stderr, "")
        self.assertNotEqual(self.mapped()[2], before)

    def test_map_authored_descriptions_survive_worktree_map(self):
        self.describe("main_only.txt", "Cartographer-authored description")
        self.assert_ok(self.sc(self.wt, "map"))
        self.assertEqual(self.desc_of("main_only.txt"), "Cartographer-authored description")

    def test_map_finalize(self):
        self.describe("main_only.txt", "Cartographer-authored description")
        done = self.sc(self.wt, "map", "finalize", "--json")
        self.assertTrue(done.stderr.startswith(self.notice("map finalize")), done.stderr)
        rows = {row["key"]: row for row in json.loads(done.stdout)["rows"]}
        self.assertFalse([e for e in rows["live_map"]["evidence"]
                          if "repo identity mismatch" in e], rows["live_map"])
        root, paths, _ = self.mapped()
        self.assertEqual(root, str(self.main))
        self.assertNotIn("wt_only.txt", paths)
        self.assertEqual(self.desc_of("main_only.txt"), "Cartographer-authored description")

    def test_map_setup(self):
        done = self.sc(self.wt, "map-setup", SC_API_TOKEN="fixture-shell-token")
        self.addCleanup(git, self.main, "config", "--unset", "core.hooksPath")
        self.assert_ok(done)
        self.assertTrue(done.stderr.startswith(self.notice("map-setup")), done.stderr)
        root, paths, _ = self.mapped()
        self.assertEqual(root, str(self.main))
        self.assertNotIn("wt_only.txt", paths)
        self.assertEqual(git(self.wt, "config", "--get", "core.hooksPath"),
                         str(self.main / ".super-coder" / "hooks"))
        helped = self.sc(self.wt, "map-setup", "--help")
        self.assert_ok(helped)
        self.assertEqual(helped.stderr, "")

    def test_map_admin_seat(self):
        done = self.sc(self.main, "map")
        self.assert_ok(done)
        self.assertEqual(done.stderr, "")
        self.assertEqual(self.mapped()[0], str(self.main))

    def test_map_extractor(self):
        """The candidate path is caller-resolved; the install is the live one."""
        source_dir = self.wt / ".sc-state" / "map_extractors"
        write(source_dir / "rootprobe.py",
              "def extract(con, repo_root, cfg):\n"
              "    return 'scanned ' + str(repo_root)\n")
        self.addCleanup(shutil.rmtree, source_dir, ignore_errors=True)
        installed = self.main / ".sc-state" / "map_extractors" / "rootprobe.py"
        self.addCleanup(installed.unlink, missing_ok=True)
        done = self.sc(self.wt, "map-extractor", "install", "rootprobe.py",
                       cwd=source_dir, SC_SHELL_FLAVOR="cartographer",
                       SC_SHELL_WORKTREE=str(self.wt))
        self.assert_ok(done)
        self.assertTrue(installed.is_file())
        mapped = self.sc(self.main, "map")
        self.assert_ok(mapped)
        self.assertIn(f"rootprobe: scanned {self.main}", mapped.stdout)


class SourceCallerEngineTest(unittest.TestCase):
    """Source-repository verbs that run the CALLER's tracked engine."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="sc_caller_src_")
        cls.addClassCleanup(shutil.rmtree, cls._tmp, ignore_errors=True)
        tmp = Path(cls._tmp).resolve()
        cls.state = tmp / "state"
        cls.state.mkdir()
        cls.main = tmp / "main"
        cls.main.mkdir()
        shutil.copytree(ENGINE, cls.main / ".super-coder", ignore=IGNORE)
        shutil.copy2(REPO / "sc", cls.main / "sc")
        git(cls.main, "init", "-q", "-b", "main")
        git(cls.main, "remote", "add", "origin", "https://github.com/jedbjorn/subfloor.git")
        git(cls.main, "add", "-A")
        git(cls.main, "commit", "-qm", "source")
        cls.wt = tmp / "linked-worktree"
        git(cls.main, "worktree", "add", "-q", "-b", "feature", str(cls.wt))

    def sc(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env.update(XDG_STATE_HOME=str(self.state), HOME=str(self.state),
                   SC_PYTHON=sys.executable, **GIT_ENV)
        return subprocess.run([str(self.wt / "sc"), *args], cwd=str(self.wt), env=env,
                              capture_output=True, text=True, timeout=600, check=False)

    def test_render_check(self):
        done = self.sc("render-check")
        self.assertIn(f"source root : {self.wt}", done.stdout)
        self.assertNotIn(f"source root : {self.main}", done.stdout)

    def test_seed_skills(self):
        skill = self.wt / ".super-coder" / "assets" / "skills" / "wt_seed_marker"
        write(skill / "SKILL.md", "---\nname: wt_seed_marker\n"
              "description: present only in the linked worktree\ncommon: false\n"
              "---\n\n# marker\n")
        # A schema-empty worktree DB is not a live DB (issues #1159, #1398).
        sqlite3.connect(self.wt / ".super-coder" / "shell_db.db").close()
        main_seed = self.main / ".super-coder" / "migrations" / "0001_seed_skills.sql"
        before = main_seed.read_bytes()
        done = self.sc("seed-skills")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        seed = self.wt / ".super-coder" / "migrations" / "0001_seed_skills.sql"
        self.assertIn("'wt_seed_marker'", seed.read_text())
        self.assertEqual(main_seed.read_bytes(), before)


class DispatcherClassificationTest(unittest.TestCase):
    """The dispatcher exports the caller identity to project verbs only."""

    DISPATCH = (ENGINE / "scripts" / "dispatch.sh").read_text()
    PROJECT = ("visual-qa", "map-extractor", "context", "sprint", "mem",
               "skill", "job", "actions-artifacts", "vm", "remote")
    LIVE = ("install", "update", "rollback", "rebuild", "migrate", "snapshot", "render",
            "remove", "eject", "sql", "map-sql", "map-setup", "models", "analytics",
            "boot", "run")

    def arm(self, verb: str) -> str:
        match = re.search(rf"(?m)^  {re.escape(verb)}\)\s+(.*)$", self.DISPATCH)
        self.assertIsNotNone(match, verb)
        return match.group(1)

    def test_project_arms_export_the_caller_identity(self):
        for verb in self.PROJECT:
            with self.subTest(verb=verb):
                self.assertIn("sc_project_env", self.arm(verb))
        self.assertIn('  sc_project_env\n  "$PY" "$S/devkit.py" run "$CALLER_ROOT"',
                      self.DISPATCH)

    def test_live_arms_never_do(self):
        for verb in self.LIVE:
            with self.subTest(verb=verb):
                self.assertNotIn("sc_project_env", self.arm(verb))

    def test_catalogue_arms_never_export(self):
        block = self.DISPATCH.split("\n  map)", 1)[1].split("\n  map-setup)", 1)[0]
        self.assertNotIn("sc_project_env", block)

    def test_inherited_identity_is_cleared_before_dispatch(self):
        head = self.DISPATCH.split("sc_invocation_cwd=", 1)[0]
        self.assertIn("unset SC_PROJECT_ROOT SC_INVOCATION_CWD SC_PROJECT_ENGINE", head)


class ProjectRootHelperTest(unittest.TestCase):
    """scripts/project_root.py: the one resolution every project verb uses."""

    def test_unbound_falls_back_to_this_engine_and_process_cwd(self):
        self.assertEqual(project_root.project_root({}), ENGINE.parent)
        self.assertEqual(project_root.invocation_cwd({}), Path.cwd())

    def test_bound_identity_names_the_caller(self):
        with tempfile.TemporaryDirectory() as td:
            caller = Path(td).resolve()
            (caller / "sub").mkdir()
            env = {project_root.ROOT_VAR: str(caller),
                   project_root.CWD_VAR: str(caller / "sub"),
                   project_root.ENGINE_VAR: str(ENGINE)}
            self.assertEqual(project_root.project_root(env), caller)
            self.assertEqual(project_root.invocation_path("x.md", env), caller / "sub" / "x.md")
            self.assertEqual(project_root.invocation_path("/abs/x.md", env), Path("/abs/x.md"))

    def test_identity_for_another_engine_is_ignored(self):
        """An inherited copy never redirects a fixture or worktree engine."""
        with tempfile.TemporaryDirectory() as td:
            env = {project_root.ROOT_VAR: td, project_root.CWD_VAR: td,
                   project_root.ENGINE_VAR: str(Path(td) / ".super-coder")}
            self.assertEqual(project_root.project_root(env), ENGINE.parent)

    def test_scrubbed_removes_only_the_identity(self):
        env = {name: "x" for name in project_root.VARIABLES} | {"KEEP": "1"}
        self.assertEqual(project_root.scrubbed(env), {"KEEP": "1"})

    def test_models_has_no_project_path(self):
        """models resolves routes from the live instance (API or DB) and emits a
        `./sc run` command; it reads no project path, so it stays unexported."""
        source = (ENGINE / "scripts" / "models.py").read_text()
        self.assertNotIn("REPO_ROOT", source)
        self.assertNotIn("project_root", source)


if __name__ == "__main__":
    unittest.main()
