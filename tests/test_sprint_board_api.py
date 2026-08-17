"""FnB Sprint board read projections, auth boundary, cursors, and sanitization."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "api"), str(ENGINE / "scripts")]

import server
import sprint_board
import route_bindings
from github_pull_requests import PullRequest
from sprint_route_binding_support import candidate as route_candidate

_DYNAMIC_EVENT_CALLS = {
    ("sprint_domain.py", "f'lifecycle.{target}'"): {"lifecycle.completed"},
    ("sprint_review_loop.py", "f'review.{verdict}'"): {
        "review.approved",
        "review.changes_requested",
    },
    ("sprint_liveness.py", "event_type"): {
        "liveness.escalated",
        "liveness.escalation_delivery_unavailable",
    },
}


def emitted_sprint_event_types() -> set[str]:
    emitted = set()
    emitter_paths = [
        *(ENGINE / "scripts").glob("sprint_*.py"),
        ENGINE / "api" / "conversation_routes.py",
        ENGINE / "api" / "server.py",
    ]
    for path in sorted(emitter_paths):
        if path.name == "sprint_board.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO sprint_events" in node.value:
                    emitted.update(re.findall(r"'([a-z_]+\.[a-z_]+)'", node.value))
                continue
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else None
            if name != "_event":
                continue
            event = node.args[1]
            if isinstance(event, ast.Constant) and isinstance(event.value, str):
                emitted.add(event.value)
                continue
            key = (path.name, ast.unparse(event))
            if key not in _DYNAMIC_EVENT_CALLS:
                raise AssertionError(f"unresolved dynamic Sprint event emitter: {key}")
            emitted.update(_DYNAMIC_EVENT_CALLS[key])
    return emitted


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintBoardApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "board.db"
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con)
            self.ids = self._seed(con)

    @staticmethod
    def _seed(con: sqlite3.Connection) -> dict[str, int]:
        con.execute("INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,?,1,?)",
            (
                (1, "Admin", "ADM1", "admin", "prompt", "admin-token"),
                (2, "Planner", "PLN1", "planner", "prompt", "planner-token"),
                (3, "Developer", "DEV1", "dev", "prompt", "dev-token"),
                (4, "Reviewer", "REV1", "reviewer", "prompt", "review-token"),
                (5, "Developer two", "DEV2", "dev", "prompt", "dev-2-token"),
                (6, "Developer three", "DEV3", "dev", "prompt", "dev-3-token"),
                (7, "Developer four", "DEV4", "dev", "prompt", "dev-4-token"),
            ),
        )
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) VALUES ('Board feature','in_progress')"
            ).lastrowid
        )
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Board spec','body')",
                (feature_id,),
            ).lastrowid
        )
        bound_body = "body"
        bound_revision = hashlib.sha256(bound_body.encode()).hexdigest()
        task_ids = [
            int(
                con.execute(
                    "INSERT INTO spec_tasks (feature_id,document_id,seq,title,status) "
                    "VALUES (?,?,?,?,?)",
                    (feature_id, document_id, seq, f"Task {seq}", "done" if seq == 1 else "pending"),
                ).lastrowid
            )
            for seq in (1, 2)
        ]
        approval_id = int(
            con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,4,'pass')",
                (document_id, bound_revision),
            ).lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled,created_at) "
                "VALUES (?,2,1,'2026-08-01 10:00:00')",
                (feature_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id,"
            "bound_revision_body) VALUES (?,?,?,?,?)",
            (sprint_id, document_id, bound_revision, approval_id, bound_body),
        )
        participant_ids = {}
        for shell_id, role in ((2, "planner"), (3, "developer"), (4, "reviewer")):
            participant_ids[role] = int(
                con.execute(
                    "INSERT INTO sprint_participants "
                    "(sprint_id,shell_id,role,harness,disposition) VALUES (?,?,?,?,?)",
                    (sprint_id, shell_id, role, "codex", "active" if role == "developer" else "idle"),
                ).lastrowid
            )
        for shell_id in (5, 6, 7):
            con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,disposition) "
                "VALUES (?,?,'developer','codex','idle')",
                (sprint_id, shell_id),
            )
        con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=4,"
            "conformance_owner_generation=1,lifecycle='armed',"
            "armed_at='2026-08-01 10:01:00' "
            "WHERE sprint_id=?",
            (sprint_id,),
        )
        unit_ids = []
        for index, disposition in enumerate(
            (
                "completed",
                "cancelled",
                "in_review",
                "fixing",
                "merge_ready",
                "active",
                "planned",
                "ready",
                "blocked",
            ),
            start=1,
        ):
            assigned_shell_id = {
                "fixing": 5,
                "merge_ready": 6,
                "active": 7,
            }.get(disposition, 3)
            unit_ids.append(
                int(
                    con.execute(
                        "INSERT INTO sprint_work_units "
                        "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
                        "planned_wave,disposition,output_kind,completion_result,completed_at) "
                        "VALUES (?,?,4,?,?,?,?,?,?,?)",
                        (
                            sprint_id,
                            assigned_shell_id,
                            f"Unit {index}",
                            f"Output {index}",
                            index,
                            disposition,
                            "report_only" if index == 2 else "code",
                            "cancelled cleanly" if disposition == "cancelled" else None,
                            "2026-08-01 11:00:00" if disposition in {"completed", "cancelled"} else None,
                        ),
                    ).lastrowid
                )
            )
        con.execute(
            "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
            (sprint_id, unit_ids[0], task_ids[0]),
        )
        con.execute(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (sprint_id, unit_ids[2], unit_ids[0]),
        )
        pr_id = int(
            con.execute(
                "INSERT INTO sprint_registered_prs "
                "(sprint_id,owner_participant_id,repository,pr_number) "
                "VALUES (?,?, 'acme/repo',42)",
                (sprint_id, participant_ids["developer"]),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_pr_work_units (sprint_id,registered_pr_id,work_unit_id) "
            "VALUES (?,?,?)",
            (sprint_id, pr_id, unit_ids[2]),
        )
        con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha,observed_at) "
            "VALUES (?,'green','green-42','abc123','2026-08-01 12:00:00')",
            (pr_id,),
        )
        con.execute(
            "INSERT INTO wake_message "
            "(sprint_id,sender_shell_id,receiver_shell_id,from_participant_id,"
            "to_participant_id,work_unit_id,message_kind,body,declared_type,"
            "actionable,idempotency_key) VALUES (?,2,3,?,?,?,'notification',"
            "'audit note','re-enter',0,'audit-1')",
            (
                sprint_id,
                participant_ids["planner"],
                participant_ids["developer"],
                unit_ids[2],
            ),
        )
        for event_type, payload in (
            ("work_unit.ready", {"work_unit_id": unit_ids[2], "message_id": 7, "token": "nope"}),
            ("mystery.internal", {"work_unit_id": unit_ids[2], "secret": "leak-me"}),
            ("work_unit.ready", {"work_unit_id": unit_ids[0], "wake_id": 9}),
        ):
            con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload,created_at) "
                "VALUES (?,?,'system',NULL,?,'2026-08-01 12:30:00')",
                (sprint_id, event_type, json.dumps(payload)),
            )
        con.execute(
            "INSERT INTO sprint_judgments "
            "(sprint_id,participant_id,work_unit_id,kind,body,created_at) "
            "VALUES (?,?,?,'decision','Use the narrow path','2026-08-01 13:00:00')",
            (sprint_id, participant_ids["reviewer"], unit_ids[2]),
        )
        con.execute(
            "INSERT INTO sprint_reports "
            "(sprint_id,report_kind,author_shell_id,body,created_at) "
            "VALUES (?,'pause',2,'Paused for verification','2026-08-01 13:00:00')",
            (sprint_id,),
        )

        # Equal list timestamps prove the cursor includes sprint_id rather than
        # dropping or duplicating one row at a timestamp boundary.
        second = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled,created_at) "
                "VALUES (?,2,1,'2026-08-01 10:00:00')",
                (feature_id,),
            ).lastrowid
        )
        third = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled,created_at) "
                "VALUES (?,2,1,'2026-07-31 09:00:00')",
                (feature_id,),
            ).lastrowid
        )
        return {
            "feature_id": feature_id,
            "document_id": document_id,
            "sprint_id": sprint_id,
            "second_sprint_id": second,
            "third_sprint_id": third,
            "unit": unit_ids[2],
            "active_unit": unit_ids[5],
            "ready_unit": unit_ids[7],
            "other_unit": unit_ids[0],
            "developer_participant_id": participant_ids["developer"],
        }

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @staticmethod
    def _headers(
        authorization: str | None = None,
        *,
        body: bytes = b"",
        extra: dict[str, str] | None = None,
    ) -> str:
        lines = ["Host: 127.0.0.1:8800", f"Content-Length: {len(body)}"]
        if authorization:
            lines.append(f"Authorization: Bearer {authorization}")
        for name, value in (extra or {}).items():
            lines.append(f"{name}: {value}")
        return "\r\n".join(lines) + "\r\n"

    def request(
        self,
        method: str,
        path: str,
        authorization: str | None = None,
        *,
        body: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        def open_db():
            return self.connect()

        raw = json.dumps(body).encode() if body is not None else b""
        with mock.patch.object(server, "db", side_effect=open_db):
            status, headers, raw = server.dispatch_http(
                method,
                path,
                self._headers(
                    authorization, body=raw, extra=extra_headers
                ),
                raw,
            )
        return status, dict(headers), json.loads(raw)

    def test_list_has_exact_counts_and_stable_equal_timestamp_cursor(self):
        first_status, _, first = self.request("GET", "/api/sprints?limit=1")
        self.assertEqual(first_status, 200, first)
        self.assertEqual([self.ids["second_sprint_id"]], [x["sprint_id"] for x in first["items"]])
        self.assertTrue(first["next_cursor"])

        status, _, second = self.request(
            "GET", f"/api/sprints?limit=1&cursor={first['next_cursor']}"
        )
        self.assertEqual(status, 200, second)
        self.assertEqual([self.ids["sprint_id"]], [x["sprint_id"] for x in second["items"]])
        self.assertEqual(
            {"done": 2, "review": 3, "dev": 1, "waiting": 2, "blocked": 1},
            second["items"][0]["column_counts"],
        )

        status, _, third = self.request(
            "GET", f"/api/sprints?limit=1&cursor={second['next_cursor']}"
        )
        self.assertEqual(status, 200, third)
        self.assertEqual([self.ids["third_sprint_id"]], [x["sprint_id"] for x in third["items"]])

    def test_board_projects_authoritative_links_and_distinguishes_cancelled(self):
        status, _, board = self.request("GET", f"/api/sprints/{self.ids['sprint_id']}")
        self.assertEqual(status, 200, board)
        self.assertEqual("Board feature", board["sprint"]["feature"]["title"])
        self.assertEqual(
            {"state": "missing", "beat_at": None, "interval_seconds": 5},
            board["runtime"],
        )
        self.assertEqual("Board spec", board["specs"][0]["title"])
        units = {row["work_unit_id"]: row for row in board["work_units"]}
        review = units[self.ids["unit"]]
        self.assertEqual("review", review["column"])
        self.assertEqual([self.ids["other_unit"]], review["prerequisite_ids"])
        self.assertEqual("green", review["pull_requests"][0]["normalized_state"])
        self.assertEqual("https://github.com/acme/repo/pull/42", review["pull_requests"][0]["url"])
        self.assertEqual("audit note", review["messages"][0]["body"])
        cancelled = next(row for row in units.values() if row["disposition"] == "cancelled")
        self.assertEqual("done", cancelled["column"])
        self.assertEqual("cancelled cleanly", cancelled["completion_result"])
        self.assertEqual(
            {"aggregate_state": None, "target_count": 0, "pending_count": 0,
             "running_count": 0, "succeeded_count": 0, "failed_count": 0},
            board["cleanup"],
        )

    def test_board_and_list_project_cleanup_aggregate_without_target_paths(self):
        with self.connect() as con:
            con.execute(
                "UPDATE sprints SET lifecycle='completed',terminal_outcome='accepted',"
                "completed_at=datetime('now') WHERE sprint_id=?",
                (self.ids["sprint_id"],),
            )
            con.executemany(
                "INSERT INTO sprint_cleanup_targets "
                "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
                "git_common_dir,expected_base_branch,state,last_error_code) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    (
                        self.ids["sprint_id"], 3, "worktree",
                        "/repo/.sc-worktrees/dev1", "/repo", "/repo/.git",
                        "shell/dev1", "failed", "fixture_failed",
                    ),
                    (
                        self.ids["sprint_id"], None, "artifact_dir",
                        f"/repo/shared/sprints/sprint-{self.ids['sprint_id']}",
                        "/repo", "/repo/.git", None, "pending", None,
                    ),
                ),
            )

        status, _, board = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}"
        )
        self.assertEqual(200, status, board)
        self.assertEqual(
            {"aggregate_state": "failed", "target_count": 2, "pending_count": 1,
             "running_count": 0, "succeeded_count": 0, "failed_count": 1},
            board["cleanup"],
        )
        status, _, listing = self.request("GET", "/api/sprints?limit=100")
        self.assertEqual(200, status, listing)
        projected = next(
            row for row in listing["items"]
            if row["sprint_id"] == self.ids["sprint_id"]
        )
        self.assertEqual(board["cleanup"], projected["cleanup"])
        self.assertNotIn("/repo", json.dumps(projected["cleanup"]))

    def test_shell_cleanup_api_projects_bounded_status_and_stable_error_codes(self):
        with self.connect() as con:
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
                "VALUES (8,'Outsider','OUT1','dev','prompt',1,'outside-token')"
            )
            con.execute(
                "UPDATE sprints SET lifecycle='completed',terminal_outcome='accepted',"
                "completed_at=datetime('now') WHERE sprint_id=?",
                (self.ids["sprint_id"],),
            )
            con.execute(
                "INSERT INTO sprint_cleanup_targets "
                "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
                "git_common_dir,expected_base_branch,state,attempt_count,"
                "last_error_code,last_error_detail,before_evidence) "
                "VALUES (?,3,'worktree','/repo/.sc-worktrees/dev1','/repo',"
                "'/repo/.git','shell/dev1','failed',3,'fixture_failed',"
                "'bounded detail',?)",
                (
                    self.ids["sprint_id"],
                    json.dumps(
                        {
                            "branch": "feat/disposable",
                            "status_count": 1,
                            "status_sample": ["?? private-name.txt"],
                        }
                    ),
                ),
            )

        status, _, body = self.request(
            "GET",
            f"/_sc/sprint/cleanup-runs/{self.ids['sprint_id']}",
            "dev-token",
        )
        self.assertEqual(200, status, body)
        self.assertEqual(("failed", 1), (body["aggregate_state"], body["target_count"]))
        self.assertEqual(".sc-worktrees/dev1", body["targets"][0]["path_label"])
        self.assertNotIn("/repo", json.dumps(body))
        self.assertNotIn("private-name", json.dumps(body))

        status, _, denied = self.request(
            "GET",
            f"/_sc/sprint/cleanup-runs/{self.ids['sprint_id']}",
            "outside-token",
        )
        self.assertEqual(403, status, denied)
        self.assertEqual("cleanup_status_forbidden", denied["details"]["code"])

        payload = {
            "sprint_id": self.ids["sprint_id"],
            "idempotency_key": "retry-board-fixture",
            "adopt_legacy": False,
        }
        status, _, denied = self.request(
            "POST", "/_sc/sprint/cleanup-runs", "dev-token", body=payload
        )
        self.assertEqual(403, status, denied)
        self.assertEqual("cleanup_retry_forbidden", denied["details"]["code"])

        status, _, first = self.request(
            "POST", "/_sc/sprint/cleanup-runs", "planner-token", body=payload
        )
        self.assertEqual(201, status, first)
        status, _, replay = self.request(
            "POST", "/_sc/sprint/cleanup-runs", "planner-token", body=payload
        )
        self.assertEqual(200, status, replay)
        self.assertEqual(
            ("requeued", True, False, first["cleanup_request_id"], [first["target_ids"][0]]),
            (
                first["action"],
                first["created"],
                replay["created"],
                replay["cleanup_request_id"],
                replay["target_ids"],
            ),
        )

    def test_health_messages_project_exact_scoped_waits_beyond_audit_history(self):
        with self.connect() as con:
            planner = int(
                con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=2",
                    (self.ids["sprint_id"],),
                ).fetchone()[0]
            )
            developer = int(
                con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=7",
                    (self.ids["sprint_id"],),
                ).fetchone()[0]
            )
            reviewer = int(
                con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=4",
                    (self.ids["sprint_id"],),
                ).fetchone()[0]
            )

            def add_message(
                *,
                key: str,
                recipient: int,
                receiver_shell_id: int,
                unit_id: int | None,
                body: str,
                intent: str = "information",
                requires_reply: bool = False,
                created_at: str = "2026-08-01 10:30:00",
                delivered_at: str | None = None,
                read_at: str | None = None,
            ) -> int:
                return int(
                    con.execute(
                        "INSERT INTO wake_message "
                        "(sprint_id,sender_shell_id,receiver_shell_id,"
                        "from_participant_id,to_participant_id,work_unit_id,"
                        "message_kind,body,declared_type,actionable,idempotency_key,"
                        "intent,requires_reply,created_at,delivered_at,read_at) "
                        "VALUES (?,2,?,?,?,?,'notification',?,'re-enter',0,?,?,?,?,?,?)",
                        (
                            self.ids["sprint_id"],
                            receiver_shell_id,
                            planner,
                            recipient,
                            unit_id,
                            body,
                            key,
                            intent,
                            int(requires_reply),
                            created_at,
                            delivered_at,
                            read_at,
                        ),
                    ).lastrowid
                )

            old_unit_wait = add_message(
                key="health-old-unit-wait",
                recipient=developer,
                receiver_shell_id=7,
                unit_id=self.ids["active_unit"],
                body="Old decision still required",
                intent="decision",
                requires_reply=True,
                created_at="2026-08-01 10:02:00",
                delivered_at="2026-08-01 10:03:00",
            )
            for index in range(100):
                add_message(
                    key=f"health-audit-{index}",
                    recipient=developer,
                    receiver_shell_id=7,
                    unit_id=self.ids["active_unit"],
                    body=f"Audit message {index}",
                    created_at="2026-08-01 10:20:00",
                )
            current_unit_wait = add_message(
                key="health-current-unit-wait",
                recipient=developer,
                receiver_shell_id=7,
                unit_id=self.ids["active_unit"],
                body="Current blocker requires action",
                intent="blocker",
                requires_reply=True,
                created_at="2026-08-01 10:40:00",
                delivered_at="2026-08-01 10:41:00",
            )
            sprint_wait = add_message(
                key="health-sprint-wait",
                recipient=reviewer,
                receiver_shell_id=4,
                unit_id=None,
                body="Sprint decision required",
                intent="question",
                requires_reply=True,
                created_at="2026-08-01 10:42:00",
                delivered_at="2026-08-01 10:43:00",
                read_at="2026-08-01 10:44:00",
            )

        status, _, board = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}"
        )
        self.assertEqual(200, status, board)
        projected = {row["message_id"]: row for row in board["health_messages"]}
        self.assertEqual(
            {old_unit_wait, current_unit_wait, sprint_wait}, set(projected)
        )
        self.assertEqual(
            {
                "scope": "work_unit",
                "work_unit_id": self.ids["active_unit"],
                "intent": "decision",
                "requires_reply": True,
                "created_at": "2026-08-01 10:02:00",
                "delivered_at": "2026-08-01 10:03:00",
                "read_at": None,
                "reply_to_message_id": None,
                "linked_reply_message_ids": [],
                "linked_reply_count": 0,
                "linked_replies_truncated": False,
            },
            {
                key: projected[old_unit_wait][key]
                for key in (
                    "scope",
                    "work_unit_id",
                    "intent",
                    "requires_reply",
                    "created_at",
                    "delivered_at",
                    "read_at",
                    "reply_to_message_id",
                    "linked_reply_message_ids",
                    "linked_reply_count",
                    "linked_replies_truncated",
                )
            },
        )
        self.assertEqual("blocker", projected[current_unit_wait]["intent"])
        self.assertEqual(
            ("sprint", None, "question", "2026-08-01 10:44:00"),
            (
                projected[sprint_wait]["scope"],
                projected[sprint_wait]["work_unit_id"],
                projected[sprint_wait]["intent"],
                projected[sprint_wait]["read_at"],
            ),
        )
        active = next(
            row
            for row in board["work_units"]
            if row["work_unit_id"] == self.ids["active_unit"]
        )
        self.assertNotIn(
            old_unit_wait,
            {message["message_id"] for message in active["messages"]},
        )
        self.assertIn(
            {"message_id": old_unit_wait}, active["health"]["message_refs"]
        )

    def test_board_projects_stale_runtime_and_bounded_pickup_exhaustion(self):
        exhausted = {
            "sprint_id": self.ids["sprint_id"],
            "participant_id": self.ids["developer_participant_id"],
            "shell": "DEV1",
            "role": "developer",
            "work_unit_id": self.ids["unit"],
            "message_id": 71,
            "wake_id": 81,
            "conversation_id": "cv_dead",
            "run_state": "unknown",
            "error_code": "HARNESS_SESSION_DISCOVERY_FAILED",
            "failure_class": "native_unknown",
            "attempt_count": 1,
            "error_detail": "Traceback: private adapter detail",
            "stack_trace": "private stack",
        }
        with self.connect() as con:
            con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES ('sprint-runtime','2000-01-01 00:00:00',5)"
            )
            con.execute(
                "UPDATE sprints SET lifecycle='paused',"
                "paused_at='2026-08-01 14:00:00' WHERE sprint_id=?",
                (self.ids["sprint_id"],),
            )
            con.executemany(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,payload,created_at) "
                "VALUES (?,?,'system',?,'2026-08-01 14:00:00')",
                (
                    (
                        self.ids["sprint_id"],
                        "wake.pickup_exhausted",
                        json.dumps(exhausted),
                    ),
                    (
                        self.ids["sprint_id"],
                        "lifecycle.paused",
                        json.dumps({"from": "armed", "reason": "wake_pickup_unknown"}),
                    ),
                ),
            )

        status, _, board = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}"
        )
        self.assertEqual(200, status, board)
        self.assertEqual(
            {
                "state": "stale",
                "beat_at": "2000-01-01 00:00:00",
                "interval_seconds": 5,
            },
            board["runtime"],
        )
        self.assertEqual("paused", board["pickup"]["action"])
        self.assertEqual("wake_pickup_unknown", board["pickup"]["pause_reason"])
        self.assertEqual(
            {
                key: value
                for key, value in exhausted.items()
                if key not in {"error_detail", "stack_trace"}
            },
            {
                key: value
                for key, value in board["pickup"]["exhausted"].items()
                if key != "recovery_instruction"
            },
        )
        self.assertEqual(
            "Inspect the pause report, repair the named route or service, "
            "then use an authorized resume.",
            board["pickup"]["exhausted"]["recovery_instruction"],
        )
        affected = next(
            unit
            for unit in board["work_units"]
            if unit["work_unit_id"] == self.ids["unit"]
        )
        self.assertEqual(
            "HARNESS_SESSION_DISCOVERY_FAILED",
            affected["pickup"]["error_code"],
        )
        rendered = json.dumps(board)
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn("private stack", rendered)

        status, _, timeline = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/events?limit=100"
        )
        self.assertEqual(200, status, timeline)
        event = next(
            item
            for item in timeline["items"]
            if item["type"] == "wake.pickup_exhausted"
        )
        self.assertEqual(
            "HARNESS_SESSION_DISCOVERY_FAILED",
            event["details"]["error_code"],
        )
        self.assertNotIn("error_detail", event["details"])
        self.assertNotIn("stack_trace", event["details"])

    def test_stale_runtime_marks_zero_attempt_pending_wake_as_unhealthy(self):
        with self.connect() as con:
            con.execute(
                "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
                "VALUES ('sprint-runtime','2000-01-01 00:00:00',5)"
            )
            wake_id = int(
                con.execute(
                    "INSERT INTO sprint_wake_outbox "
                    "(sprint_id,participant_id,receiver_shell_id,idempotency_key) "
                    "VALUES (?,?,3,'runtime-stale-zero-attempt')",
                    (
                        self.ids["sprint_id"],
                        self.ids["developer_participant_id"],
                    ),
                ).lastrowid
            )
            message_id = int(
                con.execute(
                    "INSERT INTO wake_message "
                    "(sprint_id,receiver_shell_id,to_participant_id,work_unit_id,"
                    "message_kind,body,declared_type,actionable,disposition,"
                    "idempotency_key) VALUES (?,?,?,?,'work_assignment',"
                    "'pending assignment','new',1,'pending','runtime-stale-message')",
                    (
                        self.ids["sprint_id"],
                        3,
                        self.ids["developer_participant_id"],
                        self.ids["ready_unit"],
                    ),
                ).lastrowid
            )
            con.execute(
                "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
                "VALUES (?,?,?)",
                (self.ids["sprint_id"], wake_id, message_id),
            )

        status, _, board = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}"
        )
        self.assertEqual(200, status, board)
        ready = next(
            unit
            for unit in board["work_units"]
            if unit["work_unit_id"] == self.ids["ready_unit"]
        )
        self.assertEqual("waiting", ready["column"])
        self.assertEqual(
            {
                "state": "runtime_unavailable",
                "runtime_state": "stale",
                "wake_id": wake_id,
                "attempt_count": 0,
            },
            ready["delivery"],
        )

    def test_events_are_cursor_stable_and_never_return_unknown_payload_fields(self):
        status, _, first = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/events?limit=2"
        )
        self.assertEqual(status, 200, first)
        self.assertEqual(2, len(first["items"]))
        rendered = json.dumps(first)
        self.assertNotIn("leak-me", rendered)
        self.assertNotIn("nope", rendered)
        unknown = next(row for row in first["items"] if row["type"] == "mystery.internal")
        self.assertEqual({}, unknown["details"])

        status, _, second = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/events?limit=2&cursor={first['next_cursor']}"
        )
        self.assertEqual(status, 200, second)
        self.assertEqual(1, len(second["items"]))
        ids = [row["event_id"] for row in first["items"] + second["items"]]
        self.assertEqual(3, len(ids))
        self.assertEqual(3, len(set(ids)))

    def test_event_projection_matches_emitters_and_projects_review_and_conformance_evidence(self):
        self.assertEqual(emitted_sprint_event_types(), set(sprint_board._EVENT_FIELDS))
        cases = (
            (
                "sprint.declared",
                {
                    "feature_id": self.ids["feature_id"],
                    "spec_document_ids": [self.ids["document_id"]],
                    "spec_approval_ids": [1],
                    "participant_shell_ids": [2, 3, 4],
                    "secret": "hidden",
                },
            ),
            (
                "sprint.delivery_terminal",
                {
                    "terminal_count": 3,
                    "completed_count": 2,
                    "cancelled_count": 1,
                    "secret": "hidden",
                },
            ),
            (
                "sprint.cleanup_scheduled",
                {
                    "aggregate_state": "pending",
                    "artifact_target_ids": [4],
                    "target_count": 4,
                    "worktree_target_ids": [1, 2, 3],
                    "secret": "hidden",
                },
            ),
            (
                "sprint.cleanup_adopted",
                {
                    "aggregate_state": "pending",
                    "request_kind": "adopted_legacy",
                    "target_count": 4,
                    "target_ids": [1, 2, 3, 4],
                    "secret": "hidden",
                },
            ),
            (
                "sprint.cleanup_requeued",
                {
                    "aggregate_state": "pending",
                    "request_kind": "requeued",
                    "target_count": 4,
                    "target_ids": [2],
                    "secret": "hidden",
                },
            ),
            (
                "sprint.cleanup_failed",
                {
                    "aggregate_state": "failed",
                    "attempt_count": 3,
                    "cleanup_target_id": 2,
                    "claim_generation": 4,
                    "error_code": "fetch_failed",
                    "path_label": ".sc-worktrees/dev1",
                    "target_kind": "worktree",
                    "secret": "hidden",
                },
            ),
            (
                "sprint.cleanup_completed",
                {
                    "aggregate_state": "succeeded",
                    "succeeded_count": 4,
                    "target_count": 4,
                    "secret": "hidden",
                },
            ),
            (
                "review.approved",
                {
                    "work_unit_id": self.ids["unit"],
                    "registered_pr_id": 1,
                    "message_id": 8,
                    "conversation_id": "cv-approve",
                    "head_sha": "approved-head",
                    "secret": "hidden",
                },
            ),
            (
                "review.changes_requested",
                {
                    "work_unit_id": self.ids["unit"],
                    "registered_pr_id": 1,
                    "message_id": 9,
                    "conversation_id": "cv-fix",
                    "head_sha": "fix-head",
                    "secret": "hidden",
                },
            ),
            (
                "review.request_invalidated",
                {
                    "work_unit_id": self.ids["unit"],
                    "registered_pr_id": 1,
                    "invalidated_message_id": 8,
                    "head_sha": "replacement-head",
                    "previous_head_sha": "stale-head",
                    "secret": "hidden",
                },
            ),
            (
                "conformance.recorded",
                {
                    "report_id": 4,
                    "followup_count": 2,
                    "followup_ids": [11, 12],
                    "secret": "hidden",
                },
            ),
            (
                "pr.no_checks_observed",
                {
                    "registered_pr_id": 1,
                    "subscription_id": 2,
                    "transition_id": 3,
                    "observed_head_sha": "a" * 40,
                    "secret": "hidden",
                },
            ),
        )
        with self.connect() as con:
            con.executemany(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,payload,created_at) "
                "VALUES (?,?,'system',?,'2026-08-01 14:00:00')",
                (
                    (self.ids["sprint_id"], event_type, json.dumps(payload))
                    for event_type, payload in cases
                ),
            )

        status, _, body = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/events?limit=100"
        )
        self.assertEqual(status, 200, body)
        projected = {item["type"]: item["details"] for item in body["items"]}
        for event_type, payload in cases:
            expected = {key: value for key, value in payload.items() if key != "secret"}
            self.assertEqual(expected, projected[event_type])
            self.assertNotIn("secret", projected[event_type])

    def test_summaries_paginate_equal_timestamps_without_duplicates(self):
        status, _, first = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/summaries?limit=1"
        )
        self.assertEqual(status, 200, first)
        self.assertEqual("judgment", first["items"][0]["source"])
        status, _, second = self.request(
            "GET", f"/api/sprints/{self.ids['sprint_id']}/summaries?limit=1&cursor={first['next_cursor']}"
        )
        self.assertEqual(status, 200, second)
        self.assertEqual("report", second["items"][0]["source"])
        identities = {
            (first["items"][0]["source"], first["items"][0]["id"]),
            (second["items"][0]["source"], second["items"][0]["id"]),
        }
        self.assertEqual({("judgment", 1), ("report", 1)}, identities)

    def test_filters_validate_instead_of_silently_returning_empty(self):
        for path in (
            "/api/sprints?lifecycle=running",
            "/api/sprints?limit=0",
            "/api/sprints?cursor=W10",
            f"/api/sprints/{self.ids['sprint_id']}/events?work_unit_id=999999",
            f"/api/sprints/{self.ids['sprint_id']}/summaries?work_unit_id=abc",
        ):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 422, body)
                self.assertIn(
                    body["error"]["code"],
                    {"validation_error", "work_unit_scope_mismatch", "cursor_invalid"},
                )

    def test_browser_operator_boundary_rejects_shell_bearers(self):
        status, _, body = self.request("GET", "/api/sprints", "dev-token")
        self.assertEqual(status, 403, body)
        self.assertEqual("fnb_operator_required", body["error"]["code"])

        status, _, body = self.request("GET", "/api/sprints", "wrong-token")
        self.assertEqual(status, 401, body)
        self.assertEqual("unauthorized", body["error"]["code"])

    def test_browser_reads_exact_bound_revision_without_current_substitution(self):
        with self.connect() as con:
            con.execute(
                "UPDATE documents SET body='current drift' WHERE document_id=?",
                (self.ids["document_id"],),
            )
            con.commit()
        status, _, body = self.request(
            "GET",
            f"/api/sprints/{self.ids['sprint_id']}/spec-revisions/"
            f"{self.ids['document_id']}",
        )
        self.assertEqual(200, status, body)
        self.assertEqual("body", body["body"])
        self.assertEqual("available", body["availability"])
        self.assertEqual(
            hashlib.sha256(b"body").hexdigest(), body["bound_revision_sha256"]
        )

    def test_browser_bound_revision_rejects_malformed_document_id(self):
        for document_id in ("not-an-int", "0", "-1"):
            with self.subTest(document_id=document_id):
                status, _, body = self.request(
                    "GET",
                    f"/api/sprints/{self.ids['sprint_id']}/spec-revisions/"
                    f"{document_id}",
                )
                self.assertEqual(422, status, body)
                self.assertEqual(
                    {
                        "code": "validation_error",
                        "message": "document_id must be a positive integer",
                        "details": {"document_id": document_id},
                    },
                    body["error"],
                )
                self.assertNotIn("body", json.dumps(body))

    def test_browser_bound_revision_rejects_unbound_document_id(self):
        with self.connect() as con:
            unbound_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',99,'Unbound','unbound current body')",
                    (self.ids["feature_id"],),
                ).lastrowid
            )
            con.commit()
        status, _, body = self.request(
            "GET",
            f"/api/sprints/{self.ids['sprint_id']}/spec-revisions/{unbound_id}",
        )
        self.assertEqual(404, status, body)
        self.assertEqual(
            {
                "code": "spec_revision_not_found",
                "message": "governing revision not found",
                "details": {
                    "sprint_id": self.ids["sprint_id"],
                    "document_id": unbound_id,
                },
            },
            body["error"],
        )
        self.assertNotIn("unbound current body", json.dumps(body))

    def test_browser_bound_revision_reports_legacy_unavailability(self):
        current_body = "legacy current body must remain unavailable"
        with self.connect() as con:
            con.execute(
                "UPDATE sprint_specs SET bound_revision_body=NULL "
                "WHERE sprint_id=? AND document_id=?",
                (self.ids["sprint_id"], self.ids["document_id"]),
            )
            con.execute(
                "UPDATE documents SET body=? WHERE document_id=?",
                (current_body, self.ids["document_id"]),
            )
            con.commit()
        status, _, body = self.request(
            "GET",
            f"/api/sprints/{self.ids['sprint_id']}/spec-revisions/"
            f"{self.ids['document_id']}",
        )
        self.assertEqual(409, status, body)
        error = body["error"]
        self.assertEqual("bound_revision_unavailable", error["code"])
        self.assertEqual(
            "bound governing revision is unavailable for this legacy binding",
            error["message"],
        )
        self.assertEqual(
            {
                "sprint_id": self.ids["sprint_id"],
                "document_id": self.ids["document_id"],
                "bound_revision_sha256": hashlib.sha256(b"body").hexdigest(),
                "current_revision_sha256": hashlib.sha256(
                    current_body.encode()
                ).hexdigest(),
                "availability": "unavailable_legacy_drift",
            },
            error["details"],
        )
        self.assertNotIn(current_body, json.dumps(body))

    def test_browser_document_edit_requires_origin_and_records_fnb_evidence(self):
        path = f"/api/documents/{self.ids['document_id']}"
        status, _, denied = self.request(
            "PATCH",
            path,
            body={"body": "attacker text"},
            extra_headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(403, status, denied)
        with self.connect() as con:
            self.assertEqual(
                "body",
                con.execute(
                    "SELECT body FROM documents WHERE document_id=?",
                    (self.ids["document_id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='spec.body_edited'"
                ).fetchone()[0],
            )

        status, _, response = self.request(
            "PATCH",
            path,
            body={"body": "FnB edit"},
            extra_headers={
                "Origin": "http://127.0.0.1:8800",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(200, status, response)
        with self.connect() as con:
            event = con.execute(
                "SELECT actor_kind,actor_shell_id,payload FROM sprint_events "
                "WHERE event_type='spec.body_edited'"
            ).fetchone()
            self.assertEqual(("fnb", None), event[:2])
            payload = json.loads(event["payload"])
            self.assertEqual("fnb", payload["authority"])
            self.assertEqual("review_ui", payload["editor_surface"])
            self.assertEqual("not_required", payload["notification_state"])
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE message_kind='notification' "
                    "AND body LIKE '%governing document%'"
                ).fetchone()[0],
            )

    def test_board_snapshot_opens_and_closes_one_read_transaction(self):
        with self.connect() as con:
            statements = []
            con.set_trace_callback(statements.append)
            before = con.total_changes
            board = sprint_board.SprintBoardProjection(con).board(self.ids["sprint_id"])
            self.assertEqual(self.ids["sprint_id"], board["sprint"]["sprint_id"])
            self.assertEqual(before, con.total_changes)
            self.assertFalse(con.in_transaction)
            self.assertEqual(1, sum(sql == "BEGIN" for sql in statements))
            self.assertEqual(1, sum(sql == "ROLLBACK" for sql in statements))

    def test_board_projects_prepared_legacy_and_bound_route_contracts(self):
        sprint_id = self.ids["sprint_id"]
        with self.connect() as con:
            prepared_sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id,lifecycle) "
                    "SELECT feature_id,originating_planner_shell_id,'prepared' "
                    "FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).lastrowid
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,model,effort,disposition) "
                "VALUES (?,?,'developer',?,?,?,'idle')",
                (
                    (prepared_sprint_id, 5, "codex", "gpt-5.4", "high"),
                    (prepared_sprint_id, 6, "vibe", "vibe-model", None),
                    (prepared_sprint_id, 7, "codex", None, None),
                ),
            )
            prepared = sprint_board.SprintBoardProjection(con).board(
                prepared_sprint_id
            )
            self.assertEqual(
                {"unbound-intent"},
                {row["binding_status"] for row in prepared["participants"]},
            )
            prepared_by_shell = {
                row["shortname"]: row for row in prepared["participants"]
            }
            self.assertEqual(
                (
                    None,
                    None,
                    "controlled",
                    "high",
                    None,
                    None,
                    None,
                ),
                (
                    prepared_by_shell["DEV2"]["control_state"],
                    prepared_by_shell["DEV2"]["effective_effort"],
                    prepared_by_shell["DEV2"]["intent_control_state"],
                    prepared_by_shell["DEV2"]["intent_effective_effort"],
                    prepared_by_shell["DEV2"]["route_revision"],
                    prepared_by_shell["DEV2"]["binding_digest"],
                    prepared_by_shell["DEV2"]["catalogue_generation"],
                ),
            )
            self.assertEqual(
                ("native-uncontrolled", None),
                (
                    prepared_by_shell["DEV3"]["intent_control_state"],
                    prepared_by_shell["DEV3"]["intent_effective_effort"],
                ),
            )
            self.assertEqual(
                ("harness-default", None),
                (
                    prepared_by_shell["DEV4"]["intent_control_state"],
                    prepared_by_shell["DEV4"]["intent_effective_effort"],
                ),
            )
            participant = con.execute(
                "SELECT participant_id,harness,model,effort "
                "FROM sprint_participants WHERE sprint_id=? AND role='developer' "
                "ORDER BY participant_id LIMIT 1",
                (sprint_id,),
            ).fetchone()
            con.execute(
                "UPDATE sprints SET lifecycle='paused',paused_at=datetime('now') "
                "WHERE sprint_id=?",
                (sprint_id,),
            )
            resolved = route_candidate(con, participant)
            receipt = route_bindings.ParticipantRouteBindingStore(con).bind(
                int(participant["participant_id"]),
                resolved.binding,
                resolved.binding_digest,
                transition="reroute",
                runtime_status=resolved.runtime_status,
                runtime_scope=resolved.runtime_scope,
            )
            con.commit()

            armed = sprint_board.SprintBoardProjection(con).board(sprint_id)
            bound = next(
                row for row in armed["participants"]
                if row["participant_id"] == int(participant["participant_id"])
            )
            legacy = next(
                row for row in armed["participants"]
                if row["participant_id"] != int(participant["participant_id"])
            )
            self.assertEqual(
                (
                    "bound",
                    2,
                    "harness-default",
                    1,
                    receipt["binding_digest"],
                ),
                (
                    bound["binding_status"],
                    bound["route_contract_version"],
                    bound["control_state"],
                    bound["route_revision"],
                    bound["binding_digest"],
                ),
            )
            self.assertEqual(
                ("legacy", 1, None, None),
                (
                    legacy["binding_status"],
                    legacy["route_contract_version"],
                    legacy["route_revision"],
                    legacy["binding_digest"],
                ),
            )

    def test_every_read_surface_is_side_effect_free_and_never_reads_external_prs(self):
        sprint_id = self.ids["sprint_id"]
        paths = (
            "/api/sprints?limit=2",
            f"/api/sprints/{sprint_id}",
            f"/api/sprints/{sprint_id}/events?limit=2",
            f"/api/sprints/{sprint_id}/summaries?limit=2",
        )
        with self.connect() as observer:
            before = observer.execute("PRAGMA data_version").fetchone()[0]
            with mock.patch.object(
                server.sprint_recovery.GitHubPullRequestReader,
                "get",
                side_effect=AssertionError("GET projection performed an external PR read"),
            ) as external_read:
                for path in paths:
                    with self.subTest(path=path):
                        status, _, body = self.request("GET", path)
                        self.assertEqual(200, status, body)
            after = observer.execute("PRAGMA data_version").fetchone()[0]
        self.assertEqual(before, after)
        external_read.assert_not_called()

    def test_lifecycle_patch_delegates_pause_resume_abort_and_retries_safely(self):
        sprint_id = self.ids["sprint_id"]
        origin = {"Origin": "http://127.0.0.1:8800", "Sec-Fetch-Site": "same-origin"}
        patches = (
            mock.patch.object(
                server.sprint_domain.conversation_broker,
                "notify_commit",
                return_value=True,
            ),
            mock.patch.object(
                server.sprint_domain.conversation_broker,
                "interrupt_run",
                return_value=True,
            ),
            mock.patch.object(
                server.sprint_recovery.GitHubPullRequestReader,
                "get",
                return_value=PullRequest(
                    number=42,
                    head_ref="feat/board",
                    base_ref="main",
                    head_sha="b" * 40,
                    state="OPEN",
                    merged_at=None,
                    merge_sha=None,
                    title="Board",
                    url="https://github.example/acme/repo/pull/42",
                    review_decision=None,
                    checks="SUCCESS",
                    checks_failed=False,
                ),
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        status, _, paused = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "paused", "reason": "Inspect the lane"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, paused)
        self.assertTrue(paused["changed"])
        self.assertEqual("paused", paused["sprint"]["lifecycle"])
        status, _, paused_replay = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "paused", "reason": "Inspect the lane"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, paused_replay)
        self.assertFalse(paused_replay["changed"])

        status, _, resumed = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "armed", "reason": "Inspection complete"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, resumed)
        self.assertTrue(resumed["changed"])
        self.assertEqual("armed", resumed["sprint"]["lifecycle"])
        status, _, resumed_replay = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "armed", "reason": "Inspection complete"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, resumed_replay)
        self.assertFalse(resumed_replay["changed"])

        status, _, aborted = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "aborted", "reason": "Stop the Sprint"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, aborted)
        self.assertTrue(aborted["changed"])
        self.assertEqual("aborted", aborted["sprint"]["lifecycle"])
        status, _, replay = self.request(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            body={"lifecycle": "aborted", "reason": "Stop the Sprint"},
            extra_headers=origin,
        )
        self.assertEqual(status, 200, replay)
        self.assertFalse(replay["changed"])
        with self.connect() as con:
            self.assertEqual(
                [("aborted", "aborted")],
                [tuple(row) for row in con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                )],
            )
            self.assertEqual(
                1,
                con.execute(
                    "SELECT COUNT(*) FROM sprint_reports "
                    "WHERE sprint_id=? AND report_kind='abort'",
                    (sprint_id,),
                ).fetchone()[0],
            )

    def test_lifecycle_patch_rejects_bad_input_cross_origin_and_illegal_edges(self):
        sprint_id = self.ids["sprint_id"]
        same_origin = {"Origin": "http://127.0.0.1:8800"}
        cases = (
            ({"lifecycle": "paused"}, same_origin, 422, "validation_error"),
            ({"lifecycle": "completed", "reason": "No"}, same_origin, 422, "validation_error"),
            ({"lifecycle": "paused", "reason": "x", "extra": True}, same_origin, 422, "validation_error"),
            ({"lifecycle": "paused", "reason": "x" * 2001}, same_origin, 422, "validation_error"),
            (
                {"lifecycle": "paused", "reason": "cross origin"},
                {"Origin": "https://attacker.example"},
                403,
                "same_origin_required",
            ),
        )
        for body, headers, expected_status, code in cases:
            with self.subTest(body=body, headers=headers):
                status, _, response = self.request(
                    "PATCH",
                    f"/api/sprints/{sprint_id}",
                    body=body,
                    extra_headers=headers,
                )
                self.assertEqual(expected_status, status, response)
                self.assertEqual(code, response["error"]["code"])
        status, _, conflict = self.request(
            "PATCH",
            f"/api/sprints/{self.ids['third_sprint_id']}",
            body={"lifecycle": "armed", "reason": "Cannot arm prepared here"},
            extra_headers=same_origin,
        )
        self.assertEqual(409, status, conflict)
        self.assertEqual("lifecycle_conflict", conflict["error"]["code"])
        with self.connect() as con:
            self.assertEqual(
                "armed",
                con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
            )
            self.assertEqual(
                "prepared",
                con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.ids["third_sprint_id"],),
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
