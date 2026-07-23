#!/usr/bin/env python3
"""Guard: every tracked engine file is materialized to forks on `./sc update`.

The bugs this prevents: a new top-level file under `.super-coder/` (e.g.
map_schema.sql, added with the map split) or a new tracked SUBDIRECTORY (e.g.
shadow/, added with the Interface runtime) that isn't in update.py's
ENGINE_PATHS allowlist never reaches an updating fork — the fork gets the new
code but not the files, and breaks (shadow/: 'interface_unavailable: shadow
sidecar exited' on every fresh fork; flag #59). Known subdirs (scripts/,
templates/, …) are materialized whole, so files inside them are covered; only
NEW top-level files and NEW subdirs are at risk. The file-level test asserts
every git-tracked engine file is covered by the allowlist (or is a deliberate
per-instance / super-coder-only exclusion); the dir-level one keeps the cheap
top-level signal.

Run:
    python3 tests/test_update_materialize.py
"""
from __future__ import annotations

import subprocess
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

# Tracked upstream, deliberately NOT materialized to forks (file or dir
# prefix): assets/seed/ is super-coder-only (stripped on install) — see the
# ENGINE_PATHS comment in engine_manifest.py.
NOT_MATERIALIZED = (".super-coder/assets/seed/",)


def _covered(rel: str) -> bool:
    """True when an ENGINE_PATHS entry archives `rel` (exact file or dir
    prefix — git archive emits whole trees for directory pathspecs)."""
    return any(rel == entry or rel.startswith(entry.rstrip("/") + "/")
               for entry in update.ENGINE_PATHS)


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

    def test_shadow_specifically_present(self):
        # The dir whose omission broke the Interface on every fresh fork (#59).
        self.assertIn(".super-coder/shadow", update.ENGINE_PATHS)

    def test_every_tracked_engine_file_is_materialized(self):
        """The recurrence guard for the class: any git-tracked file under
        .super-coder/ — including one inside a NEW subdirectory — must be
        covered by ENGINE_PATHS, or explicitly opted out in NOT_MATERIALIZED.
        git ls-files lists exactly the upstream-owned set a fork can receive,
        so a new engine dir can't silently miss materialization."""
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--", ".super-coder"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        missing = [rel for rel in tracked
                   if not _covered(rel)
                   and not any(rel.startswith(prefix) for prefix in NOT_MATERIALIZED)]
        self.assertEqual(
            missing, [],
            f"tracked engine file(s) absent from update.ENGINE_PATHS — forks "
            f"won't receive them on `./sc update` (add the path to ENGINE_PATHS "
            f"or opt out in NOT_MATERIALIZED): {missing}")


if __name__ == "__main__":
    unittest.main()
