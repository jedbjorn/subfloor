"""Regression coverage for map DB independence from private engine state."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import instance_state
import map_db


def section_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE dr_section ("
        "name TEXT PRIMARY KEY, path_prefix TEXT, description TEXT, sort_order INTEGER)"
    )
    return con


class PrivateStateIndependenceTests(unittest.TestCase):
    def test_import_does_not_resolve_engine_database(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "map_db_import_probe", SCRIPTS / "map_db.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)

        with mock.patch.object(
            instance_state,
            "active_database_path",
            side_effect=AssertionError("must not resolve private state at import"),
        ):
            spec.loader.exec_module(module)

    def test_existing_sections_do_not_resolve_engine_database(self) -> None:
        con = section_connection()
        self.addCleanup(con.close)
        con.execute("INSERT INTO dr_section VALUES ('Code', 'src/', 'code', 10)")

        with mock.patch.object(
            map_db.instance_state,
            "active_database_path",
            side_effect=AssertionError("must not resolve private state"),
        ) as resolve:
            map_db.seed_authored(con)

        resolve.assert_not_called()

    def test_snapshot_does_not_resolve_engine_database(self) -> None:
        con = section_connection()
        self.addCleanup(con.close)
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / "content.sql"
            snapshot.write_text(
                "INSERT INTO dr_section VALUES ('Docs', 'docs/', 'docs', 20);\n"
            )
            with mock.patch.object(map_db, "MAP_CONTENT", snapshot), mock.patch.object(
                map_db.instance_state,
                "active_database_path",
                side_effect=AssertionError("must not resolve private state"),
            ) as resolve:
                map_db.seed_authored(con)

        resolve.assert_not_called()
        self.assertEqual(
            con.execute("SELECT name FROM dr_section").fetchone()[0], "Docs"
        )

    def test_inaccessible_legacy_source_does_not_block_fresh_map(self) -> None:
        con = section_connection()
        self.addCleanup(con.close)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            map_db, "MAP_CONTENT", Path(td) / "missing.sql"
        ), mock.patch.object(
            map_db.instance_state,
            "active_database_path",
            side_effect=instance_state.InstanceStateError(
                "cannot read private state owner metadata: permission denied"
            ),
        ):
            map_db.seed_authored(con)

        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM dr_section").fetchone()[0], 0
        )

    def test_readable_legacy_source_still_imports_sections(self) -> None:
        con = section_connection()
        self.addCleanup(con.close)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            legacy = base / "shell_db.db"
            source = sqlite3.connect(legacy)
            source.execute(
                "CREATE TABLE dr_section ("
                "name TEXT, path_prefix TEXT, description TEXT, sort_order INTEGER)"
            )
            source.execute(
                "INSERT INTO dr_section VALUES ('Tests', 'tests/', 'tests', 30)"
            )
            source.commit()
            source.close()

            with mock.patch.object(
                map_db, "MAP_CONTENT", base / "missing.sql"
            ), mock.patch.object(
                map_db.instance_state, "active_database_path", return_value=legacy
            ):
                map_db.seed_authored(con)

        self.assertEqual(
            con.execute(
                "SELECT name, path_prefix FROM dr_section"
            ).fetchone(),
            ("Tests", "tests/"),
        )


if __name__ == "__main__":
    unittest.main()
