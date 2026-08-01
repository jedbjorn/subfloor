#!/usr/bin/env python3
"""Worktree-safe command targets (spec #68).

`sc` resolved ONE root — the main worktree, via git's common dir — so every
command typed in a linked worktree silently acted on the shared live instance:
`./sc migrate --help` maintained the main checkout's DB, `./sc render-check`
verified the main checkout's sources, and `./sc verify` rebuilt a DB the caller
never named. These tests drive the REAL dispatcher inside a REAL linked git
worktree, against sentinel state, and pin the three answers spec #68 allows:
run the caller's own source, deliberately reach the shared runtime, or refuse
before touching anything.

The fixture is deliberately a full engine copy rather than stubs. If the guard
were removed, `rebuild`/`migrate`/`clean-db` would really run and really replace
the sentinel DB, so the state assertions here can fail — and the message
assertions fail independently of whether a given command's mutation lands.
Distinct trees and distinct DBs on the two sides (req 9) mean a substitution in
either direction shows up as the WRONG path in the output, not as silence.

Run:  python3 tests/test_worktree_targets.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import migrate as migrate_mod  # noqa: E402

# Copied engine minus caches and anything instance-owned: the fixture's live
# state is the sentinel this module writes, never a real fork's.
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "shell_db.db*", "backups", "node_modules",
    "logs",
)

# A skill row in a migration only one checkout holds exercises that checkout's
# source independently. Generated mirrors are local-only and need not exist.
SENTINEL_MIGRATION = "9999_worktree_sentinel.sql"
SENTINEL_SKILL = "wt-sentinel-skill"
ENGINE_PIN = "1234567890abcdef1234567890abcdef12345678"
SENTINEL_SQL = (
    "INSERT INTO skills (name, description, content, common, is_deleted) "
    f"VALUES ('{SENTINEL_SKILL}', 'present only in the linked worktree', "
    "'sentinel body', 0, 0);\n"
)


def run_sc(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """`./sc <args>` from `cwd`, with the dispatcher's own `sc` — the one under
    test — resolved relative to that checkout."""
    env = dict(os.environ)
    env.pop("SC_PYTHON", None)
    return subprocess.run(
        [str(cwd / "sc"), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=600,
        check=False, env=env)


def run_bare_sc(cwd: Path, live_root: Path,
                *args: str) -> subprocess.CompletedProcess:
    """Drive the canonical launcher contract run.py gives every shell."""
    env = dict(os.environ)
    env.pop("SC_PYTHON", None)
    env["PATH"] = f"{live_root}:{env['PATH']}"
    return subprocess.run(
        ["sc", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=600,
        check=False, env=env)


def git_status(cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def state_digest(root: Path) -> dict[str, str]:
    """The live-state artifacts of an instance, by content: the DB, its WAL/SHM
    sidecars, and every backup. A replaced DB shows as a changed digest, a new
    candidate or backup as a new key."""
    engine = root / ".super-coder"
    watched = [engine / "shell_db.db", engine / "shell_db.db-wal",
               engine / "shell_db.db-shm"]
    watched += sorted((engine / "backups").rglob("*"))
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in watched if p.is_file()
    }


def make_live_db(path: Path, engine: Path) -> None:
    """A REAL database carrying the fixture's schema plus an identifying marker.

    Real because the point is that an unguarded command can destroy it: garbage
    bytes would make rebuild/migrate fail early and leave the file intact, which
    is a green test with no guard behind it.
    """
    con = sqlite3.connect(path)
    con.executescript((engine / "schema.sql").read_text())
    con.execute("CREATE TABLE live_marker (who TEXT)")
    con.execute("INSERT INTO live_marker VALUES ('LIVE-INSTANCE-DB')")
    con.commit()
    con.close()


class WorktreeFixture(unittest.TestCase):
    """One main checkout that owns the live state, one linked worktree that does
    not, both real git checkouts of the real engine."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="sc_wt_")
        tmp = Path(cls._tmp)
        cls.main = tmp / "main"
        cls.main.mkdir()
        shutil.copytree(ENGINE, cls.main / ".super-coder", ignore=IGNORE)
        shutil.copy2(REPO / "sc", cls.main / "sc")
        (cls.main / "sc").chmod(0o755)
        git = ["git", "-C", str(cls.main)]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "config", "user.email", "t@t"], check=True)
        subprocess.run([*git, "config", "user.name", "t"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
        cls.wt = tmp / "linked-worktree"
        subprocess.run([*git, "worktree", "add", "-q", str(cls.wt)], check=True)

        # The live state exists ONLY under the main checkout — the asymmetry the
        # whole spec is about.
        cls.live_db = cls.main / ".super-coder" / "shell_db.db"
        make_live_db(cls.live_db, cls.main / ".super-coder")
        state = cls.main / ".sc-state"
        state.mkdir()
        (state / "engine.ref").write_text(ENGINE_PIN + "\n")
        backups = cls.main / ".super-coder" / "backups"
        backups.mkdir(exist_ok=True)
        (backups / "shell_db.prerebuild.20260101_000000.db").write_bytes(b"old-backup")
        cls.pristine = tmp / "pristine"
        cls.pristine.mkdir()
        shutil.copy2(cls.live_db, cls.pristine / "shell_db.db")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        """Restore the live state so each test meets the same instance."""
        shutil.copy2(self.pristine / "shell_db.db", self.live_db)
        for side in ("-wal", "-shm"):
            Path(str(self.live_db) + side).unlink(missing_ok=True)
        backups = self.main / ".super-coder" / "backups"
        shutil.rmtree(backups, ignore_errors=True)
        backups.mkdir()
        (backups / "shell_db.prerebuild.20260101_000000.db").write_bytes(b"old-backup")


class LinkedWorktreeRefusalTest(WorktreeFixture):
    """Requirements 2 and 3: the live-state commands refuse from a linked
    worktree, before they open or delete anything."""

    LIVE_STATE_COMMANDS = ("rebuild", "migrate", "verify", "snapshot",
                           "render", "clean-db")

    def test_live_state_commands_refuse_and_leave_the_instance_untouched(self):
        for cmd in self.LIVE_STATE_COMMANDS:
            with self.subTest(cmd=cmd):
                self.setUp()
                before = state_digest(self.main)
                done = run_sc(self.wt, cmd)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn(f"./sc {cmd} refused", done.stderr)
                self.assertIn(str(self.wt), done.stderr)      # caller named
                self.assertIn(str(self.main), done.stderr)    # live target named
                self.assertIn(str(self.live_db), done.stderr)
                self.assertIn(f"cd {self.main} && ./sc {cmd}", done.stderr)
                self.assertEqual(state_digest(self.main), before)

    def artifact_path(self, kind: str) -> str:
        """The live instance's own answer for an artifact path — asked the way
        the dispatcher asks it, so the assertion cannot encode one artifact
        mode's spelling."""
        return subprocess.run(
            [sys.executable,
             str(self.main / ".super-coder" / "scripts" / "artifact_policy.py"),
             "path", kind],
            capture_output=True, text=True, check=True).stdout.strip()

    def test_refusal_names_the_artifact_a_command_would_overwrite(self):
        """snapshot and render do not only read the DB — they replace an
        instance artifact, so the declined target has to name that too."""
        for cmd, kind in (("snapshot", "content"), ("render", "renders")):
            with self.subTest(cmd=cmd):
                done = run_sc(self.wt, cmd)
                self.assertEqual(done.returncode, 1)
                self.assertIn(
                    f"declined target : {self.live_db} -> {self.artifact_path(kind)}",
                    done.stderr)

    def test_the_worktree_never_grows_a_partial_instance(self):
        """Decision #81: refusal, not a worktree-local runtime. Nothing may
        appear under the caller's engine either."""
        for cmd in self.LIVE_STATE_COMMANDS:
            with self.subTest(cmd=cmd):
                run_sc(self.wt, cmd)
                self.assertFalse((self.wt / ".super-coder" / "shell_db.db").exists())
                self.assertEqual(state_digest(self.wt), {})


class HelpSurvivesTheRefusalTest(WorktreeFixture):
    """Ruling R1: -h/--help answers from ANY checkout — parse first,
    refuse second, act third. Only an ACTION form is refused."""

    def test_help_forms_exit_zero_with_usage_from_the_linked_worktree(self):
        for cmd, flag, needle in (
            ("rebuild", "--help", "usage: ./sc rebuild"),
            ("rebuild", "-h", "usage: ./sc rebuild"),
            ("migrate", "--help", "usage: ./sc migrate"),
            ("migrate", "-h", "usage: ./sc migrate"),
        ):
            with self.subTest(cmd=cmd, flag=flag):
                before = state_digest(self.main)
                done = run_sc(self.wt, cmd, flag)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(needle, done.stdout)
                self.assertNotIn("refused", done.stdout + done.stderr)
                self.assertEqual(state_digest(self.main), before)

    def test_the_same_help_forms_answer_identically_from_the_root(self):
        """A help form is not a worktree concession — the root prints the same
        thing, so the two checkouts cannot drift into two contracts."""
        for cmd in ("rebuild", "migrate"):
            with self.subTest(cmd=cmd):
                from_wt = run_sc(self.wt, cmd, "--help")
                from_root = run_sc(self.main, cmd, "--help")
                self.assertEqual(from_wt.returncode, 0)
                self.assertEqual(from_root.returncode, 0)
                self.assertEqual(from_wt.stdout, from_root.stdout)

    def test_an_action_form_that_merely_looks_like_help_is_still_refused(self):
        done = run_sc(self.wt, "migrate", "--halp")
        self.assertEqual(done.returncode, 1)
        self.assertIn("./sc migrate refused", done.stderr)


class RootCheckoutUnchangedTest(WorktreeFixture):
    """Requirements 5, 6 and 8: the main checkout still acts, and now says what
    it is acting on."""

    def test_root_migrate_applies_the_chain_and_names_both_targets(self):
        done = run_sc(self.main, "migrate")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"migrate: db         {self.live_db}", done.stdout)
        self.assertIn(
            f"migrate: migrations {self.main / '.super-coder' / 'migrations'}",
            done.stdout)
        self.assertIn("migration(s) applied to", done.stdout)
        self.assertNotEqual(
            hashlib.sha256(self.live_db.read_bytes()).hexdigest(),
            hashlib.sha256((self.pristine / "shell_db.db").read_bytes()).hexdigest(),
            "root migrate must still really migrate")

    def test_root_migrate_names_its_targets_on_the_nothing_pending_outcome(self):
        run_sc(self.main, "migrate")
        done = run_sc(self.main, "migrate")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"migrate: db         {self.live_db}", done.stdout)
        self.assertIn(f"nothing pending — {self.live_db} is current", done.stdout)

    def test_root_verify_discloses_its_target_before_the_rebuild_runs(self):
        """The ordering IS the requirement, so both events are captured in ONE
        stream: a disclosure printed after the destruction is no disclosure."""
        script = self.main / ".super-coder" / "scripts" / "rebuild.py"
        original = script.read_bytes()
        script.write_text(
            "#!/usr/bin/env python3\n"
            "print('REBUILD-RAN')\n"
            "raise SystemExit(3)\n")
        try:
            done = run_sc(self.main, "verify")
        finally:
            script.write_bytes(original)
        self.assertNotEqual(done.returncode, 0)
        merged = done.stdout
        self.assertIn(str(self.live_db), merged)
        self.assertIn("REBUILD-RAN", merged)
        self.assertLess(
            merged.index(str(self.live_db)), merged.index("REBUILD-RAN"),
            "verify disclosed its target only after rebuild had already run")


class RenderCheckRunsCallerSourceTest(WorktreeFixture):
    """Requirement 1: render-check is source-pure — it verifies the sources of
    the checkout it was typed in."""

    def setUp(self):
        super().setUp()
        self.sentinel = (self.wt / ".super-coder" / "migrations" / SENTINEL_MIGRATION)
        self.sentinel.write_text(SENTINEL_SQL)
        self.addCleanup(self.sentinel.unlink, missing_ok=True)

    @staticmethod
    def drifted(done: subprocess.CompletedProcess) -> set[str]:
        body = done.stderr.split("drifted:", 1)
        return set(body[1].split()) if len(body) == 2 else set()

    def test_missing_local_render_cache_is_valid_in_each_checkout(self):
        from_wt = run_sc(self.wt, "render-check")
        from_root = run_sc(self.main, "render-check")
        self.assertEqual(from_wt.returncode, 0, from_wt.stdout + from_wt.stderr)
        self.assertEqual(from_root.returncode, 0, from_root.stdout + from_root.stderr)
        self.assertIn("local artifact mode has no rendered instance state yet",
                      from_wt.stdout)
        self.assertIn("local artifact mode has no rendered instance state yet",
                      from_root.stdout)

    def test_render_check_reports_the_caller_source_root_from_both_checkouts(self):
        for root in (self.wt, self.main):
            with self.subTest(root=root.name):
                done = run_sc(root, "render-check")
                self.assertIn(f"source root : {root}", done.stdout)
                other = self.main if root == self.wt else self.wt
                self.assertNotIn(f"source root : {other}", done.stdout)
                self.assertIn(f"engine      : {root / '.super-coder'}", done.stdout)

    def test_a_missing_caller_engine_fails_naming_that_path(self):
        """It must not fall back to the live source: that answers a question
        about a tree the caller never asked about."""
        moved = self.wt / ".super-coder" / "scripts" / "render_check.py"
        stash = moved.with_suffix(".py.stashed")
        moved.rename(stash)
        try:
            done = run_sc(self.wt, "render-check")
        finally:
            stash.rename(moved)
        self.assertEqual(done.returncode, 1)
        self.assertIn(str(self.wt / ".super-coder"), done.stderr)
        self.assertIn("does not", done.stderr)
        self.assertEqual(done.stdout, "")


class LiveSurfacesStillResolveTest(WorktreeFixture):
    """Requirement 4: the shared-runtime surfaces keep reaching the shared
    runtime. The refusal is for commands that MUTATE live state, and a guard
    that swallowed `mem`/`sql`/`map-sql` would strand every shell."""

    def test_sql_from_the_worktree_reads_the_live_instance_database(self):
        done = run_sc(self.wt, "sql", "SELECT who FROM live_marker;")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("LIVE-INSTANCE-DB", done.stdout)

    def test_map_sql_from_the_worktree_reads_the_live_catalogue(self):
        mapdb = subprocess.run(
            [sys.executable,
             str(self.main / ".super-coder" / "scripts" / "artifact_policy.py"),
             "path", "map-db"],
            capture_output=True, text=True, check=True).stdout.strip()
        Path(mapdb).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(mapdb)
        con.execute("CREATE TABLE IF NOT EXISTS map_marker (who TEXT)")
        con.execute("INSERT INTO map_marker VALUES ('LIVE-MAP-DB')")
        con.commit()
        con.close()
        self.addCleanup(Path(mapdb).unlink, missing_ok=True)

        done = run_sc(self.wt, "map-sql", "SELECT who FROM map_marker;")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("LIVE-MAP-DB", done.stdout)

    def test_engine_ref_from_the_worktree_reads_the_full_live_pin(self):
        done = run_sc(self.wt, "engine-ref")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, ENGINE_PIN + "\n")
        self.assertEqual(done.stderr, "")

    def _make_live_engine_read_only(self) -> None:
        """Model the linked shell seat: canonical source is readable, not writable."""
        engine = self.main / ".super-coder"
        engine_mode = engine.stat().st_mode
        db_mode = self.live_db.stat().st_mode
        self.live_db.chmod(0o444)
        engine.chmod(0o555)
        self.addCleanup(engine.chmod, engine_mode)
        self.addCleanup(self.live_db.chmod, db_mode)

    def test_model_list_and_resolve_read_the_read_only_live_database(self):
        con = sqlite3.connect(self.live_db)
        con.executescript(
            (self.main / ".super-coder" / "migrations" /
             "0075_model_routes.sql").read_text()
        )
        con.execute(
            "INSERT INTO model_routes (harness, selector, source, availability, "
            "headless_supported, high_effort_supported, supported_efforts, "
            "last_seen_at) VALUES ('codex', 'wt-live-model', 'live-marker', "
            "'available', 1, 1, '[\"high\"]', datetime('now'))"
        )
        con.commit()
        con.close()
        before = state_digest(self.main)
        self._make_live_engine_read_only()

        listed = run_sc(self.wt, "models", "list", "codex")
        resolved = run_sc(
            self.wt, "models", "resolve", "codex", "wt-live-model", "--json"
        )

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("codex/wt-live-model", listed.stdout)
        self.assertIn("live-marker", listed.stdout)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["selector"], "wt-live-model")
        self.assertEqual(state_digest(self.main), before)
        self.assertFalse((self.wt / ".super-coder" / "shell_db.db").exists())

    def test_skill_list_reads_the_read_only_live_database(self):
        con = sqlite3.connect(self.live_db)
        con.execute(
            "INSERT INTO skills (name, description, content, common, is_deleted) "
            "VALUES ('wt-live-skill', 'live marker', 'body', 0, 0)"
        )
        con.commit()
        con.close()
        before = state_digest(self.main)
        self._make_live_engine_read_only()

        listed = run_sc(self.wt, "skill", "list")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("wt-live-skill", listed.stdout)
        self.assertEqual(state_digest(self.main), before)
        self.assertFalse((self.wt / ".super-coder" / "shell_db.db").exists())

    def test_bare_sc_works_when_the_worktree_launcher_is_absent(self):
        launcher = self.wt / "sc"
        absent = self.wt / "sc.absent"
        launcher.rename(absent)
        try:
            self.assertFalse(launcher.exists())
            models = run_bare_sc(self.wt, self.main, "models", "--help")
            pin = run_bare_sc(self.wt, self.main, "engine-ref")
        finally:
            absent.rename(launcher)
        self.assertEqual(models.returncode, 0, models.stderr)
        self.assertIn("usage:", models.stdout)
        self.assertEqual(pin.returncode, 0, pin.stderr)
        self.assertEqual(pin.stdout, ENGINE_PIN + "\n")

    def test_bare_sprint_help_uses_canonical_dispatcher_without_dirtying_worktree(self):
        before = git_status(self.wt)

        sprint = run_bare_sc(self.wt, self.main, "sprint", "-h")
        inbox = run_bare_sc(self.wt, self.main, "sprint", "inbox", "-h")

        self.assertEqual(sprint.returncode, 0, sprint.stderr)
        self.assertIn("usage: sc sprint", sprint.stdout)
        self.assertNotIn("scripts/sprint.py", sprint.stderr)
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn("usage: sc sprint inbox", inbox.stdout)
        self.assertNotIn("scripts/sprint.py", inbox.stderr)
        self.assertEqual(git_status(self.wt), before)

    def test_engine_ref_refuses_a_missing_or_malformed_live_pin(self):
        path = self.main / ".sc-state" / "engine.ref"
        for bad in (None, "short-pin\n", "g" * 40 + "\n"):
            with self.subTest(bad=bad):
                if bad is None:
                    path.unlink()
                else:
                    path.write_text(bad)
                done = run_sc(self.wt, "engine-ref")
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertIn(str(path), done.stderr)
                path.write_text(ENGINE_PIN + "\n")


class StandaloneRootTest(unittest.TestCase):
    """Requirement 8 and the failure mode: with no second target resolvable, the
    caller IS the instance — a fork without worktrees keeps its current paths,
    and a non-checkout never guesses."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_solo_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(ENGINE, self.tmp / ".super-coder", ignore=IGNORE)
        shutil.copy2(REPO / "sc", self.tmp / "sc")
        (self.tmp / "sc").chmod(0o755)
        make_live_db(self.tmp / ".super-coder" / "shell_db.db",
                     self.tmp / ".super-coder")

    def test_a_checkout_less_root_is_not_treated_as_a_linked_worktree(self):
        done = run_sc(self.tmp, "migrate")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("refused", done.stderr)
        self.assertIn(f"migrate: db         {self.tmp / '.super-coder' / 'shell_db.db'}",
                      done.stdout)

    def test_a_symlinked_invocation_is_the_same_root_not_a_second_one(self):
        """Normalization before comparison: reaching the same tree through a
        symlink must not read as caller != live."""
        link = self.tmp.parent / (self.tmp.name + "-link")
        link.symlink_to(self.tmp)
        self.addCleanup(link.unlink)
        done = run_sc(link, "migrate")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("refused", done.stderr)


class MigratePreflightTest(unittest.TestCase):
    """migrate's own argument contract (spec #67's shape, applied to the command
    whose help form was the live reproduction): the parse decides everything
    before a database is opened."""

    def run_main(self, argv):
        out = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                migrate_mod.db_driver, "connect",
                mock.Mock(side_effect=AssertionError("opened a database"))))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                migrate_mod.parse_args(argv)
            except SystemExit as exc:
                return exc.code, out.getvalue()
        raise AssertionError(f"parse_args({argv!r}) returned without exiting")

    def test_help_prints_usage_and_opens_no_database(self):
        for argv in (["-h"], ["--help"], ["/some/db", "--help"], ["--nope", "-h"]):
            with self.subTest(argv=argv):
                code, out = self.run_main(argv)
                self.assertEqual(code, 0)
                self.assertIn("usage: ./sc migrate", out)

    def test_an_unknown_token_is_rejected_by_name(self):
        code, _ = self.run_main(["/some/db", "--dry-run"])
        self.assertEqual(code, 2)

    def test_a_lone_db_path_is_accepted(self):
        self.assertEqual(migrate_mod.parse_args(["/some/db"]), "/some/db")

    def test_the_targets_are_reported_before_the_database_is_opened(self):
        """Disclosure ahead of the work, not in a footer a crash can skip."""
        out = io.StringIO()
        boom = mock.Mock(side_effect=RuntimeError("connect failed"))
        with mock.patch.object(migrate_mod.db_driver, "connect", boom), \
                redirect_stdout(out):
            with self.assertRaises(RuntimeError):
                migrate_mod.migrate("/some/where/shell_db.db")
        self.assertIn("migrate: db         /some/where/shell_db.db", out.getvalue())
        self.assertIn(f"migrate: migrations {migrate_mod.MIGRATIONS_DIR}",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
