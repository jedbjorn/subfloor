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
FOCUSED_DEV_VERIFICATION_SKILLS = {
    "agents",
    "dev_kit",
    "spec",
    "sprint_dev",
    "sprint_pln",
}
CLOSEOUT_ROLE_SKILLS = {"sprint_close", "sprint_dev", "sprint_pln", "sprint_rev"}
FORCE_NEW_ROLE_SKILLS = {"sprint_dev", "sprint_pln", "sprint_rev"}
POLISHED_SPRINT_SKILLS = set(SKILLS) - {"sprint_prep"}
CHAT_CLEANUP_SKILLS = {"sprint_close", "sprint_pln", "sprint_rev"}
CLEANUP_RECOVERY_SKILLS = {
    "git",
    "sprint_close",
    "sprint_pln",
    "sprint_prep",
    "sprint_rev",
}
PROGRESS_CARRIER_ROLE_SKILLS = {"sprint_dev", "sprint_pln", "sprint_rev"}
LIVE_REPLAN_ROLE_SKILLS = {"sprint_pln", "sprint_rev"}
CONFORMANCE_OWNER_SKILLS = {
    "sprint_close",
    "sprint_pln",
    "sprint_prep",
    "sprint_rev",
}

SPEC_SKILL = ENGINE / "assets" / "skills" / "spec" / "SKILL.md"
CONTEXT_EFFICIENT_SKILLS = ("sprint_dev", "sprint_rev", "sprint_pln", "spec")
CONTEXT_EFFICIENT_SKILL_BYTE_CEILING = 47_212
CONTEXT_EFFICIENT_RESEED = (
    ENGINE / "migrations" / "0202_reseed_context_efficient_skills.sql"
)
CONFORMANCE_OWNER_RESEED = (
    ENGINE / "migrations" / "0206_reseed_sprint_conformance_ownership.sql"
)
INFORMATIONAL_RECEIPT_RESEED = (
    ENGINE / "migrations" / "0207_reseed_sprint_receipt_recovery.sql"
)
DISPOSABLE_SHELL_BASE_RESEED = (
    ENGINE / "migrations" / "0208_reseed_disposable_shell_base.sql"
)
GITHUB_CAPABILITY_RESEED = (
    ENGINE / "migrations" / "0209_reseed_git_github_capabilities.sql"
)
BINDING_GUIDANCE_RESEED = (
    ENGINE / "migrations" / "0215_reseed_sprint_binding_guidance.sql"
)
DISPOSITION_VERBS_RESEED = (
    ENGINE / "migrations" / "0222_reseed_sprint_pln_disposition_verbs.sql"
)
ROLE_AWARE_BOOT_RESEED = (
    ENGINE / "migrations" / "0243_role_aware_boot_contract.sql"
)
SUBFLOOR_COMMAND_RESEED = (
    ENGINE / "migrations" / "0247_reseed_subfloor_command.sql"
)


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

    def test_context_efficient_terminal_reseed_is_exact_and_idempotent(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
            )
            for index, name in enumerate(CONTEXT_EFFICIENT_SKILLS, 1):
                con.execute(
                    "INSERT INTO skills VALUES (?,?,?,?,?,?,?,1)",
                    (index, name, "stale", "stale", "stale", 1, "stale"),
                )
            con.execute(
                "INSERT INTO skills VALUES (99,'fork_only','local','fork',NULL,0,"
                "'bespoke body',0)"
            )

            migration = CONTEXT_EFFICIENT_RESEED.read_text()
            con.executescript(migration)
            con.executescript(migration)

            for name in CONTEXT_EFFICIENT_SKILLS:
                if name in HANDOFF_ROLE_SKILLS | FOCUSED_DEV_VERIFICATION_SKILLS:
                    continue  # Later reseeds deliberately supersede role bodies.
                parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                actual = con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertEqual(
                    tuple(actual),
                    (
                        parsed["description"],
                        parsed["category"],
                        parsed["command"],
                        parsed["common"],
                        parsed["content"],
                        0,
                    ),
                )
            local = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='fork_only'"
            ).fetchone()
            self.assertEqual(
                tuple(local),
                ("local", "fork", None, 0, "bespoke body", 0),
            )

    def test_conformance_owner_reseed_is_exact_and_idempotent(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
            )
            for index, name in enumerate(sorted(CONFORMANCE_OWNER_SKILLS), 1):
                con.execute(
                    "INSERT INTO skills VALUES (?,?,?,?,?,?,?,1)",
                    (index, name, "stale", "stale", "stale", 1, "stale"),
                )
            con.execute(
                "INSERT INTO skills VALUES "
                "(99,'fork_only','local','fork',NULL,0,'bespoke body',0)"
            )

            migration = CONFORMANCE_OWNER_RESEED.read_text()
            con.executescript(migration)
            con.executescript(migration)
            con.executescript(INFORMATIONAL_RECEIPT_RESEED.read_text())
            con.executescript(BINDING_GUIDANCE_RESEED.read_text())
            con.executescript(DISPOSITION_VERBS_RESEED.read_text())

            for name in sorted(CONFORMANCE_OWNER_SKILLS):
                with self.subTest(name=name):
                    if name in FOCUSED_DEV_VERIFICATION_SKILLS:
                        continue  # Migration 0234 supersedes this historical body.
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
            self.assertEqual(
                ("fork", "bespoke body", 0),
                tuple(
                    con.execute(
                        "SELECT category,content,is_deleted FROM skills "
                        "WHERE name='fork_only'"
                    ).fetchone()
                ),
            )

    def test_informational_receipt_reseed_is_exact_and_idempotent(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
            )
            for index, name in enumerate(sorted(HANDOFF_ROLE_SKILLS), 1):
                con.execute(
                    "INSERT INTO skills VALUES (?,?,?,?,?,?,?,1)",
                    (index, name, "stale", "stale", "stale", 1, "stale"),
                )
            con.execute(
                "INSERT INTO skills VALUES "
                "(99,'fork_only','local','fork',NULL,0,'bespoke body',0)"
            )

            migration = INFORMATIONAL_RECEIPT_RESEED.read_text()
            con.executescript(migration)
            con.executescript(migration)
            con.executescript(DISPOSITION_VERBS_RESEED.read_text())

            for name in sorted(HANDOFF_ROLE_SKILLS):
                with self.subTest(name=name):
                    if name in FOCUSED_DEV_VERIFICATION_SKILLS:
                        continue  # Migration 0225 deliberately supersedes this body.
                    parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                    row = con.execute(
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
            self.assertEqual(
                ("fork", "bespoke body", 0),
                tuple(
                    con.execute(
                        "SELECT category,content,is_deleted FROM skills "
                        "WHERE name='fork_only'"
                    ).fetchone()
                ),
            )

    def test_role_skills_bound_unusable_receipt_recovery(self):
        for name in sorted(HANDOFF_ROLE_SKILLS):
            with self.subTest(name=name):
                body = " ".join(
                    (ASSETS / name / "SKILL.md").read_text().lower().split()
                )
                self.assertIn("retry the exact command once", body)
                self.assertIn("normal read surface once", body)
                self.assertIn(
                    "prior inbox presence + absence of that exact message id proves "
                    "the read landed",
                    body,
                )
                for forbidden_inference in (
                    "assignment ownership",
                    "review outcome",
                    "merge authorization",
                    "lifecycle/work-unit transition",
                    "governing revision",
                    "pr head/green state",
                    "cleanup authority",
                ):
                    self.assertIn(forbidden_inference, body)
                self.assertIn("an unproved postcondition stops", body)

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
            con.executescript(
                (
                    ENGINE / "migrations" / "0203_sprint_cleanup_recovery.sql"
                ).read_text()
            )
            con.executescript(CONFORMANCE_OWNER_RESEED.read_text())
            con.executescript(BINDING_GUIDANCE_RESEED.read_text())

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

    def test_cleanup_recovery_migration_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0203_sprint_cleanup_recovery.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in CLEANUP_RECOVERY_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale',category='stale',"
                f"command='stale',common=1,content='no cleanup recovery',"
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(CLEANUP_RECOVERY_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0203_sprint_cleanup_recovery.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            con.executescript(CONFORMANCE_OWNER_RESEED.read_text())
            con.executescript(INFORMATIONAL_RECEIPT_RESEED.read_text())
            con.executescript(DISPOSABLE_SHELL_BASE_RESEED.read_text())
            con.executescript(GITHUB_CAPABILITY_RESEED.read_text())
            con.executescript(BINDING_GUIDANCE_RESEED.read_text())
            con.executescript(DISPOSITION_VERBS_RESEED.read_text())
            con.executescript(ROLE_AWARE_BOOT_RESEED.read_text())
            con.executescript(SUBFLOOR_COMMAND_RESEED.read_text())

            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='sprint_cleanup_requests'"
                ).fetchone()
            )
            for name in sorted(CLEANUP_RECOVERY_SKILLS):
                with self.subTest(name=name):
                    if name in FOCUSED_DEV_VERIFICATION_SKILLS:
                        continue  # Migration 0234 supersedes this historical body.
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

            all_content = " ".join(
                row[0]
                for row in con.execute(
                    f"SELECT content FROM skills WHERE name IN ({placeholders})",
                    tuple(sorted(CLEANUP_RECOVERY_SKILLS)),
                )
            )
            self.assertIn("sc sprint cleanup-status", all_content)
            self.assertIn("--adopt-legacy", all_content)
            self.assertIn("not reusable", all_content)
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

    def test_reopened_pr_reseed_matches_asset_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0197_reseed_reopened_pr_resubscription.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='no resubscription guidance',"
                "is_deleted=1 WHERE name='sprint_dev'"
            )

            migration = (
                ENGINE
                / "migrations"
                / "0197_reseed_reopened_pr_resubscription.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            self.assertIn(
                "replay the exact `register-pr` command",
                con.execute(
                    "SELECT content FROM skills WHERE name='sprint_dev'"
                ).fetchone()[0],
            )
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0197_reseed_reopened_pr_resubscription.sql":
                    con.executescript(later_migration.read_text())

            parsed = seed_skills.parse_skill(ASSETS / "sprint_dev" / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_dev'"
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

    def test_disposition_verbs_reseed_matches_asset_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0222_reseed_sprint_pln_disposition_verbs.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='no disposition verbs',"
                "is_deleted=1 WHERE name='sprint_pln'"
            )

            migration = (
                ENGINE
                / "migrations"
                / "0222_reseed_sprint_pln_disposition_verbs.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            self.assertIn(
                "sc sprint resolve-unit",
                con.execute(
                    "SELECT content FROM skills WHERE name='sprint_pln'"
                ).fetchone()[0],
            )
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if (
                    later_migration.name
                    > "0222_reseed_sprint_pln_disposition_verbs.sql"
                ):
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

    def test_engine_authored_review_handoff_reseed_is_idempotent(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0198_reseed_engine_authored_review_handoff.sql":
                    break
                con.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET description='stale',category='stale',"
                "command='stale',common=1,content='wait for duplicate wake',"
                "is_deleted=1 WHERE name='sprint_dev'"
            )

            migration = (
                ENGINE
                / "migrations"
                / "0198_reseed_engine_authored_review_handoff.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0198_reseed_engine_authored_review_handoff.sql":
                    con.executescript(later_migration.read_text())

            parsed = seed_skills.parse_skill(ASSETS / "sprint_dev" / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='sprint_dev'"
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
            self.assertIn("--intent <submit|resubmit>", row[4])
            self.assertIn(
                "do not wait for a second pr-fact wake",
                " ".join(row[4].lower().split()),
            )
        finally:
            con.close()

    def test_live_replanning_reseed_matches_assets_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0200_reseed_sprint_live_replanning.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in LIVE_REPLAN_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET description='stale',category='stale',"
                f"command='stale',common=1,content='immutable sprint plan',"
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(LIVE_REPLAN_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0200_reseed_sprint_live_replanning.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0200_reseed_sprint_live_replanning.sql":
                    con.executescript(later_migration.read_text())

            for name in sorted(LIVE_REPLAN_ROLE_SKILLS):
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
            for later_migration in sorted(
                (ENGINE / "migrations").glob("*.sql")
            ):
                if later_migration.name > "0195_reseed_sprint_progress_carriers.sql":
                    con.executescript(later_migration.read_text())

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

    def test_role_guidance_selects_and_recovers_one_conformance_owner(self):
        prep = " ".join(
            (ASSETS / "sprint_prep" / "SKILL.md").read_text().split()
        )
        planner = " ".join(
            (ASSETS / "sprint_pln" / "SKILL.md").read_text().split()
        )
        reviewer = " ".join(
            (ASSETS / "sprint_rev" / "SKILL.md").read_text().split()
        )
        close = " ".join(
            (ASSETS / "sprint_close" / "SKILL.md").read_text().split()
        )

        self.assertIn(
            "sc sprint arm --sprint <id> --conformance-reviewer-shell <shell-id>",
            prep,
        )
        self.assertIn("select exactly one participating Reviewer", prep)
        self.assertIn("Replace that owner only while paused", planner)
        self.assertIn("--conformance-reviewer-shell <replacement-shell-id>", planner)
        self.assertIn("new ownership generation", planner)
        self.assertIn("selected conformance owner", reviewer)
        self.assertIn("different Reviewer", reviewer)
        self.assertIn("owner shell and generation match", close)
        self.assertIn("Any other Reviewer records no conformance", close)

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

    def test_closeout_role_skills_keep_artifacts_local_and_db_records_durable(self):
        for name in sorted(CLOSEOUT_ROLE_SKILLS):
            with self.subTest(name=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                normalized = " ".join(body.split())
                self.assertGreaterEqual(body.count("shared/sprints/sprint-<n>/"), 1)
                self.assertIn("gitignored", normalized)
                self.assertRegex(normalized, r"[Nn]ever commit|never committed")
                self.assertIn("durable", normalized)
                self.assertIn("record-review", normalized)
                self.assertIn("sprint_reports", normalized)
                self.assertIn("relay", normalized)

    def test_close_skill_routes_to_owning_roles_and_keeps_fallback_bounded(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
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
            "recall-unit",
            "reroute-participant",
            "arm",
            "inbox",
            "spec-revision",
            "rebind-spec",
            "send",
            "accept",
            "decline",
            "complete-unit",
            "cancel-unit",
            "resolve-unit",
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
            "cleanup-status",
            "cleanup",
            "show",
        }
        combined = "\n".join(
            (ASSETS / name / "SKILL.md").read_text() for name in SKILLS
        )
        for command in expected - {"monitor", "spec-revision"}:
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

        self.assertIn("## Reviewer decisions and Planner actions", planner)
        self.assertIn(
            "planner independently owns operational plan structure",
            normalized_planner,
        )
        self.assertIn("pause-safe recall", normalized_planner)
        self.assertIn("repeated task lanes", normalized_planner)
        for command in (
            "sc sprint pause --sprint <id>",
            "sc sprint recall-unit --sprint <id>",
            "sc sprint reroute-participant --sprint <id>",
            "sc sprint cancel-unit --sprint <id>",
        ):
            self.assertIn(command, planner)
        self.assertIn("reviewer-authored final report", normalized_planner)
        self.assertIn("does not author a second report", normalized_planner)
        self.assertIn("do not run `complete`", normalized_planner)
        self.assertNotIn("you decide scope, sequencing, and recovery", normalized_planner)

        self.assertIn("## Conformance decisions and Planner controls", reviewer)
        self.assertIn(
            "planner independently owns operational plan structure",
            normalized_reviewer,
        )
        self.assertIn(
            "reviewer owns review, re-enter, abort, and conclude",
            normalized_reviewer,
        )
        self.assertIn("author the final Sprint report", reviewer)
        self.assertIn("sc sprint record-conformance", reviewer)
        self.assertIn("sc sprint compile-report", reviewer)
        self.assertIn(
            "`decision`: `re-enter`, `abort`, or the exact safety-critical recommendation",
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
            planner.index("## Reviewer decisions and Planner actions"):
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
        normalized_developer = " ".join(developer.split())
        self.assertIn(
            "Red/green/closed Re-enter wakes continue", normalized_developer
        )
        self.assertIn("armed -> fix red + judge/pass green", normalized_developer)
        self.assertIn(
            "paused -> fix red now + judge green, review after resume",
            normalized_developer,
        )
        self.assertIn(
            "no active Sprint -> fix red if needed", normalized_developer
        )
        self.assertIn("no action on green", normalized_developer)
        self.assertIn("Reviewer decides", developer)
        self.assertIn("recalling unreleased work", " ".join(reviewer.split()))
        self.assertIn("Compile the bounded evidence packet first", reviewer)
        self.assertIn("Developer-owned subscriptions", planner)

        boot = (ENGINE / "templates" / "boot.md").read_text()
        normalized_boot = " ".join(boot.split())
        self.assertIn("## ACTIVE CHAT DELIVERY", boot)
        self.assertIn("Every `wake_message` creates durable delivery intent", boot)
        self.assertIn("verified live turn", boot)
        self.assertIn("coordinate mode", boot)
        self.assertIn("reaper", boot)
        self.assertIn("including after a Sprint ends", boot)
        self.assertIn(
            "an armed Sprint, a paused Sprint, and no active Sprint",
            normalized_boot,
        )
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
            "the engine injects",
            "--intent <submit|resubmit>",
            "do not wait for a second pr-fact wake",
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
        body = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        reviewer = " ".join(body.split())
        red = reviewer[
            reviewer.index("### Red-check doctrine"):
            reviewer.index("Complete a unit verdict in this exact order")
        ]
        for guidance in (
            "Accepted-red is not a legal review outcome",
            "A departure that leaves checks failing is never acceptable",
            "record `changes_requested` so the Developer fixes them",
            "send the Planner a `replan` decision",
            "remains green-only, without exception or waiver",
            "do not note the failure and approve anyway",
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, red)
        self.assertLess(red.index("In-scope failure"), red.index("Out-of-scope failure"))
        self.assertNotIn("approve anyway", red.split("do not note", 1)[0].lower())

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
        self.assertIn("Planner may reassign or reroute for operational capacity", planner)
        for fact in ("scheduled dispatch", "unread wake recovery"):
            self.assertIn(fact, planner)
        self.assertIn("registered-PR watcher owns subscription observation", planner)
        self.assertNotIn("sc sprint monitor", bodies["sprint_pln"])
        self.assertIn(
            "After `register-pr` succeeds", bodies["sprint_dev"]
        )
        self.assertIn(
            "replay the exact `register-pr` command", bodies["sprint_dev"]
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

    def test_compact_developer_and_spec_keep_every_stateful_route(self):
        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        developer_sections = [
            "## Route the entry",
            "## Bound the lane",
            "## Build and verify",
            "## Report-only or no-code completion",
            "## Register and observe the PR",
            "## Review handoff and correction",
            "## Merge boundary",
            "## Post-merge handoff",
            "## Report and stop",
        ]
        positions = [developer.index(section) for section in developer_sections]
        self.assertEqual(positions, sorted(positions))
        for command in (
            "sc sprint complete-unit",
            "sc sprint register-pr",
            "sc sprint watcher-state",
            "sc sprint request-review",
            "sc sprint authorize-merge",
            "--intent handoff --key <stable-merged-handoff-key>",
        ):
            self.assertEqual(developer.count(command), 1)
        self.assertIn("created: false", developer)
        self.assertIn("--intent <submit|resubmit>", developer)
        self.assertNotIn("sc sprint pause --sprint <id>", developer)

        spec = SPEC_SKILL.read_text()
        parsed_spec = seed_skills.parse_skill(SPEC_SKILL)
        self.assertEqual(
            parsed_spec["description"],
            "Load before implementing any feature, spec, or roadmap item. "
            "Analyze viability, surface blockers, plan Preparation → implementation "
            "→ Verification, and track spec_tasks/current_state across sessions.",
        )
        spec_sections = [
            "## 1. Select the spec",
            "## 2. Analyze before planning",
            "## 3. Engage and plan",
            "## 4. Execute one task at a time",
            "## 5. Ship and hand docs to Planner",
            "## Scope change and stop rules",
        ]
        positions = [spec.index(section) for section in spec_sections]
        self.assertEqual(positions, sorted(positions))
        for command in (
            "sc mem get documents --feature <id>",
            "sc mem get documents --doc <doc_id>",
            "sc mem get tasks --doc <doc_id>",
            "sc mem task add \"Preparation\"",
            "sc mem task add \"Verification\"",
            "sc mem task start <task_id>",
            "sc mem task done <task_id>",
            "sc mem task cancel <task_id>",
            "sc mem roadmap status <feature_id> shipped",
        ):
            self.assertIn(command, spec)
        normalized = " ".join(spec.split())
        for invariant in (
            "Current Posture",
            "In Scope",
            "Out of Scope",
            "Anticipated User Activity",
            "tenancy",
            "No task plan = no implementation",
            "Do not freeze or author the shipped doc as Developer",
        ):
            self.assertIn(invariant, normalized)
        self.assertLess(spec.index("sc mem task start"), spec.index("sc mem task done"))
        self.assertNotIn("sc mem doc freeze", spec)

    def test_compact_planner_keeps_control_and_replan_routes(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        sections = [
            "## Route the entry",
            "## Durable running loop",
            "## Relay contract",
            "## Reviewer decisions and Planner actions",
            "### Pause or resume",
            "### Modify, recall, repeat, reassign, or reroute",
            "### Re-enter after conformance",
            "### Conclude or abort",
            "## Handoffs and stop",
        ]
        positions = [planner.index(section) for section in sections]
        self.assertEqual(positions, sorted(positions))
        for command in (
            "sc sprint watcher-state --sprint <id>",
            "sc sprint reconcile-pr",
            "sc sprint cancel-unit",
            "sc sprint replan-unit",
            "sc sprint recall-unit",
            "sc sprint reroute-participant",
            "sc mem task add",
            "sc sprint plan-unit",
        ):
            self.assertIn(command, planner)
        recall_route = planner[
            planner.index("Never edit a released lane in place"):
            planner.index("Recall preserves message/event history")
        ]
        ordered = [
            recall_route.index(command)
            for command in (
                "sc sprint pause",
                "sc sprint recall-unit",
                "sc sprint replan-unit",
                "sc sprint resume",
            )
        ]
        self.assertEqual(ordered, sorted(ordered))
        self.assertNotIn("sc sprint monitor", planner)

    def test_context_efficient_skills_meet_budget_without_auxiliary_resources(self):
        paths = [ASSETS / name / "SKILL.md" for name in CONTEXT_EFFICIENT_SKILLS]
        sizes = {path.parent.name: len(path.read_bytes()) for path in paths}

        self.assertLessEqual(
            sum(sizes.values()),
            CONTEXT_EFFICIENT_SKILL_BYTE_CEILING,
        )
        for path in paths:
            with self.subTest(skill=path.parent.name):
                self.assertEqual(
                    sorted(item.name for item in path.parent.iterdir()),
                    ["SKILL.md"],
                )

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
            planner.index("On an initial clean completion receipt")
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
            "when it confirms completed state, pending cleanup, and all receipt "
            "identities, stop immediately",
            normalized_stop,
        )
        self.assertIn("run no trailing command", normalized_stop)
