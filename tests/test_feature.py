#!/usr/bin/env python3
"""Tests for `./sc feature` (scripts/feature.py) — the opt-in front door.

Two layers: the REGISTRY must stay consistent with the assets it points at
(every granted skill exists in assets/skills/ and is common:false — a feature
must never grant a skill the seed doesn't ship, or auto-grant a common one
twice; every flavor has a template), and the GRANT/REVOKE SQL must do what the
registry means (grant to live shells of the named flavors only, revoke without
touching other shells' grants).

Run:
    python3 tests/test_feature.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import feature  # noqa: E402

SKILLS_DIR = ENGINE / "assets" / "skills"
SHELL_TEMPLATES = ENGINE / "templates" / "shells"


def _mini_db() -> sqlite3.Connection:
    """The minimal slice of the schema grant()/revoke() touch."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE shells (shell_id INTEGER PRIMARY KEY, flavor TEXT,
                             is_deleted INTEGER DEFAULT 0);
        CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE,
                             is_deleted INTEGER DEFAULT 0);
        CREATE TABLE shell_skills (shell_id INTEGER, skill_id INTEGER,
                                   PRIMARY KEY (shell_id, skill_id));
    """)
    con.executemany("INSERT INTO shells (shell_id, flavor, is_deleted) VALUES (?,?,?)",
                    [(1, "dev", 0), (2, "dev", 0), (3, "reviewer", 0),
                     (4, "admin", 0), (5, "dev", 1),      # deleted — never granted
                     (6, "planner", 0)])
    con.execute("INSERT INTO skills (skill_id, name) VALUES (10, 'test_authoring_pg')")
    return con


class RegistryIntegrityTest(unittest.TestCase):
    def test_granted_skills_exist_and_are_opt_in(self):
        for name, f in feature.FEATURES.items():
            for skill in f["grants"]:
                md = SKILLS_DIR / skill / "SKILL.md"
                self.assertTrue(md.exists(),
                                f"feature '{name}' grants '{skill}' but "
                                f"assets/skills/{skill}/SKILL.md does not exist")
                self.assertIn("common: false", md.read_text(),
                              f"feature '{name}' grants '{skill}' which is not "
                              f"common:false — a common skill is already auto-granted")

    def test_granted_flavors_have_templates(self):
        for name, f in feature.FEATURES.items():
            for skill, flavors in f["grants"].items():
                for fl in flavors:
                    self.assertTrue((SHELL_TEMPLATES / f"{fl}.json").exists(),
                                    f"feature '{name}' grants {skill} to flavor "
                                    f"'{fl}' which has no shell template")

    def test_registry_shape(self):
        for name, f in feature.FEATURES.items():
            self.assertIn("block", f, name)
            self.assertIn("block_auto", f, name)
            self.assertTrue(f["grants"], f"feature '{name}' grants nothing")
            if not f["block_auto"]:
                self.assertTrue(f.get("link"),
                                f"operator-linked feature '{name}' has no link steps")

    def test_pg_block_matches_pg_init(self):
        # `./sc pg-init` (in the sc dispatcher) and `feature enable pg` write the
        # same instance.json key — if this drifts, launch won't see the sidecar.
        self.assertEqual(feature.FEATURES["pg"]["block"], "pg")
        sc = (ROOT / "sc").read_text()
        self.assertIn("d['pg']={}", sc.replace(" ", ""),
                      "sc pg-init no longer writes the `pg` key feature.py expects")


class GrantRevokeTest(unittest.TestCase):
    def test_grant_targets_live_shells_of_named_flavors(self):
        con = _mini_db()
        n = feature.grant(con, "test_authoring_pg", ["dev", "reviewer"])
        self.assertEqual(n, 3)  # dev(1,2) + reviewer(3); deleted dev(5) skipped
        rows = {r[0] for r in con.execute("SELECT shell_id FROM shell_skills")}
        self.assertEqual(rows, {1, 2, 3})

    def test_grant_is_idempotent(self):
        con = _mini_db()
        feature.grant(con, "test_authoring_pg", ["dev"])
        n = feature.grant(con, "test_authoring_pg", ["dev"])
        self.assertEqual(n, 0)

    def test_grant_unknown_skill_grants_nothing(self):
        con = _mini_db()
        self.assertEqual(feature.grant(con, "no_such_skill", ["dev"]), 0)

    def test_revoke_leaves_other_flavors_grants(self):
        con = _mini_db()
        feature.grant(con, "test_authoring_pg", ["dev", "reviewer"])
        # A manual grant to a planner shell — outside the feature's flavors.
        con.execute("INSERT INTO shell_skills VALUES (6, 10)")
        n = feature.revoke(con, "test_authoring_pg", ["dev", "reviewer"])
        self.assertEqual(n, 3)
        rows = {r[0] for r in con.execute("SELECT shell_id FROM shell_skills")}
        self.assertEqual(rows, {6}, "revoke must not touch grants outside the "
                                    "feature's flavors")


if __name__ == "__main__":
    unittest.main()
