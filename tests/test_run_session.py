#!/usr/bin/env python3
"""Regression tests for atomic, contention-safe launcher session opening."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import venv
from contextlib import contextmanager, nullcontext, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db_driver  # noqa: E402
import run  # noqa: E402


SCHEMA = """
CREATE TABLE shells (
    shell_id INTEGER PRIMARY KEY,
    active_archive_id INTEGER
);
CREATE TABLE shell_memory_archives (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    date TEXT NOT NULL,
    full_narrative TEXT NOT NULL,
    started_at TEXT,
    harness TEXT,
    provider TEXT,
    model TEXT,
    UNIQUE (shell_id, session_id)
);
CREATE TABLE session_token_usage (
    archive_id INTEGER
);
CREATE TABLE skills (
    skill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    command TEXT,
    common INTEGER NOT NULL,
    content TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE lock_probe (
    probe_id INTEGER PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT INTO shells (shell_id, active_archive_id) VALUES (1, NULL);
INSERT INTO lock_probe (probe_id, value) VALUES (1, 0);
"""


class FlavorRouteDefaultsTest(unittest.TestCase):
    def test_loader_projects_nullable_per_harness_effort(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        con.execute(
            "CREATE TABLE flavor_defaults ("
            "flavor TEXT,harness TEXT,model TEXT,effort TEXT,is_default INTEGER)"
        )

        con.executemany(
            "INSERT INTO flavor_defaults VALUES (?,?,?,?,?)",
            [
                ("dev", "codex", "gpt-test", "low", 1),
                ("dev", "kimi", None, None, 0),
            ],
        )
        self.assertEqual(
            run.flavor_defaults(con),
            {"dev": {
                "default_harness": "codex",
                "models": {"codex": "gpt-test", "kimi": None},
                "efforts": {"codex": "low", "kimi": None},
            }},
        )

    def test_deepseek_provider_tracks_the_bound_provider_route(self) -> None:
        self.assertEqual(
            "ollama-cloud",
            run.session_provider(
                "deepseek", "ollama-cloud/deepseek-v4-pro:0813"
            ),
        )
        self.assertEqual(
            "deepseek-official",
            run.session_provider("deepseek", "deepseek-v4-pro"),
        )


class ShellPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.worktree = self.root / ".sc-worktrees" / "dev1"
        self.worktree.mkdir(parents=True)
        self.root_patch = mock.patch.object(run, "REPO_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def _python(self, *, executable: bool = True) -> Path:
        project_bin = self.worktree / ".venv" / "bin"
        project_bin.mkdir(parents=True)
        python = project_bin / "python"
        python.write_text("probe fixture")
        python.chmod(0o755 if executable else 0o644)
        return python

    def _completed_probe(
        self,
        *,
        version: tuple[int, int] = (3, 14),
        prefix: Path | None = None,
        base_prefix: Path | None = None,
        returncode: int = 0,
    ) -> subprocess.CompletedProcess:
        prefix = prefix or self.worktree / ".venv"
        base_prefix = base_prefix or self.root / "baseline-python"
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=json.dumps({
                "version": list(version),
                "prefix": str(prefix),
                "base_prefix": str(base_prefix),
            }),
            stderr="",
        )

    def _path_and_warning(self, completed=None) -> tuple[str, str]:
        stderr = io.StringIO()
        patcher = (
            mock.patch.object(run.subprocess, "run", return_value=completed)
            if completed is not None
            else nullcontext()
        )
        with patcher, redirect_stderr(stderr):
            path = run._shell_path(self.worktree, "/usr/local/bin:/usr/bin")
        return path, stderr.getvalue()

    def test_python_314_virtualenv_precedes_inherited_tools(self) -> None:
        python = self._python()
        completed = self._completed_probe()

        with mock.patch.object(
            run.subprocess, "run", return_value=completed
        ) as probe:
            path = run._shell_path(
                self.worktree, "/usr/local/bin:/usr/bin"
            )

        self.assertEqual(
            path,
            f"{self.root}:{python.parent}:/usr/local/bin:/usr/bin",
        )
        probe.assert_called_once_with(
            [
                str(python),
                "-I",
                "-S",
                "-c",
                run.VENV_PROBE_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    def test_real_python_314_virtualenv_is_admitted(self) -> None:
        project_venv = self.worktree / ".venv"
        venv.EnvBuilder(with_pip=False).create(project_venv)

        path, warning = self._path_and_warning()

        self.assertEqual(
            path,
            f"{self.root}:{project_venv / 'bin'}:/usr/local/bin:/usr/bin",
        )
        self.assertEqual(warning, "")

    def test_absent_environment_is_omitted_without_warning(self) -> None:
        path, warning = self._path_and_warning()
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertEqual(warning, "")

    def test_non_directory_bin_is_omitted_with_remedy(self) -> None:
        project_bin = self.worktree / ".venv" / "bin"
        project_bin.parent.mkdir(parents=True)
        project_bin.write_text("not a directory")
        path, warning = self._path_and_warning()
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn(str(self.worktree), warning)
        self.assertIn(".venv/bin is not a directory", warning)
        self.assertIn("run `sc deps`", warning)

    def test_missing_interpreter_is_omitted_with_remedy(self) -> None:
        (self.worktree / ".venv" / "bin").mkdir(parents=True)
        path, warning = self._path_and_warning()
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("missing .venv/bin/python", warning)
        self.assertIn("run `sc deps`", warning)

    def test_dangling_interpreter_is_omitted_with_remedy(self) -> None:
        project_bin = self.worktree / ".venv" / "bin"
        project_bin.mkdir(parents=True)
        (project_bin / "python").symlink_to(self.root / "missing-python")
        path, warning = self._path_and_warning()
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("dangling .venv/bin/python symlink", warning)
        self.assertTrue((project_bin / "python").is_symlink())

    def test_non_executable_interpreter_is_omitted_with_remedy(self) -> None:
        python = self._python(executable=False)
        path, warning = self._path_and_warning()
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("not an executable regular file", warning)
        self.assertEqual(python.stat().st_mode & 0o777, 0o644)

    def test_timed_out_probe_is_omitted_with_remedy(self) -> None:
        self._python()
        stderr = io.StringIO()
        with mock.patch.object(
            run.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("python", 3),
        ), redirect_stderr(stderr):
            path = run._shell_path(self.worktree, "/usr/bin")
        self.assertEqual(path, f"{self.root}:/usr/bin")
        self.assertIn("Python probe timed out after 3 seconds", stderr.getvalue())

    def test_foreign_prefix_is_omitted_with_remedy(self) -> None:
        self._python()
        foreign = self.root / "other-worktree" / ".venv"
        path, warning = self._path_and_warning(
            self._completed_probe(prefix=foreign)
        )
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn(f"Python reported foreign prefix {foreign}", warning)

    def test_python_313_environment_is_omitted_with_remedy(self) -> None:
        self._python()
        path, warning = self._path_and_warning(
            self._completed_probe(version=(3, 13))
        )
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("Python 3.14 is required; found 3.13", warning)

    def test_python_315_environment_is_omitted_with_remedy(self) -> None:
        self._python()
        path, warning = self._path_and_warning(
            self._completed_probe(version=(3, 15))
        )
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("Python 3.14 is required; found 3.15", warning)

    def test_nonzero_probe_is_omitted_with_remedy(self) -> None:
        self._python()
        path, warning = self._path_and_warning(
            self._completed_probe(returncode=9)
        )
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("Python probe exited 9", warning)

    def test_invalid_probe_report_is_omitted_with_remedy(self) -> None:
        self._python()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr=""
        )
        path, warning = self._path_and_warning(completed)
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("Python probe returned an invalid report", warning)

    def test_non_virtual_prefix_is_omitted_with_remedy(self) -> None:
        self._python()
        prefix = self.worktree / ".venv"
        path, warning = self._path_and_warning(
            self._completed_probe(prefix=prefix, base_prefix=prefix)
        )
        self.assertEqual(path, f"{self.root}:/usr/local/bin:/usr/bin")
        self.assertIn("Python reported no virtual environment", warning)

    def test_interactive_and_prepared_launches_share_the_path_builder(self) -> None:
        source = (SCRIPTS / "run.py").read_text()
        assignment = 'env["PATH"] = _shell_path(work_dir, env.get("PATH", ""))'
        self.assertEqual(source.count(assignment), 2)


class _FailAfterArchive:
    """Connection proxy that injects a lock after the archive INSERT."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def execute(self, sql: str, parameters=()):
        if sql.startswith("UPDATE shells SET active_archive_id"):
            raise sqlite3.OperationalError("database is locked")
        return self._con.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._con, name)


class _RollbackSignal:
    """Connection proxy that exposes the first failed attempt to the test."""

    def __init__(self, con: sqlite3.Connection, rolled_back: threading.Event) -> None:
        self._con = con
        self._rolled_back = rolled_back
        self.rollback_count = 0

    def rollback(self) -> None:
        self._con.rollback()
        self.rollback_count += 1
        self._rolled_back.set()

    def __getattr__(self, name: str):
        return getattr(self._con, name)


class _StopAfterSession(RuntimeError):
    """Stop main after the real boot prelude and session-open path complete."""


class OpenSessionContentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "shell.db"
        con = db_driver.connect(self.path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def _connect(self, busy_timeout_ms: int = 5000):
        con = db_driver.connect(self.path)
        con.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.addCleanup(con.close)
        return con

    def _seed_fresh_engine_skills(self, con) -> None:
        for skill in run.seed_skills._engine_specs():
            con.execute(
                "INSERT INTO skills "
                "(name, description, category, command, common, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    skill["name"],
                    skill["description"],
                    skill["category"],
                    skill["command"],
                    skill["common"],
                    skill["content"],
                ),
            )
        con.commit()

    def test_retries_from_clean_boundary_after_concurrent_writer(self) -> None:
        holder = self._connect()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE lock_probe SET value=1 WHERE probe_id=1")
        outcome: list[tuple[str, int]] = []
        errors: list[BaseException] = []
        first_attempt_rolled_back = threading.Event()
        contender_proxy: list[_RollbackSignal] = []

        def open_contender() -> None:
            contender = db_driver.connect(self.path)
            contender.execute("PRAGMA busy_timeout=30")
            proxy = _RollbackSignal(contender, first_attempt_rolled_back)
            contender_proxy.append(proxy)
            try:
                with mock.patch.object(
                        run, "SESSION_OPEN_RETRY_DELAYS_S", (0.1,)):
                    outcome.append(run.open_session(
                        proxy, 1, lifecycle={"harness": "claude"}))
            except BaseException as exc:
                errors.append(exc)
            finally:
                contender.close()

        contender_thread = threading.Thread(target=open_contender)
        contender_thread.start()
        self.assertTrue(
            first_attempt_rolled_back.wait(2),
            "the first bounded SQLite wait should expire while the writer holds",
        )
        holder.commit()
        contender_thread.join(2)

        self.assertFalse(contender_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(contender_proxy[0].rollback_count, 1)
        self.assertEqual(outcome, [("0001", 1)])
        verifier = self._connect()
        archive = verifier.execute(
            "SELECT shell_id, session_id, harness FROM shell_memory_archives"
        ).fetchone()
        self.assertEqual(tuple(archive), (1, "0001", "claude"))
        self.assertEqual(
            verifier.execute(
                "SELECT active_archive_id FROM shells WHERE shell_id=1"
            ).fetchone()[0],
            1,
        )

    def test_terminal_busy_failure_rolls_back_partial_archive(self) -> None:
        con = self._connect(busy_timeout_ms=1)
        failing = _FailAfterArchive(con)

        with mock.patch.object(run, "SESSION_OPEN_RETRY_DELAYS_S", (0,)), \
                self.assertRaises(run.SessionOpenError) as raised:
            run.open_session(failing, 1)

        message = str(raised.exception)
        self.assertIn("2 bounded session-open attempt(s)", message)
        self.assertIn("no session or archive was created", message)
        self.assertIn("Retry after the concurrent engine write finishes", message)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM shell_memory_archives").fetchone()[0],
            0,
        )
        self.assertIsNone(
            con.execute(
                "SELECT active_archive_id FROM shells WHERE shell_id=1"
            ).fetchone()[0]
        )
        self.assertFalse(con.in_transaction)

    def test_real_headless_boot_prelude_retries_from_clean_boundary(self) -> None:
        setup = self._connect()
        self._seed_fresh_engine_skills(setup)

        first_attempt_rolled_back = threading.Event()
        contender = db_driver.connect(self.path)
        contender.execute("PRAGMA busy_timeout=30")
        self.addCleanup(contender.close)
        proxy = _RollbackSignal(contender, first_attempt_rolled_back)
        holder_ready = threading.Event()
        holder_errors: list[BaseException] = []
        outcome: list[tuple[str, int]] = []

        def hold_concurrent_write() -> None:
            holder = db_driver.connect(self.path)
            holder.execute("PRAGMA busy_timeout=30")
            try:
                try:
                    holder.execute("BEGIN IMMEDIATE")
                    holder.execute(
                        "UPDATE lock_probe SET value=1 WHERE probe_id=1"
                    )
                except BaseException as exc:
                    holder_errors.append(exc)
                    holder_ready.set()
                    return
                holder_ready.set()
                if first_attempt_rolled_back.wait(2):
                    holder.commit()
                else:
                    holder.rollback()
            finally:
                holder.close()

        holder_thread: list[threading.Thread] = []
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"}
        fdefaults = {
            "dev": {"default_harness": "claude", "models": {"claude": "sonnet"}}
        }
        analytics = mock.Mock()
        analytics.sweep.return_value = {"inserted": 0, "updated": 0}
        real_open_session = run.open_session

        def pick_after_prelude(*_args):
            thread = threading.Thread(target=hold_concurrent_write)
            holder_thread.append(thread)
            thread.start()
            self.assertTrue(holder_ready.wait(2))
            return chosen

        def stop_after_session(*args, **kwargs):
            outcome.append(real_open_session(*args, **kwargs))
            raise _StopAfterSession

        @contextmanager
        def spinner(_label: str, *, enabled: bool):
            self.assertFalse(enabled)
            yield SimpleNamespace(label="")

        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.dict(sys.modules, {"analytics": analytics}), \
                mock.patch.object(
                    run.sys, "argv",
                    ["run.py", "--headless", "DEV1", "--harness", "claude"]), \
                mock.patch.object(run, "open_db", return_value=proxy), \
                mock.patch.object(
                    run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", side_effect=pick_after_prelude), \
                mock.patch.object(
                    run.shell_liveness, "compute",
                    return_value={"supported": False, "indeterminate": 0}), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(run, "load_adapter", return_value={}), \
                mock.patch.object(run, "validate_headless_request"), \
                mock.patch.object(run.style, "spinner", side_effect=spinner), \
                mock.patch.object(
                    run, "SESSION_OPEN_RETRY_DELAYS_S", (0.1,)), \
                mock.patch.object(
                    run, "open_session", side_effect=stop_after_session), \
                self.assertRaises(_StopAfterSession):
            run.main()

        holder_thread[0].join(2)
        self.assertFalse(holder_thread[0].is_alive())
        self.assertEqual(holder_errors, [])
        self.assertTrue(first_attempt_rolled_back.is_set())
        self.assertEqual(proxy.rollback_count, 1)
        self.assertEqual(outcome, [("0001", 1)])

        verifier = self._connect()
        archives = verifier.execute(
            "SELECT shell_id, session_id, harness "
            "FROM shell_memory_archives ORDER BY archive_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in archives],
            [(1, "0001", "claude")],
        )
        self.assertEqual(
            verifier.execute(
                "SELECT active_archive_id FROM shells WHERE shell_id=1"
            ).fetchone()[0],
            1,
        )


class HeadlessSessionFailureTest(unittest.TestCase):
    def test_host_admin_refuses_sandbox_before_boot_artifacts_or_database(self) -> None:
        with mock.patch.dict(run.os.environ, {"SC_SANDBOX": "1"}, clear=True), \
                mock.patch.object(run.sys, "argv", ["run.py", "--host-admin"]), \
                mock.patch.object(run.global_pointer, "write_global_pointers") as pointers, \
                mock.patch.object(run, "open_db") as open_db, \
                self.assertRaises(SystemExit) as raised:
            run.main()

        self.assertIn("run make dos-admin from a host terminal", str(raised.exception))
        pointers.assert_not_called()
        open_db.assert_not_called()

    def test_host_admin_missing_harness_refuses_before_session_creation(self) -> None:
        con = mock.Mock()
        chosen = {"shell_id": 1, "shortname": "ADM1", "flavor": "admin"}
        fdefaults = {
            "admin": {"default_harness": "codex", "models": {"codex": "gpt-test"}}
        }
        open_session = mock.Mock()
        liveness = mock.Mock()

        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.object(
                    run.sys, "argv", ["run.py", "--host-admin", "--harness", "codex"]
                ), \
                mock.patch.object(
                    run.global_pointer, "write_global_pointers"
                ) as pointers, \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "select_host_admin", return_value=chosen), \
                mock.patch.object(run, "browser_conversation_active", return_value=False), \
                mock.patch.object(run.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(run.shell_liveness, "compute", liveness), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(
                    run, "load_adapter", return_value={"harness": "codex", "launch": ["codex"]}
                ), \
                mock.patch.object(run.shutil, "which", return_value=None), \
                mock.patch.object(run, "open_session", open_session), \
                self.assertRaises(SystemExit) as raised:
            run.main()

        self.assertEqual(
            str(raised.exception),
            "sc admin: host harness 'codex' is not installed; run ./sc "
            "ensure-harness or use make dos-e for the container Admin route",
        )
        con.close.assert_called_once_with()
        pointers.assert_not_called()
        liveness.assert_not_called()
        open_session.assert_not_called()

    def test_host_admin_reaches_session_open_when_api_is_unavailable(self) -> None:
        con = mock.Mock()
        chosen = {"shell_id": 1, "shortname": "ADM1", "flavor": "admin"}
        fdefaults = {
            "admin": {"default_harness": "codex", "models": {"codex": "gpt-test"}}
        }
        open_session = mock.Mock(side_effect=_StopAfterSession)
        liveness = mock.Mock()

        with mock.patch.dict(
                run.os.environ,
                {
                    "SC_API_BASE": "http://127.0.0.1:1",
                    "SC_API_TOKEN": "unreachable-test-token",
                },
                clear=True,
            ), \
                mock.patch.object(
                    run.sys, "argv", ["run.py", "--host-admin", "--harness", "codex"]
                ), \
                mock.patch.object(run.global_pointer, "write_global_pointers"), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "select_host_admin", return_value=chosen), \
                mock.patch.object(run, "browser_conversation_active", return_value=False), \
                mock.patch.object(run.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(run.shell_liveness, "compute", liveness), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(
                    run, "load_adapter", return_value={"launch": ["codex"]}
                ), \
                mock.patch.object(run, "require_host_harness"), \
                mock.patch.object(run, "open_session", open_session), \
                self.assertRaises(_StopAfterSession):
            run.main()

        open_session.assert_called_once_with(
            con,
            1,
            lifecycle={"harness": "codex", "provider": "openai", "model": "gpt-test"},
        )
        liveness.assert_not_called()

    def test_host_admin_non_admin_reference_refuses_before_boot_artifacts(self) -> None:
        con = mock.Mock()
        pointers = mock.Mock()
        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.object(run.sys, "argv", ["run.py", "--host-admin", "DEV1"]), \
                mock.patch.object(
                    run.global_pointer, "write_global_pointers", pointers
                ), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value={}), \
                mock.patch.object(
                    run,
                    "select_host_admin",
                    side_effect=run.LaunchError(
                        "shell 'DEV1' is not the sole active Admin ('ADM1')"
                    ),
                ), \
                self.assertRaises(SystemExit) as raised:
            run.main()

        self.assertEqual(
            str(raised.exception),
            "sc admin: shell 'DEV1' is not the sole active Admin ('ADM1')",
        )
        con.close.assert_called_once_with()
        pointers.assert_not_called()

    def test_host_admin_db_failure_points_to_global_repair_before_artifacts(self) -> None:
        failure = run.db_driver.OperationalError("database disk image is malformed")
        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.object(run.sys, "argv", ["run.py", "--host-admin"]), \
                mock.patch.object(
                    run.global_pointer, "write_global_pointers"
                ) as pointers, \
                mock.patch.object(run, "open_db", side_effect=failure), \
                self.assertRaises(SystemExit) as raised:
            run.main()

        message = str(raised.exception)
        self.assertIn(str(run.DB_PATH), message)
        self.assertIn("database disk image is malformed", message)
        self.assertIn("global repair-mode instructions", message)
        pointers.assert_not_called()

    def test_removed_launch_context_flags_fail_before_database_access(self) -> None:
        for option in ("--slot", "--sprint", "--await-sprint-active"):
            with self.subTest(option=option), mock.patch.object(
                    run.sys, "argv", ["run.py", "--headless", "DEV1", option]
                ), mock.patch.object(run, "open_db") as open_db, \
                    self.assertRaises(SystemExit) as raised:
                run.main()
            self.assertIn("unknown option", str(raised.exception))
            open_db.assert_not_called()

    def test_sc_run_exits_before_worktree_or_harness_artifacts(self) -> None:
        con = mock.Mock()
        con.execute.return_value.fetchall.return_value = []
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"}
        fdefaults = {
            "dev": {"default_harness": "claude", "models": {"claude": "sonnet"}}
        }
        analytics = mock.Mock()
        analytics.sweep.return_value = {"inserted": 0, "updated": 0}
        ensure_worktree = mock.Mock()
        atomic_write = mock.Mock()
        execvpe = mock.Mock()

        @contextmanager
        def spinner(_label: str, *, enabled: bool):
            self.assertFalse(enabled)
            yield SimpleNamespace(label="")

        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.dict(sys.modules, {"analytics": analytics}), \
                mock.patch.object(
                    run.sys, "argv",
                    ["run.py", "--headless", "DEV1", "--harness", "claude"]), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(
                    run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(
                    run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", return_value=chosen), \
                mock.patch.object(
                    run.shell_liveness, "compute",
                    return_value={"supported": False, "indeterminate": 0}), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(run, "load_adapter", return_value={}), \
                mock.patch.object(run, "validate_headless_request"), \
                mock.patch.object(run.style, "spinner", side_effect=spinner), \
                mock.patch.object(
                    run, "open_session",
                    side_effect=run.SessionOpenError(
                        "engine DB remained busy; no session or archive was created")), \
                mock.patch.object(run, "ensure_worktree", ensure_worktree), \
                mock.patch.object(run, "atomic_write", atomic_write), \
                mock.patch.object(run.os, "execvpe", execvpe):
            with self.assertRaises(SystemExit) as raised:
                run.main()

        self.assertIn("sc run: engine DB remained busy", str(raised.exception))
        self.assertIn("no session or archive was created", str(raised.exception))
        con.close.assert_called_once_with()
        ensure_worktree.assert_not_called()
        atomic_write.assert_not_called()
        execvpe.assert_not_called()

    def test_sc_run_headless_refuses_missing_model_before_session_creation(self) -> None:
        con = mock.Mock()
        con.execute.return_value.fetchall.return_value = []
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"}
        fdefaults = {
            "dev": {"default_harness": "kimi", "models": {}}
        }
        open_session = mock.Mock()
        ensure_worktree = mock.Mock()
        execvpe = mock.Mock()

        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.object(
                    run.sys, "argv",
                    ["run.py", "--headless", "DEV1", "--harness", "kimi"]), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(
                    run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(
                    run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", return_value=chosen), \
                mock.patch.object(
                    run.shell_liveness, "compute",
                    return_value={"supported": False, "indeterminate": 0}), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(
                    run, "load_adapter",
                    return_value={
                        "harness": "kimi",
                        "headless": {
                            "launch": ["kimi", "--prompt", "{prompt}"],
                            "model_flag": "--model",
                            "effort": {"flag": "--effort"},
                        },
                    },
                ), \
                mock.patch.object(run, "open_session", open_session), \
                mock.patch.object(run, "ensure_worktree", ensure_worktree), \
                mock.patch.object(run.os, "execvpe", execvpe), \
                self.assertRaises(SystemExit) as raised:
            run.main()

        self.assertEqual(
            str(raised.exception),
            "sc run: harness 'kimi' cannot resolve a model: no model was "
            "supplied and no flavor default exists for it; supply an explicit model",
        )
        open_session.assert_not_called()
        ensure_worktree.assert_not_called()
        execvpe.assert_not_called()


LAUNCH_RECORDS = """
CREATE TABLE shell_launch_records (
    shell_id    INTEGER PRIMARY KEY,
    pid         INTEGER NOT NULL,
    start_ticks INTEGER NOT NULL,
    worktree    TEXT    NOT NULL,
    harness     TEXT,
    launched_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


class RecordLaunchTest(unittest.TestCase):
    """Spec #76 H-25: the launch claims the pid it is about to become."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / ".sc-worktrees" / "dev6"
        self.worktree.mkdir(parents=True)
        self.db = self.root / "shell.db"
        seed = sqlite3.connect(self.db)
        seed.executescript(LAUNCH_RECORDS)
        seed.commit()
        seed.close()
        # A stat line whose comm carries a space and parens, so the reader
        # cannot pass by splitting naively.
        rest = ["0"] * 30
        rest[19] = "5150"                      # field 22 — starttime
        self.stat = self.root / "stat"
        self.stat.write_text("42 (co dex (x)) " + " ".join(rest) + "\n")

    def _rows(self, sql: str = "SELECT shell_id, pid, start_ticks, worktree, "
                                "harness FROM shell_launch_records"):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def _record(self, *, headless: bool = True):
        # record_launch owns the connection it opens, so every call gets a fresh one.
        with mock.patch.object(
                run, "open_db",
                side_effect=lambda: sqlite3.connect(self.db)), \
                mock.patch.object(run, "PROC_SELF_STAT", self.stat):
            run.record_launch(1, self.worktree, "codex", headless=headless)
        return self._rows()

    def test_headless_launch_claims_this_pid_and_its_start_ticks(self):
        rows = self._record()
        self.assertEqual(
            [(1, os.getpid(), 5150, str(self.worktree), "codex")], rows)

    def test_an_interactive_boot_makes_no_claim(self):
        # Recording one would arrive pre-claimed on a closed terminal's
        # survivor and suppress the orphan verdict the operator needs.
        self.assertEqual([], self._record(headless=False))

    def test_relaunch_restamps_one_row_rather_than_accumulating(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO shell_launch_records "
            "(shell_id, pid, start_ticks, worktree, harness) "
            "VALUES (1, 999, 1, ?, 'claude')", (str(self.worktree),))
        con.commit()
        con.close()
        rows = self._record()
        self.assertEqual(1, len(rows))
        self.assertEqual((os.getpid(), 5150, "codex"), rows[0][1:3] + rows[0][4:])

    def test_an_unmigrated_fork_does_not_fail_the_boot(self):
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE shell_launch_records")
        con.commit()
        con.close()
        with mock.patch.object(
                run, "open_db",
                side_effect=lambda: sqlite3.connect(self.db)), \
                mock.patch.object(run, "PROC_SELF_STAT", self.stat):
            run.record_launch(1, self.worktree, "codex", headless=True)

    def test_unreadable_start_ticks_makes_no_claim(self):
        missing = self.root / "absent"
        with mock.patch.object(
                run, "open_db",
                side_effect=lambda: sqlite3.connect(self.db)), \
                mock.patch.object(run, "PROC_SELF_STAT", missing):
            run.record_launch(1, self.worktree, "codex", headless=True)
        self.assertEqual([], self._rows())


if __name__ == "__main__":
    unittest.main()
