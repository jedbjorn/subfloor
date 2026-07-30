#!/usr/bin/env python3
"""Conductor Step 4 schema, API, CLI-facing route, and drain contracts."""
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
STEP4 = MIGRATIONS / "0119_conductor_contracts.sql"

sys.path.insert(0, str(ENGINE / "api"))
import conductor_routes  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402
sys.path.insert(0, str(ENGINE / "scripts"))
import conductor_contracts as conductor_cli  # noqa: E402


def build_file_db(path: Path, *, include_step4: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if not include_step4 and migration.name >= STEP4.name:
            continue
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def response(result):
    status, _headers, body = result
    return status, json.loads(body)


class ConductorClientApiBaseTests(unittest.TestCase):
    def test_uses_the_installed_instance_port_when_launch_did_not_inject_one(self):
        with mock.patch.object(conductor_cli, "SC_API_BASE", ""), \
                mock.patch.object(
                    conductor_cli.ports_mod, "resolve",
                    return_value={"port": 8842},
                ):
            self.assertEqual(
                conductor_cli._api_base(), "http://127.0.0.1:8842")

    def test_launch_injected_api_base_wins(self):
        with mock.patch.object(
                conductor_cli, "SC_API_BASE", "http://127.0.0.1:8899/"):
            self.assertEqual(
                conductor_cli._api_base(), "http://127.0.0.1:8899")


class ConductorContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sc_conductor_")
        self.db_path = Path(self.tmp.name) / "contracts.db"
        self.con = build_file_db(self.db_path)
        self.dev_id = self.con.execute(
            "INSERT INTO shells "
            "(display_name, shortname, flavor, system_prompt, api_key) "
            "VALUES ('Dev','dev','dev','x','dev-token')"
        ).lastrowid
        self.rev_id = self.con.execute(
            "INSERT INTO shells "
            "(display_name, shortname, flavor, system_prompt, api_key) "
            "VALUES ('Rev','rev','reviewer','x','rev-token')"
        ).lastrowid
        self.con.commit()
        self.db_patch = mock.patch.object(
            conductor_routes, "DB_PATH", self.db_path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.con.close()
        self.tmp.cleanup()

    @staticmethod
    def headers(token="dev-token"):
        return f"Host: 127.0.0.1:8800\r\nAuthorization: Bearer {token}\r\n"

    def post(self, body, token="dev-token"):
        return response(conductor_routes.handle(
            "POST", "/api/directives", self.headers(token),
            json.dumps(body).encode()))

    def test_valid_directive_round_trip_and_read_routes(self):
        status, item = self.post({
            "kind": "ready-for-review",
            "target": "conductor",
            "payload": {"head": "abc"},
        })
        self.assertEqual(status, 201)
        self.assertEqual(item["issuer_shell_id"], self.dev_id)
        self.assertEqual(item["issuer_flavor"], "dev")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["payload"], {"head": "abc"})

        status, listed = response(conductor_routes.handle(
            "GET", "/api/directives?status=pending",
            "Host: localhost:8800\r\n", b""))
        self.assertEqual(status, 200)
        self.assertEqual([d["directive_id"] for d in listed["directives"]],
                         [item["directive_id"]])
        status, inspected = response(conductor_routes.handle(
            "GET", f"/api/directives/{item['directive_id']}",
            "Host: localhost:8800\r\n", b""))
        self.assertEqual(status, 200)
        self.assertEqual(inspected, item)

    def test_cross_flavor_kind_and_claimed_identity_are_refused(self):
        status, obj = self.post({
            "kind": "review-clean", "target": "conductor",
        })
        self.assertEqual((status, obj["error"]["code"]),
                         (422, "directive_kind_not_allowed"))

        status, obj = self.post({
            "kind": "ready-for-review", "target": "conductor",
            "issuer_flavor": "reviewer",
        })
        self.assertEqual((status, obj["error"]["code"]),
                         (422, "issuer_claim_mismatch"))

    def test_database_trigger_rejects_issuer_flavor_spoof(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO directives "
                "(issuer_shell_id, issuer_flavor, kind, target) "
                "VALUES (?, 'reviewer', 'review-clean', 'conductor')",
                (self.dev_id,),
            )

    def test_unit_must_belong_to_linked_sprint(self):
        status, obj = self.post({
            "kind": "ready-for-review", "target": "conductor",
            "sprint_doc_id": 99, "unit_id": 1,
        })
        self.assertEqual((status, obj["error"]["code"]),
                         (422, "unit_sprint_mismatch"))

    def test_float_ids_and_unknown_creation_fields_are_refused(self):
        status, obj = self.post({
            "kind": "ready-for-review", "target": "conductor",
            "sprint_doc_id": 1.5,
        })
        self.assertEqual((status, obj["error"]["code"]), (422, "validation"))
        status, obj = self.post({
            "kind": "ready-for-review", "target": "conductor",
            "status": "executed",
        })
        self.assertEqual((status, obj["error"]["code"]), (422, "validation"))

    def test_valid_creation_never_launches_a_conductor_process(self):
        self.assertFalse(
            hasattr(conductor_routes.conductor_runtime, "maybe_wake")
        )
        status, _item = self.post({
            "kind": "ready-for-review",
            "target": "conductor",
            "payload": {"head": "abc"},
        })
        self.assertEqual(status, 201)

    def test_planner_handoff_is_retired_in_favor_of_planner_arm(self):
        planner_id = self.con.execute(
            "INSERT INTO shells "
            "(display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES ('Planner','pln','planner','x','planner-token')"
        ).lastrowid
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body) "
            "VALUES (100,'doc','SPRINT: activation gate','x')"
        )
        self.con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,planner_shell_id,state,legacy) "
            "VALUES (100,?,'declared',1)",
            (planner_id,),
        )
        self.con.commit()

        status, item = self.post(
            {
                "kind": "handoff",
                "target": "conductor",
                "sprint_doc_id": 100,
                "payload": {},
            },
            token="planner-token",
        )

        self.assertEqual(
            (status, item["error"]["code"]),
            (409, "handoff_retired"),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM directives WHERE kind='handoff'"
            ).fetchone()[0],
            0,
        )

    def test_act_requires_conductor_token_and_calls_mechanical_runtime(self):
        conductor_id = self.con.execute(
            "INSERT INTO shells "
            "(display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES ('Conductor','con1','conductor','x','con-token')"
        ).lastrowid
        self.con.commit()
        status, item = self.post({
            "kind": "ready-for-review",
            "target": "conductor",
            "payload": {"head": "abc"},
        })
        self.assertEqual(status, 201)
        directive_id = item["directive_id"]

        status, obj = response(conductor_routes.handle(
            "POST", f"/api/directives/{directive_id}/act",
            self.headers("dev-token"), b"{}"))
        self.assertEqual((status, obj["error"]["code"]),
                         (403, "conductor_required"))

        expected = {
            "directive_id": directive_id,
            "status": "executed",
            "assignments": [{
                "conversation_id": "cv_worker",
                "role": "developer",
                "slot": "dev",
                "unit_id": 10,
            }],
            "conversation_ids": ["cv_worker"],
        }
        with (
            mock.patch.object(
                conductor_routes.conductor_runtime,
                "act",
                return_value=expected,
            ) as act,
            mock.patch.object(
                conductor_routes.conversation_events,
                "notify",
            ) as event_notify,
            mock.patch.object(
                conductor_routes.conversation_broker,
                "notify_commit",
            ) as broker_notify,
        ):
            status, obj = response(conductor_routes.handle(
                "POST", f"/api/directives/{directive_id}/act",
                self.headers("con-token"), b"{}"))
        self.assertEqual((status, obj), (200, expected))
        act.assert_called_once()
        self.assertEqual(act.call_args.args[2], conductor_id)
        event_notify.assert_called_once_with("cv_worker")
        broker_notify.assert_called_once_with()

    def test_sentinel_events_are_readable_and_append_only(self):
        event_id = conductor_routes.append_sentinel_event(
            self.con, event_kind="activity-beat",
            evidence={"last_commit": "abc"}, shell_id=self.dev_id)
        self.con.commit()
        status, listed = response(conductor_routes.handle(
            "GET", "/api/sentinel-events?event_kind=activity-beat",
            "Host: 127.0.0.1:8800\r\n", b""))
        self.assertEqual(status, 200)
        self.assertEqual(listed["events"][0]["evidence"],
                         {"last_commit": "abc"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sentinel_events SET event_kind='changed' "
                "WHERE event_id=?", (event_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "DELETE FROM sentinel_events WHERE event_id=?", (event_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO sentinel_events "
                "(event_kind, sprint_doc_id, unit_id) "
                "VALUES ('activity-beat',99,1)")

    def test_expectations_cover_every_unit_state(self):
        rows = self.con.execute(
            "SELECT unit_state, enabled, max_dwell_seconds "
            "FROM unit_expectations").fetchall()
        self.assertEqual({r["unit_state"] for r in rows}, {
            "pending", "working", "in_review", "blocked", "merged",
            "cancelled",
        })
        terminal = {r["unit_state"]: r for r in rows}
        self.assertEqual(terminal["merged"]["enabled"], 0)
        self.assertIsNone(terminal["cancelled"]["max_dwell_seconds"])

    def test_instance_contract_trail_is_rebuild_persistent(self):
        for table in (
                "directives", "sentinel_events", "wake_machine_retirements"):
            self.assertIn(table, snapshot_mod.PER_INSTANCE_TABLES)
        self.assertNotIn("unit_expectations", snapshot_mod.PER_INSTANCE_TABLES)
        self.assertNotIn("directive_kinds", snapshot_mod.PER_INSTANCE_TABLES)


class WakeDrainMigrationTests(unittest.TestCase):
    def test_live_legacy_rows_are_closed_with_audit(self):
        with tempfile.TemporaryDirectory(prefix="sc_wake_drain_") as td:
            path = Path(td) / "legacy.db"
            con = build_file_db(path, include_step4=False)
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute(
                "INSERT INTO sprint_planner_bindings "
                "(binding_id,sprint_doc_id,planner_shell_id,session_id,"
                " shell_id,generation) VALUES (7,70,8,9,8,1)")
            con.execute(
                "INSERT INTO sprint_planner_bindings "
                "(binding_id,sprint_doc_id,planner_shell_id,session_id,"
                " shell_id,generation,released_at) "
                "VALUES (8,71,9,10,9,1,'2026-01-01 00:00:00')")
            con.execute(
                "INSERT INTO planner_wake_batches "
                "(batch_id,binding_id,shell_id,generation,state) "
                "VALUES (10,7,8,1,'submitting')")
            con.execute(
                "INSERT INTO planner_wake_items "
                "(item_id,binding_id,message_id,batch_id,state) "
                "VALUES (11,7,12,10,'running')")
            con.execute(
                "INSERT INTO planner_action_receipts "
                "(receipt_id,operation,target,idem_key,state) "
                "VALUES (13,'relay','planner','k','intent')")
            con.execute(
                "INSERT INTO planner_alerts "
                "(alert_id,severity,reason,dedupe_key) "
                "VALUES (14,'warning','legacy','legacy')")
            con.commit()

            con.executescript(STEP4.read_text())

            binding = con.execute(
                "SELECT released_at, release_reason "
                "FROM sprint_planner_bindings WHERE binding_id=7").fetchone()
            self.assertIsNotNone(binding["released_at"])
            self.assertEqual(binding["release_reason"],
                             "conductor-step4-retired")
            historical = con.execute(
                "SELECT released_at, release_reason "
                "FROM sprint_planner_bindings WHERE binding_id=8").fetchone()
            self.assertEqual(historical["released_at"],
                             "2026-01-01 00:00:00")
            self.assertIsNone(historical["release_reason"])
            self.assertEqual(con.execute(
                "SELECT state FROM planner_wake_items WHERE item_id=11"
            ).fetchone()[0], "cancelled")
            self.assertEqual(con.execute(
                "SELECT state FROM planner_wake_batches WHERE batch_id=10"
            ).fetchone()[0], "complete")
            self.assertEqual(con.execute(
                "SELECT state FROM planner_action_receipts WHERE receipt_id=13"
            ).fetchone()[0], "reconciled")
            self.assertIsNotNone(con.execute(
                "SELECT resolved_at FROM planner_alerts WHERE alert_id=14"
            ).fetchone()[0])
            audit = con.execute(
                "SELECT wake_batch_count, wake_item_count "
                "FROM wake_machine_retirements WHERE binding_id=7").fetchone()
            self.assertEqual(tuple(audit), (1, 1))
            con.close()


if __name__ == "__main__":
    unittest.main()
