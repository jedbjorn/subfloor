#!/usr/bin/env python3
"""Reviewed-spec QAQC and authoritative sprint declaration contracts."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
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
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES (?,?,?,?,?,?)",
            (
                (1, "Planner One", "PLN1", "planner", "x", "plan-token"),
                (2, "Planner Two", "PLN2", "planner", "x", "plan2-token"),
                (3, "Reviewer", "REV1", "reviewer", "x", "rev-token"),
                (4, "Developer", "DEV1", "dev", "x", "dev-token"),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
