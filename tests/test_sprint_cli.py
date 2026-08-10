"""Authenticated end-to-end gates for the shell-facing Sprint commands."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api"), str(ROOT / "tests")]

import mem
import pr_cli
import server
import sprint_cli
import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
from github_pull_requests import PullRequest
from test_sprint_v2_domain import apply_schema

TOKENS = {
    "admin": "admin-token",
    "developer": "dev-token",
    "reviewer": "review-token",
    "planner": "planner-token",
}


class SprintCliDispatcherTest(unittest.TestCase):
    def test_sc_sprint_help_dispatches_to_shipped_cli(self):
        completed = subprocess.run(
            [str(ROOT / "sc"), "sprint", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Authenticated Sprints v2 actions", completed.stdout)
        self.assertIn("record-qaqc", completed.stdout)
        self.assertIn("compile-report", completed.stdout)
        self.assertIn("watcher-state", completed.stdout)
        self.assertIn("reconcile-pr", completed.stdout)


class Reader:
    def get(self, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            head_ref="feature/sprint",
            base_ref="main",
            head_sha="a" * 40,
            state="OPEN",
            merged_at=None,
            merge_sha=None,
            title="Sprint PR",
            url=f"https://github.com/acme/repo/pull/{number}",
            review_decision="APPROVED",
            checks="SUCCESS",
            checks_failed=False,
            base_sha="c" * 40,
        )


class SprintCliApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell.db"
        con = sqlite3.connect(cls.db)
        con.row_factory = sqlite3.Row
        apply_schema(con)
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,?,1,?)",
            (
                (1, "Developer", "DEV1", "dev", "prompt", TOKENS["developer"]),
                (2, "Reviewer", "REV1", "reviewer", "prompt", TOKENS["reviewer"]),
                (3, "Planner", "PLN1", "planner", "prompt", TOKENS["planner"]),
                (4, "Developer 2", "DEV2", "dev", "prompt", "dev2-token"),
                (5, "FnB", "FNB", "admin", "prompt", TOKENS["admin"]),
            ),
        )
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Sprint feature','in_progress')"
            ).lastrowid
        )
        body = "bound sprint spec"
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Spec',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
        cls.sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (cls.sprint_id, document_id, revision, approval_id),
        )
        con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,?,?,?)",
            (
                (cls.sprint_id, 3, "planner", "codex"),
                (cls.sprint_id, 1, "developer", "codex"),
                (cls.sprint_id, 2, "reviewer", "kimi"),
                (cls.sprint_id, 4, "developer", "codex"),
            ),
        )
        participants = {
            int(row["shell_id"]): int(row["participant_id"])
            for row in con.execute(
                "SELECT shell_id,participant_id FROM sprint_participants "
                "WHERE sprint_id=?",
                (cls.sprint_id,),
            )
        }
        cls.unit_id = int(
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,1,2,'Unit','Ship it')",
                (cls.sprint_id,),
            ).lastrowid
        )
        con.commit()
        initial_wake = sprint_domain.SprintLifecycleStore(
            con, probe_harness=lambda _harness: None
        ).arm(cls.sprint_id, 3)[0]
        initial_message = int(
            con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (initial_wake,),
            ).fetchone()[0]
        )
        # Stamp only the assignment wake delivered (the arming wake must stay
        # pending for the surfaces below): reading a force-new assignment
        # requires confirmed delivery.
        con.execute(
            "UPDATE wake_message SET delivered_at=datetime('now') "
            "WHERE message_id=?",
            (initial_message,),
        )
        con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',"
            "delivered_at=datetime('now') WHERE wake_id=?",
            (initial_wake,),
        )
        con.commit()
        sprint_message_delivery.SprintMessageStore(con).mark_read(initial_message, 1)
        cls.registered_pr_id = int(
            con.execute(
                "INSERT INTO sprint_registered_prs "
                "(sprint_id,owner_participant_id,repository,pr_number) "
                "VALUES (?,?,'acme/repo',42)",
                (cls.sprint_id, participants[1]),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_pr_work_units "
            "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
            (cls.sprint_id, cls.registered_pr_id, cls.unit_id),
        )
        con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha,"
            "evidence) VALUES (?,'green','green-42',?,?)",
            (
                cls.registered_pr_id,
                "a" * 40,
                json.dumps({"base_sha": "c" * 40}),
            ),
        )
        cls.dispatch_unit_id = int(
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,4,2,'Later','Dispatch it')",
                (cls.sprint_id,),
            ).lastrowid
        )
        con.commit()
        con.close()

        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.files = []

    def write(self, body: str) -> str:
        path = self.tmp / f"input-{len(self.files)}.txt"
        path.write_text(body)
        self.files.append(path)
        return str(path)

    def run_cli(self, token: str, *argv: str) -> dict:
        mem.SC_API_TOKEN = token
        output = io.StringIO()
        adapter = mock.Mock()
        adapter.probe.return_value = None
        with (
            mock.patch.object(
                server.sprint_domain,
                "adapter_for",
                return_value=adapter,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, sprint_cli.main(list(argv)))
        return json.loads(output.getvalue())

    def run_pr_cli(self, token: str, *argv: str) -> dict:
        mem.SC_API_TOKEN = token
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, pr_cli.main(list(argv)))
        return json.loads(output.getvalue())

    def test_engine_wide_subscription_uses_authenticated_developer_identity(self):
        with mock.patch.object(
            server.sprint_pr_watcher,
            "GitHubPullRequestReader",
            return_value=Reader(),
        ):
            receipt = self.run_pr_cli(
                TOKENS["developer"],
                "subscribe",
                "--repository",
                "Acme/Outside",
                "--pr",
                "85",
            )

        self.assertTrue(receipt["created"])
        con = sqlite3.connect(self.db)
        try:
            subscription = con.execute(
                "SELECT owner_shell_id,repository,pr_number,"
                "sprint_registered_pr_id FROM pr_subscriptions "
                "WHERE subscription_id=?",
                (receipt["subscription_id"],),
            ).fetchone()
            message = con.execute(
                "SELECT receiver_shell_id,sprint_id,to_participant_id,"
                "declared_type,body FROM wake_message "
                "WHERE idempotency_key LIKE 'pr-transition:%:shell:1' "
                "ORDER BY message_id DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual((1, "acme/outside", 85, None), subscription)
        self.assertEqual((1, None, None, "re-enter"), message[:4])
        self.assertIn("number=85", message[4])
        self.assertIn("event=green", message[4])

    def test_reconcile_pr_is_fnb_only_and_projects_the_durable_receipt(self):
        argv = (
            "reconcile-pr",
            "--sprint",
            str(self.sprint_id),
            "--repository",
            "Acme/Repo",
            "--pr",
            "42",
            "--work-unit",
            str(self.unit_id),
            "--reason",
            "repair aborted Sprint ownership",
        )
        with mock.patch.object(
            server.sprint_pr_watcher,
            "GitHubPullRequestReader",
            return_value=Reader(),
        ), self.assertRaisesRegex(SystemExit, "HTTP 403.*only .*FnB"):
            self.run_cli(TOKENS["developer"], *argv)

        expected = sprint_pr_watcher.RegistrationReconciliationReceipt(
            self.registered_pr_id,
            True,
            9,
            "merged",
            "a" * 40,
            "b" * 40,
            (self.unit_id,),
        )
        with mock.patch.object(
            server.sprint_pr_watcher.SprintPRWatcher,
            "reconcile_aborted_registration",
            return_value=expected,
        ) as reconcile:
            response = self.run_cli(TOKENS["admin"], *argv)

        self.assertEqual(
            {
                "changed": True,
                "completed_work_unit_ids": [self.unit_id],
                "from_sprint_id": 9,
                "head_sha": "a" * 40,
                "merge_sha": "b" * 40,
                "normalized_state": "merged",
                "registered_pr_id": self.registered_pr_id,
            },
            response,
        )
        call = reconcile.call_args
        self.assertEqual(self.sprint_id, call.args[0])
        self.assertEqual("fnb", call.kwargs["actor"].kind)
        self.assertEqual(5, call.kwargs["actor"].shell_id)
        self.assertEqual("Acme/Repo", call.kwargs["repository"])
        self.assertEqual(42, call.kwargs["pr_number"])
        self.assertEqual(self.unit_id, call.kwargs["work_unit_id"])

    def deliver_message(self, message_id: int) -> None:
        # The delivery worker is out of frame in these surface tests: stamp
        # the one queued message delivered so its recipient may read it.
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "UPDATE sprint_wake_outbox SET state='delivered',"
                "delivered_at=datetime('now') WHERE state='pending' "
                "AND wake_id IN (SELECT wake_id FROM sprint_wake_messages "
                "WHERE message_id=?)",
                (message_id,),
            )
            con.execute(
                "UPDATE wake_message SET delivered_at=datetime('now') "
                "WHERE message_id=? AND delivered_at IS NULL",
                (message_id,),
            )
            con.commit()
        finally:
            con.close()

    def deliver_sprint_messages(self, sprint_id: int) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "UPDATE sprint_wake_outbox SET state='delivered',"
                "delivered_at=datetime('now') WHERE state='pending' "
                "AND wake_id IN (SELECT wm.wake_id FROM sprint_wake_messages wm "
                "JOIN wake_message m USING (message_id) WHERE m.sprint_id=?)",
                (sprint_id,),
            )
            con.execute(
                "UPDATE wake_message SET delivered_at=datetime('now') "
                "WHERE sprint_id=? AND delivered_at IS NULL",
                (sprint_id,),
            )
            con.commit()
        finally:
            con.close()

    def seed_declaration(self, suffix: str) -> tuple[int, int, int]:
        con = sqlite3.connect(self.db)
        try:
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) VALUES (?,?)",
                    (f"Declared feature {suffix}", "in_progress"),
                ).lastrowid
            )
            body = f"declared spec {suffix}"
            document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,?,?)",
                    (feature_id, f"Spec {suffix}", body),
                ).lastrowid
            )
            approval_id = int(
                con.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (?,?,2,'pass')",
                    (document_id, hashlib.sha256(body.encode()).hexdigest()),
                ).lastrowid
            )
            task_id = int(
                con.execute(
                    "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                    "VALUES (?,?,0,?)",
                    (feature_id, document_id, f"Task {suffix}"),
                ).lastrowid
            )
            con.commit()
            return feature_id, approval_id, task_id
        finally:
            con.close()

    def participants_file(self) -> str:
        return self.write(
            json.dumps(
                [
                    {"shell_id": 3, "role": "planner", "harness": "codex"},
                    {"shell_id": 1, "role": "developer", "harness": "codex"},
                    {"shell_id": 2, "role": "reviewer", "harness": "kimi"},
                ]
            )
        )

    def use_isolated_db(self) -> None:
        original_db = self.db
        original_server_db = server.DB_PATH
        with tempfile.NamedTemporaryFile(
            dir=self.tmp, suffix=".db", delete=False
        ) as handle:
            isolated = Path(handle.name)
        con = sqlite3.connect(isolated)
        try:
            apply_schema(con)
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
            con.executemany(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
                "VALUES (?,?,?,?,?,1,?)",
                (
                    (1, "Developer", "DEV1", "dev", "prompt", TOKENS["developer"]),
                    (2, "Reviewer", "REV1", "reviewer", "prompt", TOKENS["reviewer"]),
                    (3, "Planner", "PLN1", "planner", "prompt", TOKENS["planner"]),
                    (4, "Developer 2", "DEV2", "dev", "prompt", "dev2-token"),
                    (5, "FnB", "FNB", "admin", "prompt", TOKENS["admin"]),
                ),
            )
            con.commit()
        finally:
            con.close()
        self.db = isolated
        server.DB_PATH = isolated
        self.addCleanup(setattr, self, "db", original_db)
        self.addCleanup(setattr, server, "DB_PATH", original_server_db)

    def test_watcher_state_is_authenticated_bounded_and_diagnostic(self):
        self.use_isolated_db()
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Watcher diagnosis','in_progress')"
                ).lastrowid
            )
            sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                    "VALUES (?,3,1)",
                    (feature_id,),
                ).lastrowid
            )
            con.execute(
                "UPDATE sprints SET lifecycle='armed',armed_at=? WHERE sprint_id=?",
                ("2026-08-04 11:00:00", sprint_id),
            )
            participant_id = int(
                con.execute(
                    "INSERT INTO sprint_participants "
                    "(sprint_id,shell_id,role,harness) VALUES (?,1,'developer','codex')",
                    (sprint_id,),
                ).lastrowid
            )
            subscription_ids = []
            registered_ids = []
            for pr_number in (41, 42, 43):
                registered_id = int(
                    con.execute(
                        "INSERT INTO sprint_registered_prs "
                        "(sprint_id,owner_participant_id,repository,pr_number) "
                        "VALUES (?,?,'acme/diagnosis',?)",
                        (sprint_id, participant_id, pr_number),
                    ).lastrowid
                )
                subscription_id = int(
                    con.execute(
                        "INSERT INTO pr_subscriptions "
                        "(owner_shell_id,repository,pr_number,sprint_registered_pr_id) "
                        "VALUES (1,'acme/diagnosis',?,?)",
                        (pr_number, registered_id),
                    ).lastrowid
                )
                registered_ids.append(registered_id)
                subscription_ids.append(subscription_id)
            con.executemany(
                "INSERT INTO pr_subscription_transitions "
                "(subscription_id,normalized_state,transition_key,"
                "observed_head_sha,observed_at) VALUES (?,?,?,?,?)",
                (
                    (
                        subscription_ids[0],
                        "red",
                        "diagnosis-red",
                        "a" * 40,
                        "2026-08-04 11:59:00",
                    ),
                    (
                        subscription_ids[1],
                        "pending",
                        "diagnosis-pending",
                        "b" * 40,
                        "2026-08-04 11:59:30",
                    ),
                ),
            )
            for index in range(1, 26):
                con.execute(
                    "INSERT INTO pr_subscription_poll_failures "
                    "(subscription_id,failure_count,backoff_seconds,trigger,"
                    "error_detail,failed_at,last_seen_at) VALUES (?,?,?,'pulse',?,?,?)",
                    (
                        subscription_ids[0],
                        index,
                        float(index),
                        f"failure-{index}",
                        f"2026-08-04 11:{index:02d}:00",
                        f"2026-08-04 11:{index:02d}:00",
                    ),
                )
            con.commit()

            changes_before_read = con.total_changes
            never_started = server.sprint_pr_watcher.WatcherStateStore(con).for_sprint(
                sprint_id,
                now=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(changes_before_read, con.total_changes)
            self.assertEqual("never-started", never_started["watcher"]["status"])
            self.assertEqual([], never_started["watcher"]["history"])

            con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES ('sprint-pr-watcher','2026-08-04 11:00:00',5)"
            )
            for index in range(1, 56):
                con.execute(
                    "INSERT INTO daemon_heartbeat_history "
                    "(name,beat_at,subscriptions_scanned) VALUES (?,?,?)",
                    ("sprint-pr-watcher", f"2026-08-04 11:{index % 60:02d}:00", index),
                )
            con.commit()

            state = server.sprint_pr_watcher.WatcherStateStore(con).for_sprint(
                sprint_id,
                now=datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )
            self.assertEqual("stale", state["watcher"]["status"])
            self.assertEqual(3720, state["watcher"]["age_seconds"])
            self.assertEqual(
                list(range(55, 5, -1)),
                [row["heartbeat_id"] for row in state["watcher"]["history"]],
            )
            self.assertEqual(
                ["red", "pending", None],
                [
                    item["latest_transition"]["normalized_state"]
                    if item["latest_transition"] is not None
                    else None
                    for item in state["registered_prs"]
                ],
            )
            self.assertEqual(
                [True, True, False],
                [item["has_observation"] for item in state["registered_prs"]],
            )
            self.assertEqual(
                [180, 150, None],
                [
                    item["latest_transition"]["age_seconds"]
                    if item["latest_transition"] is not None
                    else None
                    for item in state["registered_prs"]
                ],
            )
            self.assertEqual(
                list(range(25, 5, -1)),
                [row["failure_id"] for row in state["poll_failures"]],
            )
            self.assertEqual(
                registered_ids[0], state["poll_failures"][0]["registered_pr_id"]
            )
            self.assertEqual("failure-25", state["poll_failures"][0]["error_detail"])

            con.execute(
                "UPDATE daemon_heartbeats SET beat_at=datetime('now') "
                "WHERE name='sprint-pr-watcher'"
            )
            con.commit()
        finally:
            con.close()

        def unauthenticated(path: str) -> tuple[int, dict]:
            status, _headers, raw = server.dispatch_http(
                "GET",
                path,
                "Host: 127.0.0.1:8800\r\nContent-Length: 0\r\n",
                b"",
            )
            return status, json.loads(raw)

        expected_auth = unauthenticated(f"/_sc/sprint/{sprint_id}/inbox")
        self.assertEqual(
            expected_auth,
            unauthenticated(f"/_sc/sprint/watcher-state?sprint_id={sprint_id}"),
        )
        self.assertEqual(
            (401, {"error": "Authorization: Bearer <token> required"}),
            expected_auth,
        )

        cli_state = self.run_cli(
            TOKENS["planner"], "watcher-state", "--sprint", str(sprint_id)
        )
        self.assertEqual(sprint_id, cli_state["sprint_id"])
        self.assertEqual("live", cli_state["watcher"]["status"])
        self.assertEqual(50, len(cli_state["watcher"]["history"]))
        self.assertEqual(20, len(cli_state["poll_failures"]))
        self.assertEqual(
            ["red", "pending", None],
            [
                item["latest_transition"]["normalized_state"]
                if item["latest_transition"] is not None
                else None
                for item in cli_state["registered_prs"]
            ],
        )

    def test_declare_plan_lifecycle_and_registration_surfaces(self):
        self.use_isolated_db()
        feature_id, approval_id, task_id = self.seed_declaration("lifecycle")
        declaration = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )
        sprint_id = declaration["sprint_id"]
        unit = self.run_cli(
            TOKENS["planner"],
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "1",
            "--reviewer-shell",
            "2",
            "--title",
            "Shell-driven lane",
            "--expected-output-file",
            self.write("One merged fixture PR."),
            "--task",
            str(task_id),
        )
        armed = self.run_cli(TOKENS["planner"], "arm", "--sprint", str(sprint_id))
        self.assertEqual(2, len(armed["wake_ids"]))

        with mock.patch.object(
            server.sprint_pr_watcher,
            "GitHubPullRequestReader",
            return_value=Reader(),
        ):
            registration = self.run_cli(
                TOKENS["developer"],
                "register-pr",
                "--sprint",
                str(sprint_id),
                "--repository",
                "acme/repo",
                "--pr",
                "84",
                "--work-unit",
                str(unit["work_unit_id"]),
            )
        self.assertTrue(registration["created"])

        paused = self.run_cli(
            TOKENS["developer"],
            "pause",
            "--sprint",
            str(sprint_id),
            "--reason",
            "exercise participant pause",
        )
        self.assertTrue(paused["changed"])
        self.assertIsInstance(paused["report_id"], int)
        with mock.patch.object(
            server.sprint_recovery,
            "GitHubPullRequestReader",
            return_value=Reader(),
        ):
            resumed = self.run_cli(
                TOKENS["planner"],
                "resume",
                "--sprint",
                str(sprint_id),
                "--reason",
                "fixture is healthy",
            )
        self.assertTrue(resumed["changed"])
        completed = self.run_cli(
            TOKENS["planner"],
            "complete",
            "--sprint",
            str(sprint_id),
            "--reason",
            "proof complete",
            "--outcome",
            "accepted",
        )
        self.assertTrue(completed["changed"])

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                ("completed", "accepted"),
                con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone(),
            )
            self.assertEqual(
                [("sprint.declared",), ("work_unit.created",)],
                con.execute(
                    "SELECT event_type FROM sprint_events WHERE sprint_id=? "
                    "ORDER BY event_id LIMIT 2",
                    (sprint_id,),
                ).fetchall(),
            )
        finally:
            con.close()

    def test_declare_binds_current_spec_without_qaqc_evidence(self):
        self.use_isolated_db()
        con = sqlite3.connect(self.db)
        try:
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Direct declaration','in_progress')"
                ).lastrowid
            )
            body = "current direct spec body"
            document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Direct spec',?)",
                    (feature_id, body),
                ).lastrowid
            )
            con.commit()
        finally:
            con.close()

        sprint_id = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec",
            str(document_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )["sprint_id"]

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                (document_id, hashlib.sha256(body.encode()).hexdigest(), None),
                con.execute(
                    "SELECT document_id,bound_revision_sha256,approval_id "
                    "FROM sprint_specs WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone(),
            )
            self.assertEqual(
                0,
                con.execute("SELECT COUNT(*) FROM sprint_spec_approvals").fetchone()[0],
            )
            payload = json.loads(
                con.execute(
                    "SELECT payload FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='sprint.declared'",
                    (sprint_id,),
                ).fetchone()[0]
            )
            self.assertEqual([document_id], payload["spec_document_ids"])
            self.assertNotIn("spec_approval_ids", payload)
        finally:
            con.close()

    def test_legacy_selector_is_verdict_agnostic_and_mixed_input_deduplicates(self):
        self.use_isolated_db()
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
                "VALUES (6,'Active reviewer','REV2','reviewer','prompt',1,'rev2-token')"
            )
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Legacy declaration','in_progress')"
                ).lastrowid
            )
            old_body = "historically reviewed body"
            current_body = "current body after review"
            document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Legacy spec',?)",
                    (feature_id, old_body),
                ).lastrowid
            )
            approval_id = int(
                con.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (?,?,2,'fail')",
                    (document_id, hashlib.sha256(old_body.encode()).hexdigest()),
                ).lastrowid
            )
            con.execute(
                "UPDATE documents SET body=? WHERE document_id=?",
                (current_body, document_id),
            )
            con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=2")
            con.commit()
        finally:
            con.close()

        participants = self.write(
            json.dumps(
                [
                    {"shell_id": 3, "role": "planner", "harness": "codex"},
                    {"shell_id": 1, "role": "developer", "harness": "codex"},
                    {"shell_id": 6, "role": "reviewer", "harness": "kimi"},
                ]
            )
        )

        sprint_id = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec",
            str(document_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            participants,
            "--merge-grant",
        )["sprint_id"]

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                1,
                con.execute(
                    "SELECT COUNT(*) FROM sprint_specs WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                (
                    document_id,
                    hashlib.sha256(current_body.encode()).hexdigest(),
                    approval_id,
                ),
                con.execute(
                    "SELECT document_id,bound_revision_sha256,approval_id "
                    "FROM sprint_specs WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone(),
            )
        finally:
            con.close()

    def test_review_properties_never_change_authenticated_launch_eligibility(self):
        cases = (
            ("no-review", None, False, False, False),
            ("current-pass", "pass", False, False, False),
            ("current-fail", "fail", False, False, False),
            ("unresolved-findings", "pass", False, True, False),
            ("stale-review", "pass", True, False, False),
            ("deleted-signer", "pass", False, False, True),
            ("mixed-selectors", "pass", False, False, False),
        )
        for name, verdict, stale, findings, deleted_signer in cases:
            with self.subTest(name=name):
                self.use_isolated_db()
                con = sqlite3.connect(self.db)
                try:
                    reviewer_shell_id = 2
                    if deleted_signer:
                        con.execute(
                            "INSERT INTO shells "
                            "(shell_id,display_name,shortname,flavor,system_prompt,"
                            "user_id,api_key) VALUES "
                            "(6,'Active reviewer','REV2','reviewer','prompt',1,'rev2-token')"
                        )
                        con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=2")
                        reviewer_shell_id = 6
                    feature_id = int(
                        con.execute(
                            "INSERT INTO roadmap (title,roadmap_status) VALUES (?,?)",
                            (f"Matrix {name}", "in_progress"),
                        ).lastrowid
                    )
                    reviewed_body = f"reviewed body {name}"
                    current_body = (
                        f"current body {name}" if stale else reviewed_body
                    )
                    document_id = int(
                        con.execute(
                            "INSERT INTO documents "
                            "(feature_id,kind,seq,title,body) "
                            "VALUES (?,'spec',1,?,?)",
                            (feature_id, f"Spec {name}", current_body),
                        ).lastrowid
                    )
                    findings_id = None
                    if findings:
                        findings_id = int(
                            con.execute(
                                "INSERT INTO documents (kind,seq,title,body) "
                                "VALUES ('doc',1,'Unresolved findings','still open')"
                            ).lastrowid
                        )
                    approval_id = None
                    if verdict is not None:
                        approval_id = int(
                            con.execute(
                                "INSERT INTO sprint_spec_approvals "
                                "(document_id,revision_sha256,reviewer_shell_id,"
                                "verdict,findings_document_id) VALUES (?,?,?,?,?)",
                                (
                                    document_id,
                                    hashlib.sha256(reviewed_body.encode()).hexdigest(),
                                    2,
                                    verdict,
                                    findings_id,
                                ),
                            ).lastrowid
                        )
                    task_id = int(
                        con.execute(
                            "INSERT INTO spec_tasks "
                            "(feature_id,document_id,seq,title) VALUES (?,?,0,?)",
                            (feature_id, document_id, f"Task {name}"),
                        ).lastrowid
                    )
                    con.commit()
                finally:
                    con.close()
                participants = self.write(
                    json.dumps(
                        [
                            {"shell_id": 3, "role": "planner", "harness": "codex"},
                            {"shell_id": 1, "role": "developer", "harness": "codex"},
                            {
                                "shell_id": reviewer_shell_id,
                                "role": "reviewer",
                                "harness": "kimi",
                            },
                        ]
                    )
                )
                selectors = ["--spec", str(document_id)]
                if approval_id is not None and name != "mixed-selectors":
                    selectors = ["--spec-approval", str(approval_id)]
                elif approval_id is not None:
                    selectors.extend(("--spec-approval", str(approval_id)))
                declaration = self.run_cli(
                    TOKENS["planner"],
                    "declare",
                    "--feature",
                    str(feature_id),
                    *selectors,
                    "--participants-file",
                    participants,
                    "--merge-grant",
                )
                unit = self.run_cli(
                    TOKENS["planner"],
                    "plan-unit",
                    "--sprint",
                    str(declaration["sprint_id"]),
                    "--developer-shell",
                    "1",
                    "--reviewer-shell",
                    str(reviewer_shell_id),
                    "--title",
                    f"Matrix lane {name}",
                    "--expected-output-file",
                    self.write("One verified launch."),
                    "--task",
                    str(task_id),
                )
                armed = self.run_cli(
                    TOKENS["planner"],
                    "arm",
                    "--sprint",
                    str(declaration["sprint_id"]),
                )

                self.assertEqual(2, len(armed["wake_ids"]))
                con = sqlite3.connect(self.db)
                try:
                    self.assertEqual(
                        (
                            "armed",
                            1,
                            document_id,
                            hashlib.sha256(current_body.encode()).hexdigest(),
                            approval_id,
                            "ready",
                        ),
                        con.execute(
                            "SELECT s.lifecycle,COUNT(ss.document_id),ss.document_id,"
                            "ss.bound_revision_sha256,ss.approval_id,u.disposition "
                            "FROM sprints s JOIN sprint_specs ss USING (sprint_id) "
                            "JOIN sprint_work_units u USING (sprint_id) "
                            "WHERE s.sprint_id=? AND u.work_unit_id=?",
                            (declaration["sprint_id"], unit["work_unit_id"]),
                        ).fetchone(),
                    )
                finally:
                    con.close()

    def test_declaration_rejects_invalid_spec_resources_without_writes(self):
        self.use_isolated_db()
        con = sqlite3.connect(self.db)
        try:
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Declaration guards','in_progress')"
                ).lastrowid
            )
            other_feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Other feature','in_progress')"
                ).lastrowid
            )
            valid_document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Valid','valid')",
                    (feature_id,),
                ).lastrowid
            )
            wrong_feature_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Wrong feature','valid')",
                    (other_feature_id,),
                ).lastrowid
            )
            non_spec_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'doc',2,'Not a spec','valid')",
                    (feature_id,),
                ).lastrowid
            )
            unscoped_spec_id = int(
                con.execute(
                    "INSERT INTO documents (kind,seq,title,body) "
                    "VALUES ('spec',4,'Unscoped','valid')"
                ).lastrowid
            )
            empty_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',3,'Empty','   ')",
                    (feature_id,),
                ).lastrowid
            )
            approval_ids = [
                int(
                    con.execute(
                        "INSERT INTO sprint_spec_approvals "
                        "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                        "VALUES (?,?,?,?)",
                        (
                            valid_document_id,
                            hashlib.sha256(b"valid").hexdigest(),
                            reviewer_shell_id,
                            verdict,
                        ),
                    ).lastrowid
                )
                for reviewer_shell_id, verdict in ((2, "pass"), (1, "fail"))
            ]
            con.commit()
        finally:
            con.close()
        base = (
            "declare",
            "--feature",
            str(feature_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )
        cases = (
            ((), "HTTP 400.*at least one"),
            (("--spec", "999999"), "HTTP 404.*unknown Sprint spec document"),
            (
                ("--spec-approval", "999999"),
                "HTTP 404.*unknown Sprint spec approval",
            ),
            (
                ("--spec", str(wrong_feature_id)),
                "HTTP 409.*non-empty spec documents for its feature",
            ),
            (
                ("--spec", str(non_spec_id)),
                "HTTP 409.*non-empty spec documents for its feature",
            ),
            (
                ("--spec", str(unscoped_spec_id)),
                "HTTP 409.*non-empty spec documents for its feature",
            ),
            (
                ("--spec", str(empty_id)),
                "HTTP 409.*non-empty spec documents for its feature",
            ),
            (
                (
                    "--spec-approval",
                    str(approval_ids[0]),
                    "--spec-approval",
                    str(approval_ids[1]),
                ),
                "HTTP 400.*multiple spec approvals select the same document",
            ),
        )
        for selectors, error in cases:
            with self.subTest(selectors=selectors):
                with self.assertRaisesRegex(SystemExit, error):
                    self.run_cli(TOKENS["planner"], *base, *selectors)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0])
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM sprint_specs").fetchone()[0])
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0])
        finally:
            con.close()

    def test_participant_send_confirms_durable_message_wake_and_route(self):
        response = self.run_cli(
            TOKENS["developer"],
            "send",
            "--sprint",
            str(self.sprint_id),
            "--to",
            "PLN1",
            "--body-file",
            self.write("Please confirm the downstream handoff."),
            "--key",
            "cli-participant-send-retry",
        )

        self.assertEqual(
            {
                "conversation_id",
                "message_created",
                "message_id",
                "wake_id",
                "wake_state",
            },
            set(response),
        )
        self.assertTrue(response["message_created"])
        self.assertEqual("pending", response["wake_state"])

        with contextlib.closing(sqlite3.connect(self.db)) as con:
            message = con.execute(
                "SELECT from_participant_id,to_participant_id,"
                "message_kind,body,actionable,work_unit_id,disposition "
                "FROM wake_message WHERE message_id=?",
                (response["message_id"],),
            ).fetchone()
            self.assertEqual(
                (2, 1, "notification", "Please confirm the downstream handoff.", 0, None, None),
                message,
            )
            wake = con.execute(
                "SELECT wm.message_id,w.state FROM sprint_wake_outbox w "
                "JOIN sprint_wake_messages wm ON wm.wake_id=w.wake_id "
                "WHERE w.wake_id=? ORDER BY wm.message_id",
                (response["wake_id"],),
            ).fetchall()
            self.assertEqual(
                [(1, "pending"), (response["message_id"], "pending")],
                wake,
            )
            current = con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "LEFT JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.participant_id=1",
            ).fetchone()
            self.assertEqual((response["conversation_id"],), current)

        replay = self.run_cli(
            TOKENS["developer"],
            "send",
            "--sprint",
            str(self.sprint_id),
            "--to",
            "PLN1",
            "--body-file",
            self.write("Please confirm the downstream handoff."),
            "--key",
            "cli-participant-send-retry",
        )
        self.assertEqual(response["message_id"], replay["message_id"])
        self.assertEqual(response["wake_id"], replay["wake_id"])
        self.assertFalse(replay["message_created"])

    def test_participant_send_rejects_non_string_body_and_key_as_bad_requests(self):
        mem.SC_API_TOKEN = TOKENS["developer"]
        for field, value, message in (
            ("body", ["not", "text"], "message body must be a string"),
            ("idempotency_key", ["not", "text"], "idempotency key must be a string"),
        ):
            with self.subTest(field=field):
                payload = {
                    "sprint_id": self.sprint_id,
                    "to": "PLN1",
                    "body": "valid body",
                    "idempotency_key": "valid-key",
                }
                payload[field] = value
                with self.assertRaisesRegex(SystemExit, f"HTTP 400.*{message}"):
                    mem._api("POST", "/_sc/sprint/send", payload)

    def test_remediation_surfaces_are_authenticated_and_durable(self):
        self.use_isolated_db()
        con = sqlite3.connect(self.db)
        try:
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title,roadmap_status) "
                    "VALUES ('Surface remediation','in_progress')"
                ).lastrowid
            )
            document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',1,'Surface spec','REV9 body')",
                    (feature_id,),
                ).lastrowid
            )
            task_ids = [
                int(
                    con.execute(
                        "INSERT INTO spec_tasks "
                        "(feature_id,document_id,seq,title) VALUES (?,?,?,?)",
                        (feature_id, document_id, seq, title),
                    ).lastrowid
                )
                for seq, title in enumerate(("Report", "Optional lane"))
            ]
            con.commit()
        finally:
            con.close()

        approval = self.run_cli(
            TOKENS["reviewer"],
            "record-qaqc",
            "--document",
            str(document_id),
            "--verdict",
            "pass",
        )
        self.assertTrue(approval["created"])
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*Review shell"):
            self.run_cli(
                TOKENS["developer"],
                "record-qaqc",
                "--document",
                str(document_id),
                "--verdict",
                "pass",
            )

        participants = self.write(
            json.dumps(
                [
                    {"shell_id": 3, "role": "planner", "harness": "codex"},
                    {"shell_id": 1, "role": "developer", "harness": "codex"},
                    {"shell_id": 4, "role": "developer", "harness": "codex"},
                    {"shell_id": 2, "role": "reviewer", "harness": "kimi"},
                ]
            )
        )
        con = sqlite3.connect(self.db)
        try:
            bad_approval_id = int(
                con.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (?,?,1,'pass')",
                    (document_id, hashlib.sha256(b"REV9 body").hexdigest()),
                ).lastrowid
            )
            con.commit()
        finally:
            con.close()
        evidence_sprint_id = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(bad_approval_id),
            "--participants-file",
            participants,
            "--merge-grant",
        )["sprint_id"]
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                (bad_approval_id,),
                con.execute(
                    "SELECT approval_id FROM sprint_specs WHERE sprint_id=?",
                    (evidence_sprint_id,),
                ).fetchone(),
            )
        finally:
            con.close()
        sprint_id = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval["approval_id"]),
            "--participants-file",
            participants,
            "--merge-grant",
        )["sprint_id"]
        report_unit = self.run_cli(
            TOKENS["planner"],
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "1",
            "--reviewer-shell",
            "2",
            "--title",
            "Report lane",
            "--expected-output-file",
            self.write("Durable report"),
            "--task",
            str(task_ids[0]),
            "--output-kind",
            "report-only",
        )["work_unit_id"]
        optional_unit = self.run_cli(
            TOKENS["planner"],
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "4",
            "--reviewer-shell",
            "2",
            "--title",
            "Optional lane",
            "--expected-output-file",
            self.write("Optional result"),
            "--task",
            str(task_ids[1]),
        )["work_unit_id"]
        replanned = self.run_cli(
            TOKENS["planner"],
            "replan-unit",
            "--sprint",
            str(sprint_id),
            "--work-unit",
            str(optional_unit),
            "--developer-shell",
            "4",
            "--reviewer-shell",
            "2",
            "--wave",
            "7",
            "--output-kind",
            "no-code",
        )
        self.assertTrue(replanned["changed"])
        self.run_cli(TOKENS["planner"], "arm", "--sprint", str(sprint_id))
        self.deliver_sprint_messages(sprint_id)

        inbox = self.run_cli(
            TOKENS["developer"], "inbox", "--sprint", str(sprint_id)
        )
        self.assertEqual(1, len(inbox["messages"]))
        report_message = inbox["messages"][0]["message_id"]
        accepted = self.run_cli(
            TOKENS["developer"],
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(report_message),
        )
        self.assertEqual("accepted", accepted["disposition"])
        declined_inbox = self.run_cli(
            "dev2-token", "inbox", "--sprint", str(sprint_id)
        )
        declined_message = declined_inbox["messages"][0]["message_id"]
        decline = self.run_cli(
            "dev2-token",
            "decline",
            "--sprint",
            str(sprint_id),
            "--message",
            str(declined_message),
            "--reason",
            "capacity moved",
        )
        self.assertIsInstance(decline["result_message_id"], int)
        cancelled = self.run_cli(
            TOKENS["planner"],
            "cancel-unit",
            "--sprint",
            str(sprint_id),
            "--work-unit",
            str(optional_unit),
            "--reason",
            "Declined lane removed from scope",
        )
        self.assertTrue(cancelled["changed"])
        completed_unit = self.run_cli(
            TOKENS["developer"],
            "complete-unit",
            "--sprint",
            str(sprint_id),
            "--work-unit",
            str(report_unit),
            "--result-file",
            self.write("Report document #77"),
        )
        self.assertEqual([], completed_unit["wake_ids"])

        conformance = self.run_cli(
            TOKENS["reviewer"],
            "record-conformance",
            "--sprint",
            str(sprint_id),
            "--body-file",
            self.write("Conformance complete"),
            "--findings-file",
            self.write(
                json.dumps(
                    [{"severity": "Low", "title": "Note", "body": "Track it"}]
                )
            ),
            "--final-report-file",
            self.write("Final Sprint report"),
            "--reason",
            "Reviewer approved integrated conformance",
            "--outcome",
            "accepted",
            "--key",
            "surface-conformance",
        )
        self.assertTrue(conformance["completed"])
        self.assertIsInstance(conformance["final_report_id"], int)
        followup_id = conformance["followup_ids"][0]
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*only FnB"):
            self.run_cli(
                TOKENS["planner"],
                "disposition-followup",
                "--sprint",
                str(sprint_id),
                "--followup",
                str(followup_id),
                "--disposition",
                "accepted",
            )
        disposition = self.run_cli(
            TOKENS["admin"],
            "disposition-followup",
            "--sprint",
            str(sprint_id),
            "--followup",
            str(followup_id),
            "--disposition",
            "accepted",
        )
        self.assertTrue(disposition["changed"])

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                [
                    (report_unit, "report_only", "completed", "Report document #77"),
                    (
                        optional_unit,
                        "no_code",
                        "cancelled",
                        "Declined lane removed from scope",
                    ),
                ],
                con.execute(
                    "SELECT work_unit_id,output_kind,disposition,completion_result "
                    "FROM sprint_work_units WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                ).fetchall(),
            )
            self.assertEqual(
                [("final", "Final Sprint report")],
                con.execute(
                    "SELECT report_kind,body FROM sprint_reports WHERE sprint_id=? "
                    "AND report_kind='final'",
                    (sprint_id,),
                ).fetchall(),
            )
            self.assertEqual(
                "accepted",
                con.execute(
                    "SELECT disposition FROM sprint_followups WHERE followup_id=?",
                    (followup_id,),
                ).fetchone()[0],
            )
        finally:
            con.close()

    def test_admin_declares_for_planner_and_aborts_without_deleting_history(self):
        self.use_isolated_db()
        feature_id, approval_id, task_id = self.seed_declaration("abort")
        declaration = self.run_cli(
            TOKENS["admin"],
            "declare",
            "--feature",
            str(feature_id),
            "--planner-shell",
            "3",
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )
        sprint_id = declaration["sprint_id"]
        unit_id = self.run_cli(
            TOKENS["admin"],
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "1",
            "--reviewer-shell",
            "2",
            "--title",
            "FnB-planned lane",
            "--expected-output-file",
            self.write("History survives armed abort."),
            "--task",
            str(task_id),
        )["work_unit_id"]
        armed = self.run_cli(TOKENS["admin"], "arm", "--sprint", str(sprint_id))
        self.assertEqual(2, len(armed["wake_ids"]))
        aborted = self.run_cli(
            TOKENS["admin"],
            "abort",
            "--sprint",
            str(sprint_id),
            "--reason",
            "fixture cancellation",
        )
        self.assertTrue(aborted["changed"])
        self.assertIsInstance(aborted["report_id"], int)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                ("aborted", "aborted"),
                con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone(),
            )
            self.assertEqual(
                (3, "ready"),
                con.execute(
                    "SELECT COUNT(*),u.disposition FROM sprint_participants p "
                    "JOIN sprint_work_units u ON u.sprint_id=p.sprint_id "
                    "WHERE p.sprint_id=? AND u.work_unit_id=?",
                    (sprint_id, unit_id),
                ).fetchone(),
            )
        finally:
            con.close()

    def test_arm_reports_single_armed_conflict_and_rolls_back_release(self):
        feature_id, approval_id, task_id = self.seed_declaration("single-armed")
        sprint_id = self.run_cli(
            TOKENS["planner"],
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )["sprint_id"]
        unit_id = self.run_cli(
            TOKENS["planner"],
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "1",
            "--reviewer-shell",
            "2",
            "--title",
            "Blocked second Sprint",
            "--expected-output-file",
            self.write("No assignment may escape."),
            "--task",
            str(task_id),
        )["work_unit_id"]
        with self.assertRaisesRegex(SystemExit, "HTTP 409.*already armed"):
            self.run_cli(TOKENS["planner"], "arm", "--sprint", str(sprint_id))

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                ("prepared", "planned", 0),
                con.execute(
                    "SELECT s.lifecycle,u.disposition,"
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=s.sprint_id) "
                    "FROM sprints s JOIN sprint_work_units u USING(sprint_id) "
                    "WHERE s.sprint_id=? AND u.work_unit_id=?",
                    (sprint_id, unit_id),
                ).fetchone(),
            )
        finally:
            con.close()

    def test_new_surface_rejects_cross_role_without_side_effects(self):
        self.use_isolated_db()
        feature_id, approval_id, task_id = self.seed_declaration("authority")
        declare_argv = (
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.participants_file(),
            "--merge-grant",
        )
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*originating Planner"):
            self.run_cli(TOKENS["developer"], *declare_argv)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                0, con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0]
            )
        finally:
            con.close()

        sprint_id = self.run_cli(TOKENS["planner"], *declare_argv)["sprint_id"]
        plan_argv = (
            "plan-unit",
            "--sprint",
            str(sprint_id),
            "--developer-shell",
            "1",
            "--reviewer-shell",
            "2",
            "--title",
            "Authority lane",
            "--expected-output-file",
            self.write("No unauthorized writes."),
            "--task",
            str(task_id),
        )
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*owning Planner"):
            self.run_cli(TOKENS["developer"], *plan_argv)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            )
        finally:
            con.close()

        unit_id = self.run_cli(TOKENS["planner"], *plan_argv)["work_unit_id"]
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*owning Planner"):
            self.run_cli(TOKENS["reviewer"], "arm", "--sprint", str(sprint_id))
        self.run_cli(TOKENS["planner"], "arm", "--sprint", str(sprint_id))
        with self.assertRaisesRegex(SystemExit, "HTTP 409.*Developer participant"):
            self.run_cli(
                TOKENS["reviewer"],
                "register-pr",
                "--sprint",
                str(sprint_id),
                "--repository",
                "acme/repo",
                "--pr",
                "85",
                "--work-unit",
                str(unit_id),
            )
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*cannot transition"):
            self.run_cli(
                TOKENS["reviewer"],
                "complete",
                "--sprint",
                str(sprint_id),
                "--reason",
                "unauthorized",
                "--outcome",
                "accepted",
            )
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                ("armed", 0),
                con.execute(
                    "SELECT lifecycle,(SELECT COUNT(*) FROM sprint_registered_prs) "
                    "FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone(),
            )
        finally:
            con.close()

    def test_real_review_merge_dispatch_monitor_and_close_surfaces(self):
        request = self.run_cli(
            TOKENS["developer"],
            "request-review",
            "--sprint",
            str(self.sprint_id),
            "--registered-pr",
            str(self.registered_pr_id),
            "--readiness-file",
            self.write("All gates green."),
            "--key",
            "cli-review-request",
        )
        self.assertTrue(request["created"])
        self.deliver_message(request["message_id"])

        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                "accepted",
                sprint_message_delivery.SprintMessageStore(con).mark_read(
                    request["message_id"], 2
                ),
            )
        finally:
            con.close()

        review = self.run_cli(
            TOKENS["reviewer"],
            "record-review",
            "--sprint",
            str(self.sprint_id),
            "--registered-pr",
            str(self.registered_pr_id),
            "--verdict",
            "approved",
            "--body-file",
            self.write("No Medium-or-higher findings."),
            "--key",
            "cli-review-approved",
        )
        self.assertEqual("merge_ready", review["disposition"])

        with mock.patch.object(
            server.sprint_review_loop,
            "GitHubPullRequestReader",
            return_value=Reader(),
        ):
            authorization = self.run_cli(
                TOKENS["developer"],
                "authorize-merge",
                "--sprint",
                str(self.sprint_id),
                "--registered-pr",
                str(self.registered_pr_id),
            )
        self.assertEqual("a" * 40, authorization["head_sha"])

        dispatch = self.run_cli(
            TOKENS["planner"], "dispatch", "--sprint", str(self.sprint_id)
        )
        self.assertEqual(1, len(dispatch["wake_ids"]))
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                "ready",
                con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (self.dispatch_unit_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                dispatch["wake_ids"][0],
                con.execute(
                    "SELECT wm.wake_id FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING(message_id) "
                    "WHERE m.work_unit_id=? ORDER BY m.message_id DESC LIMIT 1",
                    (self.dispatch_unit_id,),
                ).fetchone()[0],
            )
        finally:
            con.close()
        monitor = self.run_cli(
            TOKENS["planner"], "monitor", "--sprint", str(self.sprint_id)
        )
        self.assertEqual([], monitor["outcomes"])
        self.assertEqual(
            {
                "action": "none",
                "requeued_wake_ids": [],
                "pause_reason": None,
            },
            monitor["pickup"],
        )
        self.assertEqual(
            {"state": "missing", "beat_at": None, "interval_seconds": 5},
            monitor["runtime"],
        )

        findings = self.write(
            json.dumps(
                [
                    {
                        "severity": "Low",
                        "title": "Follow-up",
                        "body": "Disposition after the Sprint.",
                    }
                ]
            )
        )
        conformance = self.run_cli(
            TOKENS["reviewer"],
            "record-conformance",
            "--sprint",
            str(self.sprint_id),
            "--body-file",
            self.write("Integrated conformance complete."),
            "--findings-file",
            findings,
            "--final-report-file",
            self.write("Reviewer-authored final Sprint report."),
            "--reason",
            "Reviewer approved integrated conformance",
            "--outcome",
            "accepted",
            "--key",
            "cli-conformance",
        )
        self.assertEqual(1, len(conformance["followup_ids"]))
        self.assertTrue(conformance["completed"])
        report = self.run_cli(
            TOKENS["planner"],
            "compile-report",
            "--sprint",
            str(self.sprint_id),
            "--limit",
            "10",
        )
        self.assertEqual(self.sprint_id, report["scope"]["sprint_id"])
        self.assertEqual(
            "Low", report["unresolved_work"]["followups"]["items"][0]["severity"]
        )

    def test_token_identity_blocks_cross_role_dispatch(self):
        mem.SC_API_TOKEN = TOKENS["developer"]
        with self.assertRaisesRegex(SystemExit, "HTTP 403.*owning Planner"):
            sprint_cli.main(["dispatch", "--sprint", str(self.sprint_id)])
