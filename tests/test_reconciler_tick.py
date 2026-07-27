#!/usr/bin/env python3
"""Worker-expectation classification and supervised tick (spec 58, U4)."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

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


def add_quota_tables(con: sqlite3.Connection) -> None:
    for name in (
        "0096_harness_quota_accounts.sql",
        "0097_quota_drop_account_identity.sql",
    ):
        con.executescript((MIGRATIONS / name).read_text())


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

    def test_live_durable_write_is_working_for_a_role_with_no_code(self):
        """Flag #364 defect (b) / spec #76 H-16. A planner emits `task` rows,
        never `result`, and has no branch — so Rules 2, 3 and 5 all miss it and
        Rule 6's floor pinned it at `checkup` for the rest of the sprint, where
        no healthy signal could ever resolve the alert. Its durable writes were
        read the whole time and consulted only behind `session_over`; counting
        them as evidence in the floor is what lets the signal come back."""
        planner = evidence(
            edits_code=False,
            branch_declared=None,
            branch_present=None,
            last_result_row_at=None,
            last_durable_write_at=NOW - timedelta(minutes=3),
        )
        self.assertEqual("working", pr_poller.classify(planner, NOW))

    def test_a_durable_write_older_than_the_window_is_still_checkup(self):
        """The paired negative: the rule reads a CURRENT write, not the mere
        existence of one. Without this, `working` would become unfalsifiable
        for every role that has ever written a row."""
        planner = evidence(
            edits_code=False,
            branch_declared=None,
            branch_present=None,
            last_durable_write_at=NOW - timedelta(minutes=21),
        )
        self.assertEqual("checkup", pr_poller.classify(planner, NOW))

    def test_durable_write_never_outranks_a_closed_session_report(self):
        """The durable write is evidence in Rule 6's floor and NOWHERE above
        it. Once the session is over, a durable write is a completion report,
        not a sign of life, and Rule 4 must keep winning."""
        item = evidence(
            edits_code=False,
            branch_declared=None,
            branch_present=None,
            process_present=False,
            last_durable_write_at=NOW - timedelta(minutes=1),
        )
        self.assertEqual("work_complete_unreported", pr_poller.classify(item, NOW))

    def test_a_stale_durable_write_still_resets_the_no_progress_floor(self):
        """Rule 6 counts the durable write for the same reason it already counts
        a work event: with a tighter-than-default window the write is too old to
        prove `working` on its own, but it is still the newest thing that
        happened, and the floor measures SILENCE from the newest event. Without
        it the floor would be measured from a boot clock the worker has long
        since spoken past."""
        item = evidence(
            edits_code=False,
            branch_declared=None,
            branch_present=None,
            last_durable_write_at=NOW - timedelta(minutes=10),
        )
        self.assertEqual(
            "working",
            pr_poller.classify(item, NOW, window=timedelta(minutes=5)),
        )

    def test_reviewer_without_branch_presence_is_never_not_started(self):
        """Spec #76 H-16: a reviewer inherited the dev's branch and read
        `not_started` until the dev pushed. With no code surface it is judged on
        review-shaped evidence — here, none yet, which is `checkup`, not an
        accusation that it never began."""
        reviewer = evidence(
            edits_code=False,
            branch_declared="feat/test",
            branch_present=False,
        )
        self.assertEqual("checkup", pr_poller.classify(reviewer, NOW))
        # Positive control: the same missing branch on a DEV still accuses.
        self.assertEqual(
            "not_started",
            pr_poller.classify(evidence(branch_present=False), NOW),
        )

    def test_building_dev_with_no_declared_branch_is_working(self):
        """Spec #76 H-15, the classifier half: with the reader no longer gated
        on the board's `branch` column, a heads-down dev arrives carrying a work
        event and must not fall to the 20-minute floor."""
        dev = evidence(
            branch_declared=None,
            branch_present=None,
            last_work_at=NOW - timedelta(minutes=2),
        )
        self.assertEqual("working", pr_poller.classify(dev, NOW))

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


class ExplanationTierTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.addCleanup(self.con.close)

    def test_transcript_explanation_carries_the_current_marker_time(self):
        marker = datetime(2020, 1, 1, 0, 59, tzinfo=UTC)
        parts = pr_poller._activity_explanation(evidence(marker_at=marker))

        self.assertEqual(
            "transcript mtime as of 2020-01-01T00:59:00+00:00",
            parts[0],
        )

    def test_cpu_preserves_the_raw_launch_shape_pair(self):
        interactive = pr_poller._activity_explanation(
            evidence(
                cpu_delta=12.5,
                launch_shape="interactive",
            )
        )
        headless = pr_poller._activity_explanation(
            evidence(
                cpu_delta=12.5,
                launch_shape="headless",
            )
        )

        self.assertEqual(
            "cpu launch_shape=interactive delta_ticks=12.5",
            interactive[1],
        )
        self.assertEqual(
            "cpu launch_shape=headless delta_ticks=12.5",
            headless[1],
        )
        self.assertNotEqual(
            interactive[1],
            headless[1],
            "the measured interactive/headless inversion was collapsed",
        )

    def test_probe_fault_carries_the_probe_timestamp(self):
        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={
                "openai": ("error", "2020-01-01T00:58:30Z"),
            },
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(
            [
                "anthropic quota unavailable",
                "anthropic probe status unavailable",
                "openai quota unavailable",
                "openai probe status=error as of 2020-01-01T00:58:30Z",
                "moonshot quota unavailable",
                "moonshot probe status unavailable",
            ],
            parts,
        )

    def test_fresh_process_probe_source_is_unavailable_never_ok(self):
        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(
            [
                "anthropic quota unavailable",
                "anthropic probe status unavailable",
                "openai quota unavailable",
                "openai probe status unavailable",
                "moonshot quota unavailable",
                "moonshot probe status unavailable",
            ],
            parts,
        )
        self.assertNotIn("status=ok", " | ".join(parts))

    def test_persisted_exhaustion_carries_resets_at(self):
        add_quota_tables(self.con)
        account_pk = self.con.execute(
            "INSERT INTO harness_quota_account(provider, account_ref) "
            "VALUES ('anthropic', 'acct-a')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, used_percent, resets_at, captured_at) "
            "VALUES (?, 'five_hour', 100, ?, ?)",
            (
                account_pk,
                "2020-01-01T02:00:00Z",
                "2020-01-01T00:55:00Z",
            ),
        )

        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(
            "anthropic quota exhausted window=five_hour "
            "resets_at=2020-01-01T02:00:00Z "
            "as of 2020-01-01T00:55:00Z",
            parts[0],
        )

    def test_persisted_exhaustion_and_ok_probe_both_render(self):
        add_quota_tables(self.con)
        account_pk = self.con.execute(
            "INSERT INTO harness_quota_account(provider, account_ref) "
            "VALUES ('anthropic', 'acct-a')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, used_percent, resets_at, captured_at) "
            "VALUES (?, 'five_hour', 100, ?, ?)",
            (
                account_pk,
                "2020-01-01T02:00:00Z",
                "2020-01-01T00:55:00Z",
            ),
        )

        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={
                "anthropic": ("ok", "2020-01-01T00:59:00Z"),
            },
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(6, len(parts))
        self.assertTrue(
            parts[0].startswith(
                "anthropic quota exhausted window=five_hour "
            ),
            parts[0],
        )
        self.assertTrue(
            parts[0].endswith("as of 2020-01-01T00:55:00Z"),
            parts[0],
        )
        self.assertEqual(
            [
                "anthropic probe status=ok as of 2020-01-01T00:59:00Z",
                "openai quota unavailable",
                "openai probe status unavailable",
                "moonshot quota unavailable",
                "moonshot probe status unavailable",
            ],
            parts[1:],
        )

    def test_unreadable_quota_value_degrades_to_unavailable(self):
        add_quota_tables(self.con)
        account_pk = self.con.execute(
            "INSERT INTO harness_quota_account(provider, account_ref) "
            "VALUES ('anthropic', 'acct-a')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, used_percent, captured_at) "
            "VALUES (?, 'weekly', 'not-a-number', ?)",
            (account_pk, "2020-01-01T00:55:00Z"),
        )

        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(
            [
                "anthropic quota unavailable window=weekly "
                "as of 2020-01-01T00:55:00Z",
                "anthropic probe status unavailable",
                "openai quota unavailable",
                "openai probe status unavailable",
                "moonshot quota unavailable",
                "moonshot probe status unavailable",
            ],
            parts,
        )

    def test_quota_limit_and_scope_branches_render_exactly(self):
        add_quota_tables(self.con)
        moonshot_pk = self.con.execute(
            "INSERT INTO harness_quota_account(provider, account_ref) "
            "VALUES ('moonshot', 'acct-m')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, used, limit_value, captured_at) "
            "VALUES (?, 'weekly', 5, 0, ?)",
            (moonshot_pk, "2020-01-01T00:55:00Z"),
        )
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, used, limit_value, captured_at) "
            "VALUES (?, 'five_hour', 5, 10, ?)",
            (moonshot_pk, "2020-01-01T00:55:00Z"),
        )
        openai_pk = self.con.execute(
            "INSERT INTO harness_quota_account(provider, account_ref) "
            "VALUES ('openai', 'acct-o')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk, window_kind, scope, used_percent, captured_at) "
            "VALUES (?, 'weekly', 'codex', 25, ?)",
            (openai_pk, "2020-01-01T00:55:00Z"),
        )

        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            parts = pr_poller._provider_explanation(self.con)

        self.assertEqual(
            [
                "anthropic quota unavailable",
                "anthropic probe status unavailable",
                "openai quota not exhausted window=weekly:codex "
                "as of 2020-01-01T00:55:00Z",
                "openai probe status unavailable",
                "moonshot quota not exhausted window=five_hour "
                "as of 2020-01-01T00:55:00Z",
                "moonshot quota unavailable window=weekly "
                "as of 2020-01-01T00:55:00Z",
                "moonshot probe status unavailable",
            ],
            parts,
        )

    def test_unreadable_explanation_signals_do_not_change_the_verdict(self):
        add_unit(self.con, reviewer=None)
        before_board = tuple(
            self.con.execute(
                "SELECT * FROM sprint_units ORDER BY unit_id"
            ).fetchall()
        )
        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            readable = pr_poller.reconcile_tick(
                self.con,
                now=NOW,
                reader=lambda shell, unit, now, role=None: evidence(
                    marker_at=NOW,
                    cpu_delta=7.0,
                    launch_shape="headless",
                ),
                refresh=lambda worktree: True,
            )[0]
            unreadable = pr_poller.reconcile_tick(
                self.con,
                now=NOW,
                reader=lambda shell, unit, now, role=None: evidence(
                    unreadable=["marker", "process_binding"],
                ),
                refresh=lambda worktree: True,
            )[0]

        self.assertEqual(
            ("checkup", NOW, 7.0, "headless", []),
            (
                readable.signal,
                readable.evidence.marker_at,
                readable.evidence.cpu_delta,
                readable.evidence.launch_shape,
                readable.evidence.unreadable,
            ),
        )
        self.assertEqual(
            (
                "checkup",
                None,
                None,
                None,
                ["marker", "process_binding"],
            ),
            (
                unreadable.signal,
                unreadable.evidence.marker_at,
                unreadable.evidence.cpu_delta,
                unreadable.evidence.launch_shape,
                unreadable.evidence.unreadable,
            ),
        )
        self.assertEqual(
            before_board,
            tuple(
                self.con.execute(
                    "SELECT * FROM sprint_units ORDER BY unit_id"
                ).fetchall()
            ),
        )


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

        with mock.patch.object(
            pr_poller.quota_dispatch,
            "latest_statuses",
            return_value={},
        ):
            readings = pr_poller.reconcile_tick(
                self.con,
                now=NOW,
                reader=lambda shell, unit, now, role=None: evidence(
                    branch_declared=unit["branch"]
                ),
                refresh=lambda worktree: True,
            )

        self.assertEqual(["dev", "reviewer"], [
            reading.expectation.role for reading in readings
        ])
        for reading in readings:
            self.assertEqual(NOW, reading.observed_at)
            self.assertTrue(
                reading.explanation.startswith(
                "transcript mtime unavailable"
                " | cpu launch_shape=unavailable delta_ticks=unavailable"
                ),
                reading.explanation,
            )
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

    def test_all_terminal_planner_expectation_never_reads_the_doc_title(self):
        add_unit(
            self.con,
            state=sprint_units.TERMINAL_UNIT_STATES[0],
        )
        self.con.execute(
            "UPDATE documents SET title='Worker expectation reconciler' "
            "WHERE document_id=59"
        )
        self.con.commit()
        add_binding(self.con, planner=3)

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
                for item in pr_poller.live_expectations(self.con)
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

    def test_one_refresh_per_tick_but_a_read_per_role(self):
        """Refresh stays once per tick — the shared integration ref is fetched
        for the whole run. The READ is now per role (spec #76 H-15/H-16): dev
        and reviewer are observed differently, so one shell holding both roles
        on one unit must not have the dev's answer cached and served to the
        reviewer. Caching on (shell, unit) alone is what did exactly that."""
        add_unit(self.con, dev=1, reviewer=1)
        refreshes = []
        reads = []

        result = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now, role=None: (
                reads.append((shell["shell_id"], unit["unit_id"], role))
                or evidence()
            ),
            refresh=lambda worktree: refreshes.append(worktree) or True,
        )

        self.assertEqual(1, len(refreshes))
        self.assertEqual([(1, 1, "dev"), (1, 1, "reviewer")], reads)
        self.assertEqual(2, len(result))

    def test_repeated_expectations_for_one_role_still_read_once(self):
        """The cache is not defeated, only re-keyed: the same (shell, unit,
        role) triple is read once however many times it is reached."""
        add_unit(self.con, dev=1, reviewer=None)
        reads = []

        pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now, role=None: (
                reads.append(role) or evidence()
            ),
            refresh=lambda worktree: True,
        )
        pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now, role=None: (
                reads.append(role) or evidence()
            ),
            refresh=lambda worktree: True,
        )

        self.assertEqual(["dev", "dev"], reads)   # one per tick, not two

    def test_failed_refresh_is_joined_to_each_affected_reading(self):
        add_unit(self.con)
        readings = pr_poller.reconcile_tick(
            self.con,
            now=NOW,
            reader=lambda shell, unit, now, role=None: evidence(),
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
        stale = lambda shell, unit, now, role=None: evidence()

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
            reader=lambda shell, unit, now, role=None: evidence(last_work_at=now),
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
            reader=lambda shell, unit, now, role=None: evidence(
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
