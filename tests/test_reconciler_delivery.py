#!/usr/bin/env python3
"""Worker-reconciliation alert delivery (spec 58, U5)."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_broker  # noqa: E402
import pr_poller  # noqa: E402

NOW = datetime(2020, 1, 1, tzinfo=timezone.utc)


def build_db(skip: set[str] | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if skip and migration.name in skip:
            continue
        con.executescript(migration.read_text())
    con.executescript(
        """
        INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);
        INSERT INTO shells
          (shell_id, display_name, shortname, flavor, system_prompt, user_id)
        VALUES
          (1, 'Planner 1', 'PLN1', 'planner', 'x', 1),
          (2, 'Developer 1', 'DEV1', 'dev', 'x', 1),
          (3, 'Planner 2', 'PLN2', 'planner', 'x', 1),
          (4, 'Developer 2', 'DEV2', 'dev', 'x', 1);
        """
    )
    return con


def add_sprint(
    con: sqlite3.Connection,
    doc_id: int,
    seq: str,
    *,
    state: str = "working",
) -> sqlite3.Row:
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body) "
        "VALUES (?, 'doc', ?, 'status: ACTIVE')",
        (doc_id, f"SPRINT: {doc_id}"),
    )
    con.execute(
        "INSERT INTO sprint_units "
        "(sprint_doc_id, seq, unit_title, state, dev_shell_id) "
        "VALUES (?, ?, 'delivery', ?, 2)",
        (doc_id, seq, state),
    )
    con.commit()
    return con.execute(
        "SELECT * FROM sprint_units WHERE sprint_doc_id=? AND seq=?",
        (doc_id, seq),
    ).fetchone()


def add_binding(
    con: sqlite3.Connection,
    doc_id: int,
    planner: int,
    generation: int = 1,
) -> tuple[int, int]:
    con.execute(
        "INSERT INTO interface_generations (shell_id, generation) VALUES (?,?)",
        (planner, generation),
    )
    session_id = con.execute(
        "INSERT INTO interface_sessions (shell_id, generation) VALUES (?,?)",
        (planner, generation),
    ).lastrowid
    binding_id = con.execute(
        "INSERT INTO sprint_planner_bindings "
        "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation) "
        "VALUES (?,?,?,?,?)",
        (doc_id, planner, session_id, planner, generation),
    ).lastrowid
    con.commit()
    return session_id, binding_id


class ReconcilerDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.addCleanup(self.con.close)
        self.unit = add_sprint(self.con, 59, "U5")
        self.session_id, self.binding_id = add_binding(self.con, 59, 1)

    def expectation(
        self,
        *,
        doc_id: int = 59,
        seq: str = "U5",
        unit: sqlite3.Row | None = None,
        role: str = "dev",
        shell_id: int = 2,
    ) -> pr_poller.Expectation:
        unit = unit if unit is not None else self.unit
        shell = self.con.execute(
            "SELECT * FROM shells WHERE shell_id=?",
            (shell_id,),
        ).fetchone()
        return pr_poller.Expectation(
            sprint_doc_id=doc_id,
            unit_id=unit["unit_id"],
            seq=seq,
            role=role,
            shell_id=shell_id,
            shell=shell,
            unit=unit,
        )

    def planner_expectation(
        self,
        *,
        doc_id: int = 59,
        shell_id: int = 1,
    ) -> pr_poller.Expectation:
        shell = self.con.execute(
            "SELECT * FROM shells WHERE shell_id=?",
            (shell_id,),
        ).fetchone()
        return pr_poller.Expectation(
            sprint_doc_id=doc_id,
            unit_id=None,
            seq=None,
            role="planner",
            shell_id=shell_id,
            shell=shell,
            unit=None,
        )

    def reading(
        self,
        signal: str,
        *,
        confirmed: bool = True,
        minute: int = 0,
        expectation: pr_poller.Expectation | None = None,
        explanation: str | None = None,
    ) -> pr_poller.ReconciliationReading:
        return pr_poller.ReconciliationReading(
            expectation=expectation or self.expectation(),
            signal=signal,
            confirmed=confirmed,
            # A sentinel with no Evidence fields: any U5 reach-through raises.
            evidence=object(),
            measurement={"count": 3},
            observed_at=NOW + timedelta(minutes=minute),
            explanation=explanation,
        )

    def test_confirmed_actionable_reading_writes_exact_alert_message_and_wake(self):
        before_board = tuple(self.con.execute(
            "SELECT * FROM sprint_units ORDER BY unit_id"
        ).fetchall())

        emitted = pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("checkup")],
        )

        self.assertEqual(1, len(emitted))
        alert = self.con.execute(
            "SELECT sprint_doc_id, unit_id, role, signal, shell_id, severity, "
            "reason, opened_at, resolved_at, message_id "
            "FROM planner_alerts"
        ).fetchone()
        self.assertEqual(
            (
                59,
                self.unit["unit_id"],
                "dev",
                "checkup",
                2,
                "warning",
                "worker_checkup",
                NOW.isoformat(),
                None,
                emitted[0],
            ),
            tuple(alert),
        )
        message = self.con.execute(
            "SELECT from_shell_id, to_shell_id, kind, sprint_doc_id, body "
            "FROM shell_messages"
        ).fetchone()
        self.assertEqual((1, 1, "pr_event", 59), tuple(message)[:4])
        self.assertEqual(
            "reconciler unit=U5 role=dev shell=DEV1 signal=checkup "
            'measurement={"count":3} '
            f"observed_at={NOW.isoformat()}",
            message["body"],
        )
        self.assertEqual(
            [(self.binding_id, emitted[0])],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT binding_id, message_id FROM planner_wake_items"
                )
            ],
        )
        self.assertEqual(
            before_board,
            tuple(self.con.execute(
                "SELECT * FROM sprint_units ORDER BY unit_id"
            ).fetchall()),
        )

    def test_unconfirmed_actionable_reading_writes_nothing(self):
        emitted = pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("checkup", confirmed=False)],
        )
        self.assertEqual([], emitted)
        self.assertEqual(
            (0, 0, 0),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM planner_alerts"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM shell_messages"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM planner_wake_items"
                ).fetchone()[0],
            ),
        )

    def test_unconfirmed_indeterminate_reading_writes_nothing(self):
        emitted = pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("indeterminate", confirmed=False)],
        )
        self.assertEqual([], emitted)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM planner_alerts"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM shell_messages"
            ).fetchone()[0],
        )

    def test_emitted_severity_map_and_explanation_are_exact(self):
        readings = [
            self.reading("checkup", explanation="quota resets at 01:00Z"),
            self.reading("not_started"),
            self.reading("work_complete_unreported"),
        ]
        emitted = pr_poller.deliver_reconciliation_readings(self.con, readings)
        self.assertEqual(3, len(emitted))
        self.assertEqual(
            [
                ("checkup", "warning"),
                ("not_started", "warning"),
                ("work_complete_unreported", "info"),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT signal, severity FROM planner_alerts "
                    "ORDER BY signal"
                )
            ],
        )
        body = self.con.execute(
            "SELECT body FROM shell_messages "
            "WHERE body LIKE '%signal=checkup%'"
        ).fetchone()[0]
        self.assertIn(
            'explanation="quota resets at 01:00Z"',
            body,
        )

    def test_severity_contract_has_one_documented_producerless_mapping(self):
        self.assertEqual(
            pr_poller.ReconcilerState.ACTIONABLE | {"recovery_blocked"},
            set(pr_poller.RECONCILER_SEVERITY),
        )
        self.assertEqual(
            "critical",
            pr_poller.RECONCILER_SEVERITY["recovery_blocked"],
        )
        self.assertNotIn(
            "recovery_blocked",
            pr_poller.ReconcilerState.ACTIONABLE,
        )

    def test_open_dedupe_healthy_resolve_and_rearm_are_one_lifecycle(self):
        first = self.reading("checkup", minute=0)
        first_ids = pr_poller.deliver_reconciliation_readings(
            self.con,
            [first],
        )
        replay_ids = pr_poller.deliver_reconciliation_readings(
            self.con,
            [first],
        )
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("working", confirmed=False, minute=1)],
        )
        rearmed_ids = pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("checkup", minute=2)],
        )

        self.assertEqual([], replay_ids)
        self.assertEqual(1, len(first_ids))
        self.assertEqual(1, len(rearmed_ids))
        self.assertNotEqual(first_ids[0], rearmed_ids[0])
        alerts = self.con.execute(
            "SELECT opened_at, resolved_at, message_id "
            "FROM planner_alerts WHERE signal='checkup' ORDER BY alert_id"
        ).fetchall()
        self.assertEqual(
            [
                (
                    NOW.isoformat(),
                    (NOW + timedelta(minutes=1)).isoformat(),
                    first_ids[0],
                ),
                (
                    (NOW + timedelta(minutes=2)).isoformat(),
                    None,
                    rearmed_ids[0],
                ),
            ],
            [tuple(row) for row in alerts],
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM shell_messages"
            ).fetchone()[0],
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM planner_wake_items"
            ).fetchone()[0],
        )

    def test_reported_is_also_a_healthy_auto_resolve_signal(self):
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("not_started")],
        )
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("reported", confirmed=False, minute=1)],
        )
        self.assertEqual(
            (NOW + timedelta(minutes=1)).isoformat(),
            self.con.execute(
                "SELECT resolved_at FROM planner_alerts "
                "WHERE signal='not_started'"
            ).fetchone()[0],
        )

    def test_unitless_planner_alert_resolves_on_recovery(self):
        expectation = self.planner_expectation()
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [self.reading("checkup", expectation=expectation)],
        )
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading(
                    "working",
                    confirmed=False,
                    minute=1,
                    expectation=expectation,
                )
            ],
        )

        self.assertEqual(
            (NOW + timedelta(minutes=1)).isoformat(),
            self.con.execute(
                "SELECT resolved_at FROM planner_alerts "
                "WHERE sprint_doc_id=59 AND unit_id IS NULL "
                "AND role='planner' AND signal='checkup'"
            ).fetchone()[0],
        )

    def test_one_role_recovery_does_not_resolve_the_other_role(self):
        dev = self.expectation(role="dev", shell_id=2)
        reviewer = self.expectation(role="reviewer", shell_id=4)
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading("checkup", expectation=dev),
                self.reading("checkup", expectation=reviewer),
            ],
        )
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading(
                    "working",
                    confirmed=False,
                    minute=1,
                    expectation=dev,
                )
            ],
        )

        self.assertEqual(
            [
                ("dev", (NOW + timedelta(minutes=1)).isoformat()),
                ("reviewer", None),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT role, resolved_at FROM planner_alerts "
                    "WHERE sprint_doc_id=59 AND unit_id=? "
                    "AND signal='checkup' ORDER BY role",
                    (self.unit["unit_id"],),
                )
            ],
        )

    def test_planner_recovery_does_not_resolve_another_sprint(self):
        add_sprint(self.con, 60, "U6")
        add_binding(self.con, 60, 3)
        sprint_59 = self.planner_expectation()
        sprint_60 = self.planner_expectation(doc_id=60, shell_id=3)
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading("checkup", expectation=sprint_59),
                self.reading("checkup", expectation=sprint_60),
            ],
        )
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading(
                    "working",
                    confirmed=False,
                    minute=1,
                    expectation=sprint_59,
                )
            ],
        )

        self.assertIsNone(
            self.con.execute(
                "SELECT resolved_at FROM planner_alerts "
                "WHERE sprint_doc_id=60 AND unit_id IS NULL "
                "AND role='planner' AND signal='checkup'"
            ).fetchone()[0]
        )

    def test_unitless_expectation_dedupes_on_an_explicit_key_sentinel(self):
        expectation = self.planner_expectation()
        reading = self.reading("checkup", expectation=expectation)

        first = pr_poller.deliver_reconciliation_readings(
            self.con,
            [reading],
        )
        replay = pr_poller.deliver_reconciliation_readings(
            self.con,
            [reading],
        )

        self.assertEqual(1, len(first))
        self.assertEqual([], replay)
        row = self.con.execute(
            "SELECT unit_id, dedupe_key, shell_id FROM planner_alerts "
            "WHERE role='planner'"
        ).fetchone()
        self.assertEqual(
            (None, "reconciler|59|-|planner|checkup", 1),
            tuple(row),
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM shell_messages "
                "WHERE sprint_doc_id=59 AND body LIKE '%role=planner%'"
            ).fetchone()[0],
        )

    def test_role_reassignment_does_not_change_the_open_alert_key(self):
        first = self.reading(
            "checkup",
            expectation=self.expectation(shell_id=2),
        )
        reassigned = self.reading(
            "checkup",
            expectation=self.expectation(shell_id=4),
        )

        first_ids = pr_poller.deliver_reconciliation_readings(
            self.con,
            [first],
        )
        reassigned_ids = pr_poller.deliver_reconciliation_readings(
            self.con,
            [reassigned],
        )

        self.assertEqual(1, len(first_ids))
        self.assertEqual([], reassigned_ids)
        rows = self.con.execute(
            "SELECT unit_id, role, signal, shell_id FROM planner_alerts "
            "WHERE signal='checkup'"
        ).fetchall()
        self.assertEqual(
            [(self.unit["unit_id"], "dev", "checkup", 4)],
            [tuple(row) for row in rows],
        )

    def test_never_bound_records_finding_and_condition_without_push(self):
        unit = add_sprint(self.con, 60, "U6")
        expectation = self.expectation(doc_id=60, seq="U6", unit=unit)
        finding = self.reading("checkup", expectation=expectation)

        first = pr_poller.deliver_reconciliation_readings(
            self.con,
            [finding],
        )
        replay = pr_poller.deliver_reconciliation_readings(
            self.con,
            [finding],
        )

        self.assertEqual([], first)
        self.assertEqual([], replay)
        self.assertEqual(
            [
                ("reconciler_missing_binding", None, "warning"),
                ("worker_checkup", "checkup", "warning"),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT reason, signal, severity FROM planner_alerts "
                    "WHERE sprint_doc_id=60 ORDER BY reason"
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM shell_messages WHERE sprint_doc_id=60"
            ).fetchone()[0],
        )

        add_binding(self.con, 60, 3)
        pr_poller.deliver_reconciliation_readings(
            self.con,
            [
                self.reading(
                    "indeterminate",
                    confirmed=False,
                    minute=1,
                    expectation=expectation,
                )
            ],
        )
        rows = self.con.execute(
            "SELECT reason, resolved_at FROM planner_alerts "
            "WHERE sprint_doc_id=60 ORDER BY reason"
        ).fetchall()
        self.assertEqual(
            [
                (
                    "reconciler_missing_binding",
                    (NOW + timedelta(minutes=1)).isoformat(),
                ),
                ("worker_checkup", None),
            ],
            [tuple(row) for row in rows],
        )

    def test_bound_and_never_bound_sprints_are_isolated_in_one_completed_tick(self):
        cases = (
            ("bound-first", 59, 60, 1),
            ("unbound-first", 62, 61, 3),
        )
        for label, bound_doc_id, unbound_doc_id, planner_id in cases:
            with self.subTest(order=label):
                if bound_doc_id == 59:
                    bound_unit = self.unit
                else:
                    bound_unit = add_sprint(
                        self.con,
                        bound_doc_id,
                        f"U{bound_doc_id}",
                    )
                    add_binding(self.con, bound_doc_id, planner_id)
                unbound_unit = add_sprint(
                    self.con,
                    unbound_doc_id,
                    f"U{unbound_doc_id}",
                )
                bound = self.reading(
                    "checkup",
                    expectation=self.expectation(
                        doc_id=bound_doc_id,
                        seq=f"U{bound_doc_id}",
                        unit=bound_unit,
                    ),
                )
                unbound = self.reading(
                    "checkup",
                    expectation=self.expectation(
                        doc_id=unbound_doc_id,
                        seq=f"U{unbound_doc_id}",
                        unit=unbound_unit,
                    ),
                )
                readings = sorted(
                    (bound, unbound),
                    key=lambda item: item.expectation.sprint_doc_id,
                )
                before_board = tuple(self.con.execute(
                    "SELECT * FROM sprint_units ORDER BY unit_id"
                ).fetchall())

                emitted = pr_poller.deliver_reconciliation_readings(
                    self.con,
                    readings,
                )
                pr_poller.beat(self.con, 600, name="reconcile")

                self.assertEqual(1, len(emitted))
                self.assertEqual(
                    [(bound_doc_id, 1), (unbound_doc_id, 0)],
                    [
                        (
                            doc_id,
                            self.con.execute(
                                "SELECT COUNT(*) FROM shell_messages "
                                "WHERE sprint_doc_id=?",
                                (doc_id,),
                            ).fetchone()[0],
                        )
                        for doc_id in (bound_doc_id, unbound_doc_id)
                    ],
                )
                self.assertEqual(
                    1,
                    self.con.execute(
                        "SELECT COUNT(*) FROM planner_alerts "
                        "WHERE sprint_doc_id=? AND signal='checkup'",
                        (bound_doc_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    self.con.execute(
                        "SELECT COUNT(*) FROM planner_alerts "
                        "WHERE sprint_doc_id=?",
                        (unbound_doc_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    ("reconcile", 600),
                    tuple(self.con.execute(
                        "SELECT name, interval_s FROM daemon_heartbeats "
                        "WHERE name='reconcile'"
                    ).fetchone()),
                )
                self.assertEqual(
                    before_board,
                    tuple(self.con.execute(
                        "SELECT * FROM sprint_units ORDER BY unit_id"
                    ).fetchall()),
                )

    def test_legacy_interface_alert_opens_dedupes_resolves_with_null_new_columns(self):
        interface_broker._alert(
            self.con,
            severity="critical",
            reason="turn_failure",
            session_id=self.session_id,
        )
        interface_broker._alert(
            self.con,
            severity="critical",
            reason="turn_failure",
            session_id=self.session_id,
        )
        self.con.commit()
        row = self.con.execute(
            "SELECT sprint_doc_id, unit_id, role, signal, shell_id, resolved_at "
            "FROM planner_alerts WHERE reason='turn_failure'"
        ).fetchone()
        self.assertEqual((None, None, None, None, None, None), tuple(row))

        interface_broker.close_session(
            self.con,
            self.session_id,
            "test_complete",
        )
        self.con.commit()
        rows = self.con.execute(
            "SELECT resolved_at FROM planner_alerts "
            "WHERE reason='turn_failure'"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertIsNotNone(rows[0][0])

    def test_migration_expands_a_dirty_legacy_alert_table_without_rewriting_rows(self):
        migration_name = "0102_reconciler_alert_keys.sql"
        legacy = build_db(skip={migration_name})
        self.addCleanup(legacy.close)
        interface_broker._alert(
            legacy,
            severity="warning",
            reason="legacy_open",
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
        interface_broker._alert(
            legacy,
            severity="warning",
            reason="legacy_open",
        )
        self.assertEqual(
            1,
            legacy.execute(
                "SELECT COUNT(*) FROM planner_alerts "
                "WHERE reason='legacy_open'"
            ).fetchone()[0],
        )
        legacy.execute(
            "UPDATE planner_alerts SET resolved_at=datetime('now') "
            "WHERE reason='legacy_open'"
        )
        interface_broker._alert(
            legacy,
            severity="warning",
            reason="legacy_open",
        )
        rows = legacy.execute(
            "SELECT resolved_at, sprint_doc_id, unit_id, role, signal, shell_id "
            "FROM planner_alerts WHERE reason='legacy_open' ORDER BY alert_id"
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertIsNotNone(rows[0]["resolved_at"])
        self.assertEqual(
            (None, None, None, None, None, None),
            tuple(rows[1]),
        )


if __name__ == "__main__":
    unittest.main()
