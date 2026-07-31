"""Task #166 gates for the Sprint v1 removal manifest and cutover fixture."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sprint_removal"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
DATABASE_FIXTURE = FIXTURE_DIR / "pre_removal.sql"
ENGINE = ROOT / ".super-coder"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def build_pre_removal_database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DATABASE_FIXTURE.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def current_reference_files(manifest: dict) -> set[str]:
    deny = re.compile(manifest["source_reference_pattern"], re.IGNORECASE)
    allowed = set(manifest["allowed_reference_files"])
    hits: set[str] = set()
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *manifest["scan_roots"]],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for relative in tracked:
        if relative in allowed:
            continue
        if deny.search(relative):
            hits.add(relative)
            continue
        path = ROOT / relative
        if not path.exists():
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if deny.search(text):
            hits.add(relative)
    return hits


class SprintRemovalManifestTest(unittest.TestCase):
    def test_manifest_covers_every_current_reference_file(self):
        manifest = load_manifest()
        inventoried = set(manifest["baseline_reference_files"])
        current = current_reference_files(manifest)
        self.assertEqual(
            set(),
            current - inventoried,
            "new Sprint/Conductor/Interface/watch references are not in the "
            "task #166 removal inventory",
        )
        self.assertEqual(
            len(inventoried),
            len(manifest["baseline_reference_files"]),
        )
        self.assertIn(".super-coder/api/sprint_routes.py", inventoried)
        self.assertIn(".super-coder/schema.sql", inventoried)
        self.assertIn("sc", inventoried)
        self.assertIn("README.md", inventoried)

    def test_frozen_fixture_contains_every_removed_table_and_generation(self):
        manifest = load_manifest()
        with closing(build_pre_removal_database()) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(
                set(),
                set(manifest["removed_tables"]) - tables,
                "the pre-removal fixture must freeze every disposable table",
            )
            counts = {
                table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in manifest["removed_tables"]
            }
            self.assertEqual(
                set(),
                {table for table, count in counts.items() if count == 0},
                "every removed table needs dirty data so cleanup cannot pass "
                "against an empty fixture",
            )
            generation_markers = {
                "markdown_board": con.execute(
                    "SELECT title FROM documents WHERE document_id=31"
                ).fetchone()[0],
                "db_board": con.execute(
                    "SELECT unit_title FROM sprint_units WHERE unit_id=310"
                ).fetchone()[0],
                "interface_tmux": con.execute(
                    "SELECT title FROM interface_sessions WHERE session_id=370"
                ).fetchone()[0],
                "conductor_browser": con.execute(
                    "SELECT title FROM conversations "
                    "WHERE conversation_id='cv_sprint_conductor'"
                ).fetchone()[0],
            }
            self.assertEqual(manifest["generation_markers"], generation_markers)
            migration_ledger = [
                row[0]
                for row in con.execute(
                    "SELECT filename FROM schema_migrations ORDER BY filename"
                )
            ]
            expected_ledger = manifest["baseline_migration_ledger"]
            self.assertEqual(expected_ledger["count"], len(migration_ledger))
            self.assertEqual(expected_ledger["first"], migration_ledger[0])
            self.assertEqual(expected_ledger["last"], migration_ledger[-1])

    def test_frozen_fixture_pins_retained_data_and_sprint_negative_space(self):
        with closing(build_pre_removal_database()) as con:
            retained = con.execute(
                "SELECT u.username, p.shortname, r.title, d.title, s.shortname "
                "FROM users u "
                "JOIN projects p ON p.project_id=10 "
                "JOIN roadmap r ON r.feature_id=20 "
                "JOIN documents d ON d.document_id=30 "
                "JOIN shells s ON s.shell_id=10 "
                "WHERE u.user_id=1"
            ).fetchone()
            self.assertEqual(
                tuple(retained),
                (
                    "fixture-operator",
                    "KEEP",
                    "Retained roadmap feature",
                    "Retained normal specification",
                    "DEVX",
                ),
            )
            normal_chat = con.execute(
                "SELECT c.mode, c.owner_user_id, m.body, r.state, e.event_type, "
                "o.state, g.branch_name, g.pr_number "
                "FROM conversations c "
                "JOIN conversation_messages m USING (conversation_id) "
                "JOIN conversation_runs r USING (conversation_id) "
                "JOIN conversation_events e USING (conversation_id) "
                "JOIN conversation_outbox o USING (conversation_id) "
                "JOIN conversation_git_targets g USING (conversation_id) "
                "WHERE c.conversation_id='cv_normal'"
            ).fetchone()
            self.assertEqual(
                tuple(normal_chat),
                (
                    "normal",
                    1,
                    "Retained normal prompt",
                    "succeeded",
                    "assistant.delta",
                    "dispatched",
                    "feat/retained-review",
                    900,
                ),
            )
            messages = con.execute(
                "SELECT body, kind, sprint_doc_id FROM shell_messages "
                "WHERE message_id IN (101,102) ORDER BY message_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in messages],
                [
                    ("Retained generic job result", "result", None),
                    ("Disposable Sprint PR event", "pr_event", 31),
                ],
            )
            route = con.execute(
                "SELECT display_name, availability, headless_supported "
                "FROM model_routes WHERE harness='codex' "
                "AND selector='gpt-fixture'"
            ).fetchone()
            self.assertEqual(
                tuple(route),
                ("Retained model route", "available", 1),
            )
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM sprint_conversation_bindings "
                    "WHERE conversation_id='cv_normal'"
                ).fetchone()
            )
            self.assertEqual(
                con.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(),
                [],
                "fixture relationships must be valid before the cleanup runs",
            )

    def test_task_169_runtime_modules_and_role_assets_are_deleted(self):
        removal = load_manifest()["runtime_removal"]
        removed = (
            removal["modules"]
            + removal["role_assets"]
            + removal["focused_tests"]
        )
        self.assertEqual(
            [],
            [relative for relative in removed if (ROOT / relative).exists()],
        )

    def test_task_169_runtime_modules_cannot_import_from_engine_paths(self):
        removal = load_manifest()["runtime_removal"]
        names = [Path(relative).stem for relative in removal["modules"]]
        probe = r"""
import importlib
import sys
import sysconfig
from pathlib import Path

root = Path(sys.argv[1])
stdlib = Path(sysconfig.get_paths()["stdlib"])
sys.path[:] = [
    str(root / ".super-coder" / "api"),
    str(root / ".super-coder" / "scripts"),
    str(stdlib),
    str(stdlib / "lib-dynload"),
]
for name in sys.argv[2:]:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
    else:
        raise SystemExit(f"removed module still imports: {name}")
"""
        result = subprocess.run(
            [sys.executable, "-c", probe, str(ROOT), *names],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )

    def test_task_169_conductor_flavor_and_sprint_skills_cannot_regenerate(self):
        removal = load_manifest()["runtime_removal"]
        skills = set(removal["skill_names"])
        templates = ENGINE / "templates" / "shells"
        flavors = {
            path.stem: json.loads(path.read_text())
            for path in templates.glob("*.json")
        }
        self.assertNotIn("conductor", flavors)
        for flavor, template in flavors.items():
            with self.subTest(flavor=flavor):
                self.assertEqual(
                    set(),
                    skills & set(template.get("skills", ())),
                )

        seed = (ENGINE / "migrations" / "0001_seed_skills.sql").read_text()
        for skill in skills:
            with self.subTest(skill=skill):
                self.assertNotIn(f"'{skill}'", seed)

        retained_hooks = (
            ENGINE / "scripts" / "install.py",
            ENGINE / "scripts" / "update.py",
            ENGINE / "scripts" / "init_fork.py",
            ENGINE / "scripts" / "shell_factory.py",
            ENGINE / "render" / "compose.py",
        )
        source = "\n".join(path.read_text() for path in retained_hooks)
        for marker in (
            "conductor_runtime",
            "conductor_policy",
            "reconcile_conductor",
            "CON1",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)

    def test_task_169_role_cleanup_converges_on_dirty_installed_state(self):
        removal = load_manifest()["runtime_removal"]
        cleanup = ROOT / removal["role_cleanup_migration"]
        with closing(sqlite3.connect(":memory:")) as con:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                con.executescript(migration.read_text())
            con.execute("PRAGMA foreign_keys=ON")

            skills = tuple(removal["skill_names"])
            placeholders = ",".join("?" for _ in skills)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM skills "
                    f"WHERE name IN ({placeholders}) AND is_deleted=0",
                    skills,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM flavor_defaults "
                    "WHERE flavor='conductor'"
                ).fetchone()[0],
                0,
            )

            con.execute(
                "UPDATE skills SET is_deleted=0 "
                f"WHERE name IN ({placeholders})",
                skills,
            )
            for flavor, skill in (
                ("conductor", "sprint_cond"),
                ("dev", "sprint_dev"),
                ("planner", "sprint_onboarding"),
                ("planner", "sprint_pln"),
                ("reviewer", "sprint_rev"),
            ):
                con.execute(
                    "INSERT INTO flavor_skills (flavor,skill_id) "
                    "SELECT ?,skill_id FROM skills WHERE name=?",
                    (flavor, skill),
                )
            con.execute(
                "INSERT INTO flavor_defaults "
                "(flavor,harness,model,is_default) "
                "VALUES ('conductor','opencode','removed-model',1)"
            )
            conductor_id = con.execute(
                "INSERT INTO shells "
                "(display_name,shortname,system_prompt,flavor) "
                "VALUES ('Removed Conductor','CON1','removed','conductor') "
                "RETURNING shell_id"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO shell_skills (shell_id,skill_id) "
                "SELECT ?,skill_id FROM skills WHERE name='sprint_cond'",
                (conductor_id,),
            )

            con.executescript(cleanup.read_text())
            con.executescript(cleanup.read_text())

            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM skills "
                    f"WHERE name IN ({placeholders}) AND is_deleted=0",
                    skills,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM flavor_skills fs "
                    "JOIN skills s USING(skill_id) "
                    f"WHERE fs.flavor='conductor' "
                    f"OR s.name IN ({placeholders})",
                    skills,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM shell_skills ss "
                    "JOIN skills s USING(skill_id) "
                    f"WHERE s.name IN ({placeholders})",
                    skills,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM flavor_defaults "
                    "WHERE flavor='conductor'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT is_deleted FROM shells WHERE shell_id=?",
                    (conductor_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
