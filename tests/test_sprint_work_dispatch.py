"""Stage 4 gates for Sprint work planning, dispatch, and runtime wiring."""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import db_driver
import server
import sprint_domain
import sprint_liveness
import sprint_message_delivery as delivery
import sprint_runtime
from conversation_adapters import AdapterError
from conversation_adapters.base import AdapterCapabilities, checked_probe_result


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintWorkDispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        quiet_env = mock.patch.dict(
            "os.environ", {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
        )
        quiet_env.start()
        self.addCleanup(quiet_env.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "sprint.db"
        self.con = db_driver.connect(self.db_path)
        self.addCleanup(self.con.close)
        apply_schema(self.con)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer one", "DEV1", "dev", "prompt"),
                (2, "Reviewer one", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
                (4, "Developer two", "DEV2", "dev", "prompt"),
                (5, "Reviewer two", "REV2", "reviewer", "prompt"),
            ),
        )
        self.feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Feature','in_progress')"
            ).lastrowid
        )
        body = "governing spec"
        self.document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Spec',?)",
                (self.feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            self.con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (self.document_id, revision),
            ).lastrowid
        )
        self.sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (self.feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (self.sprint_id, self.document_id, revision, approval_id),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,?,?,?)",
            (
                (self.sprint_id, 3, "planner", "codex"),
                (self.sprint_id, 1, "developer", "codex"),
                (self.sprint_id, 4, "developer", "codex"),
                (self.sprint_id, 2, "reviewer", "kimi"),
                (self.sprint_id, 5, "reviewer", "kimi"),
            ),
        )
        self.task_ids = [
            int(
                self.con.execute(
                    "INSERT INTO spec_tasks "
                    "(feature_id,document_id,seq,title) VALUES (?,?,?,?)",
                    (self.feature_id, self.document_id, seq, f"Task {seq}"),
                ).lastrowid
            )
            for seq in range(8)
        ]
        self.con.commit()
        self.units = sprint_domain.SprintWorkUnitStore(self.con)
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        )
        self.messages = delivery.SprintMessageStore(self.con)
        self.next_task = 0

    def create_unit(
        self,
        *,
        developer: int,
        reviewer: int = 2,
        wave: int = 0,
        dependencies: tuple[int, ...] = (),
        title: str | None = None,
        output_kind: str = "code",
    ) -> int:
        task_id = self.task_ids[self.next_task]
        self.next_task += 1
        return self.units.create(
            self.sprint_id,
            3,
            assigned_shell_id=developer,
            reviewer_shell_id=reviewer,
            title=title or f"Unit {self.next_task}",
            expected_output=f"Output {self.next_task}",
            task_ids=(task_id,),
            planned_wave=wave,
            dependency_ids=dependencies,
            output_kind=output_kind,
        )

    def assignment_message(self, unit_id: int) -> int:
        return int(
            self.con.execute(
                "SELECT message_id FROM wake_message "
                "WHERE work_unit_id=? AND message_kind='work_assignment' "
                "ORDER BY message_id DESC LIMIT 1",
                (unit_id,),
            ).fetchone()[0]
        )

    def deliver_pending_wakes(self) -> None:
        service = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        )
        while service.deliver_once(
            "test-wake-worker", lambda _conversation, _prompt, _key: "run-ref"
        ):
            pass

    def dispositions(self) -> list[tuple[int, str]]:
        return [
            tuple(row)
            for row in self.con.execute(
                "SELECT work_unit_id,disposition FROM sprint_work_units "
                "WHERE sprint_id=? ORDER BY work_unit_id",
                (self.sprint_id,),
            )
        ]

    def conversation_for(self, shell_id: int) -> str:
        return str(
            self.con.execute(
                "SELECT chat_id FROM active_shell_chats WHERE shell_id=?",
                (shell_id,),
            ).fetchone()[0]
        )


class DispatchGateTest(SprintWorkDispatchCase):
    def assert_arm_left_no_writes(self) -> None:
        self.assertEqual(
            ("prepared", 0, 0, 0),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0],
                self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.armed'",
                    (self.sprint_id,),
                ).fetchone()[0],
            ),
        )

    def test_arm_probes_each_distinct_harness_once_outside_write_transaction(self) -> None:
        self.create_unit(developer=1)
        observed: list[tuple[str, bool]] = []
        lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            probe_harness=lambda harness: observed.append(
                (harness, self.con.in_transaction)
            ),
        )

        lifecycle.arm(self.sprint_id, 3)

        self.assertEqual([("codex", False), ("kimi", False)], observed)

    def test_arm_default_preflight_uses_the_conversation_adapter_probe(self) -> None:
        self.create_unit(developer=1)
        adapters = {
            harness: mock.Mock(
                probe=mock.Mock(
                    side_effect=lambda: self.assertFalse(self.con.in_transaction)
                )
            )
            for harness in ("codex", "kimi")
        }
        with mock.patch.object(
            sprint_domain,
            "adapter_for",
            side_effect=lambda harness: adapters[harness],
        ) as adapter_factory:
            sprint_domain.SprintLifecycleStore(self.con).arm(self.sprint_id, 3)

        self.assertEqual(
            [mock.call("codex"), mock.call("kimi")],
            adapter_factory.call_args_list,
        )
        adapters["codex"].probe.assert_called_once_with()
        adapters["kimi"].probe.assert_called_once_with()

    def test_arm_rejects_missing_binary_before_any_write(self) -> None:
        self.create_unit(developer=1)

        def unavailable(harness: str):
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                f"cannot probe {harness}",
                retryable=True,
            )

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "HARNESS_UNAVAILABLE",
        ) as caught:
            sprint_domain.SprintLifecycleStore(
                self.con, probe_harness=unavailable
            ).arm(self.sprint_id, 3)

        self.assert_arm_left_no_writes()
        handler = object.__new__(server.Handler)
        handler._send = lambda status, body: (status, body)
        self.assertEqual(
            (422, {"error": "HARNESS_UNAVAILABLE: cannot probe codex"}),
            handler._sprint_error(caught.exception),
        )

    def test_arm_rejects_unknown_adapter_before_any_write(self) -> None:
        self.create_unit(developer=1)
        self.con.execute(
            "UPDATE sprint_participants SET harness='unknown-harness' "
            "WHERE sprint_id=? AND role='planner'",
            (self.sprint_id,),
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "unknown-harness.*no browser conversation adapter",
        ) as caught:
            self.lifecycle.arm(self.sprint_id, 3)

        self.assertIsInstance(caught.exception, sprint_domain.SprintPreflightError)
        self.assert_arm_left_no_writes()

    def test_arm_allows_newer_unverified_harness(self) -> None:
        self.create_unit(developer=1)
        observed: list[tuple[str, str]] = []
        capabilities = AdapterCapabilities(
            exact_session_resume=True,
            structured_streaming=True,
            interruption=True,
            interactive_permission_response=True,
            server_backed=True,
            session_inspection=True,
        )
        manifest = {
            "conversation": {
                "minimum_cli_version": "1.0.0",
                "maximum_cli_version_exclusive": "2.0.0",
                "verified_cli_version": "1.5.0",
            }
        }

        def newer(harness: str):
            result = checked_probe_result(
                harness=harness,
                manifest=manifest,
                capabilities=capabilities,
                version="2.0.0",
            )
            observed.append((harness, result.compatibility))
            return result

        sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=newer
        ).arm(self.sprint_id, 3)

        self.assertEqual(
            [("codex", "newer-unverified"), ("kimi", "newer-unverified")],
            observed,
        )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_arm_rejects_too_old_harness_before_any_write(self) -> None:
        self.create_unit(developer=1)

        def too_old(harness: str):
            raise AdapterError(
                "HARNESS_VERSION_UNSUPPORTED",
                f"{harness} 0.9.0 is older than required 1.0.0",
            )

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "HARNESS_VERSION_UNSUPPORTED",
        ) as caught:
            sprint_domain.SprintLifecycleStore(
                self.con, probe_harness=too_old
            ).arm(self.sprint_id, 3)

        self.assertIsInstance(caught.exception, sprint_domain.SprintPreflightError)
        self.assert_arm_left_no_writes()

    def test_arm_selection_race_returns_retryable_conflict_without_lifecycle_writes(
        self,
    ) -> None:
        self.create_unit(developer=1)
        raced = False

        def mutate_once(_harness: str):
            nonlocal raced
            if raced:
                return None
            raced = True
            self.con.execute(
                "UPDATE sprint_participants SET model='raced-model' "
                "WHERE sprint_id=? AND role='planner'",
                (self.sprint_id,),
            )
            self.con.commit()
            return None

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "changed during harness preflight; retry arm",
        ) as caught:
            sprint_domain.SprintLifecycleStore(
                self.con, probe_harness=mutate_once
            ).arm(self.sprint_id, 3)

        self.assert_arm_left_no_writes()
        handler = object.__new__(server.Handler)
        handler._send = lambda status, body: (status, body)
        self.assertEqual(
            (
                409,
                {
                    "error": (
                        "participant launch selections changed during harness "
                        "preflight; retry arm"
                    )
                },
            ),
            handler._sprint_error(caught.exception),
        )

    def test_ready_units_launch_in_parallel_regardless_of_wave_or_reviewer(self) -> None:
        late_wave = self.create_unit(developer=1, wave=9, title="Late wave")
        early_wave = self.create_unit(developer=4, wave=0, title="Early wave")

        wake_ids = self.lifecycle.arm(self.sprint_id, 3)

        self.assertEqual(3, len(wake_ids))
        self.assertEqual(
            [(late_wave, "ready"), (early_wave, "ready")],
            self.dispositions(),
        )
        self.assertEqual(
            [(late_wave, 2), (early_wave, 2)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,reviewer_shell_id "
                    "FROM sprint_work_units ORDER BY work_unit_id"
                )
            ],
            "one Reviewer may cover parallel units without consuming a lane",
        )
        self.assertEqual(
            [(early_wave, "force-new"), (late_wave, "force-new")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,declared_type FROM wake_message "
                    "WHERE message_kind='work_assignment' ORDER BY message_id"
                )
            ],
        )
        planner_wake = self.con.execute(
            "SELECT receiver_shell_id,message_kind,body,declared_type,actionable "
            "FROM wake_message WHERE receiver_shell_id=3 "
            "AND message_kind='notification'"
        ).fetchone()
        self.assertEqual(
            (3, "notification", "new", 0),
            (
                planner_wake["receiver_shell_id"],
                planner_wake["message_kind"],
                planner_wake["declared_type"],
                planner_wake["actionable"],
            ),
        )
        self.assertEqual(
            "Sprint 1 armed. Recorded participant launch selections:\n"
            "- planner PLN1: codex · model=default · effort=default\n"
            "- developer DEV1: codex · model=default · effort=default\n"
            "- developer DEV2: codex · model=default · effort=default\n"
            "- reviewer REV1: kimi · model=default · effort=default\n"
            "- reviewer REV2: kimi · model=default · effort=default",
            planner_wake["body"],
        )

    def test_arm_rejects_blank_model_selection_before_any_dispatch(self) -> None:
        self.create_unit(developer=1)
        self.con.execute(
            "UPDATE sprint_participants SET model='' "
            "WHERE sprint_id=? AND role='planner'",
            (self.sprint_id,),
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "recorded model selections",
        ):
            self.lifecycle.arm(self.sprint_id, 3)

        self.assertEqual(
            ("prepared", 0, [(1, "planned")]),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0],
                self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
                self.dispositions(),
            ),
        )

    def test_arm_rejects_blank_effort_selection_before_any_dispatch(self) -> None:
        self.create_unit(developer=1)
        self.con.execute(
            "UPDATE sprint_participants SET effort='  ' "
            "WHERE sprint_id=? AND role='reviewer'",
            (self.sprint_id,),
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "recorded model selections",
        ):
            self.lifecycle.arm(self.sprint_id, 3)

        self.assertEqual(
            ("prepared", 0, [(1, "planned")]),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0],
                self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
                self.dispositions(),
            ),
        )

    def test_dependencies_block_and_each_developer_gets_one_editing_lane(self) -> None:
        first = self.create_unit(
            developer=1, wave=0, title="First", output_kind="no_code"
        )
        same_shell = self.create_unit(developer=1, wave=1, title="Second")
        blocked = self.create_unit(
            developer=4,
            wave=0,
            dependencies=(first,),
            title="Blocked",
        )

        self.lifecycle.arm(self.sprint_id, 3)
        self.assertEqual(
            [(first, "ready"), (same_shell, "planned"), (blocked, "planned")],
            self.dispositions(),
        )
        self.deliver_pending_wakes()
        self.assertEqual(
            "accepted",
            self.messages.mark_read(self.assignment_message(first), 1),
        )
        self.assertEqual("active", self.dispositions()[0][1])

        released = self.units.complete(
            self.sprint_id, first, 1, result="Durable non-code result"
        )

        self.assertEqual(2, len(released))
        self.assertEqual(
            [(first, "completed"), (same_shell, "ready"), (blocked, "ready")],
            self.dispositions(),
        )
        self.assertEqual(
            [blocked, same_shell],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT work_unit_id FROM wake_message "
                    "WHERE message_kind='work_assignment' AND work_unit_id<>? "
                    "ORDER BY message_id",
                    (first,),
                )
            ],
        )
        self.assertEqual(
            ("no_code", "Durable non-code result"),
            tuple(
                self.con.execute(
                    "SELECT output_kind,completion_result FROM sprint_work_units "
                    "WHERE work_unit_id=?",
                    (first,),
                ).fetchone()
            ),
        )

    def test_only_explicit_non_code_lane_completes_with_exact_result(self) -> None:
        code = self.create_unit(developer=1, title="Code")
        report = self.create_unit(
            developer=4, title="Report", output_kind="report_only"
        )
        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.messages.mark_read(self.assignment_message(code), 1)
        self.messages.mark_read(self.assignment_message(report), 4)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "merge judgment chain"
        ):
            self.units.complete(self.sprint_id, code, 1, result="Not allowed")
        self.assertEqual("active", self.dispositions()[0][1])
        self.assertIsNone(
            self.con.execute(
                "SELECT completion_result FROM sprint_work_units "
                "WHERE work_unit_id=?",
                (code,),
            ).fetchone()[0]
        )

        self.assertEqual(
            [],
            self.units.complete(
                self.sprint_id, report, 4, result="Published conformance report #77"
            ),
        )
        row = self.con.execute(
            "SELECT disposition,completion_result FROM sprint_work_units "
            "WHERE work_unit_id=?",
            (report,),
        ).fetchone()
        self.assertEqual(("completed", "Published conformance report #77"), tuple(row))
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE event_type='work_unit.completed' "
            "AND actor_shell_id=4"
        ).fetchone()
        self.assertEqual(
            "Published conformance report #77", json.loads(event[0])["result"]
        )

    def test_non_code_result_accepts_8000_and_rejects_8001_without_state_change(self):
        report = self.create_unit(
            developer=1,
            title="Bounded report",
            output_kind="report_only",
        )
        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.messages.mark_read(self.assignment_message(report), 1)

        with self.assertRaisesRegex(
            ValueError,
            "work-unit completion result is 8001 characters; maximum is 8000",
        ):
            self.units.complete(self.sprint_id, report, 1, result="x" * 8001)
        self.assertEqual(
            ("active", None),
            tuple(
                self.con.execute(
                    "SELECT disposition,completion_result FROM sprint_work_units "
                    "WHERE work_unit_id=?",
                    (report,),
                ).fetchone()
            ),
        )

        self.assertEqual(
            [],
            self.units.complete(self.sprint_id, report, 1, result="x" * 8000),
        )
        self.assertEqual(
            ("completed", 8000),
            tuple(
                self.con.execute(
                    "SELECT disposition,length(completion_result) "
                    "FROM sprint_work_units WHERE work_unit_id=?",
                    (report,),
                ).fetchone()
            ),
        )

    def test_cancellation_reason_accepts_8000_and_rejects_8001_with_counts(self):
        rejected = self.create_unit(developer=1, title="Rejected cancellation")
        with self.assertRaisesRegex(
            ValueError,
            "work-unit cancellation reason is 8001 characters; maximum is 8000",
        ):
            self.units.cancel(
                self.sprint_id,
                rejected,
                3,
                reason="x" * 8001,
            )
        self.assertEqual(
            ("planned", None),
            tuple(
                self.con.execute(
                    "SELECT disposition,completion_result FROM sprint_work_units "
                    "WHERE work_unit_id=?",
                    (rejected,),
                ).fetchone()
            ),
        )

        accepted = self.create_unit(developer=1, title="Accepted cancellation")
        self.assertTrue(
            self.units.cancel(
                self.sprint_id,
                accepted,
                3,
                reason="x" * 8000,
            )
        )
        self.assertEqual(
            ("cancelled", 8000),
            tuple(
                self.con.execute(
                    "SELECT disposition,length(completion_result) "
                    "FROM sprint_work_units WHERE work_unit_id=?",
                    (accepted,),
                ).fetchone()
            ),
        )

    def test_planner_cancels_only_unreleased_lane_with_reason(self) -> None:
        cancelled = self.create_unit(developer=1, title="Cancelled")
        self.assertTrue(
            self.units.cancel(
                self.sprint_id, cancelled, 3, reason="Superseded by unit 99"
            )
        )
        self.assertEqual(
            ("cancelled", "Superseded by unit 99"),
            tuple(
                self.con.execute(
                    "SELECT disposition,completion_result FROM sprint_work_units "
                    "WHERE work_unit_id=?",
                    (cancelled,),
                ).fetchone()
            ),
        )
        self.assertFalse(
            self.units.cancel(
                self.sprint_id, cancelled, 3, reason="Superseded by unit 99"
            )
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "originating Planner"
        ):
            other = self.create_unit(developer=4, title="Still planned")
            self.units.cancel(self.sprint_id, other, 1, reason="Not mine")

    def test_declined_assignment_returns_to_pool_with_a_new_durable_identity(self) -> None:
        unit = self.create_unit(developer=1)
        self.lifecycle.arm(self.sprint_id, 3)
        first_message = self.assignment_message(unit)
        first_key = self.con.execute(
            "SELECT idempotency_key FROM wake_message WHERE message_id=?",
            (first_message,),
        ).fetchone()[0]

        self.deliver_pending_wakes()
        self.messages.decline(first_message, 1, "capacity changed")
        released = self.units.dispatch_ready(self.sprint_id)

        second_message = self.assignment_message(unit)
        second_key = self.con.execute(
            "SELECT idempotency_key FROM wake_message WHERE message_id=?",
            (second_message,),
        ).fetchone()[0]
        self.assertEqual(1, len(released))
        self.assertNotEqual(first_message, second_message)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(
            [("declined", "capacity changed"), ("pending", None)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT disposition,decline_reason FROM wake_message "
                    "WHERE work_unit_id=? AND message_kind='work_assignment' "
                    "ORDER BY message_id",
                    (unit,),
                )
            ],
        )
        self.assertEqual([(unit, "ready")], self.dispositions())

    def test_replanning_is_acyclic_and_preserves_before_after_history(self) -> None:
        upstream = self.create_unit(
            developer=1, title="Upstream", output_kind="no_code"
        )
        target = self.create_unit(
            developer=4,
            dependencies=(upstream,),
            wave=1,
            title="Target",
        )

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "acyclic"
        ):
            self.units.replan(
                self.sprint_id,
                upstream,
                3,
                assigned_shell_id=1,
                reviewer_shell_id=2,
                planned_wave=0,
                dependency_ids=(target,),
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_work_unit_dependencies "
                "WHERE work_unit_id=?",
                (upstream,),
            ).fetchone()[0],
        )

        self.assertTrue(
            self.units.replan(
                self.sprint_id,
                target,
                3,
                assigned_shell_id=4,
                reviewer_shell_id=5,
                planned_wave=7,
                dependency_ids=(),
            )
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events "
            "WHERE event_type='work_unit.replanned'"
        ).fetchone()
        payload = json.loads(event["payload"])
        self.assertEqual(
            {
                "assigned_shell_id": 4,
                "reviewer_shell_id": 2,
            "planned_wave": 1,
            "output_kind": "code",
            "dependency_ids": [upstream],
            },
            payload["before"],
        )
        self.assertEqual(
            {
                "assigned_shell_id": 4,
                "reviewer_shell_id": 5,
            "planned_wave": 7,
            "output_kind": "code",
            "dependency_ids": [],
            },
            payload["after"],
        )

        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.messages.mark_read(self.assignment_message(upstream), 1)
        self.units.complete(
            self.sprint_id, upstream, 1, result="Upstream planning result"
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "only planned"
        ):
            self.units.replan(
                self.sprint_id,
                upstream,
                3,
                assigned_shell_id=4,
                reviewer_shell_id=5,
                planned_wave=99,
                dependency_ids=(),
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='work_unit.replanned'"
            ).fetchone()[0],
            "failed replanning must not rewrite completed history",
        )

    def test_create_rejects_tasks_outside_the_bound_spec_without_partial_unit(self) -> None:
        other_feature = self.con.execute(
            "INSERT INTO roadmap (title,roadmap_status) VALUES ('Other','next')"
        ).lastrowid
        other_doc = self.con.execute(
            "INSERT INTO documents (feature_id,kind,seq,title,body) "
            "VALUES (?,'spec',1,'Other spec','body')",
            (other_feature,),
        ).lastrowid
        other_task = self.con.execute(
            "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
            "VALUES (?,?,0,'Other task')",
            (other_feature, other_doc),
        ).lastrowid
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "governing Sprint spec"
        ):
            self.units.create(
                self.sprint_id,
                3,
                assigned_shell_id=1,
                reviewer_shell_id=2,
                title="Invalid",
                expected_output="Must not persist",
                task_ids=(other_task,),
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )


class DeliveryTerminalTest(SprintWorkDispatchCase):
    def accept_arming_notification(self) -> None:
        message_id = int(
            self.con.execute(
                "SELECT m.message_id FROM wake_message m "
                "JOIN sprint_participants p ON p.participant_id=m.to_participant_id "
                "WHERE m.sprint_id=? AND p.shell_id=3 "
                "AND m.message_kind='notification' ORDER BY m.message_id LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertIsNone(self.messages.mark_read(message_id, 3))
        self.assertEqual(
            (1, None),
            tuple(
                self.con.execute(
                    "SELECT read_at IS NOT NULL,disposition "
                    "FROM wake_message WHERE message_id=?",
                    (message_id,),
                ).fetchone()
            ),
        )

    def terminal_messages(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT p.shell_id,m.message_kind,m.body,m.declared_type,"
            "m.actionable,m.disposition,m.idempotency_key "
            "FROM wake_message m JOIN sprint_participants p "
            "ON p.participant_id=m.to_participant_id "
            "WHERE m.sprint_id=? AND m.idempotency_key LIKE ? "
            "ORDER BY p.shell_id",
            (self.sprint_id, f"sprint:{self.sprint_id}:delivery-terminal:%"),
        ).fetchall()

    def terminal_events(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT actor_kind,actor_shell_id,payload FROM sprint_events "
            "WHERE sprint_id=? AND event_type='sprint.delivery_terminal' "
            "ORDER BY event_id",
            (self.sprint_id,),
        ).fetchall()

    def mark_merge_ready(self, unit_id: int, shell_id: int) -> None:
        self.deliver_pending_wakes()
        self.assertEqual(
            "accepted",
            self.messages.mark_read(self.assignment_message(unit_id), shell_id),
        )
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='merge_ready' "
            "WHERE work_unit_id=?",
            (unit_id,),
        )
        self.con.commit()

    def complete_merge(self, unit_id: int, key: str) -> None:
        with db_driver.write_transaction(self.con, "test.merge_observed"):
            self.units.complete_from_merge_in_transaction(
                self.sprint_id,
                (unit_id,),
                transition_key=key,
            )

    def assert_episode(self, terminal_count: int, completed: int, cancelled: int):
        base = f"sprint:{self.sprint_id}:delivery-terminal:{terminal_count}"
        messages = [
            row
            for row in self.terminal_messages()
            if row["idempotency_key"] == base
            or str(row["idempotency_key"]).startswith(f"{base}:")
        ]
        self.assertEqual([2, 5], [int(row["shell_id"]) for row in messages])
        participant_ids = {
            int(row["shell_id"]): int(row["participant_id"])
            for row in self.con.execute(
                "SELECT shell_id,participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND role='reviewer'",
                (self.sprint_id,),
            )
        }
        self.assertEqual(
            [
                f"{base}:reviewer:{participant_ids[2]}",
                f"{base}:reviewer:{participant_ids[5]}",
            ],
            [str(row["idempotency_key"]) for row in messages],
        )
        expected_body = (
            f"All planned delivery work for Sprint {self.sprint_id} is terminal "
            f"({terminal_count} units: {completed} completed, {cancelled} "
            "cancelled). Begin whole-Sprint conformance per sprint_rev — compile "
            "the evidence packet, judge integrated main, then send the Planner "
            "either a re-enter decision or your conclude decision."
        )
        self.assertEqual(
            [
                ("notification", expected_body, "new", 0, None),
                ("notification", expected_body, "new", 0, None),
            ],
            [
                (
                    row["message_kind"],
                    row["body"],
                    row["declared_type"],
                    int(row["actionable"]),
                    row["disposition"],
                )
                for row in messages
            ],
        )
        event = self.terminal_events()[-1]
        self.assertEqual(("system", None), (event["actor_kind"], event["actor_shell_id"]))
        self.assertEqual(
            {
                "terminal_count": terminal_count,
                "completed_count": completed,
                "cancelled_count": cancelled,
            },
            json.loads(event["payload"]),
        )

    def test_pause_resume_serial_units_wake_reviewers_on_final_merge(self) -> None:
        first = self.create_unit(developer=1, title="First")
        second = self.create_unit(
            developer=1, dependencies=(first,), title="Final"
        )
        self.lifecycle.arm(self.sprint_id, 3)
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="Reference pause",
        )
        self.lifecycle.resume(
            self.sprint_id, sprint_domain.LifecycleActor("planner", 3)
        )

        self.mark_merge_ready(first, 1)
        self.complete_merge(first, "merged-first")
        self.assertEqual([], self.terminal_messages())
        self.assertEqual([], self.terminal_events())

        self.mark_merge_ready(second, 1)
        self.complete_merge(second, "merged-final")

        self.assertEqual([(first, "completed"), (second, "completed")], self.dispositions())
        self.assertEqual(2, len(self.terminal_messages()))
        self.assertEqual(1, len(self.terminal_events()))
        self.assert_episode(2, 2, 0)

    def test_paused_report_completion_wakes_only_on_resume(self) -> None:
        report = self.create_unit(developer=1, output_kind="report_only")
        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.accept_arming_notification()
        self.messages.mark_read(self.assignment_message(report), 1)
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="Inspect report",
        )

        self.units.complete(self.sprint_id, report, 1, result="Report #1")

        self.assertEqual([], self.terminal_messages())
        self.assertEqual([], self.terminal_events())
        self.lifecycle.resume(
            self.sprint_id, sprint_domain.LifecycleActor("planner", 3)
        )
        self.assertEqual(2, len(self.terminal_messages()))
        self.assertEqual(1, len(self.terminal_events()))
        self.assert_episode(1, 1, 0)

    def test_single_reviewer_uses_the_canonical_episode_key(self) -> None:
        self.con.execute(
            "DELETE FROM sprint_participants WHERE sprint_id=? AND shell_id=5",
            (self.sprint_id,),
        )
        self.con.commit()
        report = self.create_unit(developer=1, output_kind="report_only")
        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.messages.mark_read(self.assignment_message(report), 1)

        self.units.complete(self.sprint_id, report, 1, result="Only report")

        messages = self.terminal_messages()
        self.assertEqual(1, len(messages))
        self.assertEqual(2, int(messages[0]["shell_id"]))
        self.assertEqual(
            f"sprint:{self.sprint_id}:delivery-terminal:1",
            messages[0]["idempotency_key"],
        )
        self.assertEqual(1, len(self.terminal_events()))

    def test_cancelling_the_last_planned_unit_wakes_reviewers(self) -> None:
        upstream = self.create_unit(developer=1, title="Cancelled upstream")
        downstream = self.create_unit(
            developer=4,
            dependencies=(upstream,),
            title="Cancelled downstream",
        )
        self.units.cancel(self.sprint_id, upstream, 3, reason="No longer needed")
        self.lifecycle.arm(self.sprint_id, 3)
        self.assertEqual([(upstream, "cancelled"), (downstream, "planned")], self.dispositions())

        self.units.cancel(self.sprint_id, downstream, 3, reason="No work remains")

        self.assertEqual(2, len(self.terminal_messages()))
        self.assertEqual(1, len(self.terminal_events()))
        self.assert_episode(2, 0, 2)

    def test_arming_all_cancelled_sprint_wakes_reviewers(self) -> None:
        first = self.create_unit(developer=1, title="Cancelled first")
        second = self.create_unit(developer=4, title="Cancelled second")
        self.units.cancel(self.sprint_id, first, 3, reason="No longer needed")
        self.units.cancel(self.sprint_id, second, 3, reason="No work remains")
        self.assertEqual([], self.terminal_messages())
        self.assertEqual([], self.terminal_events())

        self.lifecycle.arm(self.sprint_id, 3)

        self.assertEqual(
            [(first, "cancelled"), (second, "cancelled")], self.dispositions()
        )
        self.assertEqual(2, len(self.terminal_messages()))
        self.assertEqual(1, len(self.terminal_events()))
        self.assert_episode(2, 0, 2)

    def test_episode_replay_dedupes_and_added_scope_refires_with_fresh_key(self) -> None:
        first = self.create_unit(developer=1, output_kind="report_only")
        self.lifecycle.arm(self.sprint_id, 3)
        self.deliver_pending_wakes()
        self.accept_arming_notification()
        self.messages.mark_read(self.assignment_message(first), 1)
        self.units.complete(self.sprint_id, first, 1, result="First report")
        self.assert_episode(1, 1, 0)

        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="Replay episode",
        )
        self.lifecycle.resume(
            self.sprint_id, sprint_domain.LifecycleActor("planner", 3)
        )
        self.assertEqual(2, len(self.terminal_messages()))
        self.assertEqual(1, len(self.terminal_events()))

        second = self.create_unit(developer=1, output_kind="no_code")
        self.units.dispatch_ready(self.sprint_id)
        self.deliver_pending_wakes()
        self.messages.mark_read(self.assignment_message(second), 1)
        self.units.complete(self.sprint_id, second, 1, result="Second result")
        self.assertEqual(4, len(self.terminal_messages()))
        self.assertEqual(2, len(self.terminal_events()))
        self.assert_episode(2, 2, 0)

        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="Replay expanded episode",
        )
        self.lifecycle.resume(
            self.sprint_id, sprint_domain.LifecycleActor("planner", 3)
        )
        self.assertEqual(4, len(self.terminal_messages()))
        self.assertEqual(2, len(self.terminal_events()))


class ProductionPulseTest(SprintWorkDispatchCase):
    def test_armed_pulse_never_evaluates_historical_liveness(self) -> None:
        unit = self.create_unit(developer=1)
        self.lifecycle.arm(self.sprint_id, 3)
        message = self.con.execute(
            "SELECT message_id,to_participant_id FROM wake_message "
            "WHERE work_unit_id=? AND message_kind='work_assignment'",
            (unit,),
        ).fetchone()
        self.con.execute(
            "UPDATE wake_message SET disposition='accepted',"
            "read_at='2000-01-01 00:00:00' WHERE message_id=?",
            (message["message_id"],),
        )
        self.con.execute(
            "INSERT OR IGNORE INTO sprint_liveness_expectations "
            "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
            "last_strong_key,next_evaluation_at) VALUES (?,?,?,?,?,?,?)",
            (
                message["message_id"],
                self.sprint_id,
                message["to_participant_id"],
                "2000-01-01 00:00:00",
                "2000-01-01 00:00:00",
                f"message.accepted:{message['message_id']}",
                "2000-01-01 00:05:00",
            ),
        )
        self.con.execute(
            "UPDATE sprint_liveness_expectations SET last_strong_at=?,"
            "last_strong_key=?,next_evaluation_at=? WHERE message_id=?",
            (
                "2000-01-01 00:00:00",
                f"message.accepted:{message['message_id']}",
                "2000-01-01 00:05:00",
                message["message_id"],
            ),
        )
        self.con.commit()
        before = tuple(
            self.con.execute(
                "SELECT last_evaluated_at,next_evaluation_at,nudge_at,escalated_at "
                "FROM sprint_liveness_expectations WHERE message_id=?",
                (message["message_id"],),
            ).fetchone()
        )
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="no-liveness-runtime-test",
        )

        with mock.patch.object(
            sprint_liveness.SprintLivenessMonitor,
            "evaluate",
            side_effect=AssertionError("retired liveness evaluator called"),
        ):
            self.assertTrue(runtime.pulse_once(startup=True))

        self.assertEqual(
            before,
            tuple(
                self.con.execute(
                    "SELECT last_evaluated_at,next_evaluation_at,nudge_at,"
                    "escalated_at FROM sprint_liveness_expectations "
                    "WHERE message_id=?",
                    (message["message_id"],),
                ).fetchone()
            ),
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE message_kind IN ('nudge','escalation')"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type LIKE 'liveness.%'"
            ).fetchone()[0],
        )

    def test_successful_pulses_advance_runtime_heartbeat_but_failure_does_not(self) -> None:
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="heartbeat-runtime-test",
        )

        self.assertFalse(runtime.pulse_once())
        first = self.con.execute(
            "SELECT beat_at,interval_s FROM daemon_heartbeats "
            "WHERE name='sprint-runtime'"
        ).fetchone()
        self.assertEqual(5, first["interval_s"])
        self.assertEqual(
            {
                "state": "live",
                "beat_at": first["beat_at"],
                "interval_seconds": 5,
            },
            sprint_runtime.runtime_status(self.con),
        )

        with mock.patch.object(
            runtime,
            "_deliver_wakes",
            side_effect=RuntimeError("injected delivery failure"),
        ), self.assertRaisesRegex(RuntimeError, "injected delivery failure"):
            runtime.pulse_once()

        after_failure = self.con.execute(
            "SELECT beat_at,interval_s FROM daemon_heartbeats "
            "WHERE name='sprint-runtime'"
        ).fetchone()
        self.assertEqual(tuple(first), tuple(after_failure))

        self.assertFalse(runtime.pulse_once())
        after_success = self.con.execute(
            "SELECT beat_at,interval_s FROM daemon_heartbeats "
            "WHERE name='sprint-runtime'"
        ).fetchone()
        self.assertGreater(after_success["beat_at"], first["beat_at"])
        self.assertEqual(5, after_success["interval_s"])

    def test_initial_failing_pulse_does_not_create_runtime_heartbeat(self) -> None:
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="initial-failure-runtime-test",
        )

        with mock.patch.object(
            runtime,
            "_deliver_wakes",
            side_effect=RuntimeError("startup delivery failed"),
        ), self.assertRaisesRegex(RuntimeError, "startup delivery failed"):
            runtime.pulse_once(startup=True)

        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM daemon_heartbeats "
                "WHERE name='sprint-runtime'"
            ).fetchone()[0],
        )

    def test_armed_pulse_enqueues_every_parallel_ready_lane(self) -> None:
        first = self.create_unit(developer=1, wave=4)
        second = self.create_unit(developer=4, wave=0)
        self.lifecycle.arm(self.sprint_id, 3)
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="runtime-test",
        )

        self.assertTrue(runtime.pulse_once(startup=True))

        first_conversation = self.conversation_for(1)
        second_conversation = self.conversation_for(4)
        planner_conversation = self.conversation_for(3)
        self.assertEqual(
            [(first, "ready"), (second, "ready")],
            self.dispositions(),
        )
        self.assertEqual(
            {
                first_conversation: 1,
                second_conversation: 1,
                planner_conversation: 1,
            },
            {
                str(row[0]): int(row[1])
                for row in self.con.execute(
                    "SELECT conversation_id,COUNT(*) FROM conversation_outbox "
                    "GROUP BY conversation_id"
                )
            },
        )
        self.assertEqual(
            [("delivered", 1), ("delivered", 1), ("delivered", 1)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT state,attempt_count FROM sprint_wake_outbox "
                    "ORDER BY wake_id"
                )
            ],
        )

    def test_armed_pulse_delivers_wake_as_one_idempotent_native_turn(self) -> None:
        unit = self.create_unit(developer=1)
        wake_id = self.lifecycle.arm(self.sprint_id, 3)[0]
        wake_key = self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()[0]
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="runtime-test",
        )

        self.assertTrue(runtime.pulse_once(startup=True))
        self.assertTrue(runtime.pulse_once())
        conversation_id = self.conversation_for(1)
        wake_message_id = int(
            self.con.execute(
                "SELECT message_id FROM wake_message WHERE work_unit_id=? "
                "AND message_kind='work_assignment'",
                (unit,),
            ).fetchone()[0]
        )

        wake = self.con.execute(
            "SELECT state,attempt_count FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()
        self.assertEqual(("delivered", 1), tuple(wake))
        native = self.con.execute(
            "SELECT message_id,body,idempotency_key,state FROM conversation_messages "
            "WHERE conversation_id=? AND idempotency_key=?",
            (conversation_id, wake_key),
        ).fetchone()
        expected_prompt = (
            f"Sprint {self.sprint_id} handoff for your Developer role. Load "
            "`sprint_dev`. Run `sc sprint inbox --sprint "
            f"{self.sprint_id}` now and act on the Sprint message(s) using "
            "`sprint_dev`. Confirm every Sprint write succeeds before stopping. "
            "If a Sprint command failed or did not confirm its durable write, retry "
            "that command. Do not re-check the inbox otherwise — new messages arrive "
            "as their own wakes.\n\n"
            f"## wake_message #{wake_message_id} (declared Force-New)\n\n"
            "Unit 1\n\nOutput 1"
        )
        self.assertEqual(expected_prompt, native["body"])
        self.assertEqual(wake_key, native["idempotency_key"])
        self.assertEqual("queued", native["state"])
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_outbox WHERE message_id=?",
                (native["message_id"],),
            ).fetchone()[0],
        )
        self.assertEqual(
            [(unit, "ready")],
            self.dispositions(),
            "delivery wakes the lane but acceptance remains shell-owned",
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id=? AND idempotency_key=?",
                (conversation_id, wake_key),
            ).fetchone()[0],
            "a repeated armed pulse must not create a duplicate native turn",
        )
        replay_ref = sprint_runtime.enqueue_conversation_turn(
            self.db_path,
            conversation_id,
            expected_prompt,
            wake_key,
        )
        attempt_ref = self.con.execute(
            "SELECT native_run_ref FROM sprint_wake_attempts WHERE wake_id=?",
            (wake_id,),
        ).fetchone()[0]
        self.assertEqual(attempt_ref, replay_ref)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_outbox WHERE message_id=?",
                (native["message_id"],),
            ).fetchone()[0],
            "a crash-window retry with the same wake key must reuse the turn",
        )

    def test_prepared_pulse_performs_no_dispatch_or_native_enqueue(self) -> None:
        unit = self.create_unit(developer=1)
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="runtime-test",
        )

        self.assertFalse(runtime.pulse_once())

        self.assertEqual([(unit, "planned")], self.dispositions())
        self.assertEqual(
            (0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM wake_message),"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox),"
                    "(SELECT COUNT(*) FROM conversation_outbox)"
                ).fetchone()
            ),
        )

    def test_engine_wake_delivery_pulses_without_an_armed_sprint(self) -> None:
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="armed-independent engine notice",
            idempotency_key="engine-pulse-without-armed-sprint",
        )
        delivered: list[tuple[str, str]] = []
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="engine-only-runtime-test",
            deliver=lambda conversation, prompt, _key: (
                delivered.append((conversation, prompt)) or "engine-only-run"
            ),
        )

        self.assertTrue(runtime.pulse_once())

        self.assertEqual(1, len(delivered))
        self.assertIn("armed-independent engine notice", delivered[0][1])
        self.assertEqual(
            ("delivered", 1),
            tuple(
                self.con.execute(
                    "SELECT state,attempt_count FROM sprint_wake_outbox "
                    "WHERE wake_id=?",
                    (sent.wake_id,),
                ).fetchone()
            ),
        )

    def test_server_startup_wires_broker_before_sprint_runtime(self) -> None:
        order: list[str] = []
        preparer = object()
        broker = mock.Mock()
        runtime = mock.Mock()
        runtime.wait_ready.return_value = True
        runtime.is_alive.return_value = True
        with (
            mock.patch.object(
                server.conversation_launch,
                "ConversationLaunchPreparer",
                return_value=preparer,
            ),
            mock.patch.object(
                server.conversation_broker,
                "start_service",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("broker") or broker
                ),
            ) as broker_start,
            mock.patch.object(
                server.conversation_reaper,
                "start_service",
                side_effect=lambda *_args, **_kwargs: order.append("reaper"),
            ) as reaper_start,
            mock.patch.object(
                server.sprint_runtime,
                "start_service",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("sprint") or runtime
                ),
            ) as sprint_start,
            mock.patch.object(
                server.sprint_pr_watcher,
                "start_service",
                side_effect=lambda *_args, **_kwargs: order.append("watcher"),
            ) as watcher_start,
        ):
            server.start_runtime_services()

        self.assertEqual(["broker", "reaper", "sprint", "watcher"], order)
        broker_start.assert_called_once_with(
            server.DB_PATH,
            launch_preparer=preparer,
        )
        reaper_start.assert_called_once_with(
            server.DB_PATH,
            native_interrupt=broker.interrupt,
        )
        sprint_start.assert_called_once_with(server.DB_PATH)
        runtime.wait_ready.assert_called_once()
        runtime.is_alive.assert_called_once_with()
        watcher_start.assert_called_once_with(server.DB_PATH, repo_root=server.REPO_ROOT)
        self.assertIn(
            "on_started=start_runtime_services",
            inspect.getsource(server.main),
            "the combined startup function must remain the production callback",
        )

    def test_server_startup_refuses_runtime_that_never_becomes_ready_or_dies(self) -> None:
        for ready, alive, expected in (
            (False, True, "did not complete its first successful cycle"),
            (True, False, "died during startup"),
        ):
            with self.subTest(ready=ready, alive=alive):
                runtime = mock.Mock()
                runtime.wait_ready.return_value = ready
                runtime.is_alive.return_value = alive
                with (
                    mock.patch.object(
                        server.conversation_broker,
                        "start_service",
                        return_value=mock.Mock(interrupt=mock.Mock()),
                    ),
                    mock.patch.object(server.conversation_reaper, "start_service"),
                    mock.patch.object(
                        server.sprint_runtime,
                        "start_service",
                        return_value=runtime,
                    ),
                    mock.patch.object(
                        server.sprint_pr_watcher,
                        "start_service",
                    ) as watcher_start,
                    self.assertRaisesRegex(RuntimeError, expected),
                ):
                    server.start_runtime_services()

                runtime.stop.assert_called_once_with()
                watcher_start.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
