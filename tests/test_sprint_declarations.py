#!/usr/bin/env python3
"""Reviewed-spec QAQC and authoritative sprint declaration contracts."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))

import sprint_lifecycle  # noqa: E402
import sprint_routes  # noqa: E402
import sprint as sprint_cli  # noqa: E402
import mem as mem_cli  # noqa: E402


def build_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def decode(result):
    status, headers, body = result
    return status, dict(headers), json.loads(body)


class SprintDeclarationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc_sprint_declare_")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        self.con = build_db(self.db_path)
        self.addCleanup(self.con.close)
        self.con.execute(
            "INSERT INTO users (user_id,username,is_active) "
            "VALUES (1,'operator',1)"
        )
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES (?,?,?,?,?,?)",
            (
                (1, "Planner One", "PLN1", "planner", "x", "plan-token"),
                (2, "Planner Two", "PLN2", "planner", "x", "plan2-token"),
                (3, "Reviewer", "REV1", "reviewer", "x", "rev-token"),
                (4, "Developer", "DEV1", "dev", "x", "dev-token"),
                (5, "Conductor", "CON1", "conductor", "x", "cond-token"),
            ),
        )
        self.con.execute(
            "INSERT INTO roadmap (feature_id,title,summary) "
            "VALUES (7,'Declaration contract','x')"
        )
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,feature_id,kind,seq,title,body) "
            "VALUES (20,7,'spec',1,'Reviewed spec',?)",
            ("# exact body\n",),
        )
        for harness, selector in (
            ("claude", "opus"),
            ("codex", "gpt-5"),
            ("claude", "sonnet"),
        ):
            self.con.execute(
                "INSERT INTO model_routes "
                "(harness,selector,source,availability,headless_supported,"
                " high_effort_supported,last_seen_at) "
                "VALUES (?,?,'test','available',1,1,datetime('now'))",
                (harness, selector),
            )
        self.con.commit()
        self.db_patch = mock.patch.object(
            sprint_routes, "DB_PATH", self.db_path
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.config_patch = mock.patch.object(
            sprint_routes.conductor_runtime,
            "load_config",
            return_value=sprint_routes.conductor_runtime.ConductorConfig(
                enabled=True,
                shell="CON1",
                model="openai/gpt-5.6-luna",
            ),
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    @staticmethod
    def headers(token: str | None, key: str | None = None) -> str:
        lines = ["Host: 127.0.0.1:8800"]
        if token is not None:
            lines.append(f"Authorization: Bearer {token}")
        if key is not None:
            lines.append(f"Idempotency-Key: {key}")
        return "\r\n".join(lines) + "\r\n"

    def call(self, method: str, path: str, body=None, *,
             token: str | None = "plan-token", key: str | None = None):
        return decode(sprint_routes.handle(
            method,
            path,
            self.headers(token, key),
            json.dumps(body or {}).encode() if body is not None else b"",
        ))

    def qaqc(self, verdict="approved", *, key="qaqc-1"):
        return self.call(
            "POST",
            "/api/spec-qaqc-reviews",
            {"spec_doc_id": 20, "verdict": verdict},
            token="rev-token",
            key=key,
        )

    @staticmethod
    def declaration():
        return {
            "spec_doc_id": 20,
            "title": "Contract sprint",
            "planner_route": "claude/opus",
            "dev_route": "codex/gpt-5",
            "reviewer_route": "claude/sonnet",
        }

    def staged_sprint(self, *, key="declare-staged"):
        self.qaqc(key=f"{key}-qaqc")
        sprint = self.call(
            "POST", "/api/sprints", self.declaration(), key=key
        )[2]
        self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title,dev_shell_id,reviewer_shell_id) "
            "VALUES (?,'U1','Build the contract',4,3)",
            (sprint["sprint_doc_id"],),
        )
        self.con.commit()
        return sprint

    def test_qaqc_is_server_hashed_append_only_and_idempotent(self):
        status, _headers, first = self.qaqc()
        self.assertEqual(status, 201)
        self.assertEqual(
            first["body_sha256"],
            sprint_lifecycle.body_sha256("# exact body\n"),
        )
        status, _headers, replay = self.qaqc()
        self.assertEqual(status, 201)
        self.assertEqual(replay["review_id"], first["review_id"])
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM spec_qaqc_reviews"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE spec_qaqc_reviews SET verdict='changes_requested'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM spec_qaqc_reviews")

    def test_qaqc_rejects_claimed_hash_unknown_fields_and_non_reviewer(self):
        status, _headers, error = self.call(
            "POST",
            "/api/spec-qaqc-reviews",
            {
                "spec_doc_id": 20,
                "verdict": "approved",
                "body_sha256": "0" * 64,
            },
            token="rev-token",
            key="claimed",
        )
        self.assertEqual((status, error["error"]["code"]), (422, "validation"))
        status, _headers, error = self.call(
            "POST",
            "/api/spec-qaqc-reviews",
            {"spec_doc_id": 20, "verdict": "approved"},
            token="dev-token",
            key="dev-review",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "reviewer_required"),
        )

    def test_missing_rejected_and_stale_qaqc_leave_no_sprint_document(self):
        before = self.con.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
        status, _headers, error = self.call(
            "POST",
            "/api/sprints",
            self.declaration(),
            key="missing",
        )
        self.assertEqual((status, error["error"]["code"]), (409, "qaqc_required"))
        self.qaqc("changes_requested", key="rejected")
        status, _headers, error = self.call(
            "POST",
            "/api/sprints",
            self.declaration(),
            key="rejected-declare",
        )
        self.assertEqual((status, error["error"]["code"]), (409, "qaqc_required"))
        self.qaqc(key="approved-old")
        self.con.execute(
            "UPDATE documents SET body='# revised body\\n' WHERE document_id=20"
        )
        self.con.commit()
        status, _headers, error = self.call(
            "POST",
            "/api/sprints",
            self.declaration(),
            key="stale",
        )
        self.assertEqual((status, error["error"]["code"]), (409, "qaqc_required"))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            before,
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0],
            0,
        )

    def test_declaration_is_atomic_idempotent_and_records_exact_owner_routes(self):
        _status, _headers, review = self.qaqc()
        body = self.declaration()
        status, headers, sprint = self.call(
            "POST", "/api/sprints", body, key="declare-1"
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            headers["Location"],
            f"/api/sprints/{sprint['sprint_doc_id']}",
        )
        self.assertEqual(sprint["state"], "declared")
        self.assertEqual(sprint["planner"]["shortname"], "PLN1")
        self.assertEqual(sprint["qaqc"]["review_id"], review["review_id"])
        self.assertEqual(sprint["planner_route"], "claude/opus")
        self.assertEqual(sprint["dev_route"], "codex/gpt-5")
        self.assertEqual(sprint["reviewer_route"], "claude/sonnet")
        self.assertEqual(sprint["units"], [])

        status, headers, replay = self.call(
            "POST", "/api/sprints", body, key="declare-1"
        )
        self.assertEqual(status, 201)
        self.assertEqual(replay["sprint_doc_id"], sprint["sprint_doc_id"])
        self.assertEqual(headers["Location"], headers["Location"])
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM documents WHERE title LIKE 'SPRINT:%'"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprints SET planner_route='claude/sonnet' "
                "WHERE sprint_doc_id=?",
                (sprint["sprint_doc_id"],),
            )

    def test_mutations_require_authentic_role_and_idempotency_key(self):
        status, _headers, error = self.call(
            "POST",
            "/api/spec-qaqc-reviews",
            {"spec_doc_id": 20, "verdict": "approved"},
            token="rev-token",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (422, "idempotency_key_required"),
        )
        status, _headers, error = self.call(
            "POST",
            "/api/sprints",
            self.declaration(),
            token="dev-token",
            key="wrong-role",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "planner_required"),
        )
        status, _headers, error = self.call(
            "POST",
            "/api/sprints",
            self.declaration(),
            token="not-a-token",
            key="bad-token",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (401, "unauthorized"),
        )

    def test_failure_after_document_insert_rolls_back_every_resource_row(self):
        self.qaqc()
        self.con.executescript(
            """
            CREATE TRIGGER reject_sprint_insert
            BEFORE INSERT ON sprints
            BEGIN
              SELECT RAISE(ABORT, 'injected sprint-row failure');
            END;
            """
        )
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.call(
                "POST",
                "/api/sprints",
                self.declaration(),
                key="injected-failure",
            )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM documents WHERE title LIKE 'SPRINT:%'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0],
            0,
        )

    def test_legacy_backfill_is_needs_owner_and_operator_adoption_is_explicit(self):
        # Create a pre-0123-shaped board in a separate DB, then apply only 0123.
        path = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == "0123_sprint_declarations.sql":
                continue
            con.executescript(migration.read_text())
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES (1,'Planner','PLN1','planner','x','p')"
        )
        con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (9,'doc','SPRINT: legacy','old')"
        )
        con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title) VALUES (9,'U1','legacy unit')"
        )
        con.commit()
        con.executescript(
            (MIGRATIONS / "0123_sprint_declarations.sql").read_text()
        )
        row = con.execute("SELECT * FROM sprints").fetchone()
        self.assertEqual((row["state"], row["legacy"]),
                         ("needs_owner", 1))
        self.assertIsNone(row["planner_shell_id"])
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM sprint_units").fetchone()[0],
            1,
        )
        con.close()

    def test_legacy_adoption_is_explicit_operator_only_and_idempotent(self):
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,feature_id,kind,seq,title,body) "
            "VALUES (30,7,'doc',2,'SPRINT: old board','legacy')"
        )
        self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id,seq,unit_title) VALUES (30,'U1','kept exactly')"
        )
        self.con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy) VALUES (30,'needs_owner',1)"
        )
        self.con.commit()
        body = {
            "planner": "PLN1",
            "spec_doc_id": 20,
            "planner_route": "claude/opus",
            "dev_route": "codex/gpt-5",
            "reviewer_route": "claude/sonnet",
            "evidence": "FnB named PLN1 as durable owner",
        }
        status, _headers, error = self.call(
            "POST",
            "/api/sprints/30/adopt",
            body,
            token="plan-token",
            key="adopt-shell",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "operator_required"),
        )
        status, _headers, adopted = self.call(
            "POST",
            "/api/sprints/30/adopt",
            body,
            token=None,
            key="adopt-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(adopted["state"], "declared")
        self.assertEqual(adopted["planner"]["shortname"], "PLN1")
        self.assertTrue(adopted["legacy"])
        replay = self.call(
            "POST",
            "/api/sprints/30/adopt",
            body,
            token=None,
            key="adopt-1",
        )[2]
        self.assertEqual(replay["sprint_doc_id"], 30)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sentinel_events "
                "WHERE event_kind='sprint-adopted' AND sprint_doc_id=30"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT unit_title FROM sprint_units "
                "WHERE sprint_doc_id=30 AND seq='U1'"
            ).fetchone()[0],
            "kept exactly",
        )

    def test_planner_and_reviewer_cli_use_the_real_contracts(self):
        calls = []

        def sprint_api(method, path, body=None, idem_key=None):
            calls.append((method, path, body, idem_key))
            return {
                "sprint_doc_id": 44,
                "title": "SPRINT: CLI contract",
            }

        with mock.patch.object(sprint_cli, "_api", side_effect=sprint_api):
            self.assertEqual(
                sprint_cli.main(
                    [
                        "declare",
                        "--spec", "20",
                        "--title", "CLI contract",
                        "--planner-route", "claude/opus",
                        "--dev-route", "codex/gpt-5",
                        "--reviewer-route", "claude/sonnet",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0:2], ("POST", "/api/sprints"))
        self.assertTrue(calls[0][3].startswith("sprint-declare|20|"))

        calls.clear()
        with mock.patch.object(sprint_cli, "_api", side_effect=sprint_api):
            self.assertEqual(
                sprint_cli.main(["arm", "--sprint", "44"]),
                0,
            )
        self.assertEqual(
            calls[0][0:3],
            ("PATCH", "/api/sprints/44", {"state": "active"}),
        )
        self.assertTrue(calls[0][3].startswith("sprint-arm|44|"))

        calls.clear()
        with mock.patch.object(sprint_cli, "_api", side_effect=sprint_api):
            self.assertEqual(
                sprint_cli.main(
                    ["abort", "--sprint", "44", "--report", "Stopped safely"]
                ),
                0,
            )
        self.assertEqual(
            calls[0][0:3],
            (
                "PATCH",
                "/api/sprints/44",
                {"state": "aborted", "report": "Stopped safely"},
            ),
        )
        self.assertTrue(calls[0][3].startswith("sprint-abort|44|"))

        calls.clear()

        def mem_api(method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            return {
                "review_id": 9,
                "verdict": "approved",
                "body_sha256": "a" * 64,
            }

        with mock.patch.object(mem_cli, "_api", side_effect=mem_api):
            self.assertEqual(
                mem_cli.main(
                    ["doc", "qaqc", "20", "--verdict", "approved"]
                ),
                0,
            )
        self.assertEqual(calls[0][0:2], ("POST", "/api/spec-qaqc-reviews"))
        self.assertTrue(calls[0][3]["idempotent"])

    def test_originating_planner_arms_and_browser_has_no_arm_authority(self):
        sprint = self.staged_sprint()
        sprint_id = sprint["sprint_doc_id"]
        status, _headers, error = self.call(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            {"state": "active"},
            token=None,
            key="operator-arm",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "planner_required"),
        )
        status, _headers, error = self.call(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            {"state": "active"},
            token="plan2-token",
            key="other-arm",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "not_sprint_owner"),
        )

        with mock.patch.object(
            sprint_routes.conversation_broker, "notify_commit"
        ) as notify:
            status, _headers, armed = self.call(
                "PATCH",
                f"/api/sprints/{sprint_id}",
                {"state": "active"},
                key="planner-arm",
            )
        self.assertEqual(status, 200)
        self.assertEqual(armed["state"], "active")
        self.assertEqual(armed["conductor"]["shell"]["shortname"], "CON1")
        self.assertEqual(armed["conductor"]["state"], "queued")
        notify.assert_called_once_with()
        self.assertEqual(
            tuple(self.con.execute(
                "SELECT role,lifecycle,state FROM "
                "sprint_conversation_bindings"
            ).fetchone()),
            ("conductor", "persistent", "active"),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_outbox "
                "WHERE state='pending'"
            ).fetchone()[0],
            2,
        )
        release = self.con.execute(
            "SELECT directive_id,issuer_shell_id,issuer_flavor,kind,target,"
            "sprint_doc_id,unit_id,status FROM directives "
            "WHERE sprint_doc_id=? AND kind='sprint-armed'",
            (sprint_id,),
        ).fetchone()
        self.assertIsNotNone(release)
        self.assertEqual(
            tuple(release)[1:],
            (
                None,
                "system",
                "sprint-armed",
                "conductor",
                sprint_id,
                None,
                "pending",
            ),
        )
        queued = list(self.con.execute(
            "SELECT body FROM conversation_messages "
            "WHERE conversation_id=? ORDER BY message_id",
            (armed["conductor"]["conversation_id"],),
        ))
        self.assertEqual(len(queued), 2)
        self.assertIn(
            f'"directive_id":{release["directive_id"]}',
            queued[1]["body"],
        )

        with mock.patch.object(
            sprint_routes.conductor_runtime,
            "load_config",
            return_value=sprint_routes.conductor_runtime.ConductorConfig(),
        ):
            replay = self.call(
                "PATCH",
                f"/api/sprints/{sprint_id}",
                {"state": "active"},
                key="planner-arm",
            )[2]
        self.assertEqual(
            replay["conductor"]["conversation_id"],
            armed["conductor"]["conversation_id"],
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM conversations WHERE mode='sprint'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM directives "
                "WHERE sprint_doc_id=? AND kind='sprint-armed'",
                (sprint_id,),
            ).fetchone()[0],
            1,
        )

    def test_arm_release_enqueue_failure_rolls_back_every_activation_row(self):
        sprint = self.staged_sprint(key="arm-release-failure")
        sprint_id = sprint["sprint_doc_id"]

        with mock.patch.object(
            sprint_routes.sprint_conversations,
            "enqueue_conductor_directive",
            side_effect=sqlite3.OperationalError("injected release enqueue failure"),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "injected release enqueue failure",
            ):
                self.call(
                    "PATCH",
                    f"/api/sprints/{sprint_id}",
                    {"state": "active"},
                    key="arm-release-failure",
                )

        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprints WHERE sprint_doc_id=?",
                (sprint_id,),
            ).fetchone()[0],
            "declared",
        )
        for table in (
            "conversations",
            "sprint_conversation_bindings",
            "directives",
            "conversation_messages",
            "conversation_outbox",
        ):
            self.assertEqual(
                self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                f"{table} escaped a failed arm transaction",
            )

    def test_parallel_arm_retry_creates_exactly_one_conductor(self):
        sprint = self.staged_sprint(key="parallel-arm")
        sprint_id = sprint["sprint_doc_id"]

        def arm():
            return self.call(
                "PATCH",
                f"/api/sprints/{sprint_id}",
                {"state": "active"},
                key="same-parallel-arm",
            )

        with (
            mock.patch.object(
                sprint_routes.conversation_broker, "notify_commit"
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _index: arm(), range(2)))
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(
            {
                result[2]["conductor"]["conversation_id"]
                for result in results
            },
            {
                self.con.execute(
                    "SELECT conversation_id FROM sprint_conversation_bindings "
                    "WHERE role='conductor'"
                ).fetchone()[0]
            },
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_conversation_bindings "
                "WHERE role='conductor'"
            ).fetchone()[0],
            1,
        )

    def test_operator_cancel_stops_work_clears_projection_and_planner_aborts(self):
        sprint = self.staged_sprint(key="cancel-flow")
        sprint_id = sprint["sprint_doc_id"]
        self.call(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            {"state": "active"},
            key="arm-cancel-flow",
        )
        conductor_id = self.con.execute(
            "SELECT conversation_id FROM sprint_conversation_bindings "
            "WHERE role='conductor'"
        ).fetchone()[0]

        status, _headers, error = self.call(
            "POST",
            f"/api/sprints/{sprint_id}/cancellations",
            {"reason": "wrong actor"},
            token="plan-token",
            key="shell-cancel",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "operator_required"),
        )
        with mock.patch.object(
            sprint_routes.conversation_broker, "notify_commit"
        ):
            status, headers, cancelled = self.call(
                "POST",
                f"/api/sprints/{sprint_id}/cancellations",
                {"reason": "FnB stopped the Sprint"},
                token=None,
                key="operator-cancel",
            )
        self.assertEqual(status, 202)
        self.assertTrue(cancelled["cleared"])
        self.assertIn("/cancellations/", headers["Location"])
        cancellation = cancelled["cancellation"]
        self.assertEqual(cancellation["state"], "requested")
        self.assertEqual(cancellation["reason"], "FnB stopped the Sprint")
        self.assertEqual(
            self.call(
                "GET", headers["Location"], token=None
            )[2],
            cancellation,
        )
        self.assertNotEqual(
            cancellation["planner_conversation_id"], conductor_id
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE sprint_doc_id=?",
                (sprint_id,),
            ).fetchone()[0],
            "cancelled",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM conversation_outbox "
                "WHERE conversation_id=?",
                (conductor_id,),
            ).fetchone()[0],
            "cancelled",
        )
        self.assertEqual(
            self.call(
                "GET", "/api/sprints?status=active", token=None
            )[2],
            {"active_count": 0, "sprints": []},
        )
        planner_binding = self.con.execute(
            "SELECT role,lifecycle,required_result_kind,state "
            "FROM sprint_conversation_bindings "
            "WHERE conversation_id=?",
            (cancellation["planner_conversation_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(planner_binding),
            ("planner", "one_shot", "abort-report", "pending"),
        )

        status, _headers, error = self.call(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            {"state": "aborted", "report": "# Abort report\n\nStopped safely."},
            token="plan2-token",
            key="wrong-planner-abort",
        )
        self.assertEqual(
            (status, error["error"]["code"]),
            (403, "not_sprint_owner"),
        )
        status, _headers, aborted = self.call(
            "PATCH",
            f"/api/sprints/{sprint_id}",
            {"state": "aborted", "report": "# Abort report\n\nStopped safely."},
            key="planner-abort",
        )
        self.assertEqual(status, 200)
        self.assertEqual(aborted["state"], "aborted")
        self.assertEqual(aborted["cancellation"]["state"], "completed")
        self.assertEqual(
            aborted["cancellation"]["abort_report"],
            "# Abort report\n\nStopped safely.",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT frozen FROM documents WHERE document_id=?",
                (sprint_id,),
            ).fetchone()[0],
            1,
        )

        restored = self.con.execute(
            "SELECT * FROM sprint_cancellations WHERE sprint_doc_id=?",
            (sprint_id,),
        ).fetchone()
        self.con.execute(
            "DROP TRIGGER trg_sprint_cancellation_delete"
        )
        self.con.execute(
            "DELETE FROM sprint_cancellations WHERE sprint_doc_id=?",
            (sprint_id,),
        )
        columns = tuple(restored.keys())
        self.con.execute(
            f"INSERT INTO sprint_cancellations ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(restored),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_cancellations WHERE sprint_doc_id=?",
                (sprint_id,),
            ).fetchone()[0],
            "completed",
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
