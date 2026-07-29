"""Fresh-install and update reconciliation for the singleton Conductor."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import init_fork  # noqa: E402
import shell_factory  # noqa: E402
import update  # noqa: E402

RECONCILE_MIGRATION = MIGRATIONS / "0130_conductor_reconciliation.sql"


def build_db(
    path: Path, *, through_0129: bool = False
) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through_0129 and migration.name >= "0130_":
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def assert_conductor_contract(
    case: unittest.TestCase, con: sqlite3.Connection
) -> None:
    rows = con.execute(
        "SELECT shell_id,shortname,flavor,lineage_seed,has_identity,api_key "
        "FROM shells WHERE flavor='conductor' AND is_deleted=0"
    ).fetchall()
    case.assertEqual(len(rows), 1)
    row = rows[0]
    case.assertEqual(row["shortname"], "CON1")
    case.assertIsNone(row["lineage_seed"])
    case.assertEqual(row["has_identity"], 0)
    case.assertTrue(row["api_key"])
    case.assertEqual(
        con.execute(
            "SELECT COUNT(*) FROM shell_identity_entries WHERE shell_id=?",
            (row["shell_id"],),
        ).fetchone()[0],
        0,
    )
    case.assertEqual(
        {
            item[0]
            for item in con.execute(
                "SELECT s.name FROM flavor_skills fs "
                "JOIN skills s ON s.skill_id=fs.skill_id "
                "WHERE fs.flavor='conductor' AND s.is_deleted=0"
            )
        },
        {"sprint_cond"},
    )
    case.assertEqual(
        con.execute(
            "SELECT COUNT(*) FROM shell_skills WHERE shell_id=?",
            (row["shell_id"],),
        ).fetchone()[0],
        0,
    )
    case.assertEqual(
        tuple(con.execute(
            "SELECT harness,model FROM flavor_defaults "
            "WHERE flavor='conductor' AND is_default=1"
        ).fetchone()),
        ("opencode", "openai/gpt-5.6-luna"),
    )


class ConductorReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_conductor_reconcile_")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "shell.db"
        self.con = build_db(self.db_path)
        self.addCleanup(self.con.close)
        self.con.execute(
            "INSERT INTO users (user_id,username,is_active) VALUES (1,'Jed',1)"
        )
        self.con.commit()

    def close_fixture_connection(self) -> None:
        self.con.close()

    def test_pre_conductor_update_creates_one_and_rerun_is_idempotent(self) -> None:
        shell_factory.create_shell(
            self.con, flavor="planner", name="Planner",
            shortname="PLN1", partner="Jed",
        )
        self.con.commit()
        self.close_fixture_connection()

        with mock.patch.object(update, "DB_PATH", self.db_path):
            self.assertTrue(update.reconcile_conductor())
            self.assertFalse(update.reconcile_conductor())

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        assert_conductor_contract(self, con)

    def test_regrant_cleans_polluted_opt_out_pack(self) -> None:
        shell_factory.reconcile_conductor(self.con, partner="Jed")
        self.con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor,skill_id) "
            "SELECT 'conductor',skill_id FROM skills WHERE common=1"
        )
        self.con.commit()
        self.close_fixture_connection()

        with mock.patch.object(update, "DB_PATH", self.db_path):
            update.regrant()

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        assert_conductor_contract(self, con)

    def test_duplicate_live_conductors_are_refused(self) -> None:
        shell_factory.create_shell(
            self.con, flavor="conductor", name="Conductor", shortname="CON1"
        )
        # Simulate ambiguous legacy state from before the singleton trigger.
        self.con.execute("DROP TRIGGER trg_singleton_conductor")
        shell_factory.create_shell(
            self.con, flavor="conductor", name="Conductor 2", shortname="CON2"
        )
        with self.assertRaisesRegex(ValueError, "multiple live conductor"):
            shell_factory.reconcile_conductor(self.con, partner="Jed")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM shells "
                "WHERE flavor='conductor' AND is_deleted=0"
            ).fetchone()[0],
            2,
        )

    def test_migration_provisions_once_and_blocks_old_common_regrant(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_conductor_migration_")
        self.addCleanup(temporary.cleanup)
        db_path = Path(temporary.name) / "pre-0130.db"
        con = build_db(db_path, through_0129=True)
        self.addCleanup(con.close)
        con.execute(
            "INSERT INTO users (user_id,username,is_active) VALUES (1,'Jed',1)"
        )
        con.execute(
            "INSERT INTO shells "
            "(display_name,shortname,system_prompt,flavor,user_id,api_key) "
            "VALUES ('Planner','PLN1','prompt','planner',1,'planner-key')"
        )
        con.commit()

        con.executescript(RECONCILE_MIGRATION.read_text())
        con.executescript(RECONCILE_MIGRATION.read_text())
        # This is the old update.py behavior that runs after newly materialized
        # migrations in the same process. The trigger must keep it harmless.
        con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor,skill_id) "
            "SELECT 'conductor',skill_id FROM skills "
            "WHERE is_deleted=0 AND common=1"
        )
        con.commit()

        assert_conductor_contract(self, con)
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM shells "
                "WHERE flavor='conductor' AND is_deleted=0"
            ).fetchone()[0],
            1,
        )


class FreshInstallConductorTest(unittest.TestCase):
    def test_fresh_init_provisions_role_only_con1(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_conductor_fresh_")
        self.addCleanup(temporary.cleanup)
        db_path = Path(temporary.name) / "shell.db"
        con = build_db(db_path)
        con.close()

        with (
            mock.patch.object(init_fork, "DB_PATH", db_path),
            mock.patch.object(
                init_fork.install_mod, "seed_visual_qa_files", return_value=[]
            ),
        ):
            self.assertEqual(init_fork.main(["--username", "Jed"]), 0)

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        assert_conductor_contract(self, con)


if __name__ == "__main__":
    unittest.main()
