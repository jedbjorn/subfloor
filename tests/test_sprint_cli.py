"""Authenticated end-to-end gates for the shell-facing Sprint commands."""
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
sys.path[:0] = [str(ENGINE / "scripts"), str(ENGINE / "api"), str(ROOT / "tests")]

import mem  # noqa: E402
import server  # noqa: E402
import sprint_cli  # noqa: E402
import sprint_domain  # noqa: E402
import sprint_message_delivery  # noqa: E402
from github_pull_requests import PullRequest  # noqa: E402
from test_sprint_v2_domain import apply_schema  # noqa: E402

TOKENS = {
    "developer": "dev-token",
    "reviewer": "review-token",
    "planner": "planner-token",
}


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
        initial_wake = sprint_domain.SprintLifecycleStore(con).arm(
            cls.sprint_id, 3
        )[0]
        initial_message = int(
            con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (initial_wake,),
            ).fetchone()[0]
        )
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
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha) "
            "VALUES (?,'green','green-42',?)",
            (cls.registered_pr_id, "a" * 40),
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
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, sprint_cli.main(list(argv)))
        return json.loads(output.getvalue())

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
                    "SELECT disposition FROM sprint_work_units "
                    "WHERE work_unit_id=?",
                    (self.dispatch_unit_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                dispatch["wake_ids"][0],
                con.execute(
                    "SELECT wm.wake_id FROM sprint_wake_messages wm "
                    "JOIN sprint_messages m USING(message_id) "
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
            "--key",
            "cli-conformance",
        )
        self.assertEqual(1, len(conformance["followup_ids"]))
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
