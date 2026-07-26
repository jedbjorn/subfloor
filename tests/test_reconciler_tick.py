#!/usr/bin/env python3
"""Worker-expectation classification and supervised tick (spec 58, U4)."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import activity_readers as ar  # noqa: E402
import pr_poller  # noqa: E402
import sprint_units  # noqa: E402

UTC = timezone.utc
NOW = datetime(2020, 1, 1, 1, 0, tzinfo=UTC)
EPOCH = NOW - timedelta(minutes=40)
STATE_CLOCK = NOW - timedelta(minutes=35)


def evidence(**changes) -> ar.Evidence:
    values = {
        "epoch": EPOCH,
        "state_changed_at": STATE_CLOCK,
        "last_result_row_at": None,
        "last_work_at": None,
        "last_durable_write_at": None,
        "session_ended_at": None,
        "process_present": True,
        "edits_code": True,
        "branch_declared": "feat/test",
        "branch_present": True,
    }
    values.update(changes)
    return ar.Evidence(**values)


def build_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    con.executescript(
        """
        INSERT INTO users (user_id, username) VALUES (1, 'T');
        INSERT INTO shells
          (shell_id, display_name, shortname, flavor, system_prompt, user_id)
        VALUES
          (1, 'Dev', 'DEV1', 'dev', 'x', 1),
          (2, 'Reviewer', 'REV1', 'reviewer', 'x', 1),
          (3, 'Planner', 'PLN1', 'planner', 'x', 1),
          (4, 'Planner 2', 'PLN2', 'planner', 'x', 1);
        """
    )
    return con


def add_unit(
    con: sqlite3.Connection,
    *,
    doc_id: int = 59,
    unit_id: int = 1,
    seq: str = "U4",
    state: str = "working",
    dev: int | None = 1,
    reviewer: int | None = 2,
    branch: str | None = "feat/test",
    frozen: int = 0,
    body: str = "status: CLOSED",
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO documents "
        "(document_id, kind, title, body, frozen) VALUES (?, 'doc', ?, ?, ?)",
        (doc_id, f"SPRINT: {doc_id}", body, frozen),
    )
    con.execute(
        "INSERT INTO sprint_units "
        "(unit_id, sprint_doc_id, seq, unit_title, state, dev_shell_id, "
        " reviewer_shell_id, branch, state_changed_at) "
        "VALUES (?, ?, ?, 'tick', ?, ?, ?, ?, ?)",
        (
            unit_id,
            doc_id,
            seq,
            state,
            dev,
            reviewer,
            branch,
            STATE_CLOCK.isoformat(),
        ),
    )
    con.commit()


def add_binding(
    con: sqlite3.Connection,
    *,
    doc_id: int = 59,
    planner: int = 3,
    generation: int = 1,
    released_at: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO interface_generations (shell_id, generation) VALUES (?, ?)",
        (planner, generation),
    )
    session_id = con.execute(
        "INSERT INTO interface_sessions (shell_id, generation) VALUES (?, ?)",
        (planner, generation),
    ).lastrowid
    con.execute(
        "INSERT INTO sprint_planner_bindings "
        "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation, "
        " released_at) VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, planner, session_id, planner, generation, released_at),
    )
    con.commit()


class ClassificationPrecedenceTest(unittest.TestCase):
    def test_unreadable_result_precedes_every_positive_signal(self):
        item = evidence(
            last_result_row_at=NOW,
            last_work_at=NOW,
            unreadable=["result_row"],
        )
        self.assertEqual("indeterminate", pr_poller.classify(item, NOW))

    def test_reported_outranks_recent_work(self):
        item = evidence(last_result_row_at=NOW, last_work_at=NOW)
        self.assertEqual("reported", pr_poller.classify(item, NOW))

    def test_result_before_the_state_clock_does_not_close_the_window(self):
        item = evidence(
            last_result_row_at=STATE_CLOCK - timedelta(seconds=1),
            last_work_at=NOW,
        )
        self.assertEqual("working", pr_poller.classify(item, NOW))

    def test_recent_work_event_is_working_but_dirty_alone_never_is(self):
        self.assertEqual(
            "working",
            pr_poller.classify(
                evidence(last_work_at=NOW - timedelta(minutes=1), dirty=True),
                NOW,
            ),
        )
        self.assertEqual(
            "checkup",
            pr_poller.classify(evidence(last_work_at=None, dirty=True), NOW),
        )

    def test_untimed_delete_or_rename_falls_safe_to_checkup(self):
        item = evidence(
            dirty=True,
            unreadable=[ar.UNTIMED_DELETE_RENAME],
        )
        self.assertEqual("checkup", pr_poller.classify(item, NOW))

    def test_live_session_with_durable_write_is_not_complete(self):
        durable = EPOCH + timedelta(minutes=1)
        self.assertEqual(
            "checkup",
            pr_poller.classify(
                evidence(
                    process_present=True,
                    session_ended_at=None,
                    last_durable_write_at=durable,
                ),
                NOW,
            ),
        )

    def test_session_over_has_two_routes_and_uses_the_boot_clock(self):
        durable = EPOCH + timedelta(minutes=1)
        self.assertEqual(
            "work_complete_unreported",
            pr_poller.classify(
                evidence(
                    session_ended_at=NOW - timedelta(minutes=1),
                    last_durable_write_at=durable,
                ),
                NOW,
            ),
        )
        self.assertEqual(
            "work_complete_unreported",
            pr_poller.classify(
                evidence(
                    process_present=False,
                    last_durable_write_at=durable,
                ),
                NOW,
            ),
        )
        self.assertEqual(
            "checkup",
            pr_poller.classify(
                evidence(
                    process_present=False,
                    last_durable_write_at=EPOCH - timedelta(seconds=1),
                ),
                NOW,
            ),
        )

    def test_declared_missing_branch_uses_the_boot_grace(self):
        self.assertEqual(
            "not_started",
            pr_poller.classify(evidence(branch_present=False), NOW),
        )
        inside_grace = EPOCH + timedelta(minutes=19)
        self.assertEqual(
            "working",
            pr_poller.classify(evidence(branch_present=False), inside_grace),
        )

    def test_ledger_done_dirty_tree_stays_work_then_becomes_checkup(self):
        # The task ledger is deliberately absent from Evidence.  These are the
        # exact other three legs of the observed adversarial state.
        item = evidence(
            dirty=True,
            commits_since_epoch=0,
            last_work_at=NOW - timedelta(minutes=1),
        )
        self.assertEqual("working", pr_poller.classify(item, NOW))
        self.assertEqual(
            "checkup",
            pr_poller.classify(item, NOW + timedelta(minutes=21)),
        )

    def test_rowless_planner_is_classified_without_branch_evidence(self):
        item = evidence(
            edits_code=False,
            branch_declared=None,
            branch_present=None,
            last_result_row_at=NOW,
        )
        self.assertEqual("reported", pr_poller.classify(item, NOW))


class TickTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.addCleanup(self.con.close)

    def test_structured_live_unit_runs_without_pr_watch_or_active_prose(self):
        add_unit(self.con, body="status: CLOSED")
        before_messages = self.con.execute(
            "SELECT COUNT(*) FROM shell_messages"
        ).fetchone()[0]
        before_alerts = self.con.execute(
            "SELECT COUNT(*) FROM planner_alerts"
        ).fetchone()[0]
        before_board = tuple(
            self.con.execute(
                "SELECT * FROM sprint_units ORDER BY unit_id"
            ).fetchall()
        )

        readings = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now: evidence(
                branch_declared=unit["branch"]
            ),
            refresh=lambda worktree: True,
        )

        self.assertEqual(["dev", "reviewer"], [
            reading.expectation.role for reading in readings
        ])
        for reading in readings:
            self.assertEqual(NOW, reading.observed_at)
            self.assertIsNone(reading.explanation)
            self.assertEqual(EPOCH.isoformat(), reading.measurement["epoch"])
            self.assertEqual(
                int(pr_poller.NO_PROGRESS_WINDOW.total_seconds()),
                reading.measurement["window_seconds"],
            )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM watched_prs").fetchone()[0],
        )
        self.assertEqual(
            before_messages,
            self.con.execute("SELECT COUNT(*) FROM shell_messages").fetchone()[0],
        )
        self.assertEqual(
            before_alerts,
            self.con.execute("SELECT COUNT(*) FROM planner_alerts").fetchone()[0],
        )
        self.assertEqual(
            before_board,
            tuple(
                self.con.execute(
                    "SELECT * FROM sprint_units ORDER BY unit_id"
                ).fetchall()
            ),
        )

    def test_frozen_and_terminal_units_are_not_expectations(self):
        add_unit(self.con, doc_id=59, unit_id=1, frozen=1)
        add_unit(
            self.con,
            doc_id=60,
            unit_id=2,
            state=sprint_units.TERMINAL_UNIT_STATES[0],
        )
        self.assertEqual([], pr_poller.live_expectations(self.con))

    def test_latest_planner_binding_emits_the_rowless_planner_expectation(self):
        add_unit(self.con)
        add_binding(
            self.con,
            planner=3,
            released_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        add_binding(self.con, planner=4)

        planners = [
            expectation
            for expectation in pr_poller.live_expectations(self.con)
            if expectation.role == "planner"
        ]

        self.assertEqual(1, len(planners))
        self.assertEqual(4, planners[0].shell_id)
        self.assertEqual("PLN2", planners[0].shell["shortname"])
        self.assertIsNone(planners[0].unit_id)
        self.assertIsNone(planners[0].unit)

    def test_all_terminal_live_sprint_still_has_its_planner_expectation(self):
        add_unit(
            self.con,
            state=sprint_units.TERMINAL_UNIT_STATES[0],
        )
        add_binding(self.con, planner=3)

        expectations = pr_poller.live_expectations(self.con)

        self.assertEqual(
            [(59, None, None, "planner", 3)],
            [
                (
                    item.sprint_doc_id,
                    item.unit_id,
                    item.seq,
                    item.role,
                    item.shell_id,
                )
                for item in expectations
            ],
        )

    def test_every_nonterminal_schema_state_remains_a_live_expectation(self):
        for offset, state in enumerate(
            ("pending", "working", "in_review", "blocked"),
            start=1,
        ):
            add_unit(
                self.con,
                doc_id=100 + offset,
                unit_id=100 + offset,
                seq=f"U{offset}",
                state=state,
                reviewer=None,
            )
        self.assertEqual(
            ["U1", "U2", "U3", "U4"],
            [
                expectation.seq
                for expectation in pr_poller.live_expectations(self.con)
            ],
        )

    def test_one_refresh_per_tick_and_one_read_for_two_roles(self):
        add_unit(self.con, dev=1, reviewer=1)
        refreshes = []
        reads = []

        result = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now: (
                reads.append((shell["shell_id"], unit["unit_id"]))
                or evidence()
            ),
            refresh=lambda worktree: refreshes.append(worktree) or True,
        )

        self.assertEqual(1, len(refreshes))
        self.assertEqual([(1, 1)], reads)
        self.assertEqual(2, len(result))

    def test_failed_refresh_is_joined_to_each_affected_reading(self):
        add_unit(self.con)
        readings = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now: evidence(),
            refresh=lambda worktree: False,
        )
        for reading in readings:
            self.assertIn(
                ar.INTEGRATION_REF_REFRESH,
                reading.evidence.unreadable,
            )
            self.assertEqual("indeterminate", reading.signal)

    def test_two_consecutive_ticks_confirm_and_recovery_resets(self):
        add_unit(self.con, reviewer=None)
        state = pr_poller.ReconcilerState()
        stale = lambda shell, unit, now: evidence()

        first = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=stale,
            refresh=lambda worktree: True,
            state=state,
        )
        second = pr_poller.reconcile_tick(
            self.con,
            now=NOW + timedelta(minutes=10),
            reader=stale,
            refresh=lambda worktree: True,
            state=state,
        )
        recovered = pr_poller.reconcile_tick(
            self.con,
            now=NOW + timedelta(minutes=11),
            reader=lambda shell, unit, now: evidence(last_work_at=now),
            refresh=lambda worktree: True,
            state=state,
        )
        again = pr_poller.reconcile_tick(
            self.con,
            now=NOW + timedelta(minutes=32),
            reader=stale,
            refresh=lambda worktree: True,
            state=state,
        )

        self.assertEqual(("checkup", False), (first[0].signal, first[0].confirmed))
        self.assertEqual(("checkup", True), (second[0].signal, second[0].confirmed))
        self.assertEqual(("working", False), (
            recovered[0].signal,
            recovered[0].confirmed,
        ))
        self.assertEqual(("checkup", False), (again[0].signal, again[0].confirmed))

    def test_two_sprints_are_evaluated_per_unit(self):
        add_unit(self.con, doc_id=59, unit_id=1, seq="U4", reviewer=None)
        add_unit(self.con, doc_id=60, unit_id=2, seq="U7", reviewer=None)

        readings = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now: evidence(
                last_result_row_at=(
                    NOW if unit["sprint_doc_id"] == 60 else None
                )
            ),
            refresh=lambda worktree: True,
        )

        self.assertEqual(
            [(59, "U4", "checkup"), (60, "U7", "reported")],
            [
                (
                    item.expectation.sprint_doc_id,
                    item.expectation.seq,
                    item.signal,
                )
                for item in readings
            ],
        )

    def test_shared_terminal_constant_drives_renderer_reader_api_and_tick(self):
        self.assertIs(
            pr_poller.TERMINAL_UNIT_STATES,
            sprint_units.TERMINAL_UNIT_STATES,
        )
        self.assertIs(
            ar.TERMINAL_UNIT_STATES,
            sprint_units.TERMINAL_UNIT_STATES,
        )


if __name__ == "__main__":
    unittest.main()
