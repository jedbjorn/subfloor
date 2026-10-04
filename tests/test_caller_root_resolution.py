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
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


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

    def test_map(self):
        done = self.sc(self.wt, "map", cwd=self.wt / "sub")
        self.assert_ok(done)
        root, paths = self.map_repo_row()
        self.assertEqual(root, str(self.wt))
        self.assertIn("wt_only.txt", paths)
        self.assertNotIn("main_only.txt", paths)

    def test_map_auto(self):
        """The remap hooks' form keeps the mapped tree fresh and never lets a
        branch switch in another worktree re-point the shared catalogue."""
        self.assert_ok(self.sc(self.wt, "map"))
        skipped = self.sc(self.dev, "map", "--auto")
        self.assert_ok(skipped)
        self.assertIn("--auto skipped", skipped.stdout)
        self.assertEqual(self.map_repo_row()[0], str(self.wt))
        refreshed = self.sc(self.wt, "map", "--auto")
        self.assert_ok(refreshed)
        self.assertIn("map_repo:", refreshed.stdout)
        self.assertNotIn("skipped", refreshed.stdout)
        self.assertEqual(self.map_repo_row()[0], str(self.wt))

    def test_map_setup(self):
        # A shell token marks a launched seat: the owner-only update bridge is
        # skipped, exactly as for a Cartographer shell.
        done = self.sc(self.wt, "map-setup", SC_API_TOKEN="fixture-shell-token")
        self.assert_ok(done)
        root, paths = self.map_repo_row()
        self.assertEqual(root, str(self.wt))
        self.assertNotIn("main_only.txt", paths)
        # The hooks stay wired to the live engine: one clone, one hook set.
        self.assertEqual(git(self.wt, "config", "--get", "core.hooksPath"),
                         str(self.main / ".super-coder" / "hooks"))

    def test_map_extractor(self):
        """A candidate named relative to the operator's cwd installs into the
        live install; the next map runs it against the caller's tree."""
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
        mapped = self.sc(self.wt, "map")
        self.assert_ok(mapped)
        self.assertIn(f"rootprobe: scanned {self.wt}", mapped.stdout)

    def test_map_finalize(self):
        self.assert_ok(self.sc(self.wt, "map"))
        done = self.sc(self.wt, "map", "finalize", "--json")
        rows = {row["key"]: row for row in json.loads(done.stdout)["rows"]}
        live_map = rows["live_map"]
        self.assertFalse(
            [e for e in live_map["evidence"] if "repo identity mismatch" in e], live_map)
        # The installed-extractor check reads the live install, not the worktree.
        self.assertNotIn(str(self.wt / ".sc-state" / "map_extractors"),
                         json.dumps(rows))

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
        env.update(XDG_STATE_HOME=str(self.state), SC_PYTHON=sys.executable)
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
    PROJECT = ("visual-qa", "map-setup", "map-extractor", "context", "sprint", "mem",
               "skill", "job", "actions-artifacts", "vm", "remote")
    LIVE = ("install", "update", "rollback", "rebuild", "migrate", "snapshot", "render",
            "remove", "eject", "sql", "map-sql", "models", "analytics", "boot", "run")

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
