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
MIGRATIONS = ENGINE / "migrations"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def build_pre_removal_database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DATABASE_FIXTURE.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def build_current_database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def schema_signature(con: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {
        tuple(row)
        for row in con.execute(
            "SELECT type,name,tbl_name FROM sqlite_master "
            "WHERE type IN ('table','view','index','trigger') "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name<>'schema_migrations'"
        )
    }


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
        path = ROOT / relative
        if not path.exists():
            continue
        if deny.search(relative):
            hits.add(relative)
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
            self.assertEqual(
                [
                    (220, "cv_normal", "assistant.delta"),
                    (221, "cv_sprint_dev", "assignment.notice"),
                ],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT event_id,conversation_id,event_type "
                        "FROM conversation_events ORDER BY event_id"
                    )
                ],
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='trg_conversation_events_append_only_delete'"
                ).fetchone(),
                "dirty fixture must install the retained append-only guard",
            )

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

    def test_task_171_current_guidance_and_assets_are_clean(self):
        removal = load_manifest()["guidance_removal"]
        forbidden = re.compile(removal["forbidden_pattern"], re.IGNORECASE)

        self.assertEqual(
            [],
            [relative for relative in removal["removed_paths"]
             if (ROOT / relative).exists()],
        )

        for relative in removal["authored_scan_paths"]:
            with self.subTest(relative=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file())
                self.assertIsNone(forbidden.search(relative))
                self.assertIsNone(forbidden.search(path.read_text()))

        html = (ENGINE / "ui" / "index.html").read_text()
        style = (ENGINE / "ui" / "style.css").read_text()
        self.assertIn('<button data-tab="interface">Chats</button>', html)
        self.assertNotIn(".sprint-board", style)
        self.assertNotIn(".an-sprint", style)

    def test_task_170_historical_migrations_are_absent_and_mixed_inputs_are_clean(
        self,
    ):
        removal = load_manifest()["schema_removal"]
        self.assertEqual(
            [],
            [
                name
                for name in removal["historical_migrations_removed"]
                if (MIGRATIONS / name).exists()
            ],
        )
        for name in removal["mixed_migrations_retained"]:
            self.assertTrue((MIGRATIONS / name).is_file(), name)

        removed = set(load_manifest()["removed_tables"])
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == Path(removal["cleanup_migration"]).name:
                continue
            sql = migration.read_text().lower()
            for table in removed:
                self.assertNotIn(
                    f"create table {table}",
                    sql,
                    f"{migration.name} recreates {table}",
                )
                self.assertNotIn(
                    f"create table if not exists {table}",
                    sql,
                    f"{migration.name} recreates {table}",
                )
            self.assertNotIn("add column sprint_doc_id", sql, migration.name)
            self.assertNotIn("add column sprint_ref", sql, migration.name)
            self.assertNotIn("'sprint_dev'", sql, migration.name)
            self.assertNotIn("'sprint_pln'", sql, migration.name)
            self.assertNotIn("'sprint_rev'", sql, migration.name)
            self.assertNotIn("'sprint_cond'", sql, migration.name)

    def test_task_170_fresh_build_contains_only_retained_schema(self):
        manifest = load_manifest()
        with closing(build_current_database()) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(set(), set(manifest["removed_tables"]) & tables)
            for table, retired in manifest["shared_table_cutover"].items():
                columns = {
                    row[1]
                    for row in con.execute(f'PRAGMA table_info("{table}")')
                }
                self.assertEqual(
                    set(),
                    set(retired) & columns,
                    f"{table} retained a removed field",
                )
            con.executemany(
                "INSERT INTO shells (shell_id,display_name,system_prompt) "
                "VALUES (?,?,'test')",
                ((9001, "Sender"), (9002, "Recipient")),
            )
            con.executemany(
                "INSERT INTO shell_messages "
                "(from_shell_id,to_shell_id,body,kind) VALUES (9001,9002,?,?)",
                (("shell", "shell"), ("task", "task"), ("result", "result")),
            )
            self.assertEqual(
                ["shell", "task", "result"],
                [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT kind FROM shell_messages "
                        "ORDER BY CASE kind "
                        "WHEN 'shell' THEN 1 WHEN 'task' THEN 2 ELSE 3 END"
                    )
                ],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO shell_messages "
                    "(from_shell_id,to_shell_id,body,kind) "
                    "VALUES (9001,9002,'removed','pr_event')"
                )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_task_170_dirty_cutover_preserves_generic_data_and_discards_sprint(
        self,
    ):
        cleanup = ROOT / load_manifest()["schema_removal"]["cleanup_migration"]
        with closing(build_pre_removal_database()) as con:
            con.executescript(cleanup.read_text())
            con.executescript(cleanup.read_text())

            self.assertEqual(
                [
                    (
                        "cv_normal",
                        1,
                        "Retained normal conversation",
                        0,
                    )
                ],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT conversation_id,owner_user_id,title,starred "
                        "FROM conversations ORDER BY conversation_id"
                    )
                ],
            )
            self.assertEqual(
                [
                    (200, "Retained normal prompt", "completed"),
                ],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT message_id,body,state "
                        "FROM conversation_messages ORDER BY message_id"
                    )
                ],
            )
            self.assertEqual(
                [(210, "succeeded", 42)],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT run_id,state,archive_id "
                        "FROM conversation_runs ORDER BY run_id"
                    )
                ],
            )
            self.assertEqual(
                [(220, "assistant.delta")],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT event_id,event_type "
                        "FROM conversation_events ORDER BY event_id"
                    )
                ],
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "conversation events are append-only",
            ):
                con.execute("DELETE FROM conversation_events WHERE event_id=220")
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "conversation events are append-only",
            ):
                con.execute(
                    "UPDATE conversation_events SET event_type='mutated' "
                    "WHERE event_id=220"
                )
            self.assertEqual(
                (220, "cv_normal", "assistant.delta"),
                tuple(
                    con.execute(
                        "SELECT event_id,conversation_id,event_type "
                        "FROM conversation_events WHERE event_id=220"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                [(230, "dispatched")],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT outbox_id,state "
                        "FROM conversation_outbox ORDER BY outbox_id"
                    )
                ],
            )
            self.assertEqual(
                [("feat/retained-review", 900)],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT branch_name,pr_number "
                        "FROM conversation_git_targets"
                    )
                ],
            )
            self.assertEqual(
                [
                    (100, "shell", "generic-shell"),
                    (101, "result", "generic-job-result"),
                ],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT message_id,kind,dedupe_key "
                        "FROM shell_messages ORDER BY message_id"
                    )
                ],
            )
            self.assertEqual(
                [
                    (42, "normal-session", "codex", "openai", "gpt-fixture"),
                    (
                        43,
                        "sprint-session",
                        "claude",
                        "anthropic",
                        "conductor-fixture",
                    ),
                ],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT archive_id,session_id,harness,provider,model "
                        "FROM shell_memory_archives ORDER BY archive_id"
                    )
                ],
            )
            self.assertEqual(
                [30, 32],
                [
                    row[0]
                    for row in con.execute(
                        "SELECT document_id FROM documents ORDER BY document_id"
                    )
                ],
            )
            self.assertEqual(
                (1,),
                tuple(
                    con.execute(
                        "SELECT is_deleted FROM shells WHERE shell_id=13"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM skills WHERE lower(name) LIKE '%sprint%'"
                ).fetchone()[0],
            )
            self.assertEqual(
                [("conversation-broker",)],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT name FROM daemon_heartbeats ORDER BY name"
                    )
                ],
            )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_task_170_partial_fixture_and_fresh_build_converge(self):
        manifest = load_manifest()
        cleanup = ROOT / manifest["schema_removal"]["cleanup_migration"]
        with closing(build_pre_removal_database()) as dirty:
            dirty.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "interface_recovery_observations",
                "planner_alerts",
                "sprint_cancellations",
            ):
                dirty.execute(f'DROP TABLE "{table}"')
            dirty.execute("PRAGMA foreign_keys=ON")
            dirty.executescript(cleanup.read_text())
            dirty.executescript(cleanup.read_text())
            with closing(build_current_database()) as fresh:
                self.assertEqual(schema_signature(fresh), schema_signature(dirty))
                self.assertEqual(
                    fresh.execute("PRAGMA foreign_key_check").fetchall(),
                    dirty.execute("PRAGMA foreign_key_check").fetchall(),
                )

    def test_task_170_cleanup_rolls_back_before_ledger_stamp_on_failure(self):
        cleanup = ROOT / load_manifest()["schema_removal"]["cleanup_migration"]
        scripts = ENGINE / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            import migrate
        finally:
            sys.path.pop(0)

        with closing(build_pre_removal_database()) as con:
            def deny_shell_message_drop(
                action: int,
                arg1: str | None,
                _arg2: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_DROP_TABLE and arg1 == "shell_messages":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            con.set_authorizer(deny_shell_message_drop)
            with self.assertRaises(sqlite3.DatabaseError):
                migrate.apply(con, cleanup)
            con.set_authorizer(None)

            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='watched_prs'"
                ).fetchone()
            )
            self.assertEqual(
                4,
                con.execute("SELECT COUNT(*) FROM shell_messages").fetchone()[0],
            )
            self.assertEqual(
                [(220, "cv_normal"), (221, "cv_sprint_dev")],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT event_id,conversation_id "
                        "FROM conversation_events ORDER BY event_id"
                    )
                ],
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "conversation events are append-only",
            ):
                con.execute("DELETE FROM conversation_events WHERE event_id=221")
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM schema_migrations WHERE filename=?",
                    (cleanup.name,),
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
