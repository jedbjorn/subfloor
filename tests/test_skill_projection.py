"""Exact, bounded skill projection reconciliation regression coverage."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills  # noqa: E402
import skill_projection  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from skill_convergence_fixtures import (  # noqa: E402
    LOCAL_SKILL_NAME,
    TOMBSTONE_SKILLS,
    build_dirty_skill_fork,
)


def build_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def add_shell(con: sqlite3.Connection, shortname: str, flavor: str | None) -> int:
    return con.execute(
        "INSERT INTO shells (display_name, shortname, system_prompt, flavor) "
        "VALUES (?, ?, 'prompt', ?)",
        (shortname, shortname.upper(), flavor),
    ).lastrowid


def grant(con: sqlite3.Connection, shell_id: int, skill: str) -> None:
    skill_id = con.execute(
        "SELECT skill_id FROM skills WHERE name=?", (skill,)
    ).fetchone()[0]
    con.execute(
        "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
        (shell_id, skill_id),
    )


class SkillProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()

    def tearDown(self) -> None:
        self.con.close()

    def test_managed_roots_match_adapter_inventory(self) -> None:
        self.assertEqual(
            skill_projection.managed_skill_dirs(),
            (
                Path(".claude/skills"),
                Path(".agents/skills"),
                Path(".opencode/skills"),
            ),
        )

    def test_exact_render_prunes_stale_and_does_not_create_unused_native_root(
        self,
    ) -> None:
        shell_id = add_shell(self.con, "custom", None)
        grant(self.con, shell_id, "query_authoring_pg")
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            stale = checkout / ".opencode/skills/stale/SKILL.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("rendered_by: super-coder\nold\n")
            foreign = checkout / ".opencode/skills/operator_tool/notes.txt"
            foreign.parent.mkdir()
            foreign.write_text("operator-owned\n")

            summary = skill_projection.reconcile_shell(
                self.con,
                shell_id,
                checkout,
                ensure_dirs=(".claude/skills", ".opencode/skills"),
            )

            for relative in (".claude/skills", ".opencode/skills"):
                rendered = checkout / relative / "query_authoring_pg/SKILL.md"
                self.assertTrue(rendered.is_file())
                self.assertIn("name: query_authoring_pg", rendered.read_text())
            self.assertFalse((checkout / ".opencode/skills/stale").exists())
            self.assertIn(checkout / ".opencode/skills/stale", summary["deleted"])
            self.assertNotIn(checkout / ".opencode/skills/stale", summary["written"])
            self.assertEqual(foreign.read_text(), "operator-owned\n")
            self.assertNotIn(foreign.parent, summary["deleted"])
            self.assertFalse(checkout.joinpath(".agents/skills").exists())

    def test_revocation_deletes_exact_grant_from_every_existing_managed_root(
        self,
    ) -> None:
        shell_id = add_shell(self.con, "custom", None)
        grant(self.con, shell_id, "query_authoring_pg")
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            skill_projection.reconcile_shell(
                self.con,
                shell_id,
                checkout,
                ensure_dirs=(".claude/skills", ".agents/skills"),
            )
            self.con.execute("DELETE FROM shell_skills WHERE shell_id=?", (shell_id,))

            summary = skill_projection.reconcile_shell(self.con, shell_id, checkout)

            for relative in (".claude/skills", ".agents/skills"):
                removed = checkout / relative / "query_authoring_pg"
                self.assertFalse(removed.exists())
                self.assertIn(removed, summary["deleted"])
            self.assertFalse(checkout.joinpath(".opencode/skills").exists())

    def test_symlink_root_is_refused_without_touching_target(self) -> None:
        shell_id = add_shell(self.con, "custom", None)
        grant(self.con, shell_id, "query_authoring_pg")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            checkout = Path(tmp)
            target = Path(out)
            sentinel = target / "sentinel.txt"
            sentinel.write_text("foreign")
            (checkout / ".claude").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(
                skill_projection.ProjectionError, "escapes checkout"
            ):
                skill_projection.reconcile_shell(
                    self.con, shell_id, checkout, ensure_dirs=(".claude/skills",)
                )

            self.assertEqual(sentinel.read_text(), "foreign")
            self.assertFalse((target / "skills").exists())

    def test_stale_child_symlink_is_unlinked_without_following_target(self) -> None:
        shell_id = add_shell(self.con, "custom", None)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            checkout = Path(tmp)
            root = checkout / ".claude/skills"
            root.mkdir(parents=True)
            target = Path(out)
            sentinel = target / "sentinel.txt"
            sentinel.write_text("foreign")
            link = root / "engine_surgery"
            link.symlink_to(target, target_is_directory=True)

            summary = skill_projection.reconcile_shell(self.con, shell_id, checkout)

            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())
            self.assertEqual(sentinel.read_text(), "foreign")
            self.assertIn(link, summary["deleted"])

    def test_sweep_cleans_existing_worktree_without_creating_dormant_one(self) -> None:
        dev1 = add_shell(self.con, "dev1", "dev")
        add_shell(self.con, "dev2", "dev")
        skill_id = self.con.execute(
            "SELECT skill_id FROM skills WHERE name='query_authoring_pg'"
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO flavor_skills (flavor, skill_id) VALUES ('dev', ?)",
            (skill_id,),
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root_live = repo / ".claude/skills/query_authoring_pg/SKILL.md"
            root_live.parent.mkdir(parents=True)
            root_live.write_text("preserve live unowned projection\n")
            root_retired = repo / ".claude/skills/retired_upstream/SKILL.md"
            root_retired.parent.mkdir()
            root_retired.write_text(
                "---\nrendered_by: super-coder\n---\nremove retired projection\n"
            )
            foreign_control = (
                repo / ".claude/skills/my_custom_tool/operator-control.txt"
            )
            foreign_control.parent.mkdir()
            foreign_control.write_text("operator-owned\n")
            dev1_root = repo / ".sc-worktrees/dev1/.claude/skills"
            dev1_root.mkdir(parents=True)
            stale = dev1_root / "stale/SKILL.md"
            stale.parent.mkdir()
            stale.write_text("rendered_by: super-coder\nold\n")

            summary = skill_projection.reconcile_existing_checkouts(
                self.con, repo_root=repo
            )

            rendered = dev1_root / "query_authoring_pg/SKILL.md"
            self.assertTrue(rendered.is_file())
            self.assertFalse(stale.parent.exists())
            self.assertEqual(
                root_live.read_text(), "preserve live unowned projection\n"
            )
            self.assertFalse(root_retired.parent.exists())
            self.assertIn(root_retired.parent, summary["deleted"])
            self.assertNotIn(root_retired.parent, summary["written"])
            self.assertEqual(foreign_control.read_text(), "operator-owned\n")
            self.assertNotIn(foreign_control.parent, summary["deleted"])
            self.assertFalse((repo / ".sc-worktrees/dev2").exists())
            self.assertEqual(summary["checkouts"], [repo, repo / ".sc-worktrees/dev1"])
            self.assertEqual(
                self.con.execute(
                    "SELECT COUNT(*) FROM resolved_shell_skills WHERE shell_id=? "
                    "AND skill_id=?",
                    (dev1, skill_id),
                ).fetchone()[0],
                1,
            )

    def test_admin_owned_repo_root_preserves_foreign_directory(self) -> None:
        add_shell(self.con, "admin1", "admin")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            foreign = repo / ".claude/skills/operator_tool/notes.txt"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("operator-owned\n")
            stale = repo / ".claude/skills/stale/SKILL.md"
            stale.parent.mkdir()
            stale.write_text("rendered_by: super-coder\nold\n")

            summary = skill_projection.reconcile_existing_checkouts(
                self.con, repo_root=repo
            )

            self.assertEqual(foreign.read_text(), "operator-owned\n")
            self.assertNotIn(foreign.parent, summary["deleted"])
            self.assertFalse(stale.parent.exists())
            self.assertIn(stale.parent, summary["deleted"])
            self.assertNotIn(stale.parent, summary["written"])
            self.assertEqual(summary["checkouts"], [repo])

    def test_legacy_cleanup_removes_only_banner_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            legacy = checkout / "skills_sc"
            legacy.mkdir()
            managed = legacy / "retired.md"
            managed.write_text("---\nrendered_by: super-coder\n---\nretired\n")
            foreign = legacy / "notes.md"
            foreign.write_text("operator notes\n")

            deleted = skill_projection.cleanup_legacy_skills_sc(checkout)

            self.assertIn(managed, deleted)
            self.assertFalse(managed.exists())
            self.assertEqual(foreign.read_text(), "operator notes\n")
            self.assertTrue(legacy.is_dir())

            foreign.unlink()
            self.assertEqual(
                skill_projection.cleanup_legacy_skills_sc(checkout), [legacy]
            )
            self.assertFalse(legacy.exists())

    def test_dirty_fork_sweep_removes_retired_authority_and_preserves_local_controls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_dirty_skill_fork(Path(tmp) / "downstream")
            con = sqlite3.connect(fixture.database)
            con.row_factory = sqlite3.Row
            try:
                reconciled = seed_skills.reconcile_tombstoned_skills(con)
                summary = skill_projection.reconcile_existing_checkouts(
                    con, repo_root=fixture.root
                )
            finally:
                con.close()

            self.assertEqual(set(reconciled.changed_names), set(TOMBSTONE_SKILLS))
            self.assertEqual(reconciled.grant_count, len(TOMBSTONE_SKILLS) * 2)
            self.assertEqual(summary["checkouts"], list(fixture.checkouts))
            for skills_root in fixture.native_skill_roots:
                self.assertTrue((skills_root / LOCAL_SKILL_NAME / "SKILL.md").is_file())
                for retired in TOMBSTONE_SKILLS:
                    self.assertFalse((skills_root / retired).exists())
            for legacy_root, control in zip(
                fixture.legacy_skill_roots, fixture.control_files, strict=True
            ):
                self.assertEqual(control.read_bytes(), fixture.expected_control_file)
                self.assertEqual(list(legacy_root.iterdir()), [control])


if __name__ == "__main__":
    unittest.main()
