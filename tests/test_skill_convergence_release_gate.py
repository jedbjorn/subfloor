"""Adversarial downstream release gate for feature 33 skill convergence."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "render"))
sys.path.insert(0, str(ROOT / "tests"))

import rebuild  # noqa: E402
import skill_projection  # noqa: E402
import snapshot  # noqa: E402
import update  # noqa: E402
from skill_convergence_fixtures import (  # noqa: E402
    LOCAL_SKILL_NAME,
    TOMBSTONE_SKILLS,
    build_dirty_skill_fork,
)


def skill_state(database: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    with closing(sqlite3.connect(database)) as con:
        skills = con.execute(
            "SELECT name, description, category, content, command, common, "
            "is_deleted FROM skills ORDER BY name"
        ).fetchall()
        shell_grants = con.execute(
            "SELECT ss.shell_id, s.name FROM shell_skills ss "
            "JOIN skills s USING (skill_id) ORDER BY ss.shell_id, s.name"
        ).fetchall()
        flavor_grants = con.execute(
            "SELECT fs.flavor, s.name FROM flavor_skills fs "
            "JOIN skills s USING (skill_id) ORDER BY fs.flavor, s.name"
        ).fetchall()
    return skills, shell_grants, flavor_grants


def projection_state(fixture) -> dict[str, bytes]:
    roots = [
        *fixture.native_skill_roots,
        *fixture.legacy_skill_roots,
        fixture.catalogue_root,
    ]
    return {
        str(path.relative_to(fixture.root)): path.read_bytes()
        for root in roots
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SkillConvergenceReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc-skill-release-gate-")
        self.addCleanup(self.tmp.cleanup)

    def build_fixture(self, name: str):
        return build_dirty_skill_fork(Path(self.tmp.name) / name)

    def assert_converged(self, fixture) -> None:
        placeholders = ",".join("?" for _ in TOMBSTONE_SKILLS)
        with closing(sqlite3.connect(fixture.database)) as con:
            self.assertEqual(
                con.execute(
                    f"SELECT COUNT(*) FROM skills WHERE name IN ({placeholders})",
                    TOMBSTONE_SKILLS,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT description, category, content, command, common, "
                    "is_deleted FROM skills WHERE name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchone(),
                (
                    "Fork-owned dos-arch testing procedure",
                    "fork",
                    fixture.expected_local_content.decode(),
                    None,
                    0,
                    0,
                ),
            )
            self.assertEqual(
                con.execute(
                    "SELECT ss.shell_id FROM shell_skills ss JOIN skills s "
                    "USING (skill_id) WHERE s.name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchall(),
                [(fixture.bespoke_shell_id,)],
            )
            self.assertEqual(
                con.execute(
                    "SELECT fs.flavor FROM flavor_skills fs JOIN skills s "
                    "USING (skill_id) WHERE s.name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchall(),
                [("dev",)],
            )
            rendered_snapshot = "\n".join(
                [
                    *snapshot.dump_local_skills(con),
                    *snapshot.dump_shell_skills(con),
                    *snapshot.dump_flavor_skills(con),
                ]
            )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

        self.assertIn(f"VALUES ('{LOCAL_SKILL_NAME}',", rendered_snapshot)
        self.assertIn(f"WHERE name='{LOCAL_SKILL_NAME}'", rendered_snapshot)
        self.assertEqual(
            fixture.local_asset.read_bytes(), fixture.expected_local_asset
        )
        expected_local_projection = (
            b"---\n"
            b"name: dos_arch_testing\n"
            b"description: Fork-owned dos-arch testing procedure\n"
            b"---\n\n"
            + fixture.expected_local_content
        )
        for skills_root in fixture.native_skill_roots:
            self.assertEqual(
                skills_root.joinpath(LOCAL_SKILL_NAME, "SKILL.md").read_bytes(),
                expected_local_projection,
            )
            for name in TOMBSTONE_SKILLS:
                self.assertFalse(skills_root.joinpath(name).exists())
        for legacy_root, control in zip(
            fixture.legacy_skill_roots, fixture.control_files, strict=True
        ):
            self.assertEqual(control.read_bytes(), fixture.expected_control_file)
            self.assertEqual(list(legacy_root.iterdir()), [control])
        self.assertTrue(
            fixture.catalogue_root.joinpath(f"{LOCAL_SKILL_NAME}.md").is_file()
        )
        for name in TOMBSTONE_SKILLS:
            self.assertNotIn(f"VALUES ('{name}',", rendered_snapshot)
            self.assertNotIn(f"WHERE name='{name}'", rendered_snapshot)
            self.assertFalse(fixture.catalogue_root.joinpath(f"{name}.md").exists())

    @contextmanager
    def patched_update(self, fixture, projection_runs, catalogue_runs):
        real_projection = skill_projection.reconcile_existing_checkouts
        real_catalogue = update.flat.render_visibility

        def reconcile(con):
            summary = real_projection(con, repo_root=fixture.root)
            projection_runs.append(summary)
            return summary

        def render_catalogue(con):
            summary = real_catalogue(con, root=fixture.catalogue_root.parent)
            catalogue_runs.append(summary)
            return summary

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(update, "DB_PATH", fixture.database))
            stack.enter_context(mock.patch.object(update, "REPO_ROOT", fixture.root))
            stack.enter_context(
                mock.patch.object(
                    update, "EJECTED_MARKER", fixture.root / ".sc-state/ejected"
                )
            )
            for name in (
                "run_update_compat",
                "repair_git_worktrees",
                "migrate_engine_untrack",
                "migrate_generated_artifacts_local",
                "refresh_installed_brokers",
                "run_script",
                "snapshot_under_cutover",
            ):
                stack.enter_context(mock.patch.object(update, name))
            stack.enter_context(
                mock.patch.object(
                    update,
                    "migrate_with_service_cutover",
                    side_effect=lambda **kwargs: kwargs["reconcile"](),
                )
            )
            stack.enter_context(mock.patch.object(update, "is_source_repo", return_value=False))
            stack.enter_context(
                mock.patch.object(update, "ensure_workflows", return_value=("unchanged", []))
            )
            stack.enter_context(
                mock.patch.object(update, "expire_sandbox_harnesses", return_value=None)
            )
            stack.enter_context(mock.patch.object(update.install_mod, "ensure_harnesses"))
            stack.enter_context(
                mock.patch.object(update.install_mod, "ensure_gitignore", return_value=False)
            )
            stack.enter_context(
                mock.patch.object(update.install_mod, "wire_make_aliases", return_value=False)
            )
            stack.enter_context(
                mock.patch.object(update.seed_skills, "apply_retired", return_value=[])
            )
            stack.enter_context(
                mock.patch.object(
                    update.skill_projection,
                    "reconcile_existing_checkouts",
                    side_effect=reconcile,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    update.flat, "render_visibility", side_effect=render_catalogue
                )
            )
            yield

    @contextmanager
    def patched_rebuild(self, fixture):
        with mock.patch.multiple(
            rebuild,
            ENGINE=fixture.engine,
            REPO_ROOT=fixture.root,
            DB_PATH=fixture.database,
            SCHEMA_SQLITE=ENGINE / "schema.sql",
            SNAPSHOT=fixture.snapshot,
            SNAPSHOT_LEGACY=fixture.root / "missing-content.sql",
        ), mock.patch.object(
            rebuild.migrate_mod, "MIGRATIONS_DIR", MIGRATIONS
        ), mock.patch.object(
            rebuild.seed_skills, "apply_retired", return_value=[]
        ), mock.patch.object(rebuild.map_repo, "main"):
            yield

    @contextmanager
    def blocked_projection_root(self, fixture):
        root = fixture.root / ".claude/skills"
        parked = fixture.root / ".claude/skills-parked"
        outside = Path(self.tmp.name) / "projection-target"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"outside projection control\n")
        root.rename(parked)
        root.symlink_to(outside, target_is_directory=True)
        try:
            yield parked, sentinel
        finally:
            if root.is_symlink():
                root.unlink()
            if parked.exists():
                parked.rename(root)

    def project_and_render(self, fixture) -> tuple[dict, dict]:
        with closing(sqlite3.connect(fixture.database)) as con:
            con.row_factory = sqlite3.Row
            projected = skill_projection.reconcile_existing_checkouts(
                con, repo_root=fixture.root
            )
            catalogued = update.flat.render_visibility(
                con, root=fixture.catalogue_root.parent
            )
        return projected, catalogued

    def test_update_failure_is_loud_then_two_retries_converge_to_noop(self) -> None:
        fixture = self.build_fixture("update-fork")
        projection_runs = []
        catalogue_runs = []
        with self.patched_update(fixture, projection_runs, catalogue_runs):
            with self.blocked_projection_root(fixture) as (parked, sentinel):
                with self.assertRaisesRegex(
                    SystemExit,
                    "update catalogue reconciliation committed in the DB, but "
                    "skill projection failed: managed skill root is a symlink.*"
                    "run `./sc update --no-fetch`.*retry exact cleanup",
                ):
                    update.main(["--no-fetch"])
                self.assertEqual(sentinel.read_bytes(), b"outside projection control\n")
                self.assertTrue(parked.joinpath(TOMBSTONE_SKILLS[0]).is_dir())
                self.assertEqual(
                    [row for row in skill_state(fixture.database)[0]
                     if row[0] in TOMBSTONE_SKILLS],
                    [],
                )

            self.assertEqual(update.main(["--no-fetch"]), 0)
            first_db = skill_state(fixture.database)
            first_disk = projection_state(fixture)
            self.assert_converged(fixture)

            self.assertEqual(update.main(["--no-fetch"]), 0)

        self.assertEqual(skill_state(fixture.database), first_db)
        self.assertEqual(projection_state(fixture), first_disk)
        self.assertEqual(projection_runs[-1]["written"], [])
        self.assertEqual(projection_runs[-1]["deleted"], [])
        self.assertEqual(catalogue_runs[-1]["written"], [])
        self.assert_converged(fixture)

    def test_rebuild_twice_converges_database_and_every_projection(self) -> None:
        fixture = self.build_fixture("rebuild-fork")
        with self.patched_rebuild(fixture):
            self.assertEqual(rebuild.main(["--no-backup"]), 0)
            first_projection, first_catalogue = self.project_and_render(fixture)
            self.assertNotEqual(first_projection["written"], [])
            self.assertNotEqual(first_catalogue["written"], [])
            first_db = skill_state(fixture.database)
            first_disk = projection_state(fixture)
            self.assert_converged(fixture)

            self.assertEqual(rebuild.main(["--no-backup"]), 0)
            second_projection, second_catalogue = self.project_and_render(fixture)

        self.assertEqual(skill_state(fixture.database), first_db)
        self.assertEqual(projection_state(fixture), first_disk)
        self.assertEqual(second_projection["written"], [])
        self.assertEqual(second_projection["deleted"], [])
        self.assertEqual(second_catalogue["written"], [])
        self.assert_converged(fixture)


if __name__ == "__main__":
    unittest.main(verbosity=2)
