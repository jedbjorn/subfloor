"""Stage 10 compositional proof for serial and parallel Sprints v2 runs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ACCEPTANCE = ROOT / "tests" / "fixtures" / "sprint_v2_acceptance.json"
HANDOFF_ACCEPTANCE = (
    ROOT / "tests" / "fixtures" / "sprint_handoff_hardening_acceptance.json"
)
sys.path[:0] = [
    str(ENGINE / "scripts"),
    str(ENGINE / "api"),
    str(ROOT / "tests"),
]

import db_driver
import mem
import server
import sprint_cli
import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
import sprint_runtime
from github_pull_requests import PullRequest
from test_sprint_v2_domain import apply_schema

TOKENS = {
    1: "live-dev-1",
    2: "live-review-1",
    3: "live-planner",
    4: "live-dev-2",
    5: "live-review-2",
}


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
        base_sha: str = "e" * 40,
    ) -> PullRequest:
        head = head_sha or f"{number:040x}"
        pull_request = PullRequest(
            number=number,
            head_ref=f"live-proof/pr-{number}",
            base_ref="live-proof/base",
            head_sha=head,
            state=state,
            merged_at="2026-08-01T00:00:00Z" if state == "MERGED" else None,
            merge_sha=f"{number + 10000:040x}" if state == "MERGED" else None,
            title=f"Live proof PR {number}",
            url=f"https://github.com/acme/live-proof/pull/{number}",
            review_decision="APPROVED" if state in {"OPEN", "MERGED"} else None,
            checks=checks,
            checks_failed=checks == "FAILURE",
            base_sha=base_sha,
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
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        quiet_env = mock.patch.dict(
            "os.environ", {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
        )
        quiet_env.start()
        self.addCleanup(quiet_env.stop)
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
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,?,1,?)",
            (
                (1, "Developer one", "DEV1", "dev", "prompt", TOKENS[1]),
                (2, "Reviewer one", "REV1", "reviewer", "prompt", TOKENS[2]),
                (3, "Planner", "PLN1", "planner", "prompt", TOKENS[3]),
                (4, "Developer two", "DEV2", "dev", "prompt", TOKENS[4]),
                (5, "Reviewer two", "REV2", "reviewer", "prompt", TOKENS[5]),
            ),
        )
        self.con.commit()
        server.DB_PATH = self.db_path
        self.github = ScenarioGitHub()
        self.reader_factory = lambda _repository: self.github
        self.input_counter = 0

    def write_input(self, body: str) -> str:
        path = Path(self.temp_dir.name) / f"input-{self.input_counter}.txt"
        self.input_counter += 1
        path.write_text(body)
        return str(path)

    def run_cli(self, shell_id: int, *argv: str) -> dict:
        mem.SC_API_TOKEN = TOKENS[shell_id]
        output = io.StringIO()
        with (
            mock.patch.object(
                server.sprint_domain,
                "adapter_for",
                return_value=mock.Mock(probe=mock.Mock(return_value=None)),
            ),
            mock.patch.object(
                server.sprint_pr_watcher,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            mock.patch.object(
                server.sprint_review_loop,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            mock.patch.object(
                server.sprint_recovery,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, sprint_cli.main(list(argv)))
        return json.loads(output.getvalue())

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
        declaration = self.run_cli(
            3,
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.write_input(
                json.dumps(
                    [
                        {
                            "shell_id": 3,
                            "role": "planner",
                            "harness": "codex",
                            "model": "planner-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 1,
                            "role": "developer",
                            "harness": "codex",
                            "model": "dev-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 2,
                            "role": "reviewer",
                            "harness": "kimi",
                            "model": "review-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 4,
                            "role": "developer",
                            "harness": "codex",
                            "model": "dev-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 5,
                            "role": "reviewer",
                            "harness": "kimi",
                            "model": "review-model",
                            "effort": "high",
                        },
                    ]
                )
            ),
            "--merge-grant",
        )
        sprint_id = declaration["sprint_id"]
        unit_ids: list[int] = []
        for index, (developer, reviewer, dependency_indexes) in enumerate(lanes):
            argv = [
                "plan-unit",
                "--sprint",
                str(sprint_id),
                "--developer-shell",
                str(developer),
                "--reviewer-shell",
                str(reviewer),
                "--title",
                f"Live lane {index + 1}",
                "--expected-output-file",
                self.write_input(f"Merged live lane {index + 1}"),
                "--task",
                str(task_ids[index]),
                "--wave",
                "0",
            ]
            for dependency_index in dependency_indexes:
                argv.extend(("--depends-on", str(unit_ids[dependency_index])))
            unit_ids.append(self.run_cli(3, *argv)["work_unit_id"])
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

    def deliver_terminal_turn(self, wake_id: int) -> tuple[str, str]:
        """Deliver one wake into a native turn that has already terminated."""
        captured: list[tuple[str, str]] = []

        def deliver(conversation_id: str, prompt: str, key: str) -> str:
            captured.append((conversation_id, prompt))
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','live-proof','prompt',?,?,?,'completed',"
                    "'2026-08-01 00:00:01')",
                    (conversation_id, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                    "lease_expires_at,started_at,ended_at,exit_code) "
                    "SELECT ?,shell_id,?,'succeeded','live-proof',"
                    "'2026-08-01 00:00:01','2026-08-01 00:00:00',"
                    "'2026-08-01 00:00:01',0 FROM conversations "
                    "WHERE conversation_id=?",
                    (conversation_id, message_id, conversation_id),
                ).lastrowid
            )
            for state in ("queued", "running", "waiting"):
                self.con.execute(
                    "UPDATE conversations SET state=? WHERE conversation_id=?",
                    (state, conversation_id),
                )
            self.con.commit()
            return f"conversation-run:{run_id}"

        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(f"live-proof-terminal:{wake_id}", deliver)
        self.assertIsNotNone(outcome)
        self.assertEqual(wake_id, outcome.wake_id)
        self.assertEqual("delivered", outcome.state)
        return captured[0]

    def assignment_message(self, unit_id: int) -> int:
        row = self.con.execute(
            "SELECT message_id FROM wake_message "
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
        registration = self.run_cli(
            developer,
            "register-pr",
            "--sprint",
            str(sprint_id),
            "--repository",
            "acme/live-proof",
            "--pr",
            str(pr_number),
            "--work-unit",
            str(unit_id),
        )
        messages = sprint_message_delivery.SprintMessageStore(self.con)

        handoff = self.run_cli(
            developer,
            "request-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--readiness-file",
            self.write_input(f"PR {pr_number} is green and ready."),
            "--key",
            f"proof:{pr_number}:review:1",
        )
        self.deliver_browser_turns()
        self.assertEqual(
            "accepted", messages.mark_read(handoff["message_id"], reviewer)
        )
        if request_changes:
            outcome = self.run_cli(
                reviewer,
                "record-review",
                "--sprint",
                str(sprint_id),
                "--registered-pr",
                str(registration["registered_pr_id"]),
                "--verdict",
                "changes_requested",
                "--body-file",
                self.write_input("Exercise the correction loop before approval."),
                "--key",
                f"proof:{pr_number}:changes",
            )
            self.assertEqual("fixing", outcome["disposition"])
            self.github.set(pr_number, "OPEN", checks="FAILURE")
            self.assertTrue(watcher.poll_once())
            self.github.set(pr_number, "OPEN")
            self.assertTrue(watcher.poll_once())
            handoff = self.run_cli(
                developer,
                "request-review",
                "--sprint",
                str(sprint_id),
                "--registered-pr",
                str(registration["registered_pr_id"]),
                "--readiness-file",
                self.write_input(f"PR {pr_number} correction is green."),
                "--key",
                f"proof:{pr_number}:review:2",
            )
            self.deliver_browser_turns()
            self.assertEqual(
                "accepted", messages.mark_read(handoff["message_id"], reviewer)
            )

        approval = self.run_cli(
            reviewer,
            "record-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--verdict",
            "approved",
            "--body-file",
            self.write_input("No Medium-or-higher findings remain."),
            "--key",
            f"proof:{pr_number}:approved",
        )
        self.assertEqual("merge_ready", approval["disposition"])
        authorization = self.run_cli(
            developer,
            "authorize-merge",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
        )
        self.assertEqual(f"{pr_number:040x}", authorization["head_sha"])
        self.github.set(pr_number, "MERGED")
        self.assertTrue(watcher.poll_once())
        self.assertEqual(
            "completed",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )
        return registration["registered_pr_id"]

    def close(self, sprint_id: int, document_id: int) -> dict:
        receipt = self.run_cli(
            2,
            "record-conformance",
            "--sprint",
            str(sprint_id),
            "--body-file",
            self.write_input("Integrated live proof matches its bound contract."),
            "--findings-file",
            self.write_input("[]"),
            "--final-report-file",
            self.write_input(
                "Reviewer final report: the live proof matches its bound contract."
            ),
            "--reason",
            "delivery and conformance complete",
            "--outcome",
            "accepted",
            "--key",
            f"proof:{sprint_id}:conformance",
        )
        self.assertEqual([], receipt["followup_ids"])
        self.assertTrue(receipt["completed"])
        packet = self.run_cli(
            3,
            "compile-report",
            "--sprint",
            str(sprint_id),
            "--limit",
            "50",
        )
        self.assertEqual("completed", packet["scope"]["lifecycle"])
        self.assertEqual(
            document_id, packet["spec_revisions"]["bound"][0]["document_id"]
        )
        self.assertEqual([], packet["unresolved_work"]["work_units"]["items"])
        self.assertEqual([], packet["unresolved_work"]["followups"]["items"])
        return packet

    def test_serial_sprint_runs_correction_merge_dispatch_and_close(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (1, 2, (0,))))
        initial_wakes = self.run_cli(3, "arm", "--sprint", str(sprint_id))["wake_ids"]
        self.assertEqual(2, len(initial_wakes))
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
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations link "
                "JOIN sprint_participants p "
                "ON p.participant_id=link.sprint_participant_id "
                "WHERE p.sprint_id=? AND p.shell_id=1",
                (sprint_id,),
            ).fetchone()[0],
        )

    def test_parallel_sprint_completes_out_of_order_without_lane_overlap(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (4, 5, ())))
        initial_wakes = self.run_cli(3, "arm", "--sprint", str(sprint_id))["wake_ids"]
        self.assertEqual(3, len(initial_wakes))
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
        sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        ).arm(sprint_id, 3)
        self.deliver_browser_turns()
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
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=1",
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
                    "concurrent Sprint turn",
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

    def test_relay_review_and_terminal_pickup_recovery_form_one_durable_chain(
        self,
    ) -> None:
        sprint_id, _document_id, units = self.prepare(((1, 2, ()),))
        self.run_cli(3, "arm", "--sprint", str(sprint_id))
        self.deliver_browser_turns()
        assignment_id = self.assignment_message(units[0])
        developer_start = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            assignment_id,
            [message["message_id"] for message in developer_start["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(assignment_id),
        )

        arming_planner_conversation = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.sprint_id=? AND participant.shell_id=3",
            (sprint_id,),
        ).fetchone()[0]
        self.assertIsNotNone(
            arming_planner_conversation,
            "arming opens the overseeing Planner's fresh wake chat",
        )
        question_prefix = "Which downstream compatibility fixture owns the proof? "
        question = question_prefix + "q" * (6000 - len(question_prefix))
        question_receipt = self.run_cli(
            1,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "PLN1",
            "--body-file",
            self.write_input(question),
            "--key",
            "proof:68:question",
        )
        self.assertTrue(question_receipt["message_created"])
        self.assertEqual("pending", question_receipt["wake_state"])
        self.assertEqual(
            arming_planner_conversation,
            question_receipt["conversation_id"],
            "later Planner-bound traffic re-enters the chat opened by arming",
        )
        self.assertEqual(
            (1, 3, 6000, question),
            tuple(
                self.con.execute(
                    "SELECT sender.shell_id,recipient.shell_id,length(m.body),m.body "
                    "FROM wake_message m "
                    "JOIN sprint_participants sender "
                    "ON sender.participant_id=m.from_participant_id "
                    "JOIN sprint_participants recipient "
                    "ON recipient.participant_id=m.to_participant_id "
                    "WHERE m.message_id=?",
                    (question_receipt["message_id"],),
                ).fetchone()
            ),
        )
        self.deliver_browser_turns()
        planner_conversation = str(
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=3",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertNotEqual("None", planner_conversation)
        planner_inbox = self.run_cli(3, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            (question_receipt["message_id"], question),
            [
                (message["message_id"], message["body"])
                for message in planner_inbox["messages"]
            ],
        )
        delivered_prompt = self.con.execute(
                "SELECT cm.body FROM sprint_wake_outbox w "
                "JOIN conversation_messages cm "
                "ON cm.idempotency_key=w.idempotency_key WHERE w.wake_id=?",
                (question_receipt["wake_id"],),
            ).fetchone()[0]
        self.assertIn(
            sprint_message_delivery.wake_prompt(sprint_id, "planner"),
            delivered_prompt,
        )
        self.assertIn(question, delivered_prompt)
        self.run_cli(
            3,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(question_receipt["message_id"]),
        )

        answer_receipt = self.run_cli(
            3,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "DEV1",
            "--body-file",
            self.write_input(
                "Use the legacy update fixture with its pre-existing linked worktree."
            ),
            "--key",
            "proof:68:answer",
        )
        self.deliver_browser_turns()
        developer_answer = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            answer_receipt["message_id"],
            [message["message_id"] for message in developer_answer["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(answer_receipt["message_id"]),
        )

        self.github.set(6801, "OPEN")
        registration = self.run_cli(
            1,
            "register-pr",
            "--sprint",
            str(sprint_id),
            "--repository",
            "acme/live-proof",
            "--pr",
            "6801",
            "--work-unit",
            str(units[0]),
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT (SELECT chat_id FROM active_shell_chats "
                "WHERE shell_id=participant.shell_id) "
                "FROM sprint_participants participant "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        review_request = self.run_cli(
            1,
            "request-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--readiness-file",
            self.write_input("The downstream handoff proof is green and ready."),
            "--key",
            "proof:68:dev-to-review",
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT (SELECT chat_id FROM active_shell_chats "
                "WHERE shell_id=participant.shell_id) "
                "FROM sprint_participants participant "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.deliver_browser_turns()
        reviewer_new = str(
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertNotEqual("None", reviewer_new)
        reviewer_inbox = self.run_cli(2, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            review_request["message_id"],
            [message["message_id"] for message in reviewer_inbox["messages"]],
        )
        self.run_cli(
            2,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(review_request["message_id"]),
        )

        outcome = self.run_cli(
            2,
            "record-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--verdict",
            "changes_requested",
            "--body-file",
            self.write_input("Retain the terminal-turn recovery evidence."),
            "--key",
            "proof:68:review-to-dev",
        )
        self.deliver_browser_turns()
        developer_outcome = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            outcome["message_id"],
            [message["message_id"] for message in developer_outcome["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(outcome["message_id"]),
        )

        recovery_message = self.run_cli(
            3,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "DEV1",
            "--body-file",
            self.write_input("Preserve this separate delivered-unread recovery proof."),
            "--key",
            "proof:68:delivered-unread",
        )
        terminal_conversation, terminal_prompt = self.deliver_terminal_turn(
            recovery_message["wake_id"]
        )
        self.assertEqual(recovery_message["conversation_id"], terminal_conversation)
        self.assertIn(
            sprint_message_delivery.wake_prompt(sprint_id, "developer"),
            terminal_prompt,
        )
        self.assertIn(
            "Preserve this separate delivered-unread recovery proof.",
            terminal_prompt,
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT read_at FROM wake_message WHERE message_id=?",
                (recovery_message["message_id"],),
            ).fetchone()[0]
        )
        prior_pickup_turns = [
            int(row[0])
            for row in self.con.execute(
                "SELECT m.message_id FROM conversation_messages m "
                "JOIN sprint_participant_conversations pc "
                "ON pc.conversation_id=m.conversation_id "
                "JOIN sprint_participants p "
                "ON p.participant_id=pc.sprint_participant_id "
                "WHERE p.sprint_id=? AND p.shell_id=1 AND m.state='queued'",
                (sprint_id,),
            )
        ]
        for message_id in prior_pickup_turns:
            self.con.execute(
                "UPDATE conversation_messages SET state='running' WHERE message_id=?",
                (message_id,),
            )
            self.con.execute(
                "UPDATE conversation_messages SET state='completed',"
                "completed_at=datetime('now') WHERE message_id=?",
                (message_id,),
            )
        self.con.commit()

        self.run_cli(
            1,
            "pause",
            "--sprint",
            str(sprint_id),
            "--reason",
            "Prove delivered-unread pickup recovery",
        )
        resumed = self.run_cli(
            3,
            "resume",
            "--sprint",
            str(sprint_id),
            "--reason",
            "Restore the Developer correction handoff",
        )
        self.assertEqual(1, len(resumed["requeued_wake_ids"]))
        replacement = resumed["requeued_wake_ids"][0]
        self.assertNotEqual(recovery_message["wake_id"], replacement)
        self.deliver_browser_turns()
        recovered_inbox = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            recovery_message["message_id"],
            [message["message_id"] for message in recovered_inbox["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(recovery_message["message_id"]),
        )
        evidence = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.requeued' ORDER BY event_id DESC LIMIT 1",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("delivered", evidence["prior_wake_state"])
        self.assertEqual("completed", evidence["prior_turn_state"]["message_state"])
        self.assertEqual("succeeded", evidence["prior_turn_state"]["run_state"])
        self.assertEqual(recovery_message["wake_id"], evidence["prior_wake_id"])
        self.assertEqual(replacement, evidence["replacement_wake_id"])
        self.assertEqual(
            ("fixing", "armed", "delivered"),
            tuple(
                self.con.execute(
                    "SELECT u.disposition,s.lifecycle,w.state "
                    "FROM sprint_work_units u JOIN sprints s USING (sprint_id) "
                    "JOIN sprint_wake_outbox w ON w.wake_id=? "
                    "WHERE u.work_unit_id=?",
                    (replacement, units[0]),
                ).fetchone()
            ),
        )

    def test_handoff_hardening_manifest_references_compositional_proof(self) -> None:
        manifest = json.loads(HANDOFF_ACCEPTANCE.read_text())
        self.assertEqual(68, manifest["spec_document_id"])
        self.assertEqual(6, len(manifest["gates"]))
        identities = {(entry["file"], entry["test"]) for entry in manifest["gates"]}
        self.assertEqual(len(manifest["gates"]), len(identities))
        for entry in manifest["gates"]:
            with self.subTest(gate=entry["gate"]):
                source = ROOT.joinpath(entry["file"]).read_text()
                self.assertIn(f"def {entry['test']}", source)

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
