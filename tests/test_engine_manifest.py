#!/usr/bin/env python3
"""Tests for the engine hash manifest (scripts/engine_manifest.py) — the guard
that keeps `./sc update` from silently overwriting local engine edits.

The contract: write_manifest() right after a materialize records upstream's
on-disk state; local_edits() before the next one reports exactly the files an
overwrite would clobber (modified/deleted), and nothing else — no manifest, a
clean tree, or a locally-ADDED file (materialize never touches those) all
report clean.

Run:
    python3 tests/test_engine_manifest.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import engine_manifest  # noqa: E402


class EngineManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / ".super-coder" / "scripts").mkdir(parents=True)
        (root / "sc").write_text("#!/bin/sh\n")
        (root / ".super-coder" / "schema.sql").write_text("CREATE TABLE t(x);\n")
        (root / ".super-coder" / "scripts" / "a.py").write_text("A = 1\n")
        (root / ".super-coder" / "scripts" / "b.py").write_text("B = 2\n")
        # skipped noise: bytecode + __pycache__ must never enter the manifest
        (root / ".super-coder" / "scripts" / "__pycache__").mkdir()
        (root / ".super-coder" / "scripts" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00")
        self.root = root
        self.paths = ["sc", ".super-coder/schema.sql", ".super-coder/scripts",
                      ".super-coder/missing-upstream.txt"]  # absent entries are skipped
        self._orig = (engine_manifest.REPO_ROOT, engine_manifest.ENGINE,
                      engine_manifest.MANIFEST)
        engine_manifest.REPO_ROOT = root
        engine_manifest.ENGINE = root / ".super-coder"
        engine_manifest.MANIFEST = root / ".super-coder" / "engine.manifest"

    def tearDown(self):
        (engine_manifest.REPO_ROOT, engine_manifest.ENGINE,
         engine_manifest.MANIFEST) = self._orig
        self.tmp.cleanup()

    def test_no_manifest_reports_clean(self):
        self.assertEqual(engine_manifest.local_edits(), {})

    def test_clean_tree_after_write(self):
        n = engine_manifest.write_manifest(self.paths)
        self.assertEqual(n, 4)  # sc, schema.sql, a.py, b.py — pyc excluded
        self.assertEqual(engine_manifest.local_edits(), {})

    def test_modified_and_deleted_are_detected(self):
        engine_manifest.write_manifest(self.paths)
        (self.root / ".super-coder" / "scripts" / "a.py").write_text("A = 999\n")
        (self.root / ".super-coder" / "scripts" / "b.py").unlink()
        self.assertEqual(engine_manifest.local_edits(),
                         {".super-coder/scripts/a.py": "modified",
                          ".super-coder/scripts/b.py": "deleted"})

    def test_locally_added_file_is_not_flagged(self):
        engine_manifest.write_manifest(self.paths)
        (self.root / ".super-coder" / "scripts" / "local_extra.py").write_text("X\n")
        self.assertEqual(engine_manifest.local_edits(), {},
                         "materialize never touches locally-added files — they "
                         "must not block an update")

    def test_rewrite_rebaselines(self):
        engine_manifest.write_manifest(self.paths)
        (self.root / ".super-coder" / "scripts" / "a.py").write_text("A = 999\n")
        engine_manifest.write_manifest(self.paths)  # e.g. after the next materialize
        self.assertEqual(engine_manifest.local_edits(), {})

    def test_corrupt_manifest_reports_clean(self):
        engine_manifest.MANIFEST.write_text("{not json")
        self.assertEqual(engine_manifest.local_edits(), {},
                         "a corrupt manifest must not brick update — treat as "
                         "absent; the next write heals it")


if __name__ == "__main__":
    unittest.main()
