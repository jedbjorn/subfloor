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
import sprint_message_delivery as delivery
import sprint_runtime


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintWorkDispatchCase(unittest.TestCase):
    def setUp(self) -> None:
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
        self.lifecycle = sprint_domain.SprintLifecycleStore(self.con)
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
                "SELECT message_id FROM sprint_messages "
                "WHERE work_unit_id=? AND message_kind='work_assignment' "
                "ORDER BY message_id DESC LIMIT 1",
                (unit_id,),
            ).fetchone()[0]
        )

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
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=?",
                (self.sprint_id, shell_id),
            ).fetchone()[0]
        )


class DispatchGateTest(SprintWorkDispatchCase):
    def test_ready_units_launch_in_parallel_regardless_of_wave_or_reviewer(self) -> None:
        late_wave = self.create_unit(developer=1, wave=9, title="Late wave")
        early_wave = self.create_unit(developer=4, wave=0, title="Early wave")

        wake_ids = self.lifecycle.arm(self.sprint_id, 3)

        self.assertEqual(2, len(wake_ids))
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
            [early_wave, late_wave],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT work_unit_id FROM sprint_messages "
                    "WHERE message_kind='work_assignment' ORDER BY message_id"
                )
            ],
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
                    "SELECT work_unit_id FROM sprint_messages "
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
            "SELECT idempotency_key FROM sprint_messages WHERE message_id=?",
            (first_message,),
        ).fetchone()[0]

        self.messages.decline(first_message, 1, "capacity changed")
        released = self.units.dispatch_ready(self.sprint_id)

        second_message = self.assignment_message(unit)
        second_key = self.con.execute(
            "SELECT idempotency_key FROM sprint_messages WHERE message_id=?",
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
                    "SELECT disposition,decline_reason FROM sprint_messages "
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


class ProductionPulseTest(SprintWorkDispatchCase):
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
        self.assertEqual(
            [(first, "ready"), (second, "ready")],
            self.dispositions(),
        )
        self.assertEqual(
            {first_conversation: 1, second_conversation: 1},
            {
                str(row[0]): int(row[1])
                for row in self.con.execute(
                    "SELECT conversation_id,COUNT(*) FROM conversation_outbox "
                    "GROUP BY conversation_id"
                )
            },
        )
        self.assertEqual(
            [("delivered", 1), ("delivered", 1)],
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
        conversation_id = self.conversation_for(1)
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="runtime-test",
        )

        self.assertTrue(runtime.pulse_once(startup=True))
        self.assertTrue(runtime.pulse_once())

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
            "If the handoff is not complete, load `sprint_dev` again and run `sc "
            f"sprint inbox --sprint {self.sprint_id}` again."
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
                    "(SELECT COUNT(*) FROM sprint_messages),"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox),"
                    "(SELECT COUNT(*) FROM conversation_outbox)"
                ).fetchone()
            ),
        )

    def test_server_startup_wires_broker_before_sprint_runtime(self) -> None:
        order: list[str] = []
        preparer = object()
        with (
            mock.patch.object(
                server.conversation_launch,
                "ConversationLaunchPreparer",
                return_value=preparer,
            ),
            mock.patch.object(
                server.conversation_broker,
                "start_service",
                side_effect=lambda *_args, **_kwargs: order.append("broker"),
            ) as broker_start,
            mock.patch.object(
                server.sprint_runtime,
                "start_service",
                side_effect=lambda *_args, **_kwargs: order.append("sprint"),
            ) as sprint_start,
        ):
            server.start_runtime_services()

        self.assertEqual(["broker", "sprint"], order)
        broker_start.assert_called_once_with(
            server.DB_PATH,
            launch_preparer=preparer,
        )
        sprint_start.assert_called_once_with(server.DB_PATH)
        self.assertIn(
            "on_started=start_runtime_services",
            inspect.getsource(server.main),
            "the combined startup function must remain the production callback",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
