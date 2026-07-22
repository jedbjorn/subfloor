#!/usr/bin/env python3
"""Tests for the engine-skill staleness guard (seed_skills.stale_engine_skills)
and the render-flat refusal it backs (render._guard_fresh).

Stdlib `unittest`, matching the engine's no-dependency style. Builds a throwaway
file DB shaped like the shipped engine (schema.sql + every migration), which
seeds the engine catalogue from 0001 — i.e. the DB starts FRESH, exactly current
with assets/skills/. The tests then perturb it to prove the guard:

  • fresh DB → no stale skills
  • an engine skill whose live body was rolled back → flagged (the real bug:
    a DB built before a `seed-skills` regen carries the old body)
  • a PROJECT-LOCAL skill (name absent from assets/skills/) → never flagged,
    even when its body differs from everything. This is the load-bearing
    property: admin-authored repo-local skills must be safe from a guard that
    only knows the engine catalogue.
  • the self-heal (sync_engine_skills / render._heal_fresh) repairs stale
    engine skills from assets and leaves project-local skills untouched.

Run:
    python3 tests/test_skills_freshness.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills  # noqa: E402
import render as render_mod  # noqa: E402


def build_engine_db(path: Path) -> None:
    """A throwaway file DB shaped like the shipped engine: schema + every
    migration (so 0001 seeds the engine catalogue, current with assets)."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.commit()
    con.close()


class SkillsFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)
        self.con.row_factory = sqlite3.Row
        # A real engine skill name to perturb (first under assets/skills/).
        self.engine_name = seed_skills.engine_skill_specs()[0]["name"]

    def tearDown(self):
        self.con.close()
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.tmp.rmdir()

    def test_fresh_db_has_no_stale_skills(self):
        self.assertEqual(seed_skills.stale_engine_skills(self.con), [])

    def test_rolled_back_engine_skill_is_flagged(self):
        # Simulate a DB built before a seed-skills regen: the body lags assets.
        self.con.execute(
            "UPDATE skills SET content = content || '\n<stale tail>' WHERE name=?",
            (self.engine_name,),
        )
        self.con.commit()
        self.assertIn(self.engine_name, seed_skills.stale_engine_skills(self.con))

    def test_missing_engine_skill_is_flagged(self):
        self.con.execute("DELETE FROM skills WHERE name=?", (self.engine_name,))
        self.con.commit()
        self.assertIn(self.engine_name, seed_skills.stale_engine_skills(self.con))

    def test_project_local_skill_never_flagged(self):
        # A fork-authored skill: name absent from assets/skills/. It has no
        # upstream to lag, so the guard must ignore it entirely — even though
        # its content matches nothing in assets.
        self.con.execute(
            "INSERT INTO skills (name, description, category, command, common, "
            "content, is_deleted) VALUES "
            "('fork_only_skill', 'local', 'fork', NULL, 0, 'bespoke body', 0)"
        )
        self.con.commit()
        stale = seed_skills.stale_engine_skills(self.con)
        self.assertNotIn("fork_only_skill", stale)
        self.assertEqual(stale, [])  # local skill didn't perturb the verdict

    def test_sync_engine_skills_is_noop_when_fresh(self):
        self.assertEqual(seed_skills.sync_engine_skills(self.con), [])
        self.assertFalse(
            self.con.in_transaction,
            "a no-op boot heal must not strand an implicit transaction",
        )

    def test_sync_engine_skills_heals_stale(self):
        self.con.execute(
            "UPDATE skills SET content = 'WRONG' WHERE name=?", (self.engine_name,)
        )
        self.con.commit()
        healed = seed_skills.sync_engine_skills(self.con)
        self.assertIn(self.engine_name, healed)
        # After healing, the DB matches assets again — nothing left stale.
        self.assertEqual(seed_skills.stale_engine_skills(self.con), [])

    def test_render_heal_fresh_repairs_and_proceeds(self):
        self.con.execute(
            "UPDATE skills SET content = 'WRONG' WHERE name=?", (self.engine_name,)
        )
        self.con.commit()
        render_mod._heal_fresh(self.con)          # heals, does not raise
        self.assertEqual(seed_skills.stale_engine_skills(self.con), [])

    def test_heal_never_touches_project_local_skill(self):
        self.con.execute(
            "INSERT INTO skills (name, description, category, command, common, "
            "content, is_deleted) VALUES "
            "('fork_only_skill', 'local', 'fork', NULL, 0, 'bespoke body', 0)"
        )
        # Force an engine skill stale so the heal actually runs.
        self.con.execute(
            "UPDATE skills SET content = 'WRONG' WHERE name=?", (self.engine_name,)
        )
        self.con.commit()
        seed_skills.sync_engine_skills(self.con)
        body = self.con.execute(
            "SELECT content FROM skills WHERE name='fork_only_skill'"
        ).fetchone()[0]
        self.assertEqual(body, "bespoke body")    # local skill survived untouched


if __name__ == "__main__":
    unittest.main()
