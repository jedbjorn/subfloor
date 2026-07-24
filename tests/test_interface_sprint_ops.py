#!/usr/bin/env python3
"""Interface operator + sprint workflow surfaces — hermetic proofs (spec #20,
sprint 25 seq 10, task #86).

Covers, without tmux or a live harness:

- WAKE STATUS (GET /api/interface/sprint-bindings): the read-only
  projection — binding armed/released, sprint doc ACTIVE/frozen, derived
  wake_state, current batch, item counts, last outcome, park +
  quarantine detail — and its actor scoping (a shell sees only itself).
- ALERTS (GET /api/interface/sprint-alerts): open-by-default, resolved
  audit behind include_resolved, shell scoping.
- RETRY (POST .../sprint-bindings/{id}/retry): the operator recovery path —
  a parked input needs the explicit outcome verdict; a parked
  (delivery_unknown) batch is NEVER resubmitted (it closes as audit, its
  items requeue for a NEW batch) and is resolved even when a NEWER live
  batch has formed since the park (nothing strands 'batched'; the park
  stays visible in status while it lasts); a pre-send-stalled queue
  re-signals; only the alerts the retry actually remedied resolve
  (dedupe-while-open re-arms them); the coordinator is signalled to
  re-gate. Idempotent replay.
- CLOSE INTEGRATION: closing (status: CLOSED) or freezing the sprint doc
  through the mem API releases its bindings and cancels queued wake work
  ATOMICALLY with the doc edit — no orphan armed binding, no stranded
  queued batch, no half-landed close; messages stay unread.

Run:
    python3 tests/test_interface_sprint_ops.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import interface_broker  # noqa: E402
import interface_routes as routes  # noqa: E402
import interface_wake  # noqa: E402
import server  # noqa: E402


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    for sid in (1, 2):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
            "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (?,?,?,'test','sp',1,0,1,1)", (sid, f"S{sid}", f"s{sid}"))
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body) "
        "VALUES (1,'doc','SPRINT: test','# SPRINT: test\nstatus: ACTIVE')")
    con.commit()
    con.close()


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


OP = "Authorization: Bearer optok"
SHELL1 = "Authorization: Bearer shelltok1"
SHELL2 = "Authorization: Bearer shelltok2"


class FakeCoordinator:
    def __init__(self):
        self.bindings = []

    def notify_binding(self, binding_id):
        self.bindings.append(binding_id)

    def notify_message(self, message_id):
        pass

    def notify_session(self, session_id):
        pass


class SprintOpsRoutesTest(unittest.TestCase):
    """Route-level proofs against a hermetic engine DB (no tmux)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "shell_db.db"
        build_engine_db(self.db_path)
        run_dir = self.tmp / "run" / "interface"
        run_dir.mkdir(parents=True)
        self.patches = [
            mock.patch.object(routes, "DB_PATH", self.db_path),
            mock.patch.object(routes, "RUN_DIR", run_dir),
            mock.patch.object(routes, "OPERATOR_TOKEN_PATH",
                              run_dir / "operator.token"),
        ]
        for p in self.patches:
            p.start()
        (run_dir / "operator.token").write_text("optok")
        self.coordinator = FakeCoordinator()
        interface_wake.bind(self.coordinator)
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE shells SET api_key='shelltok1' WHERE shell_id=1")
        con.execute("UPDATE shells SET api_key='shelltok2' WHERE shell_id=2")
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        self.sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle, harness, cli_version) VALUES (1,1,'occupied','idle',"
            "'kimi','kimi-code 0.27.0')").lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'clean')", (self.sid,))
        con.commit()
        con.close()

    def tearDown(self):
        interface_wake.bind(None)
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def call(self, method, path, header_lines=(), body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        status, headers, resp = routes.handle(method, path,
                                              hdrs(*header_lines), payload)
        return status, json.loads(resp or b"{}")

    def arm(self, headers=(OP,), key="k-arm", planner=1):
        return self.call("POST", "/api/interface/sprint-bindings",
                         (*headers, f"Idempotency-Key: {key}"),
                         {"sprint_doc_id": 1, "planner_shell_id": planner})

    def q(self, sql, params=()):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()

    def queue_message(self):
        """One eligible sprint message → one queued wake item."""
        con = sqlite3.connect(self.db_path)
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'x','task',1)").lastrowid
        interface_wake.maybe_create_wake_item(con, mid)
        con.commit()
        con.close()
        return mid

    def park_batch(self, binding_id, message_id):
        """A parked delivery_unknown batch holding the message's item, plus
        the input park + the alerts the wake path would have raised."""
        con = sqlite3.connect(self.db_path)
        batch = con.execute(
            "INSERT INTO planner_wake_batches (binding_id, shell_id,"
            " generation, state) VALUES (?,1,1,'delivery_unknown')",
            (binding_id,)).lastrowid
        con.execute(
            "UPDATE planner_wake_items SET batch_id=?, state='batched' "
            "WHERE binding_id=? AND message_id=?",
            (batch, binding_id, message_id))
        interface_broker.park_delivery_unknown(con, self.sid)
        interface_broker._alert(
            con, severity="critical", reason="wake_batch_delivery_unknown",
            binding_id=binding_id)
        con.commit()
        con.close()
        return batch

    # -- wake status -------------------------------------------------------------

    def test_status_armed_projection(self):
        status, body = self.arm()
        self.assertEqual(status, 201, body)
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (OP,))
        self.assertEqual(status, 200)
        b = body["bindings"][0]
        self.assertEqual(b["wake_state"], "armed")
        self.assertEqual(b["sprint"]["active"], True)
        self.assertEqual(b["sprint"]["frozen"], False)
        self.assertIsNone(b["released_at"])
        self.assertIsNone(b["park"])
        self.assertEqual(b["retry"], {"applicable": False,
                                      "needs_outcome": False})

    def test_status_queued_counts_and_last_outcome(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid = self.queue_message()
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (OP,))
        b = body["bindings"][0]
        self.assertEqual(b["wake_state"], "queued")
        self.assertEqual(b["items"], {"queued": 1})
        # A completed batch surfaces as the last outcome with item tallies.
        import interface_state
        con = sqlite3.connect(self.db_path)
        batch = interface_broker.form_batch(con, binding_id)
        con.execute(
            "UPDATE planner_wake_batches SET state='complete', "
            "completed_at=datetime('now') WHERE batch_id=?", (batch,))
        item_id = self.q("SELECT item_id FROM planner_wake_items "
                         "WHERE message_id=?", (mid,))[0]
        for state in ("submitting", "running", "done"):
            interface_state.transition(con, "wake_item", item_id, state)
        con.commit()
        con.close()
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (OP,))
        b = body["bindings"][0]
        self.assertEqual(b["last_batch"]["state"], "complete")
        self.assertEqual(b["last_batch"]["items"], {"done": 1})

    def test_status_park_and_quarantine_projection(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid = self.queue_message()
        self.park_batch(binding_id, mid)
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'y','result',1)")
        qmid = con.execute(
            "SELECT message_id FROM shell_messages WHERE body='y'").fetchone()[0]
        con.execute(
            "INSERT INTO planner_wake_items (binding_id, message_id, state,"
            " completed_wakes, error) VALUES (?,?, 'quarantined', 3,"
            " 'survived 3 wake turns')", (binding_id, qmid))
        con.commit()
        con.close()
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (OP,))
        b = body["bindings"][0]
        self.assertEqual(b["wake_state"], "parked")
        self.assertEqual(b["park"]["reason"], "wake_batch_delivery_unknown")
        self.assertTrue(b["park"]["input_park"])
        self.assertEqual(b["retry"], {"applicable": True,
                                      "needs_outcome": True})
        self.assertEqual(len(b["quarantined"]), 1)
        self.assertEqual(b["quarantined"][0]["completed_wakes"], 3)

    def test_status_shell_actor_sees_only_itself(self):
        self.arm()
        # The planner sees its binding…
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (SHELL1,))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["bindings"]), 1)
        # …another shell sees nothing — even asking for planner 1 explicitly.
        status, body = self.call(
            "GET", "/api/interface/sprint-bindings?planner_shell_id=1",
            (SHELL2,))
        self.assertEqual(status, 200)
        self.assertEqual(body["bindings"], [])

    # -- alerts --------------------------------------------------------------------

    def test_alerts_open_default_resolved_behind_flag(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        con = sqlite3.connect(self.db_path)
        interface_broker._alert(con, severity="critical",
                                reason="wake_batch_delivery_unknown",
                                binding_id=binding_id)
        con.commit()
        con.close()
        status, body = self.call("GET", "/api/interface/sprint-alerts", (OP,))
        self.assertEqual(len(body["alerts"]), 1)
        self.assertEqual(body["alerts"][0]["reason"],
                         "wake_batch_delivery_unknown")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE planner_alerts SET resolved_at=datetime('now')")
        con.commit()
        con.close()
        status, body = self.call("GET", "/api/interface/sprint-alerts", (OP,))
        self.assertEqual(body["alerts"], [])
        status, body = self.call(
            "GET", "/api/interface/sprint-alerts?include_resolved=1", (OP,))
        self.assertEqual(len(body["alerts"]), 1)
        self.assertIsNotNone(body["alerts"][0]["resolved_at"])

    def test_alerts_shell_actor_scoped_to_own(self):
        _, body = self.arm()
        con = sqlite3.connect(self.db_path)
        interface_broker._alert(con, severity="critical",
                                reason="wake_batch_delivery_unknown",
                                binding_id=body["binding_id"])
        con.commit()
        con.close()
        status, body = self.call("GET", "/api/interface/sprint-alerts",
                                 (SHELL1,))
        self.assertEqual(len(body["alerts"]), 1)
        status, body = self.call("GET", "/api/interface/sprint-alerts",
                                 (SHELL2,))
        self.assertEqual(body["alerts"], [])

    def test_unscoped_watch_alert_is_visible_only_to_its_owner(self):
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM interface_input_state")
        con.execute("DELETE FROM interface_sessions")
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM interface_sessions "
                "WHERE shell_id=1 AND occupancy <> 'ended'").fetchone()[0],
            0)
        watch_id = con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id) "
            "VALUES ('o/r', 7, 1)").lastrowid
        con.execute(
            "INSERT INTO planner_alerts "
            "(watch_id, severity, reason, dedupe_key) VALUES "
            "(?, 'critical', 'pr_watch_unscoped', ?)",
            (watch_id, f"-|-|{watch_id}|-|pr_watch_unscoped"))
        con.commit()
        con.close()

        status, body = self.call(
            "GET", "/api/interface/sprint-alerts", (SHELL1,))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["alerts"]), 1)
        self.assertEqual(body["alerts"][0]["reason"], "pr_watch_unscoped")
        self.assertIn("--sprint <doc-id>", body["alerts"][0]["next_action"])

        status, body = self.call(
            "GET", "/api/interface/sprint-alerts", (SHELL2,))
        self.assertEqual(status, 200)
        self.assertEqual(body["alerts"], [])

        status, body = self.call(
            "GET", "/api/interface/sprint-alerts?planner_shell_id=1", (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(
            [a["reason"] for a in body["alerts"]], ["pr_watch_unscoped"])

    def test_alert_acknowledgement_dismisses_current_but_keeps_audit(self):
        con = sqlite3.connect(self.db_path)
        interface_broker._alert(
            con, severity="warning", reason="turn_failure",
            session_id=self.sid)
        alert_id = con.execute(
            "SELECT alert_id FROM planner_alerts WHERE session_id=?",
            (self.sid,)).fetchone()[0]
        con.commit()
        con.close()
        status, body = self.call(
            "POST",
            f"/api/interface/sprint-alerts/{alert_id}/acknowledge",
            (OP, "Idempotency-Key: ack-1"), {})
        self.assertEqual(status, 200)
        self.assertEqual(body["alert_id"], alert_id)
        self.assertIsNotNone(body["acknowledged_at"])
        status, body = self.call(
            "GET", f"/api/interface/sprint-alerts?session_id={self.sid}",
            (OP,))
        self.assertEqual(body["alerts"], [])
        status, body = self.call(
            "GET", f"/api/interface/sprint-alerts?session_id={self.sid}"
                   "&include_resolved=1", (OP,))
        self.assertEqual(len(body["alerts"]), 1)
        self.assertIsNotNone(body["alerts"][0]["acknowledged_at"])
        self.assertIsNone(body["alerts"][0]["resolved_at"])
        self.assertIn("meaning", body["alerts"][0])
        self.assertIn("next_action", body["alerts"][0])

    def test_extra_segment_alert_acknowledgement_leaves_alert_open(self):
        con = sqlite3.connect(self.db_path)
        interface_broker._alert(
            con, severity="warning", reason="turn_failure",
            session_id=self.sid)
        alert_id = con.execute(
            "SELECT alert_id FROM planner_alerts WHERE session_id=?",
            (self.sid,)).fetchone()[0]
        con.commit()
        con.close()

        status, body = self.call(
            "POST",
            f"/api/interface/sprint-alerts/999/{alert_id}/acknowledge",
            (OP, "Idempotency-Key: malformed-ack"), {})

        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "no_such_route")
        self.assertEqual(
            self.q(
                "SELECT acknowledged_at, acknowledged_by, resolved_at "
                "FROM planner_alerts WHERE alert_id=?", (alert_id,)),
            (None, None, None))

    def test_planner_alert_query_defaults_to_current_generation(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO interface_generations "
            "(shell_id, generation, ended_at) VALUES (1,2,datetime('now'))")
        old_sid = con.execute(
            "INSERT INTO interface_sessions "
            "(shell_id, generation, occupancy, lifecycle, ended_at) "
            "VALUES (1,2,'ended','ended',datetime('now'))").lastrowid
        interface_broker._alert(
            con, severity="warning", reason="turn_failure",
            session_id=old_sid)
        interface_broker._alert(
            con, severity="warning", reason="turn_failure",
            session_id=self.sid)
        con.commit()
        con.close()
        status, body = self.call(
            "GET", "/api/interface/sprint-alerts?planner_shell_id=1", (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(
            [a["session_id"] for a in body["alerts"]], [self.sid])

    # -- retry ---------------------------------------------------------------------

    def test_retry_parked_batch_never_resubmits_park(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid = self.queue_message()
        batch = self.park_batch(binding_id, mid)
        # A parked input refuses a verdict-less retry.
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-0"), {})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "outcome_required")
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-1"), {"outcome": "not_delivered"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["retried"])
        self.assertEqual(body["wake_state"], "queued")
        # The parking invariant: the parked batch closed as AUDIT — it was
        # not resubmitted, and no new submission happened here.
        row = self.q("SELECT state, completed_at FROM planner_wake_batches "
                     "WHERE batch_id=?", (batch,))
        self.assertEqual(row[0], "complete")
        self.assertIsNotNone(row[1])
        self.assertIsNone(self.q(
            "SELECT batch_id FROM planner_wake_batches "
            "WHERE batch_id<>?", (batch,)), "no new batch without a drain")
        # Its item requeued, unbatched, waiting for a NEW batch.
        item = self.q("SELECT state, batch_id FROM planner_wake_items "
                      "WHERE message_id=?", (mid,))
        self.assertEqual(item, ("queued", None))
        # The input park cleared per the operator's verdict.
        self.assertEqual(self.q("SELECT delivery FROM interface_input_state "
                                "WHERE session_id=?", (self.sid,))[0],
                         "normal")
        # The alerts the retry addressed resolved (re-armed if it recurs).
        self.assertIsNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL"))
        # The coordinator was signalled — the drain re-gates from live state.
        self.assertIn(binding_id, self.coordinator.bindings)

    def test_extra_segment_binding_retry_leaves_parked_work_unchanged(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid = self.queue_message()
        batch = self.park_batch(binding_id, mid)
        notifications_before = list(self.coordinator.bindings)

        status, body = self.call(
            "POST",
            f"/api/interface/sprint-bindings/999/{binding_id}/retry",
            (OP, "Idempotency-Key: malformed-retry"),
            {"outcome": "not_delivered"})

        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "no_such_route")
        self.assertEqual(
            self.q(
                "SELECT state, completed_at FROM planner_wake_batches "
                "WHERE batch_id=?", (batch,)),
            ("delivery_unknown", None))
        self.assertEqual(
            self.q(
                "SELECT state, batch_id FROM planner_wake_items "
                "WHERE message_id=?", (mid,)),
            ("batched", batch))
        self.assertEqual(
            self.q(
                "SELECT delivery FROM interface_input_state "
                "WHERE session_id=?", (self.sid,))[0],
            "delivery_unknown")
        self.assertIsNotNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE binding_id=? "
            "AND resolved_at IS NULL", (binding_id,)))
        self.assertEqual(self.coordinator.bindings, notifications_before)

    def test_extra_segment_binding_release_leaves_binding_armed(self):
        _, body = self.arm()
        binding_id = body["binding_id"]

        status, body = self.call(
            "DELETE",
            f"/api/interface/sprint-bindings/999/{binding_id}",
            (OP, "Idempotency-Key: malformed-binding-release"),
            {"reason": "must not release"})

        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "no_such_route")
        self.assertEqual(
            self.q(
                "SELECT released_at, release_reason "
                "FROM sprint_planner_bindings WHERE binding_id=?",
                (binding_id,)),
            (None, None))

    def test_retry_presend_stalled_resignals(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        self.queue_message()
        con = sqlite3.connect(self.db_path)
        interface_broker.form_batch(con, binding_id)   # stays queued
        interface_broker._alert(
            con, severity="critical",
            reason="wake_presend_retries_exhausted", binding_id=binding_id)
        con.commit()
        con.close()
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-2"), {})
        self.assertEqual(status, 200, body)
        self.assertIn("re-signalled", body["actions"][0])
        self.assertIsNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL"))
        self.assertIn(binding_id, self.coordinator.bindings)

    def test_retry_resolves_parked_batch_behind_newer_live_batch(self):
        """SC-015: parked batch1 + newer live batch2 — the COMMON case, the
        sprint keeps producing messages between the park and the operator's
        retry (a delivery_unknown batch is not 'live' per idx_pwb_live, so
        the drain forms a new one). Retry must resolve the PARKED batch and
        requeue its items — never strand them 'batched' behind the newer
        batch — and clear only the alerts it actually remedied."""
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid1 = self.queue_message()
        batch1 = self.park_batch(binding_id, mid1)
        mid2 = self.queue_message()
        con = sqlite3.connect(self.db_path)
        batch2 = interface_broker.form_batch(con, binding_id)  # stays queued
        interface_broker._alert(con, severity="warning",
                                reason="wake_item_quarantined",
                                binding_id=binding_id)
        con.commit()
        con.close()
        self.assertNotEqual(batch1, batch2)
        # The park is never INVISIBLE: status reads parked (not the newer
        # batch's state) and names the parked batch, its alert open — even
        # with a newer live batch present.
        status, body = self.call("GET", "/api/interface/sprint-bindings",
                                 (OP,))
        b = body["bindings"][0]
        self.assertEqual(b["wake_state"], "parked")
        self.assertEqual(b["park"]["batch_id"], batch1)
        self.assertEqual(b["current_batch"]["batch_id"], batch2)
        self.assertTrue(b["retry"]["applicable"])
        self.assertIsNotNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL "
            "AND reason='wake_batch_delivery_unknown'"))
        # Retry (the input park needs the operator's verdict).
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-2batch"),
            {"outcome": "not_delivered"})
        self.assertEqual(status, 200, body)
        self.assertIn(f"parked batch {batch1} resolved",
                      "; ".join(body["actions"]))
        # The PARKED batch resolved as audit; its item requeued, unbatched —
        # a stranded 'batched' item is impossible after retry.
        self.assertEqual(self.q("SELECT state FROM planner_wake_batches "
                                "WHERE batch_id=?", (batch1,))[0], "complete")
        self.assertEqual(self.q("SELECT state, batch_id FROM "
                                "planner_wake_items WHERE message_id=?",
                                (mid1,)), ("queued", None))
        self.assertIsNone(self.q(
            "SELECT 1 FROM planner_wake_items WHERE batch_id=? "
            "AND state IN ('batched','submitting','running')", (batch1,)))
        # The newer live batch is untouched — its item stays batched to it.
        self.assertEqual(self.q("SELECT state FROM planner_wake_batches "
                                "WHERE batch_id=?", (batch2,))[0], "queued")
        self.assertEqual(self.q("SELECT state, batch_id FROM "
                                "planner_wake_items WHERE message_id=?",
                                (mid2,)), ("batched", batch2))
        # The remedied alerts (batch park, input park) resolved; the
        # UNRELATED open alert was not swept up by the retry.
        self.assertIsNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL "
            "AND reason IN ('wake_batch_delivery_unknown',"
            "'crash_window_delivery_unknown')"))
        self.assertIsNotNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL "
            "AND reason='wake_item_quarantined'"))
        self.assertIn(binding_id, self.coordinator.bindings)

    def test_retry_nothing_to_retry(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-3"), {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "nothing_to_retry")

    def test_retry_authority(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        # Another shell may not retry the planner's binding.
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (SHELL2, "Idempotency-Key: k-retry-4"), {})
        self.assertEqual(status, 403)
        # A released binding is arm-fresh territory, not retry.
        self.call("DELETE", f"/api/interface/sprint-bindings/{binding_id}",
                  (OP, "Idempotency-Key: k-rel"), {"reason": "done"})
        status, body = self.call(
            "POST", f"/api/interface/sprint-bindings/{binding_id}/retry",
            (OP, "Idempotency-Key: k-retry-5"), {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "binding_released")

    def test_retry_idempotent_replay(self):
        _, body = self.arm()
        binding_id = body["binding_id"]
        mid = self.queue_message()
        batch = self.park_batch(binding_id, mid)
        for _ in range(2):
            status, body = self.call(
                "POST",
                f"/api/interface/sprint-bindings/{binding_id}/retry",
                (OP, "Idempotency-Key: k-retry-6"),
                {"outcome": "delivered"})
            self.assertEqual(status, 200, body)
        # One resolution only: the parked batch completed exactly once and
        # the replay returned the stored response (no second side effect).
        self.assertEqual(self.q("SELECT state FROM planner_wake_batches "
                                "WHERE batch_id=?", (batch,))[0], "complete")
        self.assertEqual(self.q("SELECT COUNT(*) FROM planner_wake_items "
                                "WHERE state='queued'")[0], 1)

    def test_extra_segment_receipt_patch_leaves_intent_unchanged(self):
        status, body = self.call(
            "POST", "/api/planner-action-receipts",
            (SHELL1, "Idempotency-Key: malformed-receipt-begin"),
            {"operation": "merge", "target": "#42"})
        self.assertEqual(status, 201, body)
        receipt_id = body["receipt_id"]

        status, body = self.call(
            "PATCH",
            f"/api/planner-action-receipts/999/{receipt_id}",
            (SHELL1, "Idempotency-Key: malformed-receipt-patch"),
            {"state": "complete"})

        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "no_such_route")
        self.assertEqual(
            self.q(
                "SELECT state, completed_at, reconciled_at "
                "FROM planner_action_receipts WHERE receipt_id=?",
                (receipt_id,)),
            ("intent", None, None))


class SprintCloseTest(unittest.TestCase):
    """Close integration through the real mem API (server.Handler)."""

    TOKEN = "test-token-close"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        build_engine_db(cls.db)
        con = sqlite3.connect(cls.db)
        con.execute("UPDATE shells SET api_key=? WHERE shell_id=1",
                    (cls.TOKEN,))
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        cls.sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle, harness, cli_version) VALUES (1,1,'occupied','idle',"
            "'kimi','kimi-code 0.27.0')").lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'clean')", (cls.sid,))
        con.commit()
        con.close()
        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls._real_serialize = staticmethod(server.serialize_doc_write)
        server.serialize_doc_write = lambda: {"ok": True,
                                              "output": "(test stub)"}

    @classmethod
    def tearDownClass(cls):
        server.serialize_doc_write = cls._real_serialize
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def patch(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="PATCH",
            headers={"Authorization": f"Bearer {self.TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def q(self, sql, params=()):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()

    def arm_sprint(self, doc_id, status_line="ACTIVE"):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO documents (document_id, kind, title, body) "
            "VALUES (?,'doc','SPRINT: close',?)",
            (doc_id, f"# SPRINT: close\nstatus: {status_line}"))
        binding = con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (?,1,?,1,1)", (doc_id, self.sid)).lastrowid
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'x','task',?)",
            (doc_id,)).lastrowid
        interface_wake.maybe_create_wake_item(con, mid)
        interface_broker._alert(con, severity="critical",
                                reason="wake_presend_retries_exhausted",
                                binding_id=binding)
        con.commit()
        con.close()
        return binding, mid

    def test_status_closed_releases_binding_and_cancels_queue(self):
        binding, mid = self.arm_sprint(101)
        status, body = self.patch(
            "/_sc/mem/docs/101",
            {"body": "# SPRINT: close\nstatus: CLOSED"})
        self.assertEqual(status, 200)
        self.assertEqual(body["released_bindings"], 1)
        row = self.q("SELECT released_at, release_reason FROM "
                     "sprint_planner_bindings WHERE binding_id=?", (binding,))
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "sprint closed")
        item = self.q("SELECT state, error FROM planner_wake_items "
                      "WHERE message_id=?", (mid,))
        self.assertEqual(item[0], "cancelled")
        self.assertIn("sprint closed", item[1])
        # Messages stay unread (spec Sprint Scope).
        self.assertIsNone(self.q("SELECT read_at FROM shell_messages "
                                 "WHERE message_id=?", (mid,))[0])
        # The released binding's open alerts resolve — no longer actionable.
        self.assertIsNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE binding_id=? "
            "AND resolved_at IS NULL", (binding,)))

    def test_closed_close_is_atomic_under_fault(self):
        """SC-016: a fault between the column patch and the wake close
        leaves NO partial state — the status: CLOSED edit and the binding
        release + queue cancel land in ONE transaction, edge-identical to
        the freeze path."""
        binding, mid = self.arm_sprint(104)
        with mock.patch.object(server, "_close_sprint_wake",
                               side_effect=RuntimeError("injected fault")):
            try:
                self.patch("/_sc/mem/docs/104",
                           {"body": "# SPRINT: close\nstatus: CLOSED"})
                self.fail("the injected fault should surface as a 500")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 500)
        # NEITHER side landed: doc still ACTIVE, binding still armed, wake
        # item still queued, alert still open.
        self.assertIn("status: ACTIVE", self.q(
            "SELECT body FROM documents WHERE document_id=104")[0])
        self.assertIsNone(self.q(
            "SELECT released_at FROM sprint_planner_bindings "
            "WHERE binding_id=?", (binding,))[0])
        self.assertEqual(self.q("SELECT state FROM planner_wake_items "
                                "WHERE message_id=?", (mid,))[0], "queued")
        self.assertIsNotNone(self.q(
            "SELECT 1 FROM planner_alerts WHERE binding_id=? "
            "AND resolved_at IS NULL", (binding,)))
        # Stand down: release the still-armed binding so the other close
        # tests can arm the same planner (one live binding per planner).
        con = sqlite3.connect(self.db)
        con.execute(
            "UPDATE sprint_planner_bindings SET released_at=datetime('now'),"
            " release_reason='test teardown' WHERE binding_id=?", (binding,))
        con.commit()
        con.close()

    def test_freeze_releases_binding_and_cancels_queue(self):
        binding, mid = self.arm_sprint(102)
        status, body = self.patch("/_sc/mem/docs/102/freeze", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["released_bindings"], 1)
        self.assertIsNotNone(self.q(
            "SELECT released_at FROM sprint_planner_bindings "
            "WHERE binding_id=?", (binding,))[0])
        self.assertEqual(self.q("SELECT state FROM planner_wake_items "
                                "WHERE message_id=?", (mid,))[0], "cancelled")

    def test_close_without_bindings_is_a_noop(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO documents (document_id, kind, title, body) "
            "VALUES (103,'doc','SPRINT: plain','# S\nstatus: ACTIVE')")
        con.commit()
        con.close()
        status, body = self.patch("/_sc/mem/docs/103/freeze", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["released_bindings"], 0)


if __name__ == "__main__":
    unittest.main()
