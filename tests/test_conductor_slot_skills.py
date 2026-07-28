#!/usr/bin/env python3
"""Conductor Step 7 skill catalogue, grants, and command examples."""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATION = ENGINE / "migrations" / "0120_conductor_slot_skills.sql"
SKILLS = ENGINE / "assets" / "skills"
sys.path.insert(0, str(ENGINE / "render"))
import flat  # noqa: E402

SLOT_KINDS = {
    "dev_sprint": (
        "dev",
        {"ready-for-review", "ask-planner", "merged", "unit-report"},
    ),
    "rev_sprint": (
        "reviewer",
        {"review-clean", "findings", "ask-planner"},
    ),
    "plan_sprint": (
        "planner",
        {"kickoff", "hold", "re-scope", "re-task", "close", "answer"},
    ),
}


def migrate_fresh(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    return con


class InstalledForkMigrationTest(unittest.TestCase):
    def test_legacy_ids_and_grants_become_slot_skill_ids_and_grants(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            """
            CREATE TABLE skills (
                skill_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE flavor_skills (
                flavor TEXT NOT NULL,
                skill_id INTEGER NOT NULL,
                PRIMARY KEY (flavor, skill_id)
            );
            INSERT INTO skills (skill_id,name) VALUES
                (1,'sprint_dev'),
                (2,'sprint_orchestration'),
                (3,'sprint_orchestration_recover'),
                (4,'sprint_orchestration_close'),
                (5,'sprint_review');
            INSERT INTO flavor_skills (flavor,skill_id) VALUES
                ('dev',1),
                ('planner',2),
                ('planner',3),
                ('planner',4),
                ('reviewer',5);
            """
        )

        con.executescript(MIGRATION.read_text())

        active = con.execute(
            "SELECT skill_id,name FROM skills WHERE is_deleted=0 "
            "ORDER BY skill_id"
        ).fetchall()
        self.assertEqual(
            active,
            [(1, "dev_sprint"), (2, "plan_sprint"), (5, "rev_sprint")],
        )
        grants = con.execute(
            "SELECT fs.flavor,s.name FROM flavor_skills fs "
            "JOIN skills s ON s.skill_id=fs.skill_id ORDER BY fs.flavor"
        ).fetchall()
        self.assertEqual(
            grants,
            [
                ("dev", "dev_sprint"),
                ("planner", "plan_sprint"),
                ("reviewer", "rev_sprint"),
            ],
        )


class FreshCatalogueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.con = migrate_fresh(Path(self.temp.name) / "shell.db")
        self.addCleanup(self.con.close)

    def test_fresh_rebuild_has_only_the_new_role_skills_active(self) -> None:
        active = {
            row["name"]
            for row in self.con.execute(
                "SELECT name FROM skills WHERE is_deleted=0")
        }
        self.assertTrue(set(SLOT_KINDS) <= active)
        self.assertTrue({
            "sprint_dev",
            "sprint_review",
            "sprint_orchestration",
            "sprint_orchestration_recover",
            "sprint_orchestration_close",
        }.isdisjoint(active))

    def test_templates_and_live_flavor_grants_match(self) -> None:
        for skill_name, (flavor, _kinds) in SLOT_KINDS.items():
            granted = self.con.execute(
                "SELECT 1 FROM flavor_skills fs "
                "JOIN skills s ON s.skill_id=fs.skill_id "
                "WHERE fs.flavor=? AND s.name=? AND s.is_deleted=0",
                (flavor, skill_name),
            ).fetchone()
            self.assertIsNotNone(granted, f"{flavor} lacks {skill_name}")

    def test_every_example_kind_is_valid_and_the_set_is_complete(self) -> None:
        allowed = {
            (row["issuer_flavor"], row["kind"])
            for row in self.con.execute(
                "SELECT issuer_flavor,kind FROM directive_kinds")
        }
        for skill_name, (flavor, expected) in SLOT_KINDS.items():
            body = (SKILLS / skill_name / "SKILL.md").read_text()
            emitted = set(re.findall(
                r"\bsc directives emit ([a-z-]+)", body))
            self.assertEqual(emitted, expected, skill_name)
            self.assertTrue(
                {(flavor, kind) for kind in emitted} <= allowed,
                skill_name,
            )
            command_count = len(re.findall(
                r"\bsc directives emit [a-z-]+", body))
            self.assertEqual(
                body.count("--target conductor"),
                command_count,
                f"{skill_name} has a directive example not addressed to conductor",
            )

    def test_flat_render_prunes_retired_skill_mirrors(self) -> None:
        root = Path(self.temp.name) / "renders"
        skills_root = root / "skills_sc"
        skills_root.mkdir(parents=True)
        stale = skills_root / "sprint_orchestration.md"
        stale.write_text("retired")

        summary = flat.render_visibility(self.con, root=root)

        self.assertFalse(stale.exists())
        self.assertIn(stale, summary["written"])
        for skill_name in SLOT_KINDS:
            self.assertTrue((skills_root / f"{skill_name}.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
