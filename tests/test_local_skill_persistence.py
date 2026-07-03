#!/usr/bin/env python3
"""Tests for Patch C — fork-local skill persistence (#253, #237 pt2).

The load-bearing property: the engine/local boundary is the SEED's names
(migrations/0001), not asset-file presence. A fork-authored skill keeps its
SKILL.md under assets/skills/ as authoring source and must still

  • be upserted into the live DB by `sc seed-skills` (grants resolve right
    after seeding — the #253 silent-no-op),
  • serialize into .sc-state/content.sql (snapshot classifies it local),
  • never be "healed" over by the boot/render engine-skill heal,
  • never enter the engine hash manifest (ls-tree-scoped write_manifest).

Uses a synthetic two-skill world (eng_a seeded, loc_b asset-only) by pointing
seed_skills' module globals at a tmp tree — the real assets/ and migrations/
are never touched. Stdlib `unittest`, matching the engine's style.

Run:
    python3 tests/test_local_skill_persistence.py
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import engine_manifest  # noqa: E402
import seed_skills  # noqa: E402
import skill as skill_mod  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402

SKILLS_DDL = (
    "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
    "description TEXT, category TEXT, content TEXT, command TEXT, "
    "common INTEGER NOT NULL DEFAULT 1, is_deleted INTEGER NOT NULL DEFAULT 0)")
SHELLS_DDL = (
    "CREATE TABLE shells (shell_id INTEGER PRIMARY KEY, shortname TEXT, "
    "display_name TEXT, is_deleted INTEGER DEFAULT 0)")
GRANTS_DDL = (
    "CREATE TABLE shell_skills (shell_skill_id INTEGER PRIMARY KEY, "
    "shell_id INTEGER NOT NULL, skill_id INTEGER NOT NULL, UNIQUE(shell_id, skill_id))")


def write_asset(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\ncommon: false\n---\n{body}\n")


class LocalSkillWorld(unittest.TestCase):
    """Synthetic world: assets = {eng_a, loc_b}; seed (0001) = {eng_a} only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skills_dir = self.tmp / "assets" / "skills"
        write_asset(self.skills_dir, "eng_a", "engine body v2")
        write_asset(self.skills_dir, "loc_b", "local body v1")
        self.out = self.tmp / "migrations" / "0001_seed_skills.sql"
        self.out.parent.mkdir(parents=True)
        self.out.write_text(
            "BEGIN;\nINSERT INTO skills (name, description, category, command, "
            "common, content, is_deleted) VALUES ('eng_a', 'eng_a desc', NULL, "
            "NULL, 0, 'engine body v2', 0)\nON CONFLICT(name) DO UPDATE SET\n"
            "  description=excluded.description, category=excluded.category,\n"
            "  command=excluded.command, common=excluded.common,\n"
            "  content=excluded.content, is_deleted=0;\nCOMMIT;\n")
        self._saved = (seed_skills.SKILLS_DIR, seed_skills.OUT)
        seed_skills.SKILLS_DIR = self.skills_dir
        seed_skills.OUT = self.out
        self.con = sqlite3.connect(":memory:")
        self.con.execute(SKILLS_DDL)
        # Live DB state: eng_a lags its asset (v1 on disk-of-DB, v2 in asset);
        # loc_b row exists with a body that has drifted from its asset.
        self.con.execute(
            "INSERT INTO skills (name, description, common, content) "
            "VALUES ('eng_a', 'eng_a desc', 0, 'engine body v1')")
        self.con.execute(
            "INSERT INTO skills (name, description, common, content) "
            "VALUES ('loc_b', 'loc_b desc', 0, 'local body EDITED IN DB')")
        self.con.commit()

    def tearDown(self):
        seed_skills.SKILLS_DIR, seed_skills.OUT = self._saved
        self.con.close()

    def test_seed_names_come_from_the_seed_not_assets(self):
        self.assertEqual(seed_skills.seeded_skill_names(), ["eng_a"])

    def test_seed_names_fall_back_to_assets_without_a_seed(self):
        seed_skills.OUT = self.tmp / "missing.sql"
        self.assertEqual(seed_skills.seeded_skill_names(), ["eng_a", "loc_b"])

    def test_heal_flags_engine_skill_but_never_local_asset(self):
        # eng_a lags its asset → stale; loc_b's DB row drifted from its asset
        # but the heal must not see it (its DB row is canonical once seeded).
        self.assertEqual(seed_skills.stale_engine_skills(self.con), ["eng_a"])
        healed = seed_skills.sync_engine_skills(self.con)
        self.assertEqual(healed, ["eng_a"])
        rows = dict(self.con.execute("SELECT name, content FROM skills"))
        self.assertEqual(rows["eng_a"], "engine body v2")       # healed
        self.assertEqual(rows["loc_b"], "local body EDITED IN DB")  # untouched

    def test_explicit_seed_upserts_local_assets_too(self):
        # `sc seed-skills` passes ALL asset specs — the #253 fix: a freshly
        # authored local skill lands in the live DB, grantable immediately.
        self.con.execute("DELETE FROM skills WHERE name='loc_b'")
        synced = seed_skills.sync_engine_skills(
            self.con, specs=seed_skills.engine_skill_specs())
        self.assertIn("loc_b", synced)
        row = self.con.execute(
            "SELECT content FROM skills WHERE name='loc_b'").fetchone()
        self.assertEqual(row[0], "local body v1")

    def test_snapshot_classifies_local_by_seed_despite_lingering_asset(self):
        # The #253 defect: loc_b has an asset file, but it is NOT the engine's —
        # snapshot must still serialize it into content.sql.
        lines = "\n".join(snapshot_mod.dump_local_skills(self.con))
        self.assertIn("'loc_b'", lines)
        self.assertNotIn("'engine body", lines)  # eng_a stays with the seed
        # and the DELETE guard keeps seed-owned names, nothing else.
        self.assertIn("DELETE FROM skills WHERE name NOT IN ('eng_a');", lines)


class SkillCommandTest(unittest.TestCase):
    """`./sc skill` — loud grants/revokes/rm against a throwaway DB."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        con = sqlite3.connect(self.db)
        con.executescript(SKILLS_DDL + ";" + SHELLS_DDL + ";" + GRANTS_DDL + ";")
        con.execute("INSERT INTO shells (shell_id, shortname) VALUES (1, 'dev1')")
        con.execute("INSERT INTO skills (name, common) VALUES ('eng_a', 1)")
        con.execute("INSERT INTO skills (name, common) VALUES ('loc_b', 0)")
        con.commit()
        con.close()
        self._saved_db = skill_mod.DB_PATH
        skill_mod.DB_PATH = self.db
        # Pin the engine/local line for rm: eng_a is seed-owned.
        self._saved_names = seed_skills.seeded_skill_names
        seed_skills.seeded_skill_names = lambda: ["eng_a"]

    def tearDown(self):
        skill_mod.DB_PATH = self._saved_db
        seed_skills.seeded_skill_names = self._saved_names

    def grants(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT ss.shell_id, s.name FROM shell_skills ss "
                "JOIN skills s USING (skill_id)").fetchall()
        finally:
            con.close()

    def test_grant_revoke_roundtrip_by_shortname_and_id(self):
        self.assertEqual(skill_mod.main(["grant", "loc_b", "dev1"]), 0)
        self.assertEqual(self.grants(), [(1, "loc_b")])
        self.assertEqual(skill_mod.main(["revoke", "loc_b", "1"]), 0)
        self.assertEqual(self.grants(), [])

    def test_unknown_skill_or_shell_is_a_hard_error(self):
        with self.assertRaises(SystemExit):
            skill_mod.main(["grant", "nope", "dev1"])   # the silent-no-op class
        with self.assertRaises(SystemExit):
            skill_mod.main(["grant", "loc_b", "ghost"])
        self.assertEqual(self.grants(), [])

    def test_rm_refuses_engine_and_retires_local(self):
        with self.assertRaises(SystemExit):
            skill_mod.main(["rm", "eng_a"])
        skill_mod.main(["grant", "loc_b", "dev1"])
        self.assertEqual(skill_mod.main(["rm", "loc_b"]), 0)
        con = sqlite3.connect(self.db)
        deleted = con.execute(
            "SELECT is_deleted FROM skills WHERE name='loc_b'").fetchone()[0]
        con.close()
        self.assertEqual(deleted, 1)
        self.assertEqual(self.grants(), [])


class ManifestScopeTest(unittest.TestCase):
    """write_manifest(files=…) covers exactly the given upstream list — a
    locally-added file under an engine dir never enters the manifest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "assets").mkdir()
        (self.tmp / "assets" / "upstream.md").write_text("upstream\n")
        (self.tmp / "assets" / "local_addition.md").write_text("mine\n")
        self._saved = (engine_manifest.REPO_ROOT, engine_manifest.MANIFEST)
        engine_manifest.REPO_ROOT = self.tmp
        engine_manifest.MANIFEST = self.tmp / "engine.manifest"

    def tearDown(self):
        engine_manifest.REPO_ROOT, engine_manifest.MANIFEST = self._saved

    def test_files_list_scopes_the_manifest(self):
        n = engine_manifest.write_manifest(["assets"], files=["assets/upstream.md"])
        self.assertEqual(n, 1)
        recorded = engine_manifest.MANIFEST.read_text()
        self.assertIn("assets/upstream.md", recorded)
        self.assertNotIn("local_addition", recorded)
        # …so editing/removing the local file can never block an update.
        (self.tmp / "assets" / "local_addition.md").unlink()
        self.assertEqual(engine_manifest.local_edits(), {})

    def test_disk_walk_remains_the_install_default(self):
        n = engine_manifest.write_manifest(["assets"])
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
