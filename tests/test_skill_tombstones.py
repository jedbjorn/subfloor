"""Regression tests for the upstream skill tombstone ownership boundary."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
seed_skills = importlib.import_module("seed_skills")


TOMBSTONES = [
    "dev_sprint",
    "plan_sprint",
    "rev_sprint",
    "sprint",
    "sprint_cond",
    "sprint_onboarding",
    "sprint_orchestration",
    "sprint_orchestration_close",
    "sprint_orchestration_recover",
    "sprint_review",
    "engine_surgery",
    "test_authoring_pg",
    "test_authoring_sqlite",
]

DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE skills (
    skill_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    category TEXT,
    content TEXT,
    command TEXT,
    common INTEGER NOT NULL DEFAULT 1,
    is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE shell_skills (
    shell_skill_id INTEGER PRIMARY KEY,
    shell_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL REFERENCES skills(skill_id),
    UNIQUE(shell_id, skill_id)
);
CREATE TABLE flavor_skills (
    flavor TEXT NOT NULL,
    skill_id INTEGER NOT NULL REFERENCES skills(skill_id),
    UNIQUE(flavor, skill_id)
);
"""


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.saved = seed_skills.TOMBSTONES_FILE
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "skill_tombstones.json"

    def tearDown(self):
        seed_skills.TOMBSTONES_FILE = self.saved
        self.tmp.cleanup()

    def write_registry(self, value) -> None:
        self.registry.write_text(json.dumps(value))
        seed_skills.TOMBSTONES_FILE = self.registry

    def test_tracked_registry_has_the_exact_reserved_namespace(self):
        self.assertEqual(seed_skills.tombstoned_skill_names(), TOMBSTONES)

    def test_registry_rejects_non_array_non_string_blank_malformed_and_duplicate(self):
        bad_values = [
            [],
            {"sprint": True},
            ["sprint", 7],
            ["sprint", ""],
            ["sprint", "Bad-Name"],
            ["sprint", "sprint"],
        ]
        for value in bad_values:
            with self.subTest(value=value):
                self.write_registry(value)
                with self.assertRaises((TypeError, ValueError)):
                    seed_skills.tombstoned_skill_names()

    def test_namespace_validation_rejects_active_overlap(self):
        self.write_registry(["retired_name"])
        with self.assertRaisesRegex(ValueError, "retired_name"):
            seed_skills.validate_upstream_skill_namespace(
                ["active_name", "retired_name"]
            )
        self.assertEqual(
            seed_skills.validate_upstream_skill_namespace(["active_name"]),
            ["retired_name"],
        )

    def test_active_seed_and_assets_exclude_every_tombstone(self):
        seeded = set(seed_skills.seeded_skill_names())
        assets = {spec["name"] for spec in seed_skills.engine_skill_specs()}
        for name in TOMBSTONES:
            with self.subTest(name=name):
                self.assertNotIn(name, seeded)
                self.assertNotIn(name, assets)
        self.assertEqual(
            seed_skills.validate_upstream_skill_namespace(sorted(seeded)),
            TOMBSTONES,
        )


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(DDL)
        for index, name in enumerate(TOMBSTONES, start=1):
            self.con.execute(
                "INSERT INTO skills (skill_id,name,description,content,common) "
                "VALUES (?,?,?,?,?)",
                (index, name, f"retired {index}", f"body {index}", index % 2),
            )
            self.con.execute(
                "INSERT INTO shell_skills (shell_id,skill_id) VALUES (?,?)",
                (100 + index, index),
            )
            self.con.execute(
                "INSERT INTO flavor_skills (flavor,skill_id) VALUES (?,?)",
                (f"flavor-{index}", index),
            )
        self.local_id = len(TOMBSTONES) + 1
        self.local_row = (
            self.local_id,
            "fork_specialized_testing",
            "local description",
            "local category",
            "local body byte-for-byte",
            "local command",
            0,
            1,
        )
        self.con.execute(
            "INSERT INTO skills (skill_id,name,description,category,content,command,"
            "common,is_deleted) VALUES (?,?,?,?,?,?,?,?)",
            self.local_row,
        )
        self.con.execute(
            "INSERT INTO shell_skills (shell_id,skill_id) VALUES (999,?)",
            (self.local_id,),
        )
        self.con.execute(
            "INSERT INTO flavor_skills (flavor,skill_id) VALUES ('local',?)",
            (self.local_id,),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_dirty_database_converges_twice_and_preserves_local_state_exactly(self):
        first = seed_skills.reconcile_tombstoned_skills(self.con)
        self.assertEqual(first.changed_names, tuple(sorted(TOMBSTONES)))
        self.assertEqual(first.grant_count, 26)
        self.assertEqual(
            self.con.execute("SELECT * FROM skills").fetchall(), [self.local_row]
        )
        self.assertEqual(
            self.con.execute("SELECT shell_id,skill_id FROM shell_skills").fetchall(),
            [(999, self.local_id)],
        )
        self.assertEqual(
            self.con.execute("SELECT flavor,skill_id FROM flavor_skills").fetchall(),
            [("local", self.local_id)],
        )

        second = seed_skills.reconcile_tombstoned_skills(self.con)
        self.assertEqual(second.changed_names, ())
        self.assertEqual(second.grant_count, 0)
        self.assertEqual(
            self.con.execute("SELECT * FROM skills").fetchall(), [self.local_row]
        )

    def test_failure_rolls_back_grant_deletes(self):
        self.con.execute(
            "CREATE TRIGGER refuse_skill_delete BEFORE DELETE ON skills "
            "BEGIN SELECT RAISE(ABORT, 'refuse'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refuse"):
            seed_skills.reconcile_tombstoned_skills(self.con)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM skills").fetchone()[0], 14
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM shell_skills").fetchone()[0], 14
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM flavor_skills").fetchone()[0], 14
        )


class ReservedAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (seed_skills.TOMBSTONES_FILE, seed_skills.OUT)
        tombstones = root / "skill_tombstones.json"
        tombstones.write_text('["retired_name"]\n')
        seed = root / "0001_seed_skills.sql"
        seed.write_text(
            "INSERT INTO skills (name) VALUES ('active_name') "
            "ON CONFLICT(name) DO NOTHING;\n"
        )
        seed_skills.TOMBSTONES_FILE = tombstones
        seed_skills.OUT = seed
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "description TEXT, category TEXT, content TEXT, command TEXT, "
            "common INTEGER DEFAULT 1, is_deleted INTEGER DEFAULT 0)"
        )
        self.con.execute(
            "CREATE TABLE shells (shell_id INTEGER PRIMARY KEY, shortname TEXT, "
            "display_name TEXT, flavor TEXT, api_key TEXT, is_deleted INTEGER DEFAULT 0)"
        )
        self.con.execute("INSERT INTO shells (shell_id,shortname) VALUES (1,'DEV1')")

    def tearDown(self):
        seed_skills.TOMBSTONES_FILE, seed_skills.OUT = self.saved
        self.con.close()
        self.tmp.cleanup()

    def test_explicit_seed_refuses_new_fork_asset_claim_before_writing(self):
        spec = {
            "name": "retired_name",
            "description": "must not land",
            "category": None,
            "command": None,
            "common": 0,
            "content": "must not land",
        }
        with self.assertRaisesRegex(ValueError, "retired_name"):
            seed_skills.sync_engine_skills(self.con, specs=[spec])
        self.assertEqual(self.con.execute("SELECT * FROM skills").fetchall(), [])

    def test_seed_regeneration_validates_before_replacing_0001(self):
        skills_dir = Path(self.tmp.name) / "assets" / "skills"
        asset = skills_dir / "retired_name" / "SKILL.md"
        asset.parent.mkdir(parents=True)
        asset.write_text(
            "---\nname: retired_name\ndescription: must not seed\n---\nbody\n"
        )
        before = seed_skills.OUT.read_text()
        saved_skills_dir = seed_skills.SKILLS_DIR
        saved_fork_mode = seed_skills._fork_mode
        seed_skills.SKILLS_DIR = skills_dir
        seed_skills._fork_mode = lambda: False
        try:
            with self.assertRaisesRegex(ValueError, "retired_name"):
                seed_skills.main()
        finally:
            seed_skills.SKILLS_DIR = saved_skills_dir
            seed_skills._fork_mode = saved_fork_mode
        self.assertEqual(seed_skills.OUT.read_text(), before)

    def test_legacy_seed_membership_does_not_tolerate_reserved_asset(self):
        seed_skills.TOMBSTONES_FILE.write_text('["engine_surgery"]\n')
        seed_skills.OUT.write_text(
            "INSERT INTO skills (name) VALUES ('engine_surgery') "
            "ON CONFLICT(name) DO NOTHING;\n"
        )
        spec = {
            "name": "engine_surgery",
            "description": "legacy upstream asset",
            "category": None,
            "command": None,
            "common": 0,
            "content": "legacy upstream body",
        }
        with self.assertRaisesRegex(ValueError, "engine_surgery"):
            seed_skills.sync_engine_skills(self.con, specs=[spec])
        self.assertEqual(self.con.execute("SELECT * FROM skills").fetchall(), [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
