"""Stage 10 compositional proof for serial and parallel Sprints v2 runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ACCEPTANCE = ROOT / "tests" / "fixtures" / "sprint_v2_acceptance.json"
sys.path[:0] = [str(ENGINE / "scripts"), str(ROOT / "tests")]

import db_driver
import sprint_close
import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
import sprint_review_loop
import sprint_runtime
from github_pull_requests import PullRequest
from test_sprint_v2_domain import apply_schema


class ScenarioGitHub:
    """Mutable GitHub boundary used to drive production watcher transitions."""

    def __init__(self) -> None:
        self.pull_requests: dict[int, PullRequest] = {}
        self.get_calls: list[int] = []
        self.list_calls = 0

    def set(
        self,
        number: int,
        state: str,
        *,
        checks: str | None = "SUCCESS",
        head_sha: str | None = None,
    ) -> PullRequest:
        head = head_sha or f"{number:040x}"
        pull_request = PullRequest(
            number=number,
            head_ref=f"live-proof/pr-{number}",
            base_ref="main",
            head_sha=head,
            state=state,
            merged_at="2026-08-01T00:00:00Z" if state == "MERGED" else None,
            merge_sha=f"{number + 10000:040x}" if state == "MERGED" else None,
            title=f"Live proof PR {number}",
            url=f"https://github.com/acme/live-proof/pull/{number}",
            review_decision="APPROVED" if state in {"OPEN", "MERGED"} else None,
            checks=checks,
            checks_failed=checks == "FAILURE",
        )
        self.pull_requests[number] = pull_request
        return pull_request

    def get(self, number: int) -> PullRequest:
        self.get_calls.append(number)
        return self.pull_requests[number]

    def list(self) -> list[PullRequest]:
        self.list_calls += 1
        return [self.pull_requests[number] for number in sorted(self.pull_requests)]


class SprintLiveProof(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "sprint-live-proof.db"
        seed = sqlite3.connect(self.db_path)
        try:
            apply_schema(seed)
        finally:
            seed.close()
        self.con = db_driver.connect(self.db_path)
        self.addCleanup(self.con.close)
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
        self.con.commit()
        self.github = ScenarioGitHub()
        self.reader_factory = lambda _repository: self.github

    def prepare(
        self,
        lanes: tuple[tuple[int, int, tuple[int, ...]], ...],
    ) -> tuple[int, int, list[int]]:
        """Seed one eligible declaration, then create lanes through the store."""
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Live proof feature','in_progress')"
            ).lastrowid
        )
        body = "Sprints v2 live proof contract"
        document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Live proof spec',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            self.con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
        sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (sprint_id, document_id, revision, approval_id),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) VALUES (?,?,?,?,?,?)",
            (
                (sprint_id, 3, "planner", "codex", "planner-model", "high"),
                (sprint_id, 1, "developer", "codex", "dev-model", "high"),
                (sprint_id, 2, "reviewer", "kimi", "review-model", "high"),
                (sprint_id, 4, "developer", "codex", "dev-model", "high"),
                (sprint_id, 5, "reviewer", "kimi", "review-model", "high"),
            ),
        )
        task_ids = [
            int(
                self.con.execute(
                    "INSERT INTO spec_tasks "
                    "(feature_id,document_id,seq,title) VALUES (?,?,?,?)",
                    (feature_id, document_id, index, f"Live task {index}"),
                ).lastrowid
            )
            for index in range(len(lanes))
        ]
        self.con.commit()

        units = sprint_domain.SprintWorkUnitStore(self.con)
        unit_ids: list[int] = []
        for index, (developer, reviewer, dependency_indexes) in enumerate(lanes):
            unit_ids.append(
                units.create(
                    sprint_id,
                    3,
                    assigned_shell_id=developer,
                    reviewer_shell_id=reviewer,
                    title=f"Live lane {index + 1}",
                    expected_output=f"Merged live lane {index + 1}",
                    task_ids=(task_ids[index],),
                    planned_wave=0,
                    dependency_ids=tuple(unit_ids[item] for item in dependency_indexes),
                )
            )
        return sprint_id, document_id, unit_ids

    def watcher(self) -> sprint_pr_watcher.SprintPRWatcher:
        return sprint_pr_watcher.SprintPRWatcher(
            self.con,
            repo_root=ROOT,
            reader_factory=self.reader_factory,
        )

    def deliver_browser_turns(self) -> None:
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            pulse_seconds=1,
        )
        self.assertTrue(runtime.pulse_once())

    def assignment_message(self, unit_id: int) -> int:
        row = self.con.execute(
            "SELECT message_id FROM sprint_messages "
            "WHERE work_unit_id=? AND message_kind='work_assignment' "
            "ORDER BY message_id DESC LIMIT 1",
            (unit_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    def accept_assignment(self, unit_id: int, developer: int) -> None:
        message_id = self.assignment_message(unit_id)
        self.assertEqual(
            "accepted",
            sprint_message_delivery.SprintMessageStore(self.con).mark_read(
                message_id, developer
            ),
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )

    def review_and_merge(
        self,
        watcher: sprint_pr_watcher.SprintPRWatcher,
        *,
        sprint_id: int,
        unit_id: int,
        developer: int,
        reviewer: int,
        pr_number: int,
        request_changes: bool = False,
    ) -> int:
        self.github.set(pr_number, "OPEN")
        registration = watcher.register(
            sprint_id,
            owner_shell_id=developer,
            repository="acme/live-proof",
            pr_number=pr_number,
            work_unit_ids=(unit_id,),
        )
        loop = sprint_review_loop.SprintReviewLoopStore(
            self.con,
            repo_root=ROOT,
            reader_factory=self.reader_factory,
        )
        messages = sprint_message_delivery.SprintMessageStore(self.con)

        handoff = loop.request_review(
            sprint_id,
            registration.registered_pr_id,
            developer,
            readiness=f"PR {pr_number} is green and ready.",
            idempotency_key=f"proof:{pr_number}:review:1",
        )
        self.assertEqual("accepted", messages.mark_read(handoff.message_id, reviewer))
        if request_changes:
            outcome = loop.record_review(
                sprint_id,
                registration.registered_pr_id,
                reviewer,
                verdict="changes_requested",
                body="Exercise the correction loop before approval.",
                idempotency_key=f"proof:{pr_number}:changes",
            )
            self.assertEqual("fixing", outcome.disposition)
            self.github.set(pr_number, "OPEN", checks="FAILURE")
            self.assertTrue(watcher.poll_once())
            self.github.set(pr_number, "OPEN")
            self.assertTrue(watcher.poll_once())
            handoff = loop.request_review(
                sprint_id,
                registration.registered_pr_id,
                developer,
                readiness=f"PR {pr_number} correction is green.",
                idempotency_key=f"proof:{pr_number}:review:2",
            )
            self.assertEqual(
                "accepted", messages.mark_read(handoff.message_id, reviewer)
            )

        approval = loop.record_review(
            sprint_id,
            registration.registered_pr_id,
            reviewer,
            verdict="approved",
            body="No Medium-or-higher findings remain.",
            idempotency_key=f"proof:{pr_number}:approved",
        )
        self.assertEqual("merge_ready", approval.disposition)
        authorization = loop.authorize_merge(
            sprint_id, registration.registered_pr_id, developer
        )
        self.assertEqual(f"{pr_number:040x}", authorization.head_sha)
        self.github.set(pr_number, "MERGED")
        self.assertTrue(watcher.poll_once())
        self.assertEqual(
            "completed",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )
        return registration.registered_pr_id

    def close(self, sprint_id: int, document_id: int) -> dict:
        close = sprint_close.SprintCloseStore(self.con)
        receipt = close.record_conformance(
            sprint_id,
            2,
            body="Integrated live proof matches its bound contract.",
            findings=(),
            idempotency_key=f"proof:{sprint_id}:conformance",
        )
        self.assertEqual((), receipt.followup_ids)
        self.assertTrue(
            sprint_domain.SprintLifecycleStore(self.con).transition(
                sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="delivery and conformance complete",
                terminal_outcome="accepted",
            )
        )
        packet = close.compile_evidence_packet(sprint_id, 3)
        self.assertEqual("completed", packet["scope"]["lifecycle"])
        self.assertEqual(
            document_id, packet["spec_revisions"]["bound"][0]["document_id"]
        )
        self.assertEqual([], packet["unresolved_work"]["work_units"]["items"])
        self.assertEqual([], packet["unresolved_work"]["followups"]["items"])
        return packet

    def test_serial_sprint_runs_correction_merge_dispatch_and_close(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (1, 2, (0,))))
        initial_wakes = sprint_domain.SprintLifecycleStore(self.con).arm(sprint_id, 3)
        self.assertEqual(1, len(initial_wakes))
        self.assertEqual(
            [(units[0], "ready"), (units[1], "planned")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
        )
        self.deliver_browser_turns()
        self.accept_assignment(units[0], 1)
        watcher = self.watcher()
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[0],
            developer=1,
            reviewer=2,
            pr_number=1001,
            request_changes=True,
        )
        self.assertEqual(
            "ready",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (units[1],),
            ).fetchone()[0],
        )
        self.deliver_browser_turns()
        self.accept_assignment(units[1], 1)
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[1],
            developer=1,
            reviewer=2,
            pr_number=1002,
        )
        packet = self.close(sprint_id, document_id)

        event_types = [
            row[0]
            for row in self.con.execute(
                "SELECT event_type FROM sprint_events WHERE sprint_id=? ORDER BY event_id",
                (sprint_id,),
            )
        ]
        self.assertEqual(2, event_types.count("work_unit.completed"))
        self.assertEqual(2, event_types.count("merge.authorized"))
        self.assertEqual(1, event_types.count("review.changes_requested"))
        self.assertEqual(2, event_types.count("review.approved"))
        self.assertEqual("lifecycle.completed", event_types[-1])
        self.assertEqual(2, packet["pr_outcomes"]["total"])
        self.assertEqual(
            ["work", "fix", "merge", "merge"],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT link.purpose FROM sprint_participant_conversations link "
                    "JOIN sprint_participants p "
                    "ON p.participant_id=link.sprint_participant_id "
                    "WHERE p.sprint_id=? AND p.shell_id=1 "
                    "ORDER BY link.participant_conversation_id",
                    (sprint_id,),
                )
            ],
        )

    def test_parallel_sprint_completes_out_of_order_without_lane_overlap(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (4, 5, ())))
        initial_wakes = sprint_domain.SprintLifecycleStore(self.con).arm(sprint_id, 3)
        self.assertEqual(2, len(initial_wakes))
        self.deliver_browser_turns()
        self.accept_assignment(units[0], 1)
        self.accept_assignment(units[1], 4)
        watcher = self.watcher()

        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[1],
            developer=4,
            reviewer=5,
            pr_number=2002,
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (units[0],),
            ).fetchone()[0],
        )
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[0],
            developer=1,
            reviewer=2,
            pr_number=2001,
        )
        packet = self.close(sprint_id, document_id)

        completions = [
            json.loads(row[0])["work_unit_id"]
            for row in self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='work_unit.completed' ORDER BY event_id",
                (sprint_id,),
            )
        ]
        self.assertEqual([units[1], units[0]], completions)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations "
                "WHERE sprint_id=? AND resolved_at IS NULL",
                (sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(2, packet["planned_vs_actual"]["total"])
        self.assertEqual(2, packet["pr_outcomes"]["total"])

    def test_conversation_and_watcher_writes_share_wal_without_lock_loss(self) -> None:
        sprint_id, _document_id, units = self.prepare(((1, 2, ()),))
        sprint_domain.SprintLifecycleStore(self.con).arm(sprint_id, 3)
        self.accept_assignment(units[0], 1)
        self.github.set(3001, "OPEN", checks="PENDING")
        watcher = self.watcher()
        watcher.register(
            sprint_id,
            owner_shell_id=1,
            repository="acme/live-proof",
            pr_number=3001,
            work_unit_ids=(units[0],),
        )
        self.github.set(3001, "OPEN")
        conversation_id = str(
            self.con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=1",
                (sprint_id,),
            ).fetchone()[0]
        )
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def conversation_write() -> None:
            try:
                barrier.wait()
                sprint_runtime.enqueue_conversation_turn(
                    self.db_path,
                    conversation_id,
                    sprint_message_delivery.FIXED_WAKE_PROMPT,
                    "live-proof:concurrent-conversation",
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)

        def watcher_write() -> None:
            con = db_driver.connect(self.db_path)
            try:
                barrier.wait()
                sprint_pr_watcher.SprintPRWatcher(
                    con,
                    repo_root=ROOT,
                    reader_factory=self.reader_factory,
                ).poll_once()
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)
            finally:
                con.close()

        threads = [
            threading.Thread(target=conversation_write),
            threading.Thread(target=watcher_write),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE idempotency_key='live-proof:concurrent-conversation'"
            ).fetchone()[0],
        )
        self.assertEqual(
            [("pending",), ("green",)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state FROM sprint_pr_transitions "
                    "ORDER BY transition_id"
                )
            ],
        )

    def test_adversarial_acceptance_manifest_references_real_gates(self) -> None:
        manifest = json.loads(ACCEPTANCE.read_text())
        self.assertEqual(46, manifest["spec_document_id"])
        self.assertEqual(18, len(manifest["scenarios"]))
        self.assertEqual(6, len(manifest["invariant_sweep"]))
        entries = manifest["scenarios"] + manifest["invariant_sweep"]
        identities = {(entry["file"], entry["test"]) for entry in entries}
        self.assertEqual(len(entries), len(identities))
        for entry in entries:
            with self.subTest(entry=entry.get("scenario") or entry["invariant"]):
                source = ROOT.joinpath(entry["file"]).read_text()
                self.assertIn(f"def {entry['test']}", source)


if __name__ == "__main__":
    unittest.main()
