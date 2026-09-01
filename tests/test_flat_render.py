"""Flat document mirrors converge when document sources move or disappear."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / ".super-coder" / "render"
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path[:0] = [str(RENDER), str(SCRIPTS)]

import flat  # noqa: E402


SCHEMA = """
CREATE TABLE roadmap (
    feature_id INTEGER PRIMARY KEY,
    title TEXT,
    roadmap_status TEXT
);
CREATE TABLE documents (
    document_id INTEGER PRIMARY KEY,
    feature_id INTEGER,
    kind TEXT,
    seq INTEGER,
    title TEXT,
    body TEXT,
    render_path TEXT,
    frozen INTEGER
);
"""


class FlatDocumentReconciliationTest(unittest.TestCase):
    def test_render_replaces_a_document_at_its_new_path(self):
        with tempfile.TemporaryDirectory() as td, closing(
            sqlite3.connect(":memory:")
        ) as con:
            root = Path(td)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            con.execute("INSERT INTO roadmap VALUES (7, 'Gateway', 'next')")
            con.execute(
                "INSERT INTO documents VALUES "
                "(11, 7, 'spec', 1, 'Gateway', 'Current body', "
                "'specs_sc/current.md', 0)"
            )
            current = root / "specs_sc" / "current.md"
            stale = root / "specs_sc" / "old-name.md"
            stale.parent.mkdir()
            current.write_text(
                "---\n"
                "rendered_by: super-coder\n"
                "source: db\n"
                "edit: changes here are overwritten — author via the shell or localhost GUI\n"
                "feature: Gateway\n"
                "roadmap_status: next\n"
                "frozen: false\n"
                "---\n\n"
                "Current body\n"
            )
            stale.write_text("stale body\n")

            written: list[Path] = []
            skipped: list[Path] = []
            flat._render_documents(con, written, skipped, root)

            self.assertEqual([stale], written)
            self.assertEqual([current], skipped)
            self.assertEqual(
                "---\n"
                "rendered_by: super-coder\n"
                "source: db\n"
                "edit: changes here are overwritten — author via the shell or localhost GUI\n"
                "feature: Gateway\n"
                "roadmap_status: next\n"
                "frozen: false\n"
                "---\n\n"
                "Current body\n",
                current.read_text(),
            )
            self.assertFalse(stale.exists())

    def test_render_removes_orphans_only_from_managed_document_roots(self):
        with tempfile.TemporaryDirectory() as td, closing(
            sqlite3.connect(":memory:")
        ) as con:
            root = Path(td)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            stale_spec = root / "specs_sc" / "retired.md"
            stale_doc = root / "docs_sc" / "nested" / "retired.md"
            outside = root / "notes" / "keep.md"
            for path in (stale_spec, stale_doc, outside):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{path.name}\n")

            written: list[Path] = []
            skipped: list[Path] = []
            flat._render_documents(con, written, skipped, root)

            self.assertEqual({stale_spec, stale_doc}, set(written))
            self.assertEqual([], skipped)
            self.assertFalse(stale_spec.exists())
            self.assertFalse(stale_doc.exists())
            self.assertEqual("keep.md\n", outside.read_text())

    def test_duplicate_document_targets_fail_before_any_write(self):
        with tempfile.TemporaryDirectory() as td, closing(
            sqlite3.connect(":memory:")
        ) as con:
            root = Path(td)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            con.execute("INSERT INTO roadmap VALUES (7, 'Gateway', 'next')")
            con.execute(
                "INSERT INTO documents VALUES "
                "(11, 7, 'spec', 1, 'First', 'First body', "
                "'specs_sc/shared.md', 0)"
            )
            con.execute(
                "INSERT INTO documents VALUES "
                "(12, 7, 'spec', 2, 'Second', 'Second body', "
                "'specs_sc//shared.md', 0)"
            )
            target = root / "specs_sc" / "shared.md"
            target.parent.mkdir()
            target.write_text("preserved\n")
            written: list[Path] = []
            skipped: list[Path] = []

            with self.assertRaisesRegex(
                ValueError,
                "duplicate document render path.*document IDs 11 and 12",
            ):
                flat._render_documents(con, written, skipped, root)

            self.assertEqual(target.read_text(), "preserved\n")
            self.assertEqual(written, [])
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
