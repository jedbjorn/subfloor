"""Evidence-reader proofs for spec 58, sprint 59 U3.

The tests stay below classification: they pin observations, nullable failure
isolation, epoch boundaries, true-route session selection, and CPU sampling.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import activity_readers as ar

UTC = timezone.utc
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def stamp(path: Path, when: datetime) -> None:
    seconds = when.timestamp()
    os.utime(path, (seconds, seconds))


def command(cwd: Path, *args: str, env=None, check=True):
    merged = os.environ.copy()
    merged.update(env or {})
    return subprocess.run(
        args, cwd=cwd, env=merged, text=True, capture_output=True, check=check
    )


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE shell_memory_archives (
          archive_id INTEGER PRIMARY KEY, shell_id INTEGER, started_at TEXT,
          harness TEXT
        );
        CREATE TABLE interface_sessions (
          session_id INTEGER PRIMARY KEY, shell_id INTEGER, archive_id INTEGER,
          created_at TEXT, ended_at TEXT, end_reason TEXT, harness TEXT
        );
        CREATE TABLE shell_messages (
          message_id INTEGER PRIMARY KEY, from_shell_id INTEGER,
          sprint_doc_id INTEGER, created_at TEXT, kind TEXT, body TEXT
        );
        CREATE TABLE sprint_units (
          unit_id INTEGER PRIMARY KEY, sprint_doc_id INTEGER, seq TEXT,
          branch TEXT, state TEXT, state_changed_at TEXT, updated_at TEXT,
          updated_by_shell_id INTEGER, dev_shell_id INTEGER,
          reviewer_shell_id INTEGER
        );
        CREATE TABLE sprint_planner_bindings (
          binding_id INTEGER PRIMARY KEY, sprint_doc_id INTEGER,
          planner_shell_id INTEGER
        );
        CREATE TABLE spec_tasks (
          task_id INTEGER PRIMARY KEY, status TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO shell_memory_archives VALUES (10,1,?,'codex')",
        ("2020-01-01T00:00:00Z",),
    )
    # A stale ended Interface session must not describe the current headless
    # archive.  The current archive has no Interface row at all.
    con.execute(
        "INSERT INTO interface_sessions VALUES "
        "(8,1,9,'2019-12-01T00:00:00Z','2019-12-01T00:01:00Z',"
        "'operator_end','codex')"
    )
    con.execute(
        "INSERT INTO shell_messages VALUES "
        "(1,1,59,'2020-01-02T00:00:00Z','result',"
        "'sprint 59: unit U3 fixing')"
    )
    con.execute(
        "INSERT INTO sprint_units VALUES "
        "(1,59,'U3','feat/test','working','2020-01-04T00:00:00Z',"
        "'2020-01-03T00:00:00Z',1,1,2)"
    )
    con.execute("INSERT INTO sprint_planner_bindings VALUES (1,59,1)")
    con.commit()
    con.close()


def make_adapters(path: Path) -> None:
    specs = {
        "claude": {
            "harness": "claude",
            "launch": ["claude"],
            "headless": {"launch": ["claude"], "prompt_flag": "-p"},
        },
        "codex": {
            "harness": "codex",
            "launch": ["codex"],
            "headless": {"launch": ["codex", "exec"]},
        },
        "kimi": {
            "harness": "kimi",
            "launch": ["kimi"],
            "comm_aliases": ["kimi-code"],
            "headless": {"launch": ["kimi"], "prompt_flag": "-p"},
        },
        "opencode": {
            "harness": "opencode",
            "launch": ["opencode"],
            "headless": {"launch": ["opencode", "run"]},
        },
    }
    for harness, spec in specs.items():
        directory = path / harness
        directory.mkdir(parents=True)
        (directory / "adapter.json").write_text(json.dumps(spec))


def proc_row(
    root: Path,
    pid: int,
    *,
    comm: str,
    cwd: Path,
    argv: list[str],
    ppid: int = 1,
    utime: int = 1,
    stime: int = 1,
    start: int = 100,
) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(ppid), *("0" for _ in range(18))]
    fields[11], fields[12], fields[19] = str(utime), str(stime), str(start)
    (directory / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}")
    (directory / "comm").write_text(f"{comm}\n")
    (directory / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (directory / "cwd").unlink(missing_ok=True)
    (directory / "cwd").symlink_to(cwd)


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        command(self.worktree, "git", "init", "-b", "feat/test")
        command(self.worktree, "git", "config", "user.email", "reader@test")
        command(self.worktree, "git", "config", "user.name", "Reader")
        (self.worktree / "base.txt").write_text("base\n")
        command(self.worktree, "git", "add", "base.txt")
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "base",
            env={
                "GIT_AUTHOR_DATE": "2019-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2019-01-01T00:00:00+00:00",
            },
        )
        command(
            self.worktree,
            "git",
            "update-ref",
            "refs/remotes/origin/main",
            "HEAD",
        )
        self.db = self.root / "shell.db"
        make_db(self.db)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.adapters = self.root / "adapters"
        make_adapters(self.adapters)
        self.home = self.root / "home"
        self.home.mkdir()
        self.reader = ar.ActivityReader(
            db_path=self.db,
            repo_root=self.root,
            proc=self.proc,
            adapters=self.adapters,
            home=self.home,
        )
        self.shell = {
            "shell_id": 1,
            "shortname": "DEV1",
            "flavor": "dev",
            "worktree": str(self.worktree),
        }
        self.unit = {
            "sprint_doc_id": 59,
            "seq": "U3",
            "branch": "feat/test",
            "state_changed_at": "2020-01-04T00:00:00Z",
        }

    def read(self):
        return self.reader.read(self.shell, self.unit, datetime(2020, 1, 10, tzinfo=UTC))


class DatabaseContractTest(ReaderCase):
    def test_epoch_session_identity_and_durable_lower_bound(self):
        evidence = self.read()
        self.assertEqual(EPOCH, evidence.epoch)
        self.assertIsNone(evidence.session_ended_at)
        self.assertIsNone(evidence.session_end_reason)
        self.assertEqual(
            datetime(2020, 1, 3, tzinfo=UTC), evidence.last_durable_write_at
        )
        self.assertEqual(
            datetime(2020, 1, 2, tzinfo=UTC), evidence.last_result_row_at
        )
        self.assertEqual(
            datetime(2020, 1, 4, tzinfo=UTC), evidence.state_changed_at
        )
        self.assertTrue(evidence.edits_code)
        self.assertEqual("feat/test", evidence.branch_declared)

    def test_current_archive_session_end_fields_are_observed(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_sessions VALUES "
            "(10,1,10,'2020-01-01T00:00:00Z','2020-01-05T00:00:00Z',"
            "'stop_hook','codex')"
        )
        con.commit()
        con.close()

        evidence = self.read()

        self.assertEqual(
            datetime(2020, 1, 5, tzinfo=UTC), evidence.session_ended_at
        )
        self.assertEqual("stop_hook", evidence.session_end_reason)

    def test_result_row_is_unit_scoped_when_shell_holds_multiple_units(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO sprint_units VALUES "
            "(2,59,'U4','feat/other','working','2020-01-04T00:00:00Z',"
            "'2020-01-03T00:00:00Z',1,1,3)"
        )
        con.executemany(
            "INSERT INTO shell_messages VALUES (?,?,?,?,?,?)",
            [
                (
                    2,
                    1,
                    59,
                    "2020-01-05T00:00:00Z",
                    "result",
                    "sprint 59: unit U4 ready",
                ),
                (
                    3,
                    1,
                    59,
                    "2020-01-04T12:00:00Z",
                    "result",
                    "sprint 59: unit=U3 ready",
                ),
            ],
        )
        con.commit()
        con.close()

        evidence = self.read()

        self.assertEqual(
            datetime(2020, 1, 4, 12, tzinfo=UTC),
            evidence.last_result_row_at,
        )
        self.assertEqual(
            datetime(2020, 1, 5, tzinfo=UTC),
            evidence.last_durable_write_at,
        )

    def test_missing_state_clock_is_independently_unreadable(self):
        unit = dict(self.unit)
        unit.pop("state_changed_at")

        evidence = self.reader.read(
            self.shell, unit, datetime(2020, 1, 10, tzinfo=UTC)
        )

        self.assertIsNone(evidence.state_changed_at)
        self.assertIn("state_changed_at", evidence.unreadable)
        self.assertEqual(EPOCH, evidence.epoch)
        self.assertEqual(
            datetime(2020, 1, 2, tzinfo=UTC),
            evidence.last_result_row_at,
        )

    def test_doc_unit_and_rowless_shell_never_claim_code_work(self):
        doc = dict(self.unit, branch=None)
        evidence = self.reader.read(self.shell, doc, datetime(2020, 1, 10, tzinfo=UTC))
        self.assertFalse(evidence.edits_code)
        self.assertIsNone(evidence.branch_present)
        self.assertIsNone(evidence.dirty)
        self.assertIsNone(evidence.commits_since_epoch)
        self.assertIsNone(evidence.last_work_at)

        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_sessions VALUES "
            "(9,1,NULL,'2020-01-05T00:00:00Z',NULL,NULL,'codex')"
        )
        con.commit()
        con.close()
        rowless = self.reader.read(
            self.shell, None, datetime(2020, 1, 10, tzinfo=UTC)
        )
        self.assertFalse(rowless.edits_code)
        self.assertEqual(datetime(2020, 1, 5, tzinfo=UTC), rowless.epoch)
        self.assertIsNone(rowless.branch_declared)
        self.assertEqual(
            datetime(2020, 1, 3, tzinfo=UTC),
            rowless.last_durable_write_at,
        )
        self.assertEqual(
            datetime(2020, 1, 2, tzinfo=UTC),
            rowless.last_result_row_at,
        )

    def test_each_unreadable_input_is_nullable_and_read_never_raises(self):
        broken = ar.ActivityReader(
            db_path=self.root / "missing.db",
            repo_root=self.root,
            proc=self.root / "missing-proc",
            adapters=self.root / "missing-adapters",
            home=self.home,
        )
        evidence = broken.read(
            dict(self.shell, worktree=str(self.root / "missing-worktree")),
            self.unit,
            datetime(2020, 1, 10, tzinfo=UTC),
        )
        self.assertIsInstance(evidence, ar.Evidence)
        self.assertIsNone(evidence.epoch)
        self.assertIsNone(evidence.branch_present)
        self.assertIsNone(evidence.process_present)
        self.assertIsNone(evidence.marker_at)
        self.assertEqual(
            [
                "branch",
                "durable_write",
                "epoch",
                "git_state",
                "marker",
                "process",
                "result_row",
                "session",
            ],
            evidence.unreadable,
        )

    def test_unreadable_marker_mapping_is_literal_exhaustive_and_nonempty(self):
        self.assertEqual(
            {
                "branch": frozenset({"branch_present"}),
                "commits": frozenset(
                    {"commits_since_epoch", "last_work_at"}
                ),
                "durable_write": frozenset({"last_durable_write_at"}),
                "epoch": frozenset({"epoch"}),
                "git_state": frozenset(
                    {"dirty", "commits_since_epoch", "last_work_at"}
                ),
                "last_work_at:untimed_delete_rename": frozenset(
                    {"last_work_at"}
                ),
                "marker": frozenset({"marker_at"}),
                "newest_mtime": frozenset({"newest_mtime"}),
                "process": frozenset(
                    {"process_present", "launch_shape", "cpu_delta"}
                ),
                "process_binding": frozenset(
                    {"launch_shape", "cpu_delta"}
                ),
                "result_row": frozenset({"last_result_row_at"}),
                "session": frozenset(
                    {"session_ended_at", "session_end_reason"}
                ),
                "state_changed_at": frozenset({"state_changed_at"}),
            },
            ar.UNREADABLE_FIELDS,
        )
        self.assertTrue(all(ar.UNREADABLE_FIELDS.values()))


class GitEvidenceTest(ReaderCase):
    def test_ledger_done_over_five_dirty_files_is_still_work(self):
        con = sqlite3.connect(self.db)
        con.executemany(
            "INSERT INTO spec_tasks(task_id,status) VALUES (?,'done')",
            [(n,) for n in range(1, 7)],
        )
        con.commit()
        con.close()
        newest = None
        for n in range(5):
            path = self.worktree / f"dirty-{n}.py"
            path.write_text(f"{n}\n")
            newest = datetime(2020, 1, 2, 0, 0, n, tzinfo=UTC)
            stamp(path, newest)

        evidence = self.read()
        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertEqual(newest, evidence.last_work_at)

    def test_boot_artifact_churn_is_not_work_with_positive_control(self):
        artifacts = [
            self.worktree / "CLAUDE.md",
            self.worktree / "AGENTS.md",
            self.worktree / ".claude/settings.local.json",
            self.worktree / ".claude/skills/x/SKILL.md",
            self.worktree / ".codex/hooks.json",
        ]
        for path in artifacts:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("boot\n")
            stamp(path, datetime(2020, 1, 2, tzinfo=UTC))
        evidence = self.read()
        self.assertTrue(evidence.dirty)
        self.assertIsNone(evidence.last_work_at)

        source = self.worktree / "source.py"
        source.write_text("work\n")
        worked = datetime(2020, 1, 3, tzinfo=UTC)
        stamp(source, worked)
        self.assertEqual(worked, self.read().last_work_at)

    def test_dirty_path_from_previous_generation_is_not_current_work(self):
        stale = self.worktree / "stale.py"
        stale.write_text("left by the previous launch\n")
        stamp(stale, datetime(2019, 12, 31, 23, 59, tzinfo=UTC))
        evidence = self.read()
        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertIsNone(evidence.last_work_at)

    def test_staged_delete_is_dirty_but_untimed(self):
        (self.worktree / "base.txt").unlink()
        command(self.worktree, "git", "add", "-u")

        evidence = self.read()

        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertIsNone(evidence.last_work_at)
        self.assertIn(ar.UNTIMED_DELETE_RENAME, evidence.unreadable)

    def test_staged_rename_with_pre_epoch_mtime_is_dirty_but_untimed(self):
        old = datetime(2019, 12, 31, 23, 59, tzinfo=UTC)
        stamp(self.worktree / "base.txt", old)
        command(self.worktree, "git", "mv", "base.txt", "renamed.txt")

        evidence = self.read()

        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertIsNone(evidence.last_work_at)
        self.assertIn(ar.UNTIMED_DELETE_RENAME, evidence.unreadable)

    def test_staged_rename_with_current_epoch_mtime_is_work(self):
        command(self.worktree, "git", "mv", "base.txt", "renamed.txt")
        renamed_at = datetime(2020, 1, 3, tzinfo=UTC)
        stamp(self.worktree / "renamed.txt", renamed_at)

        evidence = self.read()

        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertEqual(renamed_at, evidence.last_work_at)
        self.assertNotIn(ar.UNTIMED_DELETE_RENAME, evidence.unreadable)

    def test_author_date_counts_and_rebase_committer_date_does_not(self):
        old_author = self.worktree / "old-author.py"
        old_author.write_text("old\n")
        command(self.worktree, "git", "add", old_author.name)
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "old author new committer",
            env={
                "GIT_AUTHOR_DATE": "2019-06-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2020-06-01T00:00:00+00:00",
            },
        )
        self.assertEqual(0, self.read().commits_since_epoch)

        new_author = self.worktree / "new-author.py"
        new_author.write_text("new\n")
        command(self.worktree, "git", "add", new_author.name)
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "new author",
            env={
                "GIT_AUTHOR_DATE": "2020-02-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2020-03-01T00:00:00+00:00",
            },
        )
        evidence = self.read()
        self.assertEqual(1, evidence.commits_since_epoch)
        self.assertEqual(datetime(2020, 2, 1, tzinfo=UTC), evidence.last_work_at)

    def test_rebased_in_commits_do_not_count_but_own_commit_does(self):
        command(self.worktree, "git", "branch", "main")
        command(self.worktree, "git", "checkout", "main")
        integration = self.worktree / "integration.py"
        integration.write_text("another shell\n")
        command(self.worktree, "git", "add", integration.name)
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "integration work",
            env={
                "GIT_AUTHOR_DATE": "2020-01-05T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2020-01-05T00:00:00+00:00",
            },
        )
        command(
            self.worktree,
            "git",
            "update-ref",
            "refs/remotes/origin/main",
            "HEAD",
        )
        command(self.worktree, "git", "checkout", "feat/test")
        command(self.worktree, "git", "rebase", "main")

        rebased = self.read()

        self.assertEqual(0, rebased.commits_since_epoch)
        self.assertIsNone(rebased.last_work_at)

        own = self.worktree / "own.py"
        own.write_text("mine\n")
        command(self.worktree, "git", "add", own.name)
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "own work",
            env={
                "GIT_AUTHOR_DATE": "2020-01-06T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2020-01-06T00:00:00+00:00",
            },
        )

        contributed = self.read()

        self.assertEqual(1, contributed.commits_since_epoch)
        self.assertEqual(
            datetime(2020, 1, 6, tzinfo=UTC),
            contributed.last_work_at,
        )

    def test_conflicted_path_is_dirty_but_not_a_work_event(self):
        path = self.worktree / "conflict.txt"
        path.write_text("base\n")
        command(self.worktree, "git", "add", path.name)
        command(
            self.worktree,
            "git",
            "commit",
            "-m",
            "conflict base",
            env={
                "GIT_AUTHOR_DATE": "2019-01-02T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2019-01-02T00:00:00+00:00",
            },
        )
        command(self.worktree, "git", "checkout", "-b", "other")
        path.write_text("other\n")
        command(
            self.worktree,
            "git",
            "commit",
            "-am",
            "other",
            env={
                "GIT_AUTHOR_DATE": "2019-01-03T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2019-01-03T00:00:00+00:00",
            },
        )
        command(self.worktree, "git", "checkout", "feat/test")
        path.write_text("ours\n")
        command(
            self.worktree,
            "git",
            "commit",
            "-am",
            "ours",
            env={
                "GIT_AUTHOR_DATE": "2019-01-04T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2019-01-04T00:00:00+00:00",
            },
        )
        command(self.worktree, "git", "merge", "other", check=False)
        stamp(path, datetime(2020, 1, 4, tzinfo=UTC))

        evidence = self.read()
        self.assertTrue(evidence.dirty)
        self.assertEqual(0, evidence.commits_since_epoch)
        self.assertIsNone(evidence.last_work_at)

    def test_branch_reads_head_then_origin_never_a_local_ref(self):
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            if "symbolic-ref" in args:
                return subprocess.CompletedProcess(args, 0, "other\n", "")
            return subprocess.CompletedProcess(args, 0, "abc\trefs/heads/feat/test\n", "")

        reader = ar.ActivityReader(
            db_path=self.db,
            repo_root=self.root,
            proc=self.proc,
            adapters=self.adapters,
            home=self.home,
            run=fake,
        )
        evidence = ar.Evidence()
        present, on_head = reader._branch_present(
            evidence, self.worktree, "feat/test"
        )
        self.assertTrue(present)
        self.assertFalse(on_head)
        self.assertEqual("ls-remote", calls[1][1])
        self.assertNotIn("show-ref", " ".join(calls[1]))

        def absent(args, **kwargs):
            if "symbolic-ref" in args:
                return subprocess.CompletedProcess(args, 0, "other\n", "")
            return subprocess.CompletedProcess(args, 2, "", "")

        reader.run_command = absent
        self.assertEqual(
            (False, False),
            reader._branch_present(ar.Evidence(), self.worktree, "missing"),
        )


class ProcessReaderTest(ReaderCase):
    def test_argv_shape_rowless_and_whole_tree_cpu_delta(self):
        proc_row(
            self.proc,
            100,
            comm="codex",
            cwd=self.worktree,
            argv=["codex", "exec", "-m", "gpt-5.6-sol"],
            utime=10,
            stime=5,
            start=111,
        )
        proc_row(
            self.proc,
            101,
            comm="python",
            cwd=self.worktree,
            argv=["python", "helper.py"],
            ppid=100,
            utime=4,
            stime=1,
            start=222,
        )
        first = ar.Evidence()
        self.assertEqual("codex", self.reader._process_inputs(first, self.worktree))
        self.assertTrue(first.process_present)
        self.assertEqual("headless", first.launch_shape)
        self.assertIsNone(first.cpu_delta)

        proc_row(
            self.proc,
            100,
            comm="codex",
            cwd=self.worktree,
            argv=["codex", "exec"],
            utime=14,
            stime=6,
            start=111,
        )
        proc_row(
            self.proc,
            101,
            comm="python",
            cwd=self.worktree,
            argv=["python", "helper.py"],
            ppid=100,
            utime=8,
            stime=2,
            start=222,
        )
        second = ar.Evidence()
        self.reader._process_inputs(second, self.worktree)
        self.assertEqual(10.0, second.cpu_delta)

        # No unit row is needed to resolve launch shape from argv.
        rowless = self.reader.read(
            self.shell, None, datetime(2020, 1, 10, tzinfo=UTC)
        )
        self.assertEqual("headless", rowless.launch_shape)
        self.assertTrue(rowless.process_present)

    def test_same_binary_uses_prompt_flag_to_separate_claude_shapes(self):
        spec = json.loads(
            (self.adapters / "claude" / "adapter.json").read_text()
        )
        self.assertEqual("interactive", self.reader._shape(["claude"], spec))
        self.assertEqual(
            "headless", self.reader._shape(["claude", "-p", "task"], spec)
        )

    def test_absent_process_is_false_but_unreadable_proc_is_none(self):
        evidence = ar.Evidence()
        self.assertIsNone(self.reader._process_inputs(evidence, self.worktree))
        self.assertFalse(evidence.process_present)

        broken = ar.ActivityReader(
            db_path=self.db,
            repo_root=self.root,
            proc=self.root / "absent",
            adapters=self.adapters,
            home=self.home,
        )
        unknown = ar.Evidence()
        broken._process_inputs(unknown, self.worktree)
        self.assertIsNone(unknown.process_present)
        self.assertEqual(["process"], unknown.unreadable)

    def test_foreign_worktree_process_does_not_make_worker_present(self):
        foreign = self.root / "foreign-worktree"
        foreign.mkdir()
        proc_row(
            self.proc,
            200,
            comm="codex",
            cwd=foreign,
            argv=["codex", "exec"],
        )

        evidence = ar.Evidence()
        harness = self.reader._process_inputs(evidence, self.worktree)

        self.assertIsNone(harness)
        self.assertFalse(evidence.process_present)
        self.assertEqual([], evidence.unreadable)

    def test_multiple_matching_processes_are_present_but_binding_unreadable(self):
        for pid in (200, 201):
            proc_row(
                self.proc,
                pid,
                comm="codex",
                cwd=self.worktree,
                argv=["codex", "exec"],
            )

        evidence = ar.Evidence()
        harness = self.reader._process_inputs(evidence, self.worktree)

        self.assertIsNone(harness)
        self.assertTrue(evidence.process_present)
        self.assertIsNone(evidence.launch_shape)
        self.assertIsNone(evidence.cpu_delta)
        self.assertEqual(["process_binding"], evidence.unreadable)


class HarnessMarkerTest(ReaderCase):
    def test_claude_uses_dot_encoding_and_aggregates_own_scratchpad(self):
        fixture_root = Path("/tmp") / f"scactivity{os.getpid()}"
        self.addCleanup(shutil.rmtree, fixture_root, True)
        dotted_worktree = fixture_root / ".sc-worktrees" / "dev1"
        dotted_worktree.mkdir(parents=True)
        projects = self.home / ".claude/projects"
        expected_container = (
            f"-tmp-scactivity{os.getpid()}--sc-worktrees-dev1"
        )
        main = projects / expected_container
        scratch = (
            projects
            / f"-tmp-claude-0-{expected_container}-session-scratchpad"
        )
        foreign = projects / "-tmp-claude-0--foreign-session-scratchpad"
        for path in (main, scratch, foreign):
            path.mkdir(parents=True)
        main_log = main / "main.jsonl"
        scratch_log = scratch / "sub.jsonl"
        foreign_log = foreign / "other.jsonl"
        for path in (main_log, scratch_log, foreign_log):
            path.write_text("{}\n")
        stamp(main_log, datetime(2020, 1, 2, tzinfo=UTC))
        stamp(scratch_log, datetime(2020, 1, 3, tzinfo=UTC))
        stamp(foreign_log, datetime(2020, 1, 4, tzinfo=UTC))

        self.assertEqual(
            datetime(2020, 1, 3, tzinfo=UTC),
            self.reader._claude_marker(dotted_worktree, EPOCH),
        )

    def _rollout(self, name: str, records: list[dict], when: datetime) -> Path:
        directory = self.home / ".codex/sessions/2019/01/01"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{name}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        stamp(path, when)
        return path

    def test_codex_binds_last_turn_context_not_meta_or_newest_neighbor(self):
        mine = datetime(2020, 1, 1, 0, 10, tzinfo=UTC)
        self._rollout(
            "mine",
            [
                {"type": "session_meta", "payload": {"cwd": "/stale"}},
                {"type": "turn_context", "payload": {"cwd": str(self.worktree)}},
            ],
            mine,
        )
        self._rollout(
            "newer-foreign",
            [{"type": "turn_context", "payload": {"cwd": "/neighbor"}}],
            datetime(2020, 1, 1, 0, 20, tzinfo=UTC),
        )
        self.assertEqual(
            mine,
            self.reader._codex_marker(
                self.worktree, EPOCH, datetime(2020, 1, 1, 0, 30, tzinfo=UTC)
            ),
        )

    def test_codex_no_match_and_multi_match_are_both_ambiguous(self):
        now = datetime(2020, 1, 1, 0, 50, tzinfo=UTC)
        self._rollout(
            "foreign",
            [{"type": "turn_context", "payload": {"cwd": "/neighbor"}}],
            datetime(2020, 1, 1, 0, 10, tzinfo=UTC),
        )
        self.assertIsNone(self.reader._codex_marker(self.worktree, EPOCH, now))
        for name, minute in (("mine-a", 20), ("mine-b", 30)):
            self._rollout(
                name,
                [{"type": "turn_context", "payload": {"cwd": str(self.worktree)}}],
                datetime(2020, 1, 1, 0, minute, tzinfo=UTC),
            )
        self.assertIsNone(self.reader._codex_marker(self.worktree, EPOCH, now))

        evidence = ar.Evidence(epoch=EPOCH)
        self.reader._marker_input(evidence, "codex", self.worktree, now)
        self.assertIsNone(evidence.marker_at)
        self.assertEqual(["marker"], evidence.unreadable)

    def test_kimi_uses_workdir_and_wire_never_state_updated_at(self):
        session = self.home / ".kimi-code/sessions/wd_test_hash/session_1"
        wire = session / "agents/main/wire.jsonl"
        wire.parent.mkdir(parents=True)
        state = session / "state.json"
        state.write_text(json.dumps({"workDir": str(self.worktree)}))
        wire.write_text("{}\n")
        wire_time = datetime(2020, 1, 3, tzinfo=UTC)
        stamp(wire, wire_time)
        stamp(state, datetime(2020, 1, 9, tzinfo=UTC))
        self.assertEqual(
            wire_time, self.reader._kimi_marker(self.worktree, EPOCH)
        )

    def test_opencode_binds_exact_directory_and_session_clock(self):
        db = self.home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE session(id TEXT, directory TEXT, time_updated INTEGER)"
        )
        con.executemany(
            "INSERT INTO session VALUES (?,?,?)",
            [
                ("mine", str(self.worktree), int(datetime(2020, 1, 2, tzinfo=UTC).timestamp() * 1000)),
                ("foreign", f"{self.worktree}-other", 1_578_096_000_000),
            ],
        )
        con.commit()
        con.close()
        self.assertEqual(
            datetime(2020, 1, 2, tzinfo=UTC),
            self.reader._opencode_marker(self.worktree, EPOCH),
        )

    def test_marker_never_uses_filesystem_without_live_process_binding(self):
        evidence = self.read()
        self.assertFalse(evidence.process_present)
        self.assertIsNone(evidence.marker_at)
        self.assertIn("marker", evidence.unreadable)


if __name__ == "__main__":
    unittest.main()
