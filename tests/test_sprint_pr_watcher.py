"""Stage 5 gates for registered-PR observation and routed wakes."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(ROOT / "tests")]

import sprint_domain
import sprint_pr_watcher
from github_pull_requests import GitHubReadError, PullRequest, normalize_pull_request
from test_sprint_message_delivery import SprintMessageCase, apply_schema


def pull_request(
    *,
    number: int = 42,
    state: str = "OPEN",
    checks: str | None = "FAILURE",
    checks_failed: bool = True,
    head_sha: str = "a" * 40,
) -> PullRequest:
    return PullRequest(
        number=number,
        head_ref=f"feature/pr-{number}",
        base_ref="main",
        head_sha=head_sha,
        state=state,
        merged_at="2026-07-31T20:00:00Z" if state == "MERGED" else None,
        merge_sha="b" * 40 if state == "MERGED" else None,
        title=f"PR {number}",
        url=f"https://github.example/acme/repo/pull/{number}",
        review_decision=None,
        checks=checks,
        checks_failed=checks_failed,
    )


class FakeReader:
    def __init__(self, current: PullRequest | Exception) -> None:
        self.current = current
        self.by_number: dict[int, PullRequest] = {}
        self.get_calls: list[int] = []
        self.list_calls = 0

    def get(self, number: int) -> PullRequest:
        self.get_calls.append(number)
        if isinstance(self.current, Exception):
            raise self.current
        return self.by_number.get(number, self.current)

    def list(self) -> list[PullRequest]:
        self.list_calls += 1
        if isinstance(self.current, Exception):
            raise self.current
        return list(self.by_number.values()) or [self.current]


class WatcherHeartbeatMigrationTest(unittest.TestCase):
    def test_history_is_capped_to_newest_fifty_rows_per_daemon(self):
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0174_reseed_force_new_wake_skills.sql")
            con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES ('existing-daemon','2026-08-01 00:00:00',30)"
            )
            con.executescript(
                (
                    ROOT
                    / ".super-coder"
                    / "migrations"
                    / "0175_daemon_heartbeat_history.sql"
                ).read_text()
            )
            con.executemany(
                "INSERT INTO daemon_heartbeat_history "
                "(name,subscriptions_scanned) VALUES ('sprint-pr-watcher',?)",
                ((value,) for value in range(75)),
            )
            con.executemany(
                "INSERT INTO daemon_heartbeat_history "
                "(name,subscriptions_scanned) VALUES ('another-daemon',?)",
                ((value,) for value in range(55)),
            )

            watcher_rows = con.execute(
                "SELECT subscriptions_scanned FROM daemon_heartbeat_history "
                "WHERE name='sprint-pr-watcher' ORDER BY heartbeat_id"
            ).fetchall()
            other_rows = con.execute(
                "SELECT subscriptions_scanned FROM daemon_heartbeat_history "
                "WHERE name='another-daemon' ORDER BY heartbeat_id"
            ).fetchall()

            self.assertEqual(list(range(25, 75)), [row[0] for row in watcher_rows])
            self.assertEqual(list(range(5, 55)), [row[0] for row in other_rows])
            self.assertEqual(
                ("existing-daemon", "2026-08-01 00:00:00", 30),
                tuple(
                    con.execute(
                        "SELECT name,beat_at,interval_s FROM daemon_heartbeats "
                        "WHERE name='existing-daemon'"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM daemon_heartbeat_history "
                    "WHERE (name='sprint-pr-watcher' AND subscriptions_scanned < 25) "
                    "OR (name='another-daemon' AND subscriptions_scanned < 5)"
                ).fetchone()[0],
            )


class WatcherStatusTest(unittest.TestCase):
    def test_status_distinguishes_never_started_live_and_stale(self):
        now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        threshold = 3 * (
            5 + sprint_pr_watcher.GITHUB_TIMEOUT_SECONDS
        )

        self.assertEqual("never-started", sprint_pr_watcher.derive_watcher_status(None))
        self.assertEqual(
            "live",
            sprint_pr_watcher.derive_watcher_status(
                {
                    "beat_at": (now - timedelta(seconds=threshold)).isoformat(),
                    "interval_s": 5,
                },
                now=now,
            ),
        )
        self.assertEqual(
            "stale",
            sprint_pr_watcher.derive_watcher_status(
                {
                    "beat_at": (
                        now - timedelta(seconds=threshold + 1)
                    ).isoformat(),
                    "interval_s": 5,
                },
                now=now,
            ),
        )


class SprintPRWatcherCase(SprintMessageCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = [0.0]
        self.reader = FakeReader(pull_request())
        self.repositories: list[str] = []

        def reader_factory(repository: str) -> FakeReader:
            self.repositories.append(repository)
            return self.reader

        self.watcher = sprint_pr_watcher.SprintPRWatcher(
            self.con,
            repo_root=ROOT,
            reader_factory=reader_factory,
            monotonic=lambda: self.clock[0],
        )

    def register(self, *, number: int = 42):
        return self.watcher.register(
            self.sprint_id,
            owner_shell_id=1,
            repository="Acme/Repo",
            pr_number=number,
            work_unit_ids=(self.unit_id,),
        )

    def _states(self) -> list[str]:
        return [
            str(row[0])
            for row in self.con.execute(
                "SELECT normalized_state FROM sprint_pr_transitions "
                "ORDER BY transition_id"
            )
        ]


class RegistrationTest(SprintPRWatcherCase):
    def test_registration_is_exactly_idempotent_and_takes_initial_snapshot(self):
        first = self.register()
        second = self.register()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.registered_pr_id, second.registered_pr_id)
        self.assertEqual([42], self.reader.get_calls)
        self.assertEqual(["acme/repo"], self.repositories)
        self.assertEqual(
            [("acme/repo", 42)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT repository,pr_number FROM sprint_registered_prs"
                )
            ],
        )
        self.assertEqual(
            [(self.unit_id,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id FROM sprint_pr_work_units"
                )
            ],
        )
        self.assertEqual(
            [(1, "acme/repo", 42, first.registered_pr_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT owner_shell_id,repository,pr_number,"
                    "sprint_registered_pr_id FROM pr_subscriptions"
                )
            ],
        )
        self.assertEqual(
            [("red", "a" * 40)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state,observed_head_sha "
                    "FROM sprint_pr_transitions"
                )
            ],
        )

    def test_registration_rejects_multiple_work_units_without_side_effects(self):
        other_unit = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,planned_wave) VALUES (?,1,2,'Other','No',2)",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "exactly one owning work unit",
        ):
            self.watcher.register(
                self.sprint_id,
                owner_shell_id=1,
                repository="acme/repo",
                pr_number=41,
                work_unit_ids=(self.unit_id, other_unit),
            )

        self.assertEqual([], self.reader.get_calls)
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM sprint_registered_prs").fetchone()[
                0
            ],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM sprint_pr_work_units").fetchone()[0],
        )

    def test_registration_rejects_non_owner_work_and_allows_paused_sprint(self):
        other_unit = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,planned_wave) VALUES (?,2,2,'Other','No',2)",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.commit()
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "owning Developer"
        ):
            self.watcher.registration.register(
                self.sprint_id,
                owner_shell_id=1,
                repository="acme/repo",
                pr_number=41,
                work_unit_ids=(other_unit,),
            )

        sprint_domain.SprintLifecycleStore(self.con).transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="test",
        )
        receipt = self.watcher.registration.register(
            self.sprint_id,
            owner_shell_id=1,
            repository="acme/repo",
            pr_number=41,
            work_unit_ids=(self.unit_id,),
        )
        self.assertTrue(receipt.created)
        self.assertEqual(
            1,
            self.con.execute("SELECT COUNT(*) FROM sprint_registered_prs").fetchone()[
                0
            ],
        )
        self.assertEqual(
            [(1, "acme/repo", 41)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT owner_shell_id,repository,pr_number "
                    "FROM pr_subscriptions"
                )
            ],
        )


class TransitionRoutingTest(SprintPRWatcherCase):
    def test_queued_checkrun_stays_pending_without_green_owner_wake(self):
        raw = {
            "number": 42,
            "headRefName": "feature/pr-42",
            "baseRefName": "main",
            "headRefOid": "a" * 40,
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "title": "PR 42",
            "url": "https://github.example/acme/repo/pull/42",
            "reviewDecision": None,
            "statusCheckRollup": [
                {
                    "name": "fast-tests",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
                {"name": "pytest", "status": "QUEUED", "conclusion": None},
            ],
        }
        self.reader.current = normalize_pull_request(raw)

        self.register()

        self.assertEqual(["pending"], self._states())
        active_recipients = self.con.execute(
            "SELECT m.receiver_shell_id FROM wake_message m "
            "JOIN sprint_wake_messages wm USING (message_id) "
            "WHERE m.idempotency_key LIKE 'pr-transition:%'"
        ).fetchall()
        self.assertEqual([], active_recipients)

        raw["statusCheckRollup"][1] = {
            "name": "pytest",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        self.reader.current = normalize_pull_request(raw)
        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(["pending", "green"], self._states())
        active_recipients = self.con.execute(
            "SELECT m.receiver_shell_id FROM wake_message m "
            "JOIN sprint_wake_messages wm USING (message_id) "
            "WHERE m.idempotency_key LIKE 'pr-transition:%'"
        ).fetchall()
        self.assertEqual([(1,)], [tuple(row) for row in active_recipients])

    def test_first_red_wakes_only_the_owning_developer(self):
        self.register()

        transition = self.con.execute(
            "SELECT transition_id,normalized_state,evidence FROM sprint_pr_transitions"
        ).fetchone()
        self.assertEqual("red", transition["normalized_state"])
        self.assertEqual("registration", json.loads(transition["evidence"])["trigger"])
        routed = self.con.execute(
            "SELECT message_id,receiver_shell_id,sprint_id,to_participant_id,"
            "declared_type,body FROM wake_message "
            "WHERE idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY receiver_shell_id"
        ).fetchall()
        self.assertEqual(
            [1],
            [int(row["receiver_shell_id"]) for row in routed],
        )
        self.assertEqual(
            {
                "GitHub PR event: repository=acme/repo, number=42, head_sha="
                + "a" * 40
                + ", event=red. Your PR went red; fix it."
            },
            {str(row["body"]) for row in routed},
        )
        self.assertEqual(
            [(None, None, "re-enter")],
            [
                (row["sprint_id"], row["to_participant_id"], row["declared_type"])
                for row in routed
            ],
        )
        wakes = self.con.execute(
            "SELECT m.receiver_shell_id,w.wake_id,w.state "
            "FROM wake_message m "
            "JOIN sprint_wake_messages wm USING (message_id) "
            "JOIN sprint_wake_outbox w USING (wake_id) "
            "WHERE m.idempotency_key LIKE 'pr-transition:%'"
        ).fetchall()
        self.assertEqual([1], [int(row[0]) for row in wakes])
        self.assertEqual({"pending"}, {str(row["state"]) for row in wakes})
        self.assertEqual(1, len({int(row["wake_id"]) for row in wakes}))

    def test_red_green_red_occurrences_wake_once_each_and_coalesce(self):
        self.register()
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )
        self.assertTrue(self.watcher.poll_once())
        self.reader.current = pull_request(
            checks="FAILURE", checks_failed=True, head_sha="b" * 40
        )
        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            ["red", "green", "red"],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT normalized_state FROM sprint_pr_transitions "
                    "ORDER BY transition_id"
                )
            ],
        )
        owner_messages = self.con.execute(
            "SELECT message_id FROM wake_message "
            "WHERE receiver_shell_id=? AND idempotency_key LIKE 'pr-transition:%'",
            (1,),
        ).fetchall()
        self.assertEqual(3, len(owner_messages))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(DISTINCT wm.wake_id) FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) "
                "WHERE m.receiver_shell_id=? "
                "AND m.idempotency_key LIKE 'pr-transition:%'",
                (1,),
            ).fetchone()[0],
        )

    def test_head_change_invalidates_approval_without_waking_reviewer(self):
        self.reader.current = pull_request(checks="SUCCESS", checks_failed=False)
        self.register()
        green_message_id = self.con.execute(
            "SELECT message_id FROM wake_message "
            "WHERE idempotency_key LIKE 'pr-transition:%'"
        ).fetchone()[0]
        self.assertIsNone(self.messages.mark_read(green_message_id, 1))
        approval_notice = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="Review approved for the current head.",
            actionable=False,
            declared_type="re-enter",
            idempotency_key="approved-head-notice",
        )
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='merge_ready' "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        )
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,payload) "
            "VALUES (?,'review.approved','participant',?)",
            (
                self.sprint_id,
                json.dumps(
                    {
                        "message_id": approval_notice.message_id,
                        "work_unit_id": self.unit_id,
                    }
                ),
            ),
        )
        self.con.commit()

        self.reader.current = pull_request(
            checks="PENDING",
            checks_failed=False,
            head_sha="b" * 40,
        )
        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            "fixing",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertIsNotNone(
            self.con.execute(
                "SELECT read_at FROM wake_message WHERE message_id=?",
                (approval_notice.message_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            "cancelled",
            self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (approval_notice.wake_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE receiver_shell_id=2 AND idempotency_key LIKE 'pr-%'"
            ).fetchone()[0],
        )

    def test_closed_without_merge_wakes_only_the_owning_developer(self):
        self.reader.current = pull_request(
            state="CLOSED", checks=None, checks_failed=False
        )
        self.register()

        active_recipients = [
            int(row[0])
            for row in self.con.execute(
                "SELECT m.receiver_shell_id FROM wake_message m "
                "JOIN sprint_wake_messages wm USING (message_id) "
                "WHERE m.idempotency_key LIKE 'pr-transition:%'"
            )
        ]
        self.assertEqual([1], active_recipients)
        self.assertEqual(
            [1],
            [
                int(row[0])
                for row in self.con.execute(
                    "SELECT receiver_shell_id FROM wake_message "
                    "WHERE idempotency_key LIKE 'pr-transition:%'"
                )
            ],
        )


class RecoveryAndFailureTest(SprintPRWatcherCase):
    def test_restart_and_unchanged_resume_emit_no_duplicate(self):
        self.register()
        before = tuple(
            self.con.execute(
                "SELECT (SELECT COUNT(*) FROM sprint_pr_transitions),"
                "(SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key LIKE 'pr-transition:%'),"
                "(SELECT COUNT(*) FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) "
                "WHERE m.idempotency_key LIKE 'pr-transition:%')"
            ).fetchone()
        )
        restarted = sprint_pr_watcher.SprintPRWatcher(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: self.reader,
        )
        self.assertTrue(restarted.poll_once(startup=True))

        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        lifecycle.transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="test",
        )
        calls_before_pause = len(self.reader.get_calls)
        self.assertTrue(restarted.poll_once())
        self.assertEqual(calls_before_pause + 1, len(self.reader.get_calls))
        lifecycle.transition(
            self.sprint_id,
            "armed",
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertTrue(restarted.poll_once())

        after = tuple(
            self.con.execute(
                "SELECT (SELECT COUNT(*) FROM sprint_pr_transitions),"
                "(SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key LIKE 'pr-transition:%'),"
                "(SELECT COUNT(*) FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) "
                "WHERE m.idempotency_key LIKE 'pr-transition:%')"
            ).fetchone()
        )
        self.assertEqual(before, after)

    def test_paused_change_is_observed_immediately_and_deduplicated(self):
        self.register()
        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        lifecycle.transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="test",
        )
        self.reader.current = pull_request(checks="SUCCESS", checks_failed=False)
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(["red", "green"], self._states())

        lifecycle.transition(
            self.sprint_id,
            "armed",
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(["red", "green"], self._states())
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(["red", "green"], self._states())

    def test_terminal_sprint_does_not_gate_an_active_subscription(self):
        self.register()
        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        lifecycle.transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            terminal_outcome="delivered",
        )
        calls = len(self.reader.get_calls)

        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(calls + 1, len(self.reader.get_calls))
        self.assertEqual(0, self.reader.list_calls)

    def test_failure_is_durable_backs_off_and_never_invents_state(self):
        self.reader.current = GitHubReadError("rate limit reached")
        self.register()
        self.assertEqual([42], self.reader.get_calls)
        self.assertEqual([], self._states())
        failure = self.con.execute(
            "SELECT payload FROM sprint_events WHERE event_type='pr.poll_failed'"
        ).fetchone()
        payload = json.loads(failure["payload"])
        self.assertEqual(60.0, payload["backoff_seconds"])
        self.assertEqual("rate limit reached", payload["error"])

        self.reader.current = pull_request(checks="SUCCESS", checks_failed=False)
        self.clock[0] = 59.0
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual([42], self.reader.get_calls)
        self.assertEqual([], self._states())
        self.clock[0] = 60.0
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual([42, 42], self.reader.get_calls)
        self.assertEqual(["green"], self._states())
        active_recipient = self.con.execute(
            "SELECT m.receiver_shell_id FROM wake_message m "
            "JOIN sprint_wake_messages wm USING (message_id) "
            "WHERE m.idempotency_key LIKE 'pr-transition:%'"
        ).fetchone()
        self.assertEqual(1, int(active_recipient[0]))


class EngineWideSubscriptionTest(SprintPRWatcherCase):
    def test_non_sprint_subscription_observes_and_wakes_owning_dev(self):
        receipt = self.watcher.subscribe(
            owner_shell_id=1,
            repository="Acme/Repo",
            pr_number=42,
        )

        self.assertTrue(receipt.created)
        self.assertEqual(
            [(receipt.subscription_id, "red", "a" * 40)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT subscription_id,normalized_state,observed_head_sha "
                    "FROM pr_subscription_transitions"
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_pr_transitions"
            ).fetchone()[0],
        )
        message = self.con.execute(
            "SELECT receiver_shell_id,sprint_id,to_participant_id,declared_type,body "
            "FROM wake_message WHERE idempotency_key LIKE 'pr-transition:%'"
        ).fetchone()
        self.assertEqual(
            (1, None, None, "re-enter"),
            tuple(message)[:4],
        )
        self.assertIn("repository=acme/repo, number=42", message["body"])
        self.assertIn("head_sha=" + "a" * 40 + ", event=red", message["body"])

    def test_no_subscriptions_performs_zero_github_calls(self):
        self.assertFalse(self.watcher.poll_once())
        self.assertEqual([], self.reader.get_calls)
        self.assertEqual([], self.repositories)

    def test_closed_subscription_quiesces_until_resubscribed_after_reopen(self):
        self.reader.current = pull_request(
            state="CLOSED", checks=None, checks_failed=False
        )
        first = self.watcher.subscribe(
            owner_shell_id=1,
            repository="acme/repo",
            pr_number=42,
        )
        calls = len(self.reader.get_calls)

        self.reader.current = pull_request(checks="SUCCESS", checks_failed=False)
        self.assertFalse(self.watcher.poll_once())
        self.assertEqual(calls, len(self.reader.get_calls))

        second = self.watcher.subscribe(
            owner_shell_id=1,
            repository="acme/repo",
            pr_number=42,
        )
        self.assertFalse(second.created)
        self.assertEqual(first.subscription_id, second.subscription_id)
        self.assertEqual(calls + 1, len(self.reader.get_calls))
        self.assertEqual(
            [("closed", "a" * 40), ("green", "a" * 40)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state,observed_head_sha "
                    "FROM pr_subscription_transitions ORDER BY transition_id"
                )
            ],
        )

        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(calls + 2, len(self.reader.get_calls))
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key LIKE 'pr-transition:%'"
            ).fetchone()[0],
        )

    def test_merged_subscription_is_quiescent_on_pulse(self):
        self.reader.current = pull_request(
            state="MERGED", checks=None, checks_failed=False
        )
        self.watcher.subscribe(
            owner_shell_id=1,
            repository="acme/repo",
            pr_number=42,
        )
        calls = len(self.reader.get_calls)

        self.assertFalse(self.watcher.poll_once())
        self.assertEqual(calls, len(self.reader.get_calls))
        self.assertEqual(
            [("merged", "a" * 40)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state,observed_head_sha "
                    "FROM pr_subscription_transitions ORDER BY transition_id"
                )
            ],
        )

    def test_non_developer_cannot_own_a_subscription(self):
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "Developer shell",
        ):
            self.watcher.subscribe(
                owner_shell_id=2,
                repository="acme/repo",
                pr_number=42,
            )

        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM pr_subscriptions").fetchone()[0],
        )
        self.assertEqual([], self.reader.get_calls)

    def test_sprint_registration_can_attach_an_existing_subscription(self):
        with sprint_pr_watcher.db_driver.write_transaction(
            self.con, "test.subscribe"
        ):
            generic = self.watcher.subscriptions.subscribe(
                owner_shell_id=1,
                repository="acme/repo",
                pr_number=42,
            )

        registered = self.register()

        linked = self.con.execute(
            "SELECT subscription_id,sprint_registered_pr_id "
            "FROM pr_subscriptions WHERE repository='acme/repo' AND pr_number=42"
        ).fetchone()
        self.assertEqual(generic.subscription_id, linked["subscription_id"])
        self.assertEqual(registered.registered_pr_id, linked["sprint_registered_pr_id"])
        self.assertEqual([42], self.reader.get_calls)


class WatcherHeartbeatTest(SprintPRWatcherCase):
    def service(self) -> sprint_pr_watcher.SprintPRWatcherService:
        return sprint_pr_watcher.SprintPRWatcherService(
            ROOT / "unused.db",
            repo_root=ROOT,
        )

    def test_zero_subscription_start_pulse_records_current_and_history(self):
        heartbeat = sprint_pr_watcher.WatcherHeartbeat(
            self.con,
            interval_seconds=5,
        )

        self.assertFalse(
            self.service()._pulse(self.watcher, heartbeat, startup=True)
        )

        current = self.con.execute(
            "SELECT name,interval_s FROM daemon_heartbeats "
            "WHERE name='sprint-pr-watcher'"
        ).fetchone()
        history = self.con.execute(
            "SELECT name,subscriptions_scanned FROM daemon_heartbeat_history "
            "ORDER BY heartbeat_id"
        ).fetchall()
        self.assertEqual(("sprint-pr-watcher", 5), tuple(current))
        self.assertEqual([("sprint-pr-watcher", 0)], [tuple(row) for row in history])
        self.assertEqual([], self.reader.get_calls)
        self.assertEqual([], self.repositories)

    def test_each_repository_group_beats_with_cumulative_scan_count(self):
        with sprint_pr_watcher.db_driver.write_transaction(
            self.con, "test.heartbeat-subscriptions"
        ):
            self.watcher.subscriptions.subscribe(
                owner_shell_id=1,
                repository="acme/one",
                pr_number=42,
            )
            self.watcher.subscriptions.subscribe(
                owner_shell_id=1,
                repository="acme/two",
                pr_number=43,
            )
        self.reader.by_number = {
            42: pull_request(number=42),
            43: pull_request(number=43),
        }
        ticks = iter((0.0, 61.0, 122.0))
        heartbeat = sprint_pr_watcher.WatcherHeartbeat(
            self.con,
            interval_seconds=5,
            monotonic=lambda: next(ticks),
        )

        self.assertTrue(
            self.service()._pulse(self.watcher, heartbeat, startup=True)
        )

        history = self.con.execute(
            "SELECT subscriptions_scanned FROM daemon_heartbeat_history "
            "WHERE name='sprint-pr-watcher' ORDER BY heartbeat_id"
        ).fetchall()
        self.assertEqual([0, 1, 2], [row[0] for row in history])
        self.assertEqual(["acme/one", "acme/two"], self.repositories)

    def test_history_uses_sixty_second_cadence_between_start_rows(self):
        ticks = iter((0.0, 5.0, 5.0, 60.0, 60.0))
        heartbeat = sprint_pr_watcher.WatcherHeartbeat(
            self.con,
            interval_seconds=5,
            monotonic=lambda: next(ticks),
        )
        service = self.service()

        service._pulse(self.watcher, heartbeat, startup=True)
        service._pulse(self.watcher, heartbeat, startup=False)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM daemon_heartbeat_history "
                "WHERE name='sprint-pr-watcher'"
            ).fetchone()[0],
        )

        service._pulse(self.watcher, heartbeat, startup=False)
        self.assertEqual(
            [0, 0],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT subscriptions_scanned "
                    "FROM daemon_heartbeat_history "
                    "WHERE name='sprint-pr-watcher' ORDER BY heartbeat_id"
                )
            ],
        )

    def test_service_restart_appends_history_without_erasing_prior_gap(self):
        with sprint_pr_watcher.db_driver.write_transaction(
            self.con, "test.prior-heartbeat"
        ):
            self.con.execute(
                "INSERT INTO daemon_heartbeat_history "
                "(name,beat_at,subscriptions_scanned) "
                "VALUES ('sprint-pr-watcher','2026-08-01 00:00:00',9)"
            )
        heartbeat = sprint_pr_watcher.WatcherHeartbeat(
            self.con,
            interval_seconds=5,
        )

        self.service()._pulse(self.watcher, heartbeat, startup=True)

        history = self.con.execute(
            "SELECT beat_at,subscriptions_scanned FROM daemon_heartbeat_history "
            "WHERE name='sprint-pr-watcher' ORDER BY heartbeat_id"
        ).fetchall()
        self.assertEqual(("2026-08-01 00:00:00", 9), tuple(history[0]))
        self.assertEqual(0, history[1]["subscriptions_scanned"])
        self.assertNotEqual(history[0]["beat_at"], history[1]["beat_at"])


class BatchAndNormalizationTest(SprintPRWatcherCase):
    def test_multiple_registered_prs_share_one_repository_list_read(self):
        self.reader.by_number = {42: pull_request(number=42)}
        self.register(number=42)
        self.reader.by_number[43] = pull_request(number=43)
        self.register(number=43)
        get_count = len(self.reader.get_calls)

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(1, self.reader.list_calls)
        self.assertEqual(get_count, len(self.reader.get_calls))
        self.assertEqual(
            [(42, "red"), (43, "red")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT p.pr_number,t.normalized_state "
                    "FROM sprint_registered_prs p "
                    "JOIN sprint_pr_transitions t USING (registered_pr_id) "
                    "ORDER BY p.pr_number"
                )
            ],
        )

    def test_normalized_state_precedence_is_literal(self):
        closed_with_merge_evidence = replace(
            pull_request(state="CLOSED", checks=None, checks_failed=False),
            merged_at="2026-07-31T20:00:00Z",
            merge_sha="b" * 40,
        )
        cases = (
            (pull_request(state="MERGED", checks="FAILURE"), "merged"),
            (closed_with_merge_evidence, "merged"),
            (pull_request(state="CLOSED", checks="SUCCESS"), "closed"),
            (pull_request(), "red"),
            (pull_request(checks="SUCCESS", checks_failed=False), "green"),
            (pull_request(checks="PENDING", checks_failed=False), "pending"),
            (pull_request(checks=None, checks_failed=False), "created"),
        )
        for observed, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, sprint_pr_watcher.normalize_state(observed))

    def test_service_default_is_the_five_second_subscription_pulse(self):
        service = sprint_pr_watcher.SprintPRWatcherService(
            ROOT / "unused.db", repo_root=ROOT
        )
        self.assertEqual(5.0, service.pulse_seconds)
        self.assertEqual(60.0, service.history_seconds)


if __name__ == "__main__":
    unittest.main()
