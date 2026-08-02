#!/usr/bin/env python3
"""Tests for `./sc feature` (scripts/feature.py) — the opt-in front door.

Two layers: the REGISTRY must stay consistent with the assets it points at
(every granted skill exists in assets/skills/ and is common:false — a feature
must never grant a skill the seed doesn't ship, or auto-grant a common one
twice; every flavor has a template), and the GRANT/REVOKE SQL must do what the
registry means (grant each named flavor pack once and leave other packs alone).

Run:
    python3 tests/test_feature.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

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
        CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER,
                                    PRIMARY KEY (flavor, skill_id));
    """)
    con.executemany("INSERT INTO shells (shell_id, flavor, is_deleted) VALUES (?,?,?)",
                    [(1, "dev", 0), (2, "dev", 0), (3, "reviewer", 0),
                     (4, "admin", 0), (5, "dev", 1),      # deleted — never granted
                     (6, "planner", 0)])
    con.execute("INSERT INTO skills (skill_id, name) VALUES (10, 'query_authoring_pg')")
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

    def test_blockless_features_are_procedure_only(self):
        # block: None = no infrastructure half — enable/disable must never
        # touch instance.json for these, so block_auto cannot be True and the
        # link steps are the whole how-to.
        for name, f in feature.FEATURES.items():
            if f["block"] is None:
                self.assertFalse(f["block_auto"],
                                 f"block-less feature '{name}' cannot auto-create a block")
                self.assertTrue(f.get("link"),
                                f"block-less feature '{name}' has no link steps")

    def test_pg_block_matches_pg_init(self):
        # `./sc pg-init` (in the sc dispatcher) and `feature enable pg` write the
        # same instance.json key — if this drifts, launch won't see the sidecar.
        self.assertEqual(feature.FEATURES["pg"]["block"], "pg")
        sc = (ROOT / "sc").read_text()
        self.assertIn("d['pg']={}", sc.replace(" ", ""),
                      "sc pg-init no longer writes the `pg` key feature.py expects")

    def test_pg_grants_only_diagnostic_sql(self):
        self.assertEqual(
            feature.FEATURES["pg"]["grants"],
            {"query_authoring_pg": ["dev", "reviewer", "planner"]},
        )


class GrantRevokeTest(unittest.TestCase):
    def test_grant_targets_each_named_flavor_once(self):
        con = _mini_db()
        n = feature.grant(con, "query_authoring_pg", ["dev", "reviewer"])
        self.assertEqual(n, 2)
        rows = {r[0] for r in con.execute("SELECT flavor FROM flavor_skills")}
        self.assertEqual(rows, {"dev", "reviewer"})

    def test_grant_is_idempotent(self):
        con = _mini_db()
        feature.grant(con, "query_authoring_pg", ["dev"])
        n = feature.grant(con, "query_authoring_pg", ["dev"])
        self.assertEqual(n, 0)

    def test_grant_unknown_skill_grants_nothing(self):
        con = _mini_db()
        self.assertEqual(feature.grant(con, "no_such_skill", ["dev"]), 0)

    def test_revoke_leaves_other_flavors_grants(self):
        con = _mini_db()
        feature.grant(con, "query_authoring_pg", ["dev", "reviewer"])
        # A manual planner-pack grant — outside the feature's flavors.
        con.execute("INSERT INTO flavor_skills VALUES ('planner', 10)")
        n = feature.revoke(con, "query_authoring_pg", ["dev", "reviewer"])
        self.assertEqual(n, 2)
        rows = {r[0] for r in con.execute("SELECT flavor FROM flavor_skills")}
        self.assertEqual(rows, {"planner"}, "revoke must not touch packs outside "
                                           "the feature's flavors")


class ProjectionTriggerTest(unittest.TestCase):
    def _run(self, command) -> list[tuple[sqlite3.Connection, list[str]]]:
        con = _mini_db()
        calls: list[tuple[sqlite3.Connection, list[str]]] = []

        def capture(target_con, flavors) -> dict:
            calls.append((target_con, list(flavors)))
            return {"written": [], "skipped": [], "deleted": [], "checkouts": []}

        with (
            mock.patch.object(feature, "DB_PATH", ROOT / "sc"),
            mock.patch.object(feature.db_driver, "connect", return_value=con),
            mock.patch.object(feature.skill_projection, "reconcile_flavors",
                              side_effect=capture),
            mock.patch.object(feature, "_instance", return_value={"pg": {}}),
            mock.patch.object(feature, "_write_instance"),
            mock.patch.object(feature, "_snapshot"),
        ):
            self.assertEqual(command("pg"), 0)
        return calls

    def test_enable_reconciles_every_granted_flavor(self):
        calls = self._run(feature.cmd_enable)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], ["dev", "reviewer", "planner"])

    def test_disable_reconciles_every_revoked_flavor(self):
        calls = self._run(feature.cmd_disable)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], ["dev", "reviewer", "planner"])


if __name__ == "__main__":
    unittest.main()
