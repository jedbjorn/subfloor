"""Conductor Step 3 retirement of binding-addressed reconciliation delivery."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import pr_poller  # noqa: E402

NOW = datetime(2020, 1, 1, tzinfo=timezone.utc)


def build_db(skip: set[str] | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if not skip or migration.name not in skip:
            con.executescript(migration.read_text())
    con.executescript(
        """
        INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);
        INSERT INTO shells
          (shell_id, display_name, shortname, flavor, system_prompt, user_id)
        VALUES
          (1, 'Planner 1', 'PLN1', 'planner', 'x', 1),
          (2, 'Developer 1', 'DEV1', 'dev', 'x', 1);
        INSERT INTO documents
          (document_id, kind, title, body)
        VALUES
          (59, 'doc', 'SPRINT: 59', 'status: ACTIVE');
        INSERT INTO sprint_units
          (sprint_doc_id, seq, unit_title, state, dev_shell_id)
        VALUES
          (59, 'U1', 'delivery', 'working', 2);
        """
    )
    return con


class ReconcilerDecouplingTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.addCleanup(self.con.close)
        self.unit = self.con.execute(
            "SELECT * FROM sprint_units WHERE sprint_doc_id=59 AND seq='U1'"
        ).fetchone()

    def test_readings_do_not_write_the_retired_alert_or_binding_message_path(self):
        shell = self.con.execute(
            "SELECT * FROM shells WHERE shell_id=2"
        ).fetchone()
        expectation = pr_poller.Expectation(
            sprint_doc_id=59,
            unit_id=self.unit["unit_id"],
            seq="U1",
            role="dev",
            shell_id=2,
            shell=shell,
            unit=self.unit,
        )
        reading = pr_poller.ReconciliationReading(
            expectation=expectation,
            signal="checkup",
            confirmed=True,
            evidence=object(),
            measurement={"status": "quiet"},
            observed_at=NOW,
            explanation=None,
        )
        before_board = tuple(self.con.execute(
            "SELECT * FROM sprint_units ORDER BY unit_id"
        ).fetchall())

        self.assertEqual(
            [],
            pr_poller.deliver_reconciliation_readings(self.con, [reading]),
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM planner_alerts").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM shell_messages").fetchone()[0],
        )
        self.assertEqual(
            before_board,
            tuple(self.con.execute(
                "SELECT * FROM sprint_units ORDER BY unit_id"
            ).fetchall()),
        )

    def test_dirty_legacy_alert_migration_preserves_the_row(self):
        migration_name = "0102_reconciler_alert_keys.sql"
        legacy = build_db(skip={migration_name})
        self.addCleanup(legacy.close)
        legacy.execute(
            "INSERT INTO planner_alerts (severity, reason, dedupe_key) "
            "VALUES ('warning','legacy_open','-|-|-|legacy_open')"
        )
        legacy.commit()
        before = legacy.execute(
            "SELECT alert_id, severity, reason, dedupe_key, resolved_at "
            "FROM planner_alerts"
        ).fetchone()

        legacy.executescript((MIGRATIONS / migration_name).read_text())

        after = legacy.execute(
            "SELECT alert_id, severity, reason, dedupe_key, resolved_at, "
            "sprint_doc_id, unit_id, role, signal, shell_id "
            "FROM planner_alerts"
        ).fetchone()
        self.assertEqual(
            tuple(before) + (None, None, None, None, None),
            tuple(after),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
