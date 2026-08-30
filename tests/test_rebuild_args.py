#!/usr/bin/env python3
"""Argument preflight for rebuild (spec #67).

`./sc rebuild --help` and every typo used to run a REAL rebuild: main() read the
schema, carried the outgoing api_keys, probed live Interface state and took a
backup before it looked at argv at all. These tests pin the inverse — help and
invalid input decide everything they decide before any of that machinery is
reachable — so they arm each pre-action seam to raise and then assert the seams
stayed unfired AND the sentinel tree is byte-identical.

An unfired tripwire only means something if the tripwire can fire, so
`test_action_forms_reach_the_armed_seams` is the positive control: the same
patched seams, driven by the two valid forms, must raise.
"""
from __future__ import annotations

import hashlib
import io
import shutil
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
import rebuild  # noqa: E402


class Tripwire(Exception):
    """Raised by a seam that a non-action form must never reach."""


def tripwire(name: str):
    def fail(*args, **kwargs):
        raise Tripwire(name)
    return fail


def tree_state(root: Path) -> dict[str, str]:
    """Every file under root, by content. A created candidate or backup shows up
    as a new key, a mutated DB as a changed digest — one observation covering
    both halves of 'byte-for-byte unchanged'."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def sentinel_tree(tmp: Path) -> Path:
    """An outgoing DB with live WAL/SHM sidecars and a populated backup dir.

    The bytes are not a real database on purpose: any seam that opens or copies
    them fails loudly rather than quietly succeeding.
    """
    (tmp / "shell_db.db").write_bytes(b"outgoing-database")
    (tmp / "shell_db.db-wal").write_bytes(b"outgoing-wal")
    (tmp / "shell_db.db-shm").write_bytes(b"outgoing-shm")
    backups = tmp / "backups"
    backups.mkdir()
    (backups / "shell_db.prerebuild.20260101_000000.db").write_bytes(b"old-backup")
    (tmp / "schema.sql").write_text("-- sentinel schema\n")
    return tmp / "shell_db.db"


def armed(stack: ExitStack, tmp: Path, **overrides):
    """Point rebuild at the sentinel tree with every post-parse seam armed.

    Seams are armed by NAME so a failure says which one ran. Both database entry
    points (db_driver.connect, sqlite3.connect) are included: 'no database open'
    is a claim about opening, not about a particular helper.
    """
    patches = {
        "DB_PATH": tmp / "shell_db.db",
        "REPO_ROOT": tmp,
        "ENGINE": tmp,
        "SCHEMA_SQLITE": tmp / "schema.sql",
        "SNAPSHOT": tmp / "content.sql",
        "SNAPSHOT_LEGACY": tmp / "legacy-content.sql",
        "read_existing_keys": tripwire("read_existing_keys"),
        "restore_keys": tripwire("restore_keys"),
        "backup_existing": tripwire("backup_existing"),
        "backup_dir": tripwire("backup_dir"),
        "backup_db": tripwire("backup_db"),
        "prune_backups": tripwire("prune_backups"),
        "snapshot_path": tripwire("snapshot_path"),
        "validate_foreign_keys": tripwire("validate_foreign_keys"),
        "_remove_database": tripwire("_remove_database"),
    }
    patches.update(overrides)
    stack.enter_context(mock.patch.multiple(rebuild, **patches))
    stack.enter_context(mock.patch.object(
        rebuild.db_driver, "connect", tripwire("db_driver.connect")))
    stack.enter_context(mock.patch.object(
        rebuild.sqlite3, "connect", tripwire("sqlite3.connect")))
    stack.enter_context(mock.patch.object(
        rebuild.db_backup_mod, "select_backup_dir",
        tripwire("db_backup.select_backup_dir")))
    stack.enter_context(mock.patch.object(
        rebuild.migrate_mod, "migrate", tripwire("migrate")))
    stack.enter_context(mock.patch.object(
        rebuild.map_repo, "main", tripwire("map_repo.main")))
    stack.enter_context(mock.patch.object(
        rebuild.backfill_shell_api_keys, "backfill", tripwire("backfill")))
    stack.enter_context(mock.patch.object(
        rebuild.seed_skills, "apply_retired", tripwire("apply_retired")))


def run_main(argv: list[str]) -> tuple[int, str, str]:
    """main(argv) -> (exit code, stdout, stderr). A form that neither exits nor
    raises is itself a failure: every form under test is a terminating one."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rebuild.main(argv)
        except SystemExit as exc:
            code = exc.code
        else:
            raise AssertionError(f"main({argv!r}) returned without exiting")
    return code, out.getvalue(), err.getvalue()


class RebuildHelpTest(unittest.TestCase):
    def test_help_exits_zero_with_usage_and_touches_no_state(self):
        for argv in (["-h"], ["--help"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                sentinel_tree(tmp)
                before = tree_state(tmp)
                with ExitStack() as stack:
                    armed(stack, tmp)
                    code, out, err = run_main(argv)
                self.assertEqual(code, 0)
                self.assertIn("usage: ./sc rebuild [--no-backup]", out)
                self.assertIn("--no-backup", out)
                self.assertIn("skip the pre-rebuild backup", out)
                self.assertEqual(err, "")
                self.assertEqual(tree_state(tmp), before)

    def test_help_exits_zero_when_the_schema_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sentinel_tree(tmp)
            (tmp / "schema.sql").unlink()
            before = tree_state(tmp)
            with ExitStack() as stack:
                armed(stack, tmp, SCHEMA_SQLITE=tmp / "schema.sql")
                code, out, err = run_main(["--help"])
            self.assertEqual(code, 0)
            self.assertIn("usage: ./sc rebuild", out)
            self.assertNotIn("missing", err)
            self.assertEqual(tree_state(tmp), before)

    def test_help_wins_over_action_and_unknown_input_and_performs_no_action(self):
        for argv in (
            ["--no-backup", "--help"],
            ["--help", "--no-backup"],
            ["-h", "--no-backup"],
            ["--bogus", "-h"],
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                sentinel_tree(tmp)
                before = tree_state(tmp)
                with ExitStack() as stack:
                    armed(stack, tmp)
                    code, out, err = run_main(argv)
                self.assertEqual(code, 0)
                self.assertIn("usage: ./sc rebuild", out)
                self.assertNotIn("done ->", out)
                self.assertEqual(err, "")
                self.assertEqual(tree_state(tmp), before)


class RebuildInvalidArgumentTest(unittest.TestCase):
    def test_unknown_argument_exits_two_naming_the_token_and_touches_no_state(self):
        for argv, rejected in (
            (["--dry-run"], "--dry-run"),
            (["-n"], "-n"),
            (["oops"], "oops"),
            (["--no-backup", "--force"], "--force"),
            (["--force", "--no-backup"], "--force"),
            (["--no-backup=1"], "--no-backup=1"),
            ([""], ""),
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                sentinel_tree(tmp)
                before = tree_state(tmp)
                with ExitStack() as stack:
                    armed(stack, tmp)
                    code, out, err = run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn(f"unknown argument '{rejected}'", err)
                self.assertIn("usage: ./sc rebuild [--no-backup]", err)
                self.assertEqual(out, "")
                self.assertEqual(tree_state(tmp), before)

    def test_first_unknown_token_is_the_one_named(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sentinel_tree(tmp)
            with ExitStack() as stack:
                armed(stack, tmp)
                code, _, err = run_main(["--first", "--second"])
            self.assertEqual(code, 2)
            self.assertIn("'--first'", err)
            self.assertNotIn("'--second'", err)


class RebuildActionFormTest(unittest.TestCase):
    """The positive control plus the two valid forms' meaning.

    Each case lets the seams BEFORE the one under test behave, and names the
    seam that must then be reached. That proves the tripwires in the tests above
    are live rather than merely unreachable, and pins --no-backup's meaning:
    the default form must reach backup_existing, and --no-backup must get past
    it without calling it.
    """

    def test_action_forms_reach_the_armed_seams(self):
        for argv, expected in (
            ([], "read_existing_keys"),
            (["--no-backup"], "read_existing_keys"),
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                sentinel_tree(tmp)
                with ExitStack() as stack:
                    armed(stack, tmp)
                    with self.assertRaises(Tripwire) as ctx:
                        rebuild.main(argv)
                self.assertEqual(str(ctx.exception), expected)

    def test_default_form_still_backs_up_and_no_backup_still_skips_it(self):
        for argv, expected in (
            ([], "backup_existing"),
            (["--no-backup"], "_remove_database"),
            (["--no-backup", "--no-backup"], "_remove_database"),
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                sentinel_tree(tmp)
                with ExitStack() as stack:
                    armed(
                        stack, tmp,
                        read_existing_keys=lambda *a, **k: {},
                    )
                    with self.assertRaises(Tripwire) as ctx:
                        rebuild.main(argv)
                self.assertEqual(str(ctx.exception), expected)


class RebuildDispatchParityTest(unittest.TestCase):
    """Requirement 1: rebuild.py owns the WHOLE contract, so `./sc rebuild` and
    a direct script call cannot differ — which holds only while the dispatcher
    forwards verbatim. Both halves are pinned: the forwarder's text, and the
    script's own end-to-end behavior in a fresh interpreter."""

    def test_dispatcher_forwards_every_argument_verbatim(self):
        # Spec #68 (U10) put a linked-worktree refusal in front of the forward,
        # so the case is two lines now — pinned as two lines, deliberately. The
        # requirement is unchanged and is what the second line still says: the
        # dispatcher adds nothing to argv and removes nothing from it. The first
        # line is pinned too because ORDER is the contract: the help question is
        # asked before the refusal, which resolves the active DB through the
        # canonical seam before the exec.
        lines = [ln.strip() for ln in (REPO / ".super-coder" / "scripts" / "dispatch.sh").read_text().splitlines()]
        start = next(i for i, ln in enumerate(lines) if ln.startswith("rebuild)"))
        self.assertEqual(
            lines[start],
            'rebuild)      sc_help_form "$@" || sc_refuse_linked rebuild "$(sc_engine_db)"')
        self.assertEqual(
            lines[start + 1], 'exec "$PY" "$S/rebuild.py" "$@" ;;')

    def test_script_invocation_handles_help_and_rejection_end_to_end(self):
        # A COPY of the engine scripts, deliberately without schema.sql: this
        # subprocess is the one place a regression could start a real rebuild,
        # so the only instance it can reach is a throwaway that rebuild refuses.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            shutil.copytree(ENGINE / "scripts", tmp / ".super-coder" / "scripts")
            script = tmp / ".super-coder" / "scripts" / "rebuild.py"

            for argv, code, stream, needle in (
                (["--help"], 0, "stdout", "usage: ./sc rebuild [--no-backup]"),
                (["-h"], 0, "stdout", "--no-backup  skip the pre-rebuild backup"),
                (["--wat"], 2, "stderr", "unknown argument '--wat'"),
                (["bogus"], 2, "stderr", "unknown argument 'bogus'"),
            ):
                with self.subTest(argv=argv):
                    done = subprocess.run(
                        [sys.executable, str(script), *argv],
                        capture_output=True, text=True, cwd=str(tmp),
                        timeout=120, check=False)
                    self.assertEqual(done.returncode, code, done.stderr)
                    self.assertIn(needle, getattr(done, stream))
                    quiet = "stderr" if stream == "stdout" else "stdout"
                    self.assertEqual(getattr(done, quiet), "")
                    self.assertFalse(
                        (tmp / ".super-coder" / "shell_db.db").exists())


if __name__ == "__main__":
    unittest.main()
