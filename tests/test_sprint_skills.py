"""Stage 9 gates for the five Sprints v2 engine skills."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills
import sprint_cli

SKILLS = {
    "sprint_prep": "planner",
    "sprint_pln": "planner",
    "sprint_dev": "dev",
    "sprint_rev": "reviewer",
    "sprint_close": "planner",
}
RESEEDED_SKILLS = set(SKILLS) | {"db_map"}
AUTHORITY_SPLIT_SKILLS = {"sprint_pln", "sprint_rev"}
V21_ROLE_SKILLS = set(SKILLS)
HANDOFF_ROLE_SKILLS = {"sprint_dev", "sprint_rev", "sprint_pln"}
CLOSEOUT_ROLE_SKILLS = {"sprint_close", "sprint_dev", "sprint_pln", "sprint_rev"}
FORCE_NEW_ROLE_SKILLS = {"sprint_dev", "sprint_pln", "sprint_rev"}
POLISHED_SPRINT_SKILLS = set(SKILLS) - {"sprint_prep"}
CHAT_CLEANUP_SKILLS = {"sprint_close", "sprint_pln", "sprint_rev"}
PROGRESS_CARRIER_ROLE_SKILLS = {"sprint_dev", "sprint_pln", "sprint_rev"}

ARTIFACT_PATH_RULE = """## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only."""


class SprintSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = sqlite3.connect(":memory:")
        cls.con.row_factory = sqlite3.Row
        cls.con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            cls.con.executescript(migration.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_catalogue_bodies_match_assets_and_role_grants_are_exact(self):
        for name, flavor in SKILLS.items():
            with self.subTest(name=name):
                parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                row = self.con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertEqual(
                    (
                        parsed["description"],
                        parsed["category"],
                        parsed["command"],
                        parsed["common"],
                        parsed["content"],
                        0,
                    ),
                    tuple(row),
                )
                grants = [
                    grant[0]
                    for grant in self.con.execute(
                        "SELECT fs.flavor FROM flavor_skills fs "
                        "JOIN skills s ON s.skill_id=fs.skill_id "
                        "WHERE s.name=? ORDER BY fs.flavor",
                        (name,),
                    )
                ]
                self.assertEqual([flavor], grants)

    def test_handoff_migration_converges_a_drifted_existing_skill_body(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0153_harden_sprint_handoff_skills.sql":
                        break
                    target.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET content='fork-local drift' WHERE name='sprint_dev'"
            )

            migration = (
                ENGINE / "migrations" / "0153_harden_sprint_handoff_skills.sql"
            ).read_text()
            con.executescript(migration)
            reference.executescript(migration)

            self.assertEqual(
                reference.execute(
                    "SELECT content FROM skills WHERE name='sprint_dev'"
                ).fetchone()[0],
                con.execute(
                    "SELECT content FROM skills WHERE name='sprint_dev'"
                ).fetchone()[0],
            )
        finally:
            con.close()
            reference.close()

    def test_native_wake_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0159_reseed_sprint_native_wake_skills.sql":
                        break
                    target.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in RESEEDED_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0159 body' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(RESEEDED_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0159_reseed_sprint_native_wake_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            reference.executescript(migration)

            for name in sorted(RESEEDED_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT content, is_deleted FROM skills WHERE name=?", (name,)
                    ).fetchall()
                    expected = reference.execute(
                        "SELECT content, is_deleted FROM skills WHERE name=?", (name,)
                    ).fetchone()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), tuple(expected))
        finally:
            con.close()
            reference.close()

    def test_authority_split_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0167_reseed_sprint_authority_split.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in AUTHORITY_SPLIT_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0167 authority' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(AUTHORITY_SPLIT_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0167_reseed_sprint_authority_split.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            for name in sorted(AUTHORITY_SPLIT_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(0, rows[0][5])
                    self.assertIn("Reviewer decides", rows[0][4])
                    self.assertIn("Planner", rows[0][4])
        finally:
            con.close()

    def test_terminal_handoff_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0170_reseed_sprint_handoff_order.sql":
                        break
                    target.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in HANDOFF_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0170 guidance' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(HANDOFF_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0170_reseed_sprint_handoff_order.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            reference.executescript(migration)

            for name in sorted(HANDOFF_ROLE_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    expected = reference.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchone()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), tuple(expected))
        finally:
            con.close()
            reference.close()

    def test_closeout_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0171_reseed_sprint_closeout_skills.sql":
                        break
                    target.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in CLOSEOUT_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0171 closeout guidance', "
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(CLOSEOUT_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0171_reseed_sprint_closeout_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            reference.executescript(migration)

            for name in sorted(CLOSEOUT_ROLE_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    expected = reference.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchone()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), tuple(expected))
        finally:
            con.close()
            reference.close()

    def test_sanctioned_pause_reseed_converges_developer_guidance(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0172_sanctioned_pause_liveness.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET content='stale pre-0172 guidance',is_deleted=1 "
                "WHERE name='sprint_dev'"
            )

            migration = (
                ENGINE / "migrations" / "0172_sanctioned_pause_liveness.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            actual = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_dev'"
            ).fetchone()
            self.assertEqual(0, actual[5])
            self.assertIn("This is a once-only\npre-handoff check", actual[4])
            self.assertIn(
                "paused awaiting a native PR-fact or verdict\nwake", actual[4]
            )
        finally:
            con.close()

    def test_force_new_reseed_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0174_reseed_force_new_wake_skills.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in FORCE_NEW_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale', category='stale', "
                f"command='stale', common=1, content='stale pre-0174 guidance', "
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(FORCE_NEW_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0174_reseed_force_new_wake_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0174_reseed_force_new_wake_skills.sql":
                    con.executescript(later_migration.read_text())

            for name in sorted(FORCE_NEW_ROLE_SKILLS):
                with self.subTest(name=name):
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        tuple(rows[0]),
                        (
                            parsed["description"],
                            parsed["category"],
                            parsed["command"],
                            parsed["common"],
                            parsed["content"],
                            0,
                        ),
                    )
        finally:
            con.close()

    def test_red_check_doctrine_reseed_matches_asset_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0176_reseed_sprint_red_check_doctrine.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale', category='stale', "
                "command='stale', common=1, content='accepted-red is okay', "
                "is_deleted=1 WHERE name='sprint_rev'"
            )

            migration = (
                ENGINE
                / "migrations"
                / "0176_reseed_sprint_red_check_doctrine.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0176_reseed_sprint_red_check_doctrine.sql":
                    con.executescript(later_migration.read_text())

            parsed = seed_skills.parse_skill(ASSETS / "sprint_rev" / "SKILL.md")
            rows = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_rev'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                tuple(rows[0]),
                (
                    parsed["description"],
                    parsed["category"],
                    parsed["command"],
                    parsed["common"],
                    parsed["content"],
                    0,
                ),
            )
        finally:
            con.close()

    def test_watcher_state_reseed_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0178_reseed_sprint_watcher_state_skills.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='wait blindly',is_deleted=1 "
                "WHERE name IN ('sprint_dev','sprint_pln')"
            )

            migration = (
                ENGINE / "migrations" / "0178_reseed_sprint_watcher_state_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0178_reseed_sprint_watcher_state_skills.sql":
                    con.executescript(later_migration.read_text())

            for name in ("sprint_dev", "sprint_pln"):
                with self.subTest(name=name):
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        tuple(rows[0]),
                        (
                            parsed["description"],
                            parsed["category"],
                            parsed["command"],
                            parsed["common"],
                            parsed["content"],
                            0,
                        ),
                    )
                    normalized = " ".join(parsed["content"].split())
                    self.assertIn("sc sprint watcher-state --sprint <id>", normalized)
                    self.assertIn("Do not repeat", normalized)
        finally:
            con.close()

    def test_optional_qaqc_reseed_matches_asset_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0185_optional_sprint_qaqc.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='review gates launch',"
                "is_deleted=1 WHERE name='sprint_prep'"
            )

            migration = (
                ENGINE / "migrations" / "0185_optional_sprint_qaqc.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            parsed = seed_skills.parse_skill(ASSETS / "sprint_prep" / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_prep'"
            ).fetchone()
            self.assertEqual(
                (
                    parsed["description"],
                    parsed["category"],
                    parsed["command"],
                    parsed["common"],
                    parsed["content"],
                    0,
                ),
                tuple(row),
            )
            normalized = " ".join(parsed["content"].split())
            for guidance in (
                "The FnB decides whether pre-Sprint QA/QC is useful",
                "never blocks declaration or arming",
                "--spec <spec-document-id>",
                "server reads and hashes the body inside the declaration transaction",
                "no current non-empty `spec` document",
                "a selected task belongs to no work unit or more than one work unit",
                "participant routes or required capacity are unavailable",
                "merge grant was not committed",
                "State whether pre-Sprint QA/QC was performed",
            ):
                self.assertIn(guidance, normalized)
            self.assertNotIn("qualifying QAQC approval", normalized)
            self.assertNotIn("Use `fail` until", normalized)
            self.assertNotIn("lacks Review-shell QAQC approval", normalized)
        finally:
            con.close()

    def test_sprint_polish_reseed_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0184_reseed_sprint_skill_polish.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in POLISHED_SPRINT_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale',category='stale',"
                f"command='stale',common=1,content='over-specified workflow',"
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(POLISHED_SPRINT_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0184_reseed_sprint_skill_polish.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0184_reseed_sprint_skill_polish.sql":
                    con.executescript(later_migration.read_text())

            for name in sorted(POLISHED_SPRINT_SKILLS):
                with self.subTest(name=name):
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        tuple(rows[0]),
                        (
                            parsed["description"],
                            parsed["category"],
                            parsed["command"],
                            parsed["common"],
                            parsed["content"],
                            0,
                        ),
                    )
        finally:
            con.close()

    def test_successful_chat_cleanup_reseed_matches_assets_and_is_idempotent(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0190_reseed_successful_sprint_chat_cleanup.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in CHAT_CLEANUP_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale',category='stale',"
                f"command='stale',common=1,content='manual peer close',"
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(CHAT_CLEANUP_SKILLS)),
            )
            developer_before = tuple(
                con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name='sprint_dev'"
                ).fetchone()
            )

            migration = (
                ENGINE
                / "migrations"
                / "0190_reseed_successful_sprint_chat_cleanup.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            self.assertEqual(
                developer_before,
                tuple(
                    con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name='sprint_dev'"
                    ).fetchone()
                ),
            )
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0190_reseed_successful_sprint_chat_cleanup.sql":
                    con.executescript(later_migration.read_text())

            for name in sorted(CHAT_CLEANUP_SKILLS):
                with self.subTest(name=name):
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(1, len(rows))
                    self.assertEqual(
                        (
                            parsed["description"],
                            parsed["category"],
                            parsed["command"],
                            parsed["common"],
                            parsed["content"],
                            0,
                        ),
                        tuple(rows[0]),
                    )
                    normalized = " ".join(parsed["content"].split())
                    self.assertIn("originating Planner", normalized)
                    self.assertIn("report-authoring Reviewer", normalized)
                    self.assertIn("Do not manually close peer chats", normalized)
                    self.assertIn("failed conformance", normalized)
        finally:
            con.close()

    def test_pr_recovery_reseed_matches_asset_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0192_reseed_sprint_pr_recovery.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='no recovery surface',"
                "is_deleted=1 WHERE name='sprint_pln'"
            )

            migration = (
                ENGINE / "migrations" / "0192_reseed_sprint_pr_recovery.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            self.assertIn(
                "sc sprint reconcile-pr",
                con.execute(
                    "SELECT content FROM skills WHERE name='sprint_pln'"
                ).fetchone()[0],
            )
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0192_reseed_sprint_pr_recovery.sql":
                    con.executescript(later_migration.read_text())

            parsed = seed_skills.parse_skill(ASSETS / "sprint_pln" / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_pln'"
            ).fetchone()
            self.assertEqual(
                (
                    parsed["description"],
                    parsed["category"],
                    parsed["command"],
                    parsed["common"],
                    parsed["content"],
                    0,
                ),
                tuple(row),
            )
        finally:
            con.close()

    def test_progress_carrier_reseed_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0195_reseed_sprint_progress_carriers.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in PROGRESS_CARRIER_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale',category='stale',"
                f"command='stale',common=1,content='legacy liveness workflow',"
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(PROGRESS_CARRIER_ROLE_SKILLS)),
            )

            migration = (
                ENGINE
                / "migrations"
                / "0195_reseed_sprint_progress_carriers.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            for name in sorted(PROGRESS_CARRIER_ROLE_SKILLS):
                with self.subTest(name=name):
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(1, len(rows))
                    self.assertEqual(
                        (
                            parsed["description"],
                            parsed["category"],
                            parsed["command"],
                            parsed["common"],
                            parsed["content"],
                            0,
                        ),
                        tuple(rows[0]),
                    )
        finally:
            con.close()

    def test_flags_output_reseed_matches_fresh_seed_and_replays_idempotently(self):
        upgraded = sqlite3.connect(":memory:")
        fresh = sqlite3.connect(":memory:")
        try:
            for con in (upgraded, fresh):
                con.executescript((ENGINE / "schema.sql").read_text())
            fresh.executescript(
                (ENGINE / "migrations" / "0001_seed_skills.sql").read_text()
            )
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0161_reseed_flags_output_guidance.sql":
                    break
                upgraded.executescript(migration.read_text())
            upgraded.execute(
                "UPDATE skills SET content='stale flags guidance', is_deleted=1 "
                "WHERE name='flags'"
            )

            migration = (
                ENGINE / "migrations" / "0161_reseed_flags_output_guidance.sql"
            ).read_text()
            upgraded.executescript(migration)
            upgraded.executescript(migration)

            expected = seed_skills.parse_skill(
                ASSETS / "flags" / "SKILL.md"
            )["content"]
            for con in (fresh, upgraded):
                rows = con.execute(
                    "SELECT content, is_deleted FROM skills WHERE name='flags'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(tuple(rows[0]), (expected, 0))
        finally:
            upgraded.close()
            fresh.close()

    def test_reviewer_skill_owns_severity_and_closeout_timing_judgment(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        self.assertIn("## Severity rubric", reviewer)
        for severity in ("Critical", "Major", "Medium", "Low"):
            self.assertIn(f"**{severity}**", reviewer)
        self.assertIn("severity does not decide timing", reviewer)
        self.assertIn("requires in-Sprint patching", reviewer)
        for name in SKILLS.keys() - {"sprint_rev"}:
            self.assertNotIn(
                "## Severity rubric", (ASSETS / name / "SKILL.md").read_text()
            )

    def test_reviewer_delivery_terminal_section_branches_before_recording(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        section = reviewer[
            reviewer.index("## Delivery-terminal closeout"):
            reviewer.index("## Whole-Sprint conformance")
        ]
        normalized = " ".join(section.lower().split())
        for guidance in (
            "sc sprint compile-report",
            "if any non-terminal unit is visible, the wake is stale",
            "`abort`, not `conclude`",
            "do not run `record-conformance`",
            "title and description",
            "grouping, waves, dependencies",
            "after three re-entry episodes",
            "clean or post-sprint-only findings",
        ):
            self.assertIn(guidance, normalized)
        self.assertLess(
            normalized.index("do not run `record-conformance`"),
            normalized.index("clean or post-sprint-only findings"),
        )

    def test_planner_executes_reenter_and_does_not_initiate_conformance(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        reenter = planner[
            planner.index("### Re-enter after conformance"):
            planner.index("### Conclude or abort")
        ]
        for command in (
            "sc mem task add",
            "sc sprint plan-unit",
            "--depends-on <work-unit-id>",
            "sc sprint dispatch --sprint <id>",
        ):
            self.assertIn(command, reenter)
        self.assertIn("FnB-directed fallback", planner)
        self.assertNotIn(
            "When all planned delivery work is terminal and merged or explicitly no-code",
            planner,
        )

    def test_closeout_role_skills_share_the_exact_artifact_path_rule(self):
        for name in sorted(CLOSEOUT_ROLE_SKILLS):
            with self.subTest(name=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                self.assertEqual(body.count(ARTIFACT_PATH_RULE), 1)

    def test_close_skill_routes_to_owning_roles_and_keeps_fallback_bounded(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        normalized = " ".join(close.split())
        self.assertIn("## Route the entry", close)
        self.assertIn("Load `sprint_rev`", close)
        self.assertIn("Load `sprint_pln`", close)
        self.assertIn("The Planner does not initiate this pass", close)
        self.assertIn("only when FnB explicitly directs it", close)
        self.assertIn("shared/sprints/sprint-<n>/evidence.json", close)
        self.assertNotIn("## Whole-Sprint conformance", close)
        self.assertNotIn("## Final report", close)

    def test_clean_closeout_records_and_notifies_planner_in_one_command(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        clean = reviewer[
            reviewer.index("## Whole-Sprint conformance"):
            reviewer.index("## Stop")
        ]
        for guidance in (
            "--final-report-file <final-report>",
            "--reason <reason> --outcome <outcome>",
            "final report id",
            "completed state",
            "Planner message id",
            "Planner wake id",
            "informational engine-wide Planner Re-enter",
            "send no conclude message",
        ):
            self.assertIn(guidance, clean)
        self.assertLess(
            clean.index("Before recording conformance, author the final Sprint report"),
            clean.index("sc sprint record-conformance"),
        )
        normalized_planner = " ".join(planner.split())
        self.assertIn("clean `record-conformance` command atomically", normalized_planner)
        self.assertIn("Do not run `complete`", planner)
        self.assertIn(
            "notification is informational because closure is already terminal",
            normalized_planner,
        )
        normalized_close = " ".join(close.split())
        self.assertIn("completes the Sprint", normalized_close)
        self.assertIn("informational Planner receipt", normalized_close)

    def test_originating_planner_owns_pr_reconciliation(self):
        planner = " ".join(
            (ASSETS / "sprint_pln" / "SKILL.md").read_text().split()
        )

        self.assertIn("originating Planner may reconcile that identity", planner)
        self.assertIn("refuses a live source Sprint or target Sprint", planner)
        self.assertIn("a non-originating Planner", planner)
        self.assertIn("separate Reviewer decision before resuming", planner)

    def test_skills_use_only_the_shipped_shell_command_surface(self):
        expected = {
            "record-qaqc",
            "declare",
            "plan-unit",
            "replan-unit",
            "arm",
            "inbox",
            "send",
            "accept",
            "decline",
            "complete-unit",
            "cancel-unit",
            "register-pr",
            "reconcile-pr",
            "pause",
            "resume",
            "complete",
            "abort",
            "request-review",
            "record-review",
            "authorize-merge",
            "dispatch",
            "monitor",
            "watcher-state",
            "record-conformance",
            "disposition-followup",
            "compile-report",
        }
        combined = "\n".join(
            (ASSETS / name / "SKILL.md").read_text() for name in SKILLS
        )
        for command in expected - {"monitor"}:
            self.assertIn(f"sc sprint {command}", combined)
        self.assertNotIn("sc sprint monitor", combined)
        dispatcher = (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text()
        self.assertIn(
            'sprint)       sc_python_probe; exec "$PY" "$S/sprint_cli.py" "$@" ;;',
            dispatcher,
        )
        parser = sprint_cli.build_parser()
        commands = next(
            action
            for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        self.assertEqual(expected, set(commands))

    def test_role_skills_cover_every_handoff_contingency_with_real_commands(self):
        role_skills = {"sprint_pln", "sprint_dev", "sprint_rev"}
        for name in role_skills:
            with self.subTest(name=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                for command in (
                    "sc sprint inbox --sprint <id>",
                    "sc sprint accept --sprint <id> --message <message-id>",
                    "sc sprint decline --sprint <id> --message <message-id>",
                    "--intent question --requires-reply --work-unit <work-unit-id>",
                    "--intent decision --requires-reply --sprint-level",
                    "--intent information --reply-to <message-id>",
                ):
                    self.assertIn(command, body)
                normalized = " ".join(body.lower().split())
                for guidance in (
                    "on every entry",
                    "route the entry",
                    "original message",
                    "inherits its",
                    "blocker",
                    "decision boundary",
                    "duplicate",
                    "command is rejected or transport fails",
                    "6,000 characters",
                    "8,000",
                    "wc -m < <path>",
                    "command exits successfully",
                    "informational message",
                    "marks the message read",
                    "does not change sprint or work-unit state",
                    "re-run `sc sprint inbox --sprint <id>`",
                ):
                    self.assertIn(guidance, normalized)
                self.assertIn("reuse it only", normalized)
                self.assertIn("when any of those fields changes", normalized)
                for invented in ("ASK:", "ANSWER:", "BLOCKED:"):
                    self.assertNotIn(invented, body)

    def test_role_messages_are_scoped_and_progress_carrier_driven(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in PROGRESS_CARRIER_ROLE_SKILLS
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                normalized = " ".join(body.split())
                self.assertIn(
                    "--intent question --requires-reply --work-unit <work-unit-id>",
                    normalized,
                )
                self.assertIn("Use `--intent blocker`", normalized)
                self.assertIn(
                    "--intent decision --requires-reply --sprint-level",
                    normalized,
                )
                self.assertIn(
                    "--intent information --reply-to <message-id>",
                    normalized,
                )
                self.assertIn(
                    "never add `--work-unit` or `--sprint-level` to a reply",
                    normalized,
                )
                self.assertNotIn("liveness", body.lower())
                self.assertNotIn("sc sprint monitor", body)

        self.assertIn(
            "--intent handoff --key <stable-merged-handoff-key>",
            " ".join(bodies["sprint_dev"].split()),
        )
        reviewer = " ".join(bodies["sprint_rev"].split())
        self.assertIn("retain that exact message id", reviewer)
        self.assertIn(
            "accepted request's message id, registered PR, work unit, and exact head",
            reviewer,
        )
        self.assertIn("exact notification message id", reviewer)
        self.assertIn("Only an armed Sprint whose units are all terminal", reviewer)

    def test_authority_split_assigns_reviewer_decisions_and_planner_actions(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        normalized_planner = " ".join(planner.lower().split())
        normalized_reviewer = " ".join(reviewer.lower().split())

        self.assertIn("## Reviewer decision actions", planner)
        self.assertIn(
            "pause, cancel, re-enter, and abort are reviewer decisions",
            normalized_planner,
        )
        self.assertIn("planner actions", normalized_planner)
        for command in (
            "sc sprint pause --sprint <id>",
            "sc sprint cancel-unit --sprint <id>",
        ):
            self.assertIn(command, planner)
        self.assertIn("reviewer-authored final report", normalized_planner)
        self.assertIn("does not author a second report", normalized_planner)
        self.assertIn("do not run `complete`", normalized_planner)
        self.assertNotIn("you decide scope, sequencing, and recovery", normalized_planner)

        self.assertIn("## Control and conclude decisions", reviewer)
        self.assertIn(
            "owns all pause, cancel, and conclude decisions", normalized_reviewer
        )
        self.assertIn("author the final Sprint report", reviewer)
        self.assertIn("sc sprint record-conformance", reviewer)
        self.assertIn("sc sprint compile-report", reviewer)
        self.assertIn(
            "`decision`: `pause`, `resume`, `replan`, `re-enter`, `cancel`, or `abort`",
            reviewer,
        )
        self.assertNotIn("`cancel`, `conclude`", reviewer)
        self.assertNotIn("the planner decides whether", normalized_reviewer)
        self.assertNotIn("sc sprint pause --sprint <id>", reviewer)

        for body in (planner, reviewer):
            normalized = " ".join(body.lower().split())
            self.assertIn("fnb board-level override", normalized)
            self.assertIn("decision #46", normalized)

    def test_planner_control_decisions_reply_before_accept_and_action(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        control = planner[
            planner.index("## Reviewer decision actions"):
            planner.index("The FnB board-level override")
        ]

        linked_reply = "--reply-to <decision-message-id>"
        accept = "sc sprint accept --sprint <id> --message <decision-message-id>"
        action = "execute the requested transition"
        self.assertIn(linked_reply, control)
        self.assertIn(accept, control)
        self.assertIn(action, control)
        self.assertLess(control.index(linked_reply), control.index(accept))
        self.assertLess(control.index(accept), control.index(action))
        self.assertIn(
            "reply command to confirm its durable message and wake",
            control,
        )
        self.assertIn(
            "linked reply must precede any pause or\n"
            "   abort that makes the Sprint relay unavailable",
            control,
        )

    def test_developer_reports_integrity_concerns_without_taking_pause_action(self):
        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        normalized = " ".join(developer.lower().split())
        self.assertNotIn("sc sprint pause --sprint <id>", developer)
        self.assertIn("evidence, impact", normalized)
        self.assertIn("recommendation", normalized)
        self.assertIn("does not pause the sprint", normalized)
        self.assertIn("relay itself fails", normalized)

    def test_close_skill_routes_decisions_without_owning_judgment(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        normalized = " ".join(close.lower().split())
        self.assertIn("the reviewer decides", normalized)
        self.assertIn("the planner executes", normalized)
        self.assertIn("decision #46", normalized)
        self.assertIn("load `sprint_rev`", normalized)
        self.assertIn("load `sprint_pln`", normalized)
        self.assertIn("do not substitute another transition", normalized)
        self.assertNotIn("sc sprint pause --sprint <id>", close)

    def test_v21_delivery_contract_is_folded_into_roles_and_boot(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in SKILLS
        }
        combined = "\n".join(bodies.values())
        for phrase in (
            "natural boundary",
            "PR-event wakes",
            "Defaults satisfy the gate",
        ):
            self.assertIn(phrase, combined)

        developer = bodies["sprint_dev"]
        reviewer = bodies["sprint_rev"]
        planner = bodies["sprint_pln"]
        self.assertIn("outside an armed Sprint", developer)
        self.assertIn("Reviewer decides", developer)
        self.assertIn("replan, cancel", reviewer)
        self.assertIn("Compile the bounded evidence packet first", reviewer)
        self.assertIn("Developer-owned subscriptions", planner)

        boot = (ENGINE / "templates" / "boot.md").read_text()
        self.assertIn("## ACTIVE CHAT DELIVERY", boot)
        self.assertIn("Every `wake_message` creates durable delivery intent", boot)
        self.assertIn("verified live turn", boot)
        self.assertIn("coordinate mode", boot)
        self.assertIn("reaper", boot)
        self.assertIn("defaults satisfy the gate", boot)

    def test_force_new_and_blind_review_contracts_are_folded_into_roles(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in FORCE_NEW_ROLE_SKILLS
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                normalized = " ".join(body.lower().split())
                for guidance in (
                    "force-new delivery",
                    "re-enter",
                    "natural boundary",
                    "runtime owns",
                    "stop after a successful typed handoff",
                ):
                    self.assertIn(guidance, normalized)

        developer = " ".join(bodies["sprint_dev"].lower().split())
        reviewer = " ".join(bodies["sprint_rev"].lower().split())
        for guidance in (
            "bare one-line locator",
            "no scope narrative",
            "verification evidence",
            "review-focus steering",
            "work-unit id and spec reference",
            "write no pr comments or annotations",
        ):
            self.assertIn(guidance, developer)
        for guidance in (
            "bare locator",
            "full diff",
            "each round is clean",
            "prior findings",
            "no prior developer evidence",
        ):
            self.assertIn(guidance, reviewer)

    def test_reviewer_forbids_accepted_red_and_routes_failures(self):
        reviewer = " ".join(
            (ASSETS / "sprint_rev" / "SKILL.md").read_text().split()
        )
        for guidance in (
            "Accepted-red is not a legal review outcome",
            "A departure that leaves checks failing is never acceptable",
            "record `changes_requested` so the Developer fixes them",
            "send the Planner a `replan` decision",
            "remains green-only, without exception or waiver",
            "do not note the failure and approve anyway",
            "`Note it and pass anyway` is the acceptance-shaped anti-pattern",
            "In the dos-arch incident",
            "created a deadlock: the green-only handoff gate could never pass",
            "Decision #93 records why this no-waiver rule exists",
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, reviewer)

    def test_every_affected_file_argument_names_the_hard_ceiling(self):
        parser = sprint_cli.build_parser()
        commands = next(
            action
            for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        for command, arguments in {
            "send": ("--body-file",),
            "complete-unit": ("--result-file",),
            "request-review": ("--readiness-file",),
            "record-review": ("--body-file",),
            "record-conformance": (
                "--body-file",
                "--findings-file",
                "--final-report-file",
            ),
            "disposition-followup": ("--resolution-file",),
            "complete": ("--report-file",),
        }.items():
            with self.subTest(command=command):
                for argument in arguments:
                    action = next(
                        action
                        for action in commands[command]._actions
                        if argument in action.option_strings
                    )
                    self.assertIn("8,000 characters", action.help)

    def test_role_contracts_assign_scheduled_coordination_to_native_wakes(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in SKILLS
        }
        prep = " ".join(bodies["sprint_prep"].split())
        self.assertIn(
            "participant pickup belongs to native delivery", prep
        )
        self.assertIn(
            "Neither minimum headcount nor maximum shell occupancy is a goal", prep
        )
        self.assertIn(
            "one Developer and one Reviewer", prep
        )
        self.assertIn(
            "analyze the task ledger and dependency graph", prep
        )
        self.assertIn(
            "ready reviews can run alongside ongoing independent development",
            prep,
        )
        self.assertIn(
            "Add a Developer only when another independent lane", prep
        )
        self.assertIn(
            "Add Reviewer capacity when expected concurrent review demand", prep
        )
        self.assertIn(
            "Use every eligible shell only when the work graph and review demand",
            prep,
        )
        self.assertIn("capacity rationale and reserve", prep)
        reviewer = " ".join(bodies["sprint_rev"].split())
        self.assertIn(
            "independent lanes, expected review overlap, useful reserve", reviewer
        )
        planner = " ".join(bodies["sprint_pln"].split())
        self.assertIn("dependency graph and capacity plan match the decision", planner)
        self.assertIn("rather than silently re-routing", planner)
        for fact in ("scheduled dispatch", "unread wake recovery"):
            self.assertIn(fact, planner)
        self.assertIn("registered-PR watcher owns subscription observation", planner)
        self.assertNotIn("sc sprint monitor", bodies["sprint_pln"])
        self.assertIn(
            "After `register-pr` succeeds", bodies["sprint_dev"]
        )
        self.assertIn(
            "stop and await the native verdict wake", bodies["sprint_dev"]
        )
        close = " ".join(bodies["sprint_close"].split())
        self.assertIn("Reviewer receives `sprint.delivery_terminal`", close)
        self.assertIn("The Planner does not initiate this pass", close)

        combined = "\n".join(bodies.values()).lower()
        for shell_owned_loop in (
            "while true",
            "./sc watch pr",
            "gh pr checks --watch",
            "sc job start",
        ):
            self.assertNotIn(shell_owned_loop, combined)

    def test_sprint_skills_share_adaptive_stance_and_explicit_entry_routing(self):
        for name in SKILLS:
            with self.subTest(name=name):
                normalized = " ".join(
                    (ASSETS / name / "SKILL.md").read_text().split()
                )
                self.assertIn(
                    "Use the simplest path supported by current durable state",
                    normalized,
                )
                self.assertIn("as hard boundaries", normalized)
                self.assertIn(
                    "Repeat a read only when later activity could have changed it",
                    normalized,
                )

        for name in ("sprint_pln", "sprint_dev", "sprint_close"):
            self.assertIn(
                "## Route the entry",
                (ASSETS / name / "SKILL.md").read_text(),
            )
        self.assertIn(
            "Classify the entry before reading an inbox",
            (ASSETS / "sprint_rev" / "SKILL.md").read_text(),
        )

        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        self.assertIn("## Report-only or no-code completion", developer)
        self.assertIn("Do not manufacture a Sprint inbox item", developer)

        planner = " ".join(
            (ASSETS / "sprint_pln" / "SKILL.md").read_text().split()
        )
        self.assertIn("do not run the Sprint inbox, accept it", planner)

    def test_role_handoffs_are_explicitly_ordered_and_message_last(self):
        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        post_merge = developer[
            developer.index("## Post-merge handoff"):
            developer.index("## Report and stop")
        ]
        self.assertLess(
            post_merge.index("Clean the worktree"),
            post_merge.index("Re-run `sc sprint inbox"),
        )
        self.assertLess(
            post_merge.index("Re-run `sc sprint inbox"),
            post_merge.index("sc sprint send --sprint <id>"),
        )
        self.assertLess(
            post_merge.index("sc sprint send --sprint <id>"),
            post_merge.index("Run no trailing Git"),
        )

        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        unit_verdict = reviewer[
            reviewer.index("Complete a unit verdict in this exact order"):
            reviewer.index("## Whole-Sprint conformance")
        ]
        self.assertLess(
            unit_verdict.index("Re-run `sc sprint inbox"),
            unit_verdict.index("sc sprint record-review"),
        )
        self.assertLess(
            unit_verdict.index("sc sprint record-review"),
            unit_verdict.index("Run no trailing command"),
        )

        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        wave_handoff = planner[
            planner.index("Never dispatch the next wave"):
            planner.index("On a clean completion receipt")
        ]
        self.assertIn(
            "merged-work handoff wake is the only normal next-wave dispatch trigger",
            wave_handoff,
        )
        self.assertLess(
            wave_handoff.index("Run `sc sprint inbox"),
            wave_handoff.index("sc sprint dispatch --sprint <id>"),
        )
        self.assertLess(
            wave_handoff.index("sc sprint dispatch --sprint <id>"),
            wave_handoff.index("Run no trailing command"),
        )

    def test_reviewer_entry_separates_predeclaration_qaqc_from_armed_inbox(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        normalized = " ".join(reviewer.split())
        qaqc = reviewer.index("sc sprint record-qaqc")
        inbox = reviewer.index("sc sprint inbox --sprint <id>")
        self.assertLess(qaqc, inbox)
        self.assertIn(
            "there is no Sprint id or Sprint inbox to inspect yet", normalized
        )
        self.assertIn("sc mem get flags <flag-id>", reviewer)
        self.assertIn(
            "sc mem get flags --feature <feature-id> --resolved", reviewer
        )

    def test_reviewer_drains_before_atomic_completion_and_stops_after_success(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        normalized_reviewer = " ".join(reviewer.split())
        drain = normalized_reviewer.index("Re-run `sc sprint inbox --sprint <id>`")
        terminal = normalized_reviewer.index(
            "run the atomic `record-conformance` command above as the literal "
            "final action"
        )
        self.assertLess(drain, terminal)
        stop = reviewer[reviewer.index("## Stop"):]
        normalized_stop = " ".join(stop.lower().split())
        self.assertIn(
            "when it confirms completed state and all receipt identities, stop "
            "immediately",
            normalized_stop,
        )
        self.assertIn("run no trailing command", normalized_stop)
