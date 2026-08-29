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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import engine_manifest
import seed_skills
import skill as skill_mod
import snapshot as snapshot_mod

SKILLS_DDL = (
    "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
    "description TEXT, category TEXT, content TEXT, command TEXT, "
    "common INTEGER NOT NULL DEFAULT 1, is_deleted INTEGER NOT NULL DEFAULT 0)")
SHELLS_DDL = (
    "CREATE TABLE shells (shell_id INTEGER PRIMARY KEY, shortname TEXT, "
    "display_name TEXT, flavor TEXT, api_key TEXT, is_deleted INTEGER DEFAULT 0)")
GRANTS_DDL = (
    "CREATE TABLE shell_skills (shell_skill_id INTEGER PRIMARY KEY, "
    "shell_id INTEGER NOT NULL, skill_id INTEGER NOT NULL, UNIQUE(shell_id, skill_id));"
    "CREATE TABLE flavor_skills (flavor TEXT NOT NULL, skill_id INTEGER NOT NULL, "
    "UNIQUE(flavor, skill_id));"
    "CREATE VIEW resolved_shell_skills AS "
    "SELECT sh.shell_id, fs.skill_id FROM shells sh "
    "JOIN flavor_skills fs ON fs.flavor=sh.flavor WHERE sh.flavor IS NOT NULL "
    "UNION ALL SELECT ss.shell_id, ss.skill_id FROM shell_skills ss "
    "JOIN shells sh ON sh.shell_id=ss.shell_id WHERE sh.flavor IS NULL")


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


class LocalSkillManagementSeedTest(unittest.TestCase):
    def test_engine_asset_and_generated_seed_match_for_design_skill(self):
        spec = seed_skills.parse_skill(
            ENGINE / "assets" / "skills" / "fork_skill_design" / "SKILL.md"
        )
        con = sqlite3.connect(":memory:")
        try:
            con.execute(SKILLS_DDL)
            con.executescript(
                (ENGINE / "migrations" / "0001_seed_skills.sql").read_text()
            )
            self.assertEqual(
                con.execute(
                    "SELECT description, category, command, common, content "
                    "FROM skills WHERE name='fork_skill_design'"
                ).fetchone(),
                tuple(
                    spec[key]
                    for key in (
                        "description",
                        "category",
                        "command",
                        "common",
                        "content",
                    )
                ),
            )
        finally:
            con.close()


class SkillCommandTest(unittest.TestCase):
    """`./sc skill` — loud grants/revokes/rm against a throwaway DB."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        con = sqlite3.connect(self.db)
        con.executescript(SKILLS_DDL + ";" + SHELLS_DDL + ";" + GRANTS_DDL + ";")
        con.execute(
            "INSERT INTO shells (shell_id, shortname, api_key) "
            "VALUES (1, 'dev1', 'dev-token')"
        )
        con.execute(
            "INSERT INTO shells (shell_id, shortname, flavor, api_key) "
            "VALUES (2, 'PLN1', 'planner', 'planner-token')"
        )
        con.execute("INSERT INTO skills (name, common) VALUES ('eng_a', 1)")
        con.execute("INSERT INTO skills (name, common) VALUES ('loc_b', 0)")
        con.commit()
        con.close()
        self._saved_db = skill_mod.DB_PATH
        skill_mod.DB_PATH = self.db
        # Pin the engine/local line for rm: eng_a is seed-owned.
        self._saved_names = seed_skills.seeded_skill_names
        seed_skills.seeded_skill_names = lambda: ["eng_a"]
        self._saved_token = skill_mod.mem.SC_API_TOKEN
        skill_mod.mem.SC_API_TOKEN = "planner-token"
        self.persist_snapshot = mock.patch.object(
            skill_mod, "_persist_snapshot"
        ).start()
        self.persist_render = mock.patch.object(
            skill_mod, "_persist_render"
        ).start()

    def tearDown(self):
        mock.patch.stopall()
        skill_mod.DB_PATH = self._saved_db
        seed_skills.seeded_skill_names = self._saved_names
        skill_mod.mem.SC_API_TOKEN = self._saved_token

    def grants(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT ss.shell_id, s.name FROM shell_skills ss "
                "JOIN skills s USING (skill_id)").fetchall()
        finally:
            con.close()

    def write_draft(
        self,
        name: str,
        body: str,
        *,
        description: str = "local workflow",
        common: str = "false",
    ) -> Path:
        path = self.tmp / f"{name}.md"
        path.write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "category: substrate\n"
            f"common: {common}\n"
            "---\n\n"
            f"{body}\n"
        )
        return path

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

    def test_put_creates_ungranted_skill_and_update_preserves_grant(self):
        draft = self.write_draft("loc_new", "First procedure")
        self.assertEqual(skill_mod.main(["put", "--file", str(draft)]), 0)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT description, category, command, common, content, "
                    "is_deleted FROM skills WHERE name='loc_new'"
                ).fetchone(),
                ("local workflow", "substrate", None, 0, "First procedure", 0),
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM shell_skills ss JOIN skills s "
                    "USING (skill_id) WHERE s.name='loc_new'"
                ).fetchone()[0],
                0,
            )
        finally:
            con.close()

        self.assertEqual(skill_mod.main(["grant", "loc_new", "dev1"]), 0)
        draft = self.write_draft(
            "loc_new", "Second procedure", description="updated workflow"
        )
        self.assertEqual(skill_mod.main(["put", "--file", str(draft)]), 0)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT description, content FROM skills WHERE name='loc_new'"
                ).fetchone(),
                ("updated workflow", "Second procedure"),
            )
        finally:
            con.close()
        self.assertEqual(self.grants(), [(1, "loc_new")])

    def test_put_refuses_engine_name_and_implicit_common_grant(self):
        with self.assertRaisesRegex(SystemExit, "ENGINE-owned"):
            skill_mod.main(
                ["put", "--file", str(self.write_draft("eng_a", "replacement"))]
            )
        with self.assertRaisesRegex(SystemExit, "must use `common: false`"):
            skill_mod.main(
                [
                    "put",
                    "--file",
                    str(self.write_draft("loc_common", "body", common="true")),
                ]
            )
        self.persist_snapshot.assert_not_called()
        self.persist_render.assert_not_called()

    def test_put_requires_the_launched_planner_identity(self):
        draft = self.write_draft("loc_auth", "procedure")
        skill_mod.mem.SC_API_TOKEN = "dev-token"
        with self.assertRaisesRegex(SystemExit, "`put` is Planner-owned"):
            skill_mod.main(["put", "--file", str(draft)])
        skill_mod.mem.SC_API_TOKEN = "missing-token"
        with self.assertRaisesRegex(SystemExit, "does not resolve"):
            skill_mod.main(["put", "--file", str(draft)])
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM skills WHERE name='loc_auth'"
                ).fetchone()[0],
                0,
            )
        finally:
            con.close()

    def test_put_validates_frontmatter_name_body_and_size_before_write(self):
        bad_name = self.write_draft("Bad-Name", "procedure")
        with self.assertRaisesRegex(SystemExit, "frontmatter `name` must be"):
            skill_mod.main(["put", "--file", str(bad_name)])

        empty_body = self.write_draft("loc_empty", "")
        with self.assertRaisesRegex(SystemExit, "non-empty procedure body"):
            skill_mod.main(["put", "--file", str(empty_body)])

        oversized = self.tmp / "oversized.md"
        oversized.write_bytes(b"x" * (skill_mod.MAX_SKILL_FILE_BYTES + 1))
        with self.assertRaisesRegex(SystemExit, "maximum is"):
            skill_mod.main(["put", "--file", str(oversized)])

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM skills WHERE name='loc_empty'"
                ).fetchone()[0],
                0,
            )
        finally:
            con.close()

    def test_snapshot_failure_reports_db_only_and_retry_converges(self):
        draft = self.write_draft("loc_partial", "procedure")
        self.persist_snapshot.side_effect = OSError("snapshot unwritable")
        with self.assertRaisesRegex(
            SystemExit,
            "put loc_partial committed in the DB, but snapshot persistence failed: "
            "snapshot unwritable.*Flat render and skill projection were not attempted",
        ):
            skill_mod.main(["put", "--file", str(draft)])
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT content, is_deleted FROM skills WHERE name='loc_partial'"
                ).fetchone(),
                ("procedure", 0),
            )
        finally:
            con.close()
        self.persist_render.assert_not_called()

        self.persist_snapshot.side_effect = None
        self.assertEqual(skill_mod.main(["put", "--file", str(draft)]), 0)
        self.persist_render.assert_called_once()

    def test_serialization_lock_failure_reports_every_unattempted_layer(self):
        draft = self.write_draft("loc_lock", "procedure")
        with mock.patch.object(
            skill_mod.artifact_policy,
            "content_write_lock",
            side_effect=OSError("lock unwritable"),
        ), self.assertRaisesRegex(
            SystemExit,
            "serialization lock failed before persistence: lock unwritable.*"
            "Snapshot, flat render, and skill projection were not attempted",
        ):
            skill_mod.main(["put", "--file", str(draft)])
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT content FROM skills WHERE name='loc_lock'"
                ).fetchone()[0],
                "procedure",
            )
        finally:
            con.close()
        self.persist_snapshot.assert_not_called()
        self.persist_render.assert_not_called()

    def test_remove_retry_reconciles_a_committed_soft_delete(self):
        self.assertEqual(
            skill_mod.main(
                ["put", "--file", str(self.write_draft("loc_remove", "procedure"))]
            ),
            0,
        )
        with mock.patch.object(
            skill_mod.skill_projection,
            "reconcile_existing_checkouts",
            side_effect=skill_mod.skill_projection.ProjectionError("blocked root"),
        ), self.assertRaisesRegex(
            SystemExit,
            "DB, snapshot, and flat render, but skill projection failed: blocked root",
        ):
            skill_mod.main(["rm", "loc_remove"])
        self.assertEqual(skill_mod.main(["rm", "loc_remove"]), 0)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT is_deleted FROM skills WHERE name='loc_remove'"
                ).fetchone()[0],
                1,
            )
        finally:
            con.close()


class LocalSkillPersistenceIntegrationTest(unittest.TestCase):
    """Exercise real snapshot + flat-render persistence on a full schema."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp = Path(self.tmp_dir.name)
        self.db = self.tmp / "shell_db.db"
        self.snapshot = self.tmp / "content.sql"
        self.render_root = self.tmp / "renders"
        schema = (ENGINE / "schema.sql").read_text()
        con = sqlite3.connect(self.db)
        con.executescript(schema)
        con.execute("ALTER TABLE shells ADD COLUMN api_key TEXT")
        con.execute("INSERT INTO users (user_id, username) VALUES (1, 'operator')")
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, role, "
            "system_prompt, flavor, user_id, api_key) VALUES "
            "(7, 'Planner', 'PLN1', 'Planner', 'plan', 'planner', 1, 'planner-token')"
        )
        con.execute(
            "INSERT INTO skills (name, description, common, content) "
            "VALUES ('eng_a', 'engine', 0, 'engine body')"
        )
        con.commit()
        con.close()

        self.patches = [
            mock.patch.object(skill_mod, "DB_PATH", self.db),
            mock.patch.object(skill_mod.mem, "SC_API_TOKEN", "planner-token"),
            mock.patch.object(snapshot_mod, "OUT_PATH", self.snapshot),
            mock.patch.object(
                skill_mod.artifact_policy, "prepare_local_state", return_value=[]
            ),
            mock.patch.object(
                skill_mod.artifact_policy,
                "render_root",
                return_value=self.render_root,
            ),
            mock.patch.object(seed_skills, "seeded_skill_names", return_value=["eng_a"]),
            mock.patch.object(
                seed_skills, "tombstoned_skill_names", return_value=["retired_eng"]
            ),
            mock.patch.object(
                skill_mod.skill_projection,
                "reconcile_existing_checkouts",
                return_value={"written": [], "skipped": [], "deleted": []},
            ),
            mock.patch.object(
                skill_mod.skill_projection,
                "reconcile_assignment_targets",
                return_value={"written": [], "skipped": [], "deleted": []},
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_draft(self, body: str) -> Path:
        path = self.tmp / "SKILL.md"
        path.write_text(
            "---\n"
            "name: repo_helper\n"
            "description: Run the fork helper workflow.\n"
            "category: workflow\n"
            "common: false\n"
            "---\n\n"
            f"{body}\n"
        )
        return path

    def rebuilt_row(self) -> tuple:
        rebuilt = sqlite3.connect(":memory:")
        try:
            rebuilt.executescript((ENGINE / "schema.sql").read_text())
            rebuilt.execute("ALTER TABLE shells ADD COLUMN api_key TEXT")
            rebuilt.executescript(self.snapshot.read_text())
            return rebuilt.execute(
                "SELECT s.description, s.category, s.common, s.content, "
                "s.is_deleted, (SELECT COUNT(*) FROM flavor_skills fs "
                "WHERE fs.skill_id=s.skill_id) FROM skills s "
                "WHERE s.name='repo_helper'"
            ).fetchone()
        finally:
            rebuilt.close()

    def test_create_update_grant_rebuild_revoke_and_remove_persist(self):
        draft = self.write_draft("First procedure")
        self.assertEqual(skill_mod.main(["put", "--file", str(draft)]), 0)
        self.assertEqual(skill_mod.main(["grant", "repo_helper", "PLN1"]), 0)
        self.assertTrue(self.snapshot.is_file())
        self.assertTrue((self.render_root / "skills_sc" / "repo_helper.md").is_file())

        draft = self.write_draft("Updated procedure")
        self.assertEqual(skill_mod.main(["put", "--file", str(draft)]), 0)
        self.assertEqual(
            self.rebuilt_row(),
            (
                "Run the fork helper workflow.",
                "workflow",
                0,
                "Updated procedure",
                0,
                1,
            ),
        )

        self.assertEqual(skill_mod.main(["revoke", "repo_helper", "PLN1"]), 0)
        self.assertEqual(skill_mod.main(["rm", "repo_helper"]), 0)
        self.assertEqual(
            self.rebuilt_row(),
            (
                "Run the fork helper workflow.",
                "workflow",
                0,
                "Updated procedure",
                1,
                0,
            ),
        )
        self.assertFalse(
            (self.render_root / "skills_sc" / "repo_helper.md").exists()
        )


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
