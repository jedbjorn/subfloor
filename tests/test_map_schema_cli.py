#!/usr/bin/env python3
"""Regression coverage for the read-only sc map-schema surface."""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import artifact_policy  # noqa: E402
import map_schema_cli  # noqa: E402


class MapSchemaCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "instance" / ".sc-state" / "local"
        self.db = self.local / "map" / "map.db"
        self.db.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE dr_alpha (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL DEFAULT 'new',
                note TEXT
            );
            CREATE INDEX dr_alpha_label_idx ON dr_alpha(label);
            CREATE TABLE dr_zeta (value TEXT);
            CREATE VIEW dr_alpha_view AS SELECT id, label FROM dr_alpha;
            CREATE TABLE private_state (secret TEXT);
            """
        )
        connection.close()
        self.local_patch = mock.patch.object(artifact_policy, "LOCAL_DIR", self.local)
        self.local_patch.start()
        self.addCleanup(self.local_patch.stop)

    def capture(self, argv: list[str]) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, map_schema_cli.main(argv))
        return output.getvalue()

    def test_lists_only_dr_objects_in_stable_order(self):
        self.assertEqual(
            "dr_alpha\ttable\ndr_alpha_view\tview\ndr_zeta\ttable\n",
            self.capture([]),
        )

    def test_detail_pins_columns_nullability_defaults_primary_key_and_indexes(self):
        output = self.capture(["dr_alpha"])

        self.assertIn("object: dr_alpha (table)", output)
        self.assertIn("0\tid\tINTEGER\tno\t-\t1", output)
        self.assertIn("1\tlabel\tTEXT\tno\t'new'\t0", output)
        self.assertIn("2\tnote\tTEXT\tyes\t-\t0", output)
        self.assertIn("dr_alpha_label_idx\t0\tc\t0", output)

    def test_unknown_non_map_and_extra_arguments_are_rejected(self):
        cases = (
            (["users"], "exact dr_"),
            (["dr_missing"], "available: dr_alpha, dr_alpha_view, dr_zeta"),
            (["dr_alpha", "extra"], "usage"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(map_schema_cli.MapSchemaError, message):
                    map_schema_cli.main(argv)

    def test_resolution_is_cwd_independent_and_database_remains_read_only(self):
        before = self.db.read_bytes()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(elsewhere)
            output = self.capture(["dr_alpha_view"])
        finally:
            os.chdir(previous)

        self.assertIn("object: dr_alpha_view (view)", output)
        self.assertEqual(before, self.db.read_bytes())

    def test_missing_database_fails_without_creating_it(self):
        self.db.unlink()
        with self.assertRaisesRegex(map_schema_cli.MapSchemaError, "read-only"):
            map_schema_cli.main([])
        self.assertFalse(self.db.exists())

    def test_dispatcher_routes_the_dedicated_schema_module(self):
        dispatch = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertIn(
            'map-schema)   exec "$PY" "$S/map_schema_cli.py" "$@" ;;',
            dispatch,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
