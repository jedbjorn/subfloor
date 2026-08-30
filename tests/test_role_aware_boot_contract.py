"""Role/repo-aware control-plane guidance and prompt convergence."""

from __future__ import annotations

import importlib
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "render"), str(ENGINE / "scripts")]

compose = importlib.import_module("compose")
shell_factory = importlib.import_module("shell_factory")


WORKER_FORBIDDEN = (
    "shell_db.db",
    "schema.sql",
    "engine migrations",
    "snapshot",
    "sc sql",
    "SC_ROOT",
    "SC_ENGINE_DIR",
    "private instance state",
)


class BoundaryRenderingTest(unittest.TestCase):
    def test_fork_worker_routes_each_data_surface_without_engine_internals(self):
        boundary = compose.render_data_boundaries("dev", False, "host")
        rendered = (
            compose.TEMPLATE_PATH.read_text()
            .replace("{{project_vs_engine}}", compose.PROJECT_VS_ENGINE_FORK)
            .replace("{{data_boundaries}}", boundary)
        )

        self.assertIn("## DATA BOUNDARIES", rendered)
        self.assertIn("`sc mem`", boundary)
        self.assertIn("`sc map-schema`", boundary)
        self.assertIn("`sc map-sql`", boundary)
        self.assertIn("app code, migrations", boundary)
        self.assertIn("app database connection", boundary)
        self.assertIn("absent from this shell's engine-state view", boundary)
        for text in WORKER_FORBIDDEN:
            self.assertNotIn(text, rendered)

        self.assertIn("NEVER use the harness's auto-memory system", rendered)
        self.assertIn("Overrides\nharness default by design", rendered)

    def test_source_worker_sees_tracked_source_but_not_live_state(self):
        boundary = compose.render_data_boundaries("dev", True, "container")

        self.assertIn("Tracked engine schema and migrations are project source", boundary)
        self.assertIn("live instance state remains Admin-maintained", boundary)
        self.assertNotIn("shell_db.db", boundary)
        self.assertNotIn("sc sql", boundary)

    def test_admin_gets_exact_private_target_and_maintenance_routing(self):
        boundary = compose.render_data_boundaries(
            "admin",
            True,
            "host",
            database_path="/private/subfloor/instance-1/shell_db.db",
        )

        self.assertIn("## ENGINE MAINTENANCE", boundary)
        self.assertIn(f"`{compose.ENGINE}`", boundary)
        self.assertIn("`/private/subfloor/instance-1`", boundary)
        self.assertIn("`sc sql` is read-only diagnosis", boundary)
        self.assertIn("stopped-runtime", boundary)
        self.assertIn("`engine_database`", boundary)
        self.assertIn("tracked engine schema and migrations", boundary)
        self.assertIn("host Admin boot remains valid", boundary)

    def test_global_pointer_is_path_free_and_repair_is_admin_only(self):
        pointer = (ENGINE / "templates" / "global_pointer.md").read_text()

        self.assertIn("Admin-only repair mode", pointer)
        self.assertNotIn("shell_db.db", pointer)
        self.assertNotIn("schema.sql", pointer)
        self.assertNotIn(".super-coder/", pointer)


class StandardPromptRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE shells ("
            "shell_id INTEGER PRIMARY KEY, display_name TEXT, role TEXT, "
            "mandate TEXT, flavor TEXT, system_prompt TEXT, is_deleted INTEGER DEFAULT 0)"
        )
        self.con.execute(
            "INSERT INTO shells VALUES (1,'Dev One','Custom Dev Role',"
            "'Preserve this mandate','dev','legacy physical prompt',0)"
        )
        self.con.execute(
            "INSERT INTO shells VALUES (2,'Bespoke One','Bespoke Role',"
            "'Bespoke mandate',NULL,'authored bespoke prompt',0)"
        )

    def tearDown(self) -> None:
        self.con.close()

    def test_refresh_updates_standard_only_and_is_idempotent(self):
        self.assertEqual(
            shell_factory.refresh_standard_prompts(self.con, repo="sample-app"),
            1,
        )
        standard = self.con.execute(
            "SELECT role, mandate, system_prompt FROM shells WHERE shell_id=1"
        ).fetchone()
        bespoke = self.con.execute(
            "SELECT system_prompt FROM shells WHERE shell_id=2"
        ).fetchone()[0]

        self.assertEqual(standard[0], "Custom Dev Role")
        self.assertEqual(standard[1], "Preserve this mandate")
        self.assertIn("## CONTROL-PLANE MEMORY", standard[2])
        self.assertIn("Preserve this mandate", standard[2])
        for text in WORKER_FORBIDDEN:
            self.assertNotIn(text, standard[2])
        self.assertEqual(bespoke, "authored bespoke prompt")
        self.assertEqual(
            shell_factory.refresh_standard_prompts(self.con, repo="sample-app"),
            0,
        )


class SkillSplitTest(unittest.TestCase):
    def test_common_guidance_is_api_only_and_admin_skill_owns_internals(self):
        for name in (
            "cartographer",
            "db_map",
            "docs",
            "fork_skill_design",
            "git",
            "memory",
            "messaging",
            "onboard",
            "surface_catalogue",
        ):
            body = (ENGINE / "assets" / "skills" / name / "SKILL.md").read_text()
            with self.subTest(skill=name):
                self.assertNotIn("shell_db.db", body)
                self.assertNotIn("SC_ROOT", body)
                self.assertNotIn("SC_ENGINE_DIR", body)
                self.assertNotIn("sc sql", body)

        admin = (
            ENGINE / "assets" / "skills" / "engine_database" / "SKILL.md"
        ).read_text()
        self.assertIn("common: false", admin)
        self.assertIn("shell_db.db", admin)
        self.assertIn("schema.sql", admin)
        self.assertIn("`sc sql`", admin)

        dispatch = (ENGINE / "scripts" / "dispatch.sh").read_text()
        self.assertNotIn('sc sql "<query>"', dispatch)
        self.assertIn('sc map-sql "<query>"', dispatch)

        dogfood = (ENGINE / "scripts" / "seed_dogfood.py").read_text()
        self.assertNotIn("shell_db.db", dogfood)
        self.assertNotIn("schema.sql", dogfood)
        extractor_help = (
            ENGINE / "templates" / "map_extractors" / "README.md"
        ).read_text()
        self.assertNotIn("SC_ROOT", extractor_help)
        self.assertNotIn("SC_ENGINE_DIR", extractor_help)
        self.assertNotIn("SC_SHELL_WORKTREE", extractor_help)


if __name__ == "__main__":
    unittest.main(verbosity=2)
