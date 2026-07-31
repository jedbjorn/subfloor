"""Task #166 gates for the Sprint v1 removal manifest and cutover fixture."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sprint_removal"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
DATABASE_FIXTURE = FIXTURE_DIR / "pre_removal.sql"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
