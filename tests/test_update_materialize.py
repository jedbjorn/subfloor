#!/usr/bin/env python3
"""Guard: every top-level engine file is materialized to forks on `./sc update`.

The bug this prevents: a new top-level file under `.super-coder/` (e.g.
map_schema.sql, added with the map split) that isn't in update.py's ENGINE_PATHS
allowlist never reaches an updating fork — the fork gets the new code but not the
file, and breaks. Subdirs (scripts/, templates/, …) are materialized whole, so
files inside them are covered; only NEW TOP-LEVEL files are at risk. This asserts
each one is in the allowlist (or is a deliberate per-instance exclusion).

Run:
    python3 tests/test_update_materialize.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402

# Deliberately NOT materialized — per-instance / gitignored / runtime, per the
# ENGINE_PATHS comment in update.py.
PER_INSTANCE = {
    "instance.json", "shell_db.db", "shell_db.db-wal", "shell_db.db-shm", "map.db",
    "engine.manifest",  # derived hash baseline — rewritten by each materialize
}


class EnginePathsCoverageTest(unittest.TestCase):
    def test_every_top_level_engine_file_is_materialized(self):
        listed = set(update.ENGINE_PATHS)
        missing = []
        for entry in sorted(ENGINE.iterdir()):
            if not entry.is_file():
                continue
            if entry.name in PER_INSTANCE or entry.suffix == ".pyc":
                continue
            rel = f".super-coder/{entry.name}"
            if rel not in listed:
                missing.append(rel)
        self.assertEqual(
            missing, [],
            f"top-level engine file(s) absent from update.ENGINE_PATHS — forks "
            f"won't receive them on `./sc update`: {missing}")

    def test_map_schema_specifically_present(self):
        # The file whose omission caused the dos-arch update failure.
        self.assertIn(".super-coder/map_schema.sql", update.ENGINE_PATHS)


if __name__ == "__main__":
    unittest.main()
