#!/usr/bin/env python3
"""Active sprints warn during update but never withhold the recovery path."""
from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402


def build_live_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            con.executescript(migration.read_text())
        con.execute(
            "INSERT INTO users (user_id, username, is_active) "
            "VALUES (1,'operator',1)")
        con.execute(
            "INSERT INTO shells "
            "(shell_id, display_name, shortname, mandate, system_prompt, "
            "user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (1,'Admin','AMI','test','test',1,0,1,1)")
        con.execute(
            "INSERT INTO documents (document_id, kind, title, frozen) "
            "VALUES (59,'doc','SPRINT: update recovery',0)")
        con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id, seq, unit_title, state) "
            "VALUES (59,'U1','recovery proof','working')")
        con.execute(
            "INSERT INTO sprints (sprint_doc_id, state, legacy) "
            "VALUES (59,'active',1)")
        con.commit()
    finally:
        con.close()


class Stop(Exception):
    """Sentinel proving main() reached the first installed-floor mutation."""


class UpdateActiveSprintWarningTest(unittest.TestCase):
    def test_active_sprints_warn_and_proceed(self):
        out = io.StringIO()
        with mock.patch.object(
            update, "active_sprint_ids", return_value={59, 60}
        ), contextlib.redirect_stdout(out):
            self.assertIsNone(update.warn_live_state())
        warning = out.getvalue()
        self.assertIn("ACTIVE sprint(s) exist: 59, 60", warning)
        self.assertIn("continuing", warning)
        self.assertIn("preserved", warning)

    def test_no_active_sprint_is_quiet(self):
        out = io.StringIO()
        with mock.patch.object(
            update, "active_sprint_ids", return_value=set()
        ), contextlib.redirect_stdout(out):
            self.assertIsNone(update.warn_live_state())
        self.assertEqual(out.getvalue(), "")

    def test_real_active_sprint_warning_does_not_mutate_board_or_lifecycle(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            build_live_db(db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as con:
                before = (
                    con.execute(
                        "SELECT sprint_doc_id,state,closed_at FROM sprints"
                    ).fetchall(),
                    con.execute(
                        "SELECT sprint_doc_id,seq,state FROM sprint_units"
                    ).fetchall(),
                    con.execute(
                        "SELECT document_id,frozen FROM documents "
                        "WHERE document_id=59"
                    ).fetchall(),
                )

            out = io.StringIO()
            with mock.patch.object(update, "DB_PATH", db_path), \
                    contextlib.redirect_stdout(out):
                update.warn_live_state()

            self.assertIn("ACTIVE sprint(s) exist: 59", out.getvalue())
            with contextlib.closing(sqlite3.connect(db_path)) as con:
                after = (
                    con.execute(
                        "SELECT sprint_doc_id,state,closed_at FROM sprints"
                    ).fetchall(),
                    con.execute(
                        "SELECT sprint_doc_id,seq,state FROM sprint_units"
                    ).fetchall(),
                    con.execute(
                        "SELECT document_id,frozen FROM documents "
                        "WHERE document_id=59"
                    ).fetchall(),
                )
            self.assertEqual(after, before)

    def test_installed_fork_main_continues_past_active_sprint_warning(self):
        out = io.StringIO()
        with mock.patch.object(update, "is_source_repo", return_value=False), \
                mock.patch.object(update, "sync_repo_checkout") as sync, \
                mock.patch.object(
                    update, "fetch_update_ref", return_value="a" * 40
                ) as fetch, mock.patch.object(
                    update, "active_sprint_ids", return_value={59}
                ), mock.patch.object(
                    update, "migrate_engine_untrack", side_effect=Stop
                ) as first_mutation, contextlib.redirect_stdout(out):
            with self.assertRaises(Stop):
                update.main([])

        sync.assert_called_once()
        fetch.assert_called_once_with("main", ref=None)
        first_mutation.assert_called_once()
        self.assertIn("ACTIVE sprint(s) exist: 59", out.getvalue())
        self.assertIn("continuing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
