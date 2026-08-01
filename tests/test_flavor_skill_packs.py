"""Flavor-owned skill packs and Bespoke shell regression coverage."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
ROOT_ONLY_MARKER = "<!-- sc-root-only:"

sys.path.insert(0, str(ENGINE / "scripts"))
import shell_factory  # noqa: E402
import snapshot  # noqa: E402
import run  # noqa: E402

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402

sys.path.insert(0, str(ENGINE / "render"))
import compose  # noqa: E402
import flat  # noqa: E402


def build_db(*, through_0105: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if not through_0105 and path.name >= "0105_":
            break
        con.executescript(path.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def add_shell(con: sqlite3.Connection, name: str, flavor: str | None) -> int:
    return con.execute(
        "INSERT INTO shells (display_name, shortname, system_prompt, flavor) "
        "VALUES (?, ?, 'prompt', ?)",
        (name, name.upper(), flavor),
    ).lastrowid


def skill_id(con: sqlite3.Connection, name: str) -> int:
    return con.execute(
        "SELECT skill_id FROM skills WHERE name=?", (name,)
    ).fetchone()[0]


def resolved_names(con: sqlite3.Connection, shell_id: int) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT s.name FROM resolved_shell_skills rs "
            "JOIN skills s ON s.skill_id=rs.skill_id "
            "WHERE rs.shell_id=? AND s.is_deleted=0",
            (shell_id,),
        )
    }


def unexplained_relative_sc(body: str) -> list[tuple[int, str]]:
    """Return worktree-relative launcher references without a root-only note."""
    findings = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if "./sc" not in line:
            continue
        previous = lines[index - 1] if index else ""
        if ROOT_ONLY_MARKER in line or ROOT_ONLY_MARKER in previous:
            continue
        findings.append((index + 1, line.strip()))
    return findings


class HardCutoverMigrationTest(unittest.TestCase):
    def test_seeded_packs_match_common_plus_shipped_template_opt_ins(self) -> None:
        con = build_db()
        common = {
            r[0]
            for r in con.execute(
                "SELECT name FROM skills WHERE common=1 AND is_deleted=0"
            )
        }
        for path in sorted((ENGINE / "templates" / "shells").glob("*.json")):
            template = json.loads(path.read_text())
            actual = {
                r[0]
                for r in con.execute(
                    "SELECT s.name FROM flavor_skills fs "
                    "JOIN skills s ON s.skill_id=fs.skill_id "
                    "WHERE fs.flavor=? AND s.is_deleted=0",
                    (template["flavor"],),
                )
            }
            inherited = (
                common if template.get("inherit_common_skills", True) else set()
            )
            self.assertEqual(
                actual,
                inherited | set(template.get("skills", [])),
                template["flavor"],
            )

    def test_non_admin_skill_packs_use_the_canonical_sc_command(self) -> None:
        con = build_db()
        skills = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT DISTINCT s.name,s.content FROM flavor_skills fs "
                "JOIN skills s ON s.skill_id=fs.skill_id "
                "WHERE fs.flavor <> 'admin' AND s.is_deleted=0"
            )
        }
        findings = {}
        for name, body in sorted(skills.items()):
            found = unexplained_relative_sc(body)
            if found:
                findings[name] = found
        self.assertEqual(
            findings,
            {},
            "non-admin shells boot in worktrees where ./sc may be absent; "
            "use bare sc, or explain an intentional root-only instruction on "
            "the preceding line with '<!-- sc-root-only: reason -->'",
        )

    def test_root_only_marker_applies_to_the_adjacent_instruction_only(self) -> None:
        body = (
            "<!-- sc-root-only: operator runs this from the main checkout -->\n"
            "./sc update\n"
            "./sc mem get roadmap\n"
        )
        self.assertEqual(
            unexplained_relative_sc(body),
            [(3, "./sc mem get roadmap")],
        )

    def test_flavored_rows_are_discarded_bespoke_rows_survive(self) -> None:
        con = build_db(through_0105=False)
        dev1 = add_shell(con, "dev1", "dev")
        dev2 = add_shell(con, "dev2", "dev")
        bespoke = add_shell(con, "custom", None)
        kid = skill_id(con, "query_authoring_pg")
        con.executemany(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            [(dev1, kid), (bespoke, kid)],
        )

        con.executescript(
            (MIGRATIONS / "0105_flavor_skill_packs.sql").read_text()
        )

        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM shell_skills WHERE shell_id IN (?, ?)",
                (dev1, dev2),
            ).fetchone()[0],
            0,
        )
        self.assertNotIn("query_authoring_pg", resolved_names(con, dev1))
        self.assertEqual(resolved_names(con, dev1), resolved_names(con, dev2))
        self.assertIn("query_authoring_pg", resolved_names(con, bespoke))

    def test_stale_flavored_shell_row_is_never_effective(self) -> None:
        con = build_db()
        dev = add_shell(con, "dev", "dev")
        kid = skill_id(con, "query_authoring_pg")
        con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (dev, kid),
        )
        self.assertNotIn("query_authoring_pg", resolved_names(con, dev))


class GrantApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.dev1 = add_shell(self.con, "dev1", "dev")
        self.dev2 = add_shell(self.con, "dev2", "dev")
        self.custom1 = add_shell(self.con, "custom1", None)
        self.custom2 = add_shell(self.con, "custom2", None)
        self.kid = skill_id(self.con, "query_authoring_pg")

    def tearDown(self) -> None:
        self.con.close()

    def test_flavor_write_changes_every_sibling(self) -> None:
        ok, err = server.set_flavor_grant(
            self.con, "dev", self.kid, True
        )
        self.assertTrue(ok, err)
        self.assertIn("query_authoring_pg", resolved_names(self.con, self.dev1))
        self.assertIn("query_authoring_pg", resolved_names(self.con, self.dev2))

        ok, err = server.set_grant(self.con, self.dev1, self.kid, False)
        self.assertFalse(ok)
        self.assertIn("edit the flavor pack", err)
        self.assertIn("query_authoring_pg", resolved_names(self.con, self.dev2))

    def test_bespoke_writes_can_diverge(self) -> None:
        ok, err = server.set_grant(self.con, self.custom1, self.kid, True)
        self.assertTrue(ok, err)
        self.assertIn("query_authoring_pg", resolved_names(self.con, self.custom1))
        self.assertNotIn("query_authoring_pg", resolved_names(self.con, self.custom2))

    def test_unknown_targets_do_not_write(self) -> None:
        self.assertFalse(
            server.set_flavor_grant(
                self.con, "invented", self.kid, True
            )[0]
        )
        self.assertFalse(server.set_grant(self.con, 999999, self.kid, True)[0])
        self.assertFalse(
            server.set_flavor_grant(self.con, "dev", 999999, True)[0]
        )

    def test_catalogue_reports_flavors_and_bespoke_shells_separately(self) -> None:
        server.set_flavor_grant(self.con, "dev", self.kid, True)
        server.set_grant(self.con, self.custom1, self.kid, True)
        row = next(
            s
            for s in server.get_skills(self.con)["skills"]
            if s["skill_id"] == self.kid
        )
        self.assertIn("dev", row["granted_flavors"])
        self.assertEqual(row["granted_shells"], [self.custom1])


class ShellFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.con.execute(
            "INSERT INTO users (user_id, username) VALUES (1, 'operator')"
        )

    def tearDown(self) -> None:
        self.con.close()

    def test_bespoke_shell_is_untemplated_with_its_own_common_baseline(self) -> None:
        sid = shell_factory.create_shell(
            self.con, flavor=None, name="One Off"
        )
        row = self.con.execute(
            "SELECT flavor, role, shortname FROM shells WHERE shell_id=?", (sid,)
        ).fetchone()
        self.assertIsNone(row["flavor"])
        self.assertEqual(row["role"], "Bespoke shell")
        self.assertTrue(row["shortname"].startswith("BSP"))
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?", (sid,)
            ).fetchone()[0],
            self.con.execute(
                "SELECT COUNT(*) FROM skills "
                "WHERE common=1 AND is_deleted=0"
            ).fetchone()[0],
        )

    def test_standard_shell_stores_no_per_shell_grants(self) -> None:
        sid = shell_factory.create_shell(
            self.con, flavor="dev", name="Builder"
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?", (sid,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM resolved_shell_skills WHERE shell_id=?",
                (sid,),
            ).fetchone()[0],
            self.con.execute(
                "SELECT COUNT(*) FROM flavor_skills WHERE flavor='dev'"
            ).fetchone()[0],
        )

    def test_operational_shell_has_no_personal_identity_by_default(self) -> None:
        sid = shell_factory.create_shell(
            self.con, flavor="dev", name="Developer"
        )
        row = self.con.execute(
            "SELECT lineage_seed, has_identity FROM shells WHERE shell_id=?",
            (sid,),
        ).fetchone()
        self.assertIsNone(row["lineage_seed"])
        self.assertEqual(row["has_identity"], 0)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=?",
                (sid,),
            ).fetchone()[0],
            0,
        )

    def test_designated_primary_gets_lineage_and_one_genesis_seed(self) -> None:
        sid = shell_factory.create_shell(
            self.con, flavor="planner", name="Planner", seed_identity=True
        )
        row = self.con.execute(
            "SELECT lineage_seed, has_identity FROM shells WHERE shell_id=?",
            (sid,),
        ).fetchone()
        self.assertEqual(row["lineage_seed"], shell_factory.LINEAGE_SEED)
        self.assertEqual(row["has_identity"], 1)
        seeds = self.con.execute(
            "SELECT source_tag, body FROM shell_identity_entries "
            "WHERE shell_id=? AND kind='seed'",
            (sid,),
        ).fetchall()
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["source_tag"], "fork")
        self.assertIn("planning shell", seeds[0]["body"])


class RenderAndSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.dev = add_shell(self.con, "dev", "dev")
        self.custom = add_shell(self.con, "custom", None)
        self.kid = skill_id(self.con, "query_authoring_pg")

    def tearDown(self) -> None:
        self.con.close()

    def test_boot_and_flat_render_use_resolved_grants(self) -> None:
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.dev, self.kid),
        )
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.custom, self.kid),
        )
        self.assertNotIn("query_authoring_pg", compose.render_skills(
            self.con, self.dev
        ))
        self.assertIn("query_authoring_pg", compose.render_skills(
            self.con, self.custom
        ))
        with tempfile.TemporaryDirectory() as tmp:
            flat.render_skill_md(
                self.con, self.dev, work_dir=Path(tmp) / "dev"
            )
            flat.render_skill_md(
                self.con, self.custom, work_dir=Path(tmp) / "custom"
            )
            self.assertFalse(
                (Path(tmp) / "dev" / ".claude" / "skills"
                 / "query_authoring_pg").exists()
            )
            self.assertTrue(
                (Path(tmp) / "custom" / ".claude" / "skills"
                 / "query_authoring_pg" / "SKILL.md").exists()
            )

    def test_skill_render_supports_a_harness_native_directory(self) -> None:
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.custom, self.kid),
        )
        with tempfile.TemporaryDirectory() as tmp:
            flat.render_skill_md(
                self.con,
                self.custom,
                work_dir=Path(tmp),
                skills_dir=Path(".opencode/skills"),
            )

            self.assertTrue(
                (Path(tmp) / ".opencode" / "skills"
                 / "query_authoring_pg" / "SKILL.md").exists()
            )

    def test_codex_adapter_renders_and_prunes_native_skill_mirror(self) -> None:
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.custom, self.kid),
        )
        adapter = json.loads(
            (ENGINE / "adapters" / "codex" / "adapter.json").read_text()
        )
        self.assertEqual(
            adapter["skill_dirs"],
            [".claude/skills", ".agents/skills"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = run.render_harness_skills(
                self.con, self.custom, root, adapter
            )
            self.assertEqual(
                summary["dirs"], [".claude/skills", ".agents/skills"]
            )
            for skill_root in (".claude", ".agents"):
                rendered = (
                    root / skill_root / "skills" / "query_authoring_pg" / "SKILL.md"
                )
                self.assertTrue(rendered.exists())
                self.assertIn("name: query_authoring_pg", rendered.read_text())

            self.con.execute(
                "DELETE FROM shell_skills WHERE shell_id=? AND skill_id=?",
                (self.custom, self.kid),
            )
            run.render_harness_skills(self.con, self.custom, root, adapter)
            for skill_root in (".claude", ".agents"):
                self.assertFalse(
                    (root / skill_root / "skills" / "query_authoring_pg").exists()
                )

    def test_snapshot_serializes_pack_grants_by_skill_name(self) -> None:
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.dev, self.kid),
        )
        self.con.execute(
            "INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (self.custom, self.kid),
        )
        flavor_dump = "\n".join(snapshot.dump_flavor_skills(self.con))
        shell_dump = "\n".join(snapshot.dump_shell_skills(self.con))
        self.assertIn("DELETE FROM flavor_skills;", flavor_dump)
        self.assertIn("WHERE name='git'", flavor_dump)
        self.assertIn("DELETE FROM shell_skills;", shell_dump)
        self.assertIn(
            f"INSERT INTO shell_skills (shell_id, skill_id) "
            f"SELECT {self.custom}, skill_id FROM skills "
            "WHERE name='query_authoring_pg';",
            shell_dump,
        )
        self.assertNotIn(
            f"SELECT {self.dev}, skill_id FROM skills "
            "WHERE name='query_authoring_pg';",
            shell_dump,
        )


if __name__ == "__main__":
    unittest.main()
