#!/usr/bin/env python3
"""Transactional brokered planner wake — hermetic proofs (spec #20, sprint
25 seq 8, task #84).

Covers, without tmux or a live harness:

- Event ingress: maybe_create_wake_item eligibility (Sprint Scope) — typed
  sprint events only, ACTIVE unfrozen sprint, live binding/generation,
  mandatory hooks; atomic unique (binding, message) dedupe.
- Flag #49 (decisions #28/#31): the quiet baseline keys off REAL provider
  readiness (provider session_start stamp), never the pre-exec
  occupied_at — a >3s boot can no longer submit into an unpainted TUI.
- Gate hardening: mandatory-hook capability, unmanaged-writable probe
  (decision #15 disarm), PreSendError (definite pre-send failure → queued,
  never parked) vs ambiguous failure (parked, never auto-retried).
- Stop-hook reconciliation: ambiguity parking (action receipts),
  quarantine after three completed wakes, read-during-turn completion.
- The coordinator: event-driven drain, quiet-deadline reschedule,
  bounded 1s/5s/30s pre-send retries, and the proof that NO wake path
  bypasses delivery_unknown parking (decision #22).
- Flag #50: the emitter's flock is held through the POST — commit order
  can never invert allocation order.

Run:
    python3 tests/test_interface_wake.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_broker  # noqa: E402
import interface_hook  # noqa: E402
import interface_wake  # noqa: E402

QUIET = 0.2  # tight debounce for fast hermetic drains


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


def _age(con, table, col, row_id, seconds, pk):
    """Backdate a timestamp column so quiet-debounce arithmetic is exact."""
    con.execute(
        f"UPDATE {table} SET {col}=datetime('now', ?) WHERE {pk}=?",
        (f"-{seconds} seconds", row_id))


class WakeFixture(unittest.TestCase):
    """An armed sprint: occupied+idle+clean planner session (kimi, full
    hooks), an ACTIVE sprint doc, an unreleased binding."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        self.sid = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle, harness, cli_version) VALUES (1,1,'occupied','idle',"
            "'kimi','kimi-code 0.27.0')").lastrowid
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'clean')", (self.sid,))
        self.binding = self.con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,?,1,1)", (self.sid,)).lastrowid
        # Age the session so the quiet debounce is already satisfied unless
        # a test freshens a baseline.
        _age(self.con, "interface_sessions", "occupied_at", self.sid, 60,
             "session_id")
        _age(self.con, "interface_sessions", "created_at", self.sid, 60,
             "session_id")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def add_message(self, kind="task", sprint_doc_id=1, to_shell_id=1,
                    read=False):
        cur = self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,?,?,?,?)",
            (to_shell_id, f"wake me ({kind})", kind, sprint_doc_id))
        if read:
            self.con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id=?", (cur.lastrowid,))
        self.con.commit()
        return cur.lastrowid

    def batch_state(self, batch_id):
        return self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch_id,)).fetchone()[0]

    def item_states(self):
        return self.con.execute(
            "SELECT item_id, message_id, state, completed_wakes, batch_id "
            "FROM planner_wake_items ORDER BY item_id").fetchall()

    def form(self):
        bid = interface_broker.form_batch(self.con, self.binding)
        self.con.commit()
        return bid

    def submit(self, batch_id, writer, quiet_s=QUIET, probe=None):
        return interface_broker.submit_wake_batch(
            self.con, batch_id, writer,
            self.con.execute("SELECT datetime('now')").fetchone()[0],
            quiet_s=quiet_s, unmanaged_writable=probe)


# ── Event ingress (spec Sprint Scope) ────────────────────────────────────────

class WakeIngressTest(WakeFixture):

    def test_eligible_task_message_creates_item(self):
        mid = self.add_message("task")
        item = interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        self.assertIsNotNone(item)
        rows = self.item_states()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "queued")
        self.assertEqual(rows[0][1], mid)

    def test_result_and_pr_event_are_eligible(self):
        for kind in ("result", "pr_event"):
            mid = self.add_message(kind)
            self.assertIsNotNone(
                interface_wake.maybe_create_wake_item(self.con, mid))

    def test_shell_kind_never_wakes(self):
        mid = self.add_message("shell")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))
        self.assertEqual(self.item_states(), [])

    def test_unscoped_message_never_wakes(self):
        mid = self.add_message("task", sprint_doc_id=None)
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_wrong_recipient_never_wakes(self):
        mid = self.add_message("task", to_shell_id=2)  # not the planner
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_closed_sprint_never_wakes(self):
        self.con.execute(
            "UPDATE documents SET body='# SPRINT: test\nstatus: CLOSED' "
            "WHERE document_id=1")
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_frozen_sprint_never_wakes(self):
        self.con.execute("UPDATE documents SET frozen=1 WHERE document_id=1")
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_released_binding_never_wakes(self):
        self.con.execute(
            "UPDATE sprint_planner_bindings SET released_at=datetime('now') "
            "WHERE binding_id=?", (self.binding,))
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_replaced_generation_never_wakes(self):
        self.con.execute(
            "UPDATE interface_sessions SET generation=2 WHERE session_id=?",
            (self.sid,))
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_mandatory_hook_gap_never_wakes(self):
        self.con.execute(
            "UPDATE interface_sessions SET harness='codex', "
            "cli_version='codex-cli 0.100.0' WHERE session_id=?", (self.sid,))
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_duplicate_send_creates_one_item(self):
        mid = self.add_message("task")
        first = interface_wake.maybe_create_wake_item(self.con, mid)
        second = interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        self.assertIsNotNone(first)
        self.assertIsNone(second, "unique (binding, message) dedupes")
        self.assertEqual(len(self.item_states()), 1)


# ── Flag #49: quiet baseline keys off REAL provider readiness ─────────────────

class WakeReadinessTest(WakeFixture):

    def test_provider_session_start_stamps_readiness(self):
        interface_broker.record_hook(self.con, 1, 1, 2, "session_start",
                                     source="provider")
        self.con.commit()
        ready = self.con.execute(
            "SELECT provider_ready_at FROM interface_sessions "
            "WHERE session_id=?", (self.sid,)).fetchone()[0]
        self.assertIsNotNone(ready)

    def test_entrypoint_session_start_is_NOT_readiness(self):
        interface_broker.record_hook(self.con, 1, 1, 1, "session_start",
                                     source="entrypoint")
        self.con.commit()
        ready = self.con.execute(
            "SELECT provider_ready_at FROM interface_sessions "
            "WHERE session_id=?", (self.sid,)).fetchone()[0]
        self.assertIsNone(ready, "the pre-exec identity claim is never "
                                 "readiness — that was flag #49's defect")

    def test_slow_boot_blocks_submit_despite_old_occupied_at(self):
        """The #49 defect: occupied_at aged >3s during a slow claude/codex
        boot let a wake submit into an unpainted TUI. With provider
        readiness 1s old, the gate must still owe the debounce."""
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        batch_id = self.form()
        # Provider proved readiness only 1s ago; occupied_at is 60s old.
        self.con.execute(
            "UPDATE interface_sessions SET provider_ready_at="
            "datetime('now', '-1 seconds') WHERE session_id=?", (self.sid,))
        self.con.commit()
        out = self.submit(batch_id, lambda n: None, quiet_s=3.0)
        self.assertFalse(out["submitted"])
        self.assertIn("quiet", out["reason"])
        self.assertAlmostEqual(out["retry_after"], 2.0, delta=0.5)
        self.assertEqual(self.batch_state(batch_id), "queued")

    def test_readiness_older_than_debounce_passes(self):
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        batch_id = self.form()
        self.con.execute(
            "UPDATE interface_sessions SET provider_ready_at="
            "datetime('now', '-30 seconds') WHERE session_id=?", (self.sid,))
        self.con.commit()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertTrue(out["submitted"], out)
        self.assertEqual(writes, [len(interface_broker.WAKE_PROMPT) + 1])

    def test_human_input_after_readiness_resets_the_baseline(self):
        """max() semantics: the most recent activity owns the debounce."""
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        batch_id = self.form()
        self.con.execute(
            "UPDATE interface_sessions SET provider_ready_at="
            "datetime('now', '-30 seconds') WHERE session_id=?", (self.sid,))
        self.con.execute(
            "UPDATE interface_input_state SET last_human_input_at="
            "datetime('now', '-1 seconds') WHERE session_id=?", (self.sid,))
        self.con.commit()
        out = self.submit(batch_id, lambda n: None, quiet_s=3.0)
        self.assertFalse(out["submitted"])
        self.assertIn("quiet", out["reason"])


# ── Gate hardening: hooks capability, unmanaged probe, PreSendError ───────────

class WakeGateHardeningTest(WakeFixture):

    def _armed_batch(self):
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        return self.form()

    def test_mandatory_hook_gap_blocks_submit(self):
        self.con.execute(
            "UPDATE interface_sessions SET harness='codex', "
            "cli_version='codex-cli 0.100.0' WHERE session_id=?", (self.sid,))
        self.con.commit()
        batch_id = self._armed_batch()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertFalse(out["submitted"])
        self.assertIn("mandatory", out["reason"])
        self.assertEqual(writes, [])
        self.assertEqual(self.batch_state(batch_id), "queued")

    def test_unmanaged_writable_client_disarms_and_alerts(self):
        batch_id = self._armed_batch()
        writes = []
        out = self.submit(batch_id, writes.append,
                          probe=lambda: True)
        self.assertFalse(out["submitted"])
        self.assertTrue(out["disarmed"])
        self.assertEqual(writes, [], "no byte may move")
        row = self.con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=?",
            (self.sid,)).fetchone()
        self.assertEqual(row[0], "unknown",
                         "decision #15: detection sets composer unknown")
        alert = self.con.execute(
            "SELECT severity FROM planner_alerts "
            "WHERE reason='unmanaged_writable_client' AND resolved_at IS NULL"
        ).fetchone()
        self.assertIsNotNone(alert)
        self.assertEqual(self.batch_state(batch_id), "queued")

    def test_pre_send_failure_requeues_without_parking(self):
        batch_id = self._armed_batch()

        def presend(n):
            raise interface_broker.PreSendError("preflight proved no byte")

        with self.assertRaises(interface_broker.PreSendError):
            self.submit(batch_id, presend)
        self.assertEqual(self.batch_state(batch_id), "queued",
                         "a DEFINITE pre-send failure never parks")
        items = self.item_states()
        self.assertEqual(items[0][2], "queued")
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM planner_alerts WHERE reason="
            "'wake_batch_delivery_unknown'").fetchone())
        # The retry: same batch, a healthy writer, submits normally.
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertTrue(out["submitted"], out)
        self.assertEqual(writes, [len(interface_broker.WAKE_PROMPT) + 1])
        self.assertEqual(self.batch_state(batch_id), "submitting")

    def test_ambiguous_failure_parks_and_never_auto_retries(self):
        batch_id = self._armed_batch()

        def crash(n):
            raise RuntimeError("tmux died mid-write")

        with self.assertRaises(RuntimeError):
            self.submit(batch_id, crash)
        self.assertEqual(self.batch_state(batch_id), "delivery_unknown")
        # No broker/coordinator path resubmits it: a second submit attempt
        # refuses because the batch is not queued.
        with self.assertRaises(interface_broker.BrokerError):
            self.submit(batch_id, lambda n: None)


# ── Stop-hook reconciliation: ambiguity, quarantine, read-during-turn ─────────

class BatchReconcileTest(WakeFixture):

    def setUp(self):
        super().setUp()
        self._hseq = 1  # durable hook sequences are monotonic per generation

    def hook(self, event):
        self._hseq += 1
        result = interface_broker.record_hook(
            self.con, 1, 1, self._hseq, event)
        self.con.commit()
        return result

    def _running_batch(self, mids):
        for m in mids:
            interface_wake.maybe_create_wake_item(self.con, m)
        self.con.commit()
        batch_id = self.form()
        out = self.submit(batch_id, lambda n: None)
        assert out["submitted"], out
        self.hook("prompt_submit")
        return batch_id

    def test_unread_with_ambiguous_action_parks_reconcile(self):
        mid = self.add_message("task")
        batch_id = self._running_batch([mid])
        self.con.execute(
            "INSERT INTO planner_action_receipts (message_id, operation,"
            " target, idem_key) VALUES (?,'edit','file.py','k1')", (mid,))
        self.con.commit()
        self.hook("turn_stop")
        self.assertEqual(self.batch_state(batch_id), "complete")
        item = self.item_states()[0]
        self.assertEqual(item[2], "reconcile",
                         "unread + durable ambiguous action must park, "
                         "never requeue blind")
        self.assertIsNotNone(self.con.execute(
            "SELECT 1 FROM planner_alerts WHERE reason='wake_item_reconcile'"
        ).fetchone())

    def test_receipt_unknown_state_also_parks(self):
        mid = self.add_message("task")
        self._running_batch([mid])
        self.con.execute(
            "INSERT INTO planner_action_receipts (message_id, operation,"
            " target, idem_key, state) VALUES (?,'merge','#1','k2','unknown')",
            (mid,))
        self.con.commit()
        self.hook("turn_stop")
        self.assertEqual(self.item_states()[0][2], "reconcile")

    def test_three_completed_wakes_quarantine(self):
        mid = self.add_message("task")
        for _ in range(3):
            self._running_batch([mid])
            self.hook("turn_stop")
        item = self.item_states()[0]
        self.assertEqual(item[2], "quarantined")
        self.assertEqual(item[3], 3)
        self.assertIsNotNone(self.con.execute(
            "SELECT 1 FROM planner_alerts WHERE reason='wake_item_quarantined'"
        ).fetchone())

    def test_quarantine_does_not_block_newer_work(self):
        poison = self.add_message("task")
        for _ in range(3):
            self._running_batch([poison])
            self.hook("turn_stop")
        fresh = self.add_message("task")
        self._running_batch([fresh])
        self.con.execute(
            "UPDATE shell_messages SET read_at=datetime('now') "
            "WHERE message_id=?", (fresh,))
        self.hook("turn_stop")
        states = {r[1]: r[2] for r in self.item_states()}
        self.assertEqual(states[poison], "quarantined")
        self.assertEqual(states[fresh], "done")

    def test_message_read_during_turn_completes_without_its_own_batch(self):
        mid_a = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid_a)
        self.con.commit()
        batch_id = self.form()
        out = self.submit(batch_id, lambda n: None)
        assert out["submitted"], out
        self.hook("prompt_submit")
        # A second message arrives DURING the turn and is read in it.
        mid_b = self.add_message("result")
        interface_wake.maybe_create_wake_item(self.con, mid_b)
        self.con.execute(
            "UPDATE shell_messages SET read_at=datetime('now') "
            "WHERE message_id=?", (mid_b,))
        self.con.commit()
        self.hook("turn_stop")
        states = {r[1]: r[2] for r in self.item_states()}
        self.assertEqual(states[mid_b], "done",
                         "handled in the turn → completed, never woken again")

    def test_read_message_marks_item_done(self):
        mid = self.add_message("task")
        self._running_batch([mid])
        self.con.execute(
            "UPDATE shell_messages SET read_at=datetime('now') "
            "WHERE message_id=?", (mid,))
        self.con.commit()
        self.hook("turn_stop")
        self.assertEqual(self.item_states()[0][2], "done")


# ── The coordinator: event-driven drain, retries, parking non-bypass ─────────

class WakeCoordinatorTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dbpath = self.tmp / "shell_db.db"
        build_engine_db(self.dbpath)
        con = sqlite3.connect(self.dbpath)
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
        self.binding = con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,?,1,1)", (self.sid,)).lastrowid
        _age(con, "interface_sessions", "occupied_at", self.sid, 60,
             "session_id")
        _age(con, "interface_sessions", "created_at", self.sid, 60,
             "session_id")
        con.commit()
        con.close()
        self.writes = []
        self.attempts = 0
        self.writer_error = None
        def writer(n):
            self.attempts += 1
            if self.writer_error is not None:
                raise self.writer_error
            self.writes.append(n)

        self.probe_result = False
        self.coord = interface_wake.WakeCoordinator(
            str(self.dbpath),
            writer_factory=lambda session_id: writer,
            unmanaged_probe=lambda session_id: self.probe_result,
            quiet_s=QUIET)

    def tearDown(self):
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    # -- helpers (sync DB access from the test's async context) ---------------

    def connect(self):
        return sqlite3.connect(self.dbpath)

    def add_message(self, kind="task", read=False):
        con = self.connect()
        cur = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'evt',?,1)", (kind,))
        mid = cur.lastrowid
        if read:
            con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id=?", (mid,))
        interface_wake.maybe_create_wake_item(con, mid)
        con.commit()
        con.close()
        return mid

    def one(self, sql, params=()):
        con = self.connect()
        row = con.execute(sql, params).fetchone()
        con.close()
        return row[0] if row else None

    async def wait_for(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(0.01)
        return False

    # -- tests ------------------------------------------------------------------

    async def test_event_drains_to_submission(self):
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task")
        self.coord.notify_binding(self.binding)
        ok = await self.wait_for(lambda: len(self.writes) == 1)
        self.assertTrue(ok, "the eligible event must drive one submission")
        self.assertEqual(self.writes,
                         [len(interface_broker.WAKE_PROMPT) + 1])
        state = self.one("SELECT state FROM planner_wake_batches")
        self.assertEqual(state, "submitting")

    async def test_busy_lifecycle_awaits_events_no_poll(self):
        con = self.connect()
        con.execute("UPDATE interface_sessions SET lifecycle='busy' "
                    "WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task")
        self.coord.notify_binding(self.binding)
        await asyncio.sleep(QUIET * 2)
        self.assertEqual(self.writes, [], "busy queues — no byte, no retry")
        # The turn ends: the hook-driven signal submits.
        con = self.connect()
        con.execute("UPDATE interface_sessions SET lifecycle='idle' "
                    "WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        self.coord.notify_binding(self.binding)
        ok = await self.wait_for(lambda: len(self.writes) == 1)
        self.assertTrue(ok)

    async def test_quiet_debounce_reschedules_at_the_deadline(self):
        con = self.connect()
        con.execute("UPDATE interface_input_state SET last_human_input_at="
                    "datetime('now') WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task")
        self.coord.notify_binding(self.binding)
        await asyncio.sleep(QUIET / 2)
        self.assertEqual(self.writes, [], "inside the debounce: no byte")
        ok = await self.wait_for(lambda: len(self.writes) == 1,
                                 timeout=QUIET * 5)
        self.assertTrue(ok, "the deadline timer must re-attempt exactly once")

    async def test_pre_send_retries_are_bounded(self):
        self.writer_error = interface_broker.PreSendError("preflight down")
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task")
        with mock.patch.object(interface_wake, "RETRY_DELAYS_S",
                               (0.02, 0.02, 0.02)):
            self.coord.notify_binding(self.binding)
            ok = await self.wait_for(
                lambda: self.one(
                    "SELECT COUNT(*) FROM planner_alerts WHERE reason="
                    "'wake_presend_retries_exhausted'") == 1)
        self.assertTrue(ok, "retries must stop after the third delay + alert")
        self.assertEqual(self.one(
            "SELECT state FROM planner_wake_batches"), "queued")
        # initial attempt + exactly 3 bounded retries (1s/5s/30s) — then stop
        self.assertEqual(self.attempts, 4)
        self.assertEqual(self.coord._pre_send_attempts, {})

    async def test_delivery_unknown_is_never_resubmitted(self):
        """Decision #22 non-bypass proof: a parked batch stays parked through
        coordinator drains and the startup pass; only operator resolution
        requeues the WORK (as a NEW batch), never the parked submission."""
        self.coord.start(asyncio.get_running_loop())
        # Park a batch live: an ambiguous writer failure mid-submit.
        self.writer_error = RuntimeError("tmux died mid-write")
        self.add_message("task")
        self.coord.notify_binding(self.binding)
        ok = await self.wait_for(
            lambda: self.one("SELECT state FROM planner_wake_batches")
            == "delivery_unknown")
        self.assertTrue(ok)
        writes_at_park = len(self.writes)
        self.writer_error = None
        # Drains + the startup pass must NOT touch the parked batch.
        self.coord.notify_binding(self.binding)
        self.coord.startup_pass()
        await asyncio.sleep(QUIET * 2)
        self.assertEqual(len(self.writes), writes_at_park,
                         "no wake path may replay a parked submission")
        self.assertEqual(self.one(
            "SELECT state FROM planner_wake_batches"), "delivery_unknown")
        # The sanctioned path: operator resolves → items requeue → the NEXT
        # drain forms a NEW batch and submits it once.
        con = self.connect()
        batch_id = con.execute("SELECT batch_id FROM planner_wake_batches"
                               ).fetchone()[0]
        interface_broker.resolve_batch(con, batch_id)
        con.commit()
        con.close()
        self.coord.notify_binding(self.binding)
        ok = await self.wait_for(lambda: len(self.writes) > writes_at_park)
        self.assertTrue(ok, "operator-resolved work requeues as a NEW batch")
        states = self.one("SELECT COUNT(*) FROM planner_wake_batches")
        self.assertEqual(states, 2)

    async def test_startup_pass_drains_queued_work(self):
        self.add_message("task")
        self.coord.start(asyncio.get_running_loop())
        self.coord.startup_pass()
        ok = await self.wait_for(lambda: len(self.writes) == 1)
        self.assertTrue(ok)


# ── Flag #50: hook commit ordering (flock held through the POST) ──────────────

class HookCommitOrderingTest(unittest.TestCase):

    def test_commit_order_never_inverts_allocation_order(self):
        tmp = Path(tempfile.mkdtemp())
        posts = []
        barrier = threading.Barrier(2)

        def fake_post(api_base, token, body, **kw):
            # The inversion the old code allowed: the FIRST allocated seq
            # sleeps inside its POST, so without the lock the later seq
            # would commit first and the earlier hook would be rejected as
            # stale — stranding a wake batch 'submitting' (restart-only
            # recovery, decision #31).
            if body["hook_seq"] == 2:
                time.sleep(0.1)
            posts.append(body["hook_seq"])
            return True

        def emit(event):
            barrier.wait()
            interface_hook.emit_locked(
                tmp, 1, 1, {"shell_id": 1, "generation": 1, "event": event,
                            "source": "provider"}, "http://x", "tok")

        with mock.patch.object(interface_hook, "post_callback", fake_post):
            threads = [threading.Thread(target=emit, args=(e,))
                       for e in ("prompt_submit", "turn_stop")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts, sorted(posts),
                         "allocation order must BE commit order (flag #50)")

    def test_failed_post_leaves_a_gap_never_a_duplicate(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(interface_hook, "post_callback",
                               return_value=False):
            interface_hook.emit_locked(tmp, 1, 1, {"event": "turn_stop"},
                                       "http://x", "tok")
        with mock.patch.object(interface_hook, "post_callback",
                               return_value=True) as p:
            interface_hook.emit_locked(tmp, 1, 1, {"event": "session_end"},
                                       "http://x", "tok")
        self.assertEqual(p.call_args[0][2]["hook_seq"], 3,
                         "a lost hook is a gap (safe), never a re-issued seq")


if __name__ == "__main__":
    unittest.main()


# ── Routes: sprint bindings, action receipts, #51 rejection audit ────────────

import hashlib  # noqa: E402
import json  # noqa: E402

sys.path.insert(0, str(ENGINE / "api"))
import interface_routes as routes  # noqa: E402

OP = "Authorization: Bearer optok"
SHELL1 = "Authorization: Bearer shelltok1"


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


class WakeRoutesTest(unittest.TestCase):

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
        for p in self.patches:
            p.stop()
        for f in self.tmp.glob("*"):
            if f.is_file():
                f.unlink()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, header_lines=(), body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        status, headers, resp = routes.handle(method, path,
                                              hdrs(*header_lines), payload)
        return status, json.loads(resp or b"{}")

    def arm(self, doc=1, planner=1, headers=(OP,), key="k-arm"):
        return self.call("POST", "/api/interface/sprint-bindings",
                         (*headers, f"Idempotency-Key: {key}"),
                         {"sprint_doc_id": doc, "planner_shell_id": planner})

    # -- sprint bindings ---------------------------------------------------------

    def test_arm_happy_path_and_wake_state_surface(self):
        status, body = self.arm()
        self.assertEqual(status, 201, body)
        self.assertEqual(body["wake_state"], "armed")
        status, detail = self.call("GET",
                                   f"/api/interface/sessions/{self.sid}",
                                   (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(detail["wake_state"], "armed")
        # A queued wake item surfaces as 'queued'.
        con = sqlite3.connect(self.db_path)
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'x','task',1)").lastrowid
        import interface_wake
        interface_wake.maybe_create_wake_item(con, mid)
        con.commit()
        con.close()
        status, detail = self.call("GET",
                                   f"/api/interface/sessions/{self.sid}",
                                   (OP,))
        self.assertEqual(detail["wake_state"], "queued")

    def test_double_arm_refused(self):
        status, _ = self.arm()
        self.assertEqual(status, 201)
        status, body = self.arm(key="k-arm-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "already_armed")

    def test_arm_requires_active_unfrozen_sprint(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE documents SET body='# S\nstatus: CLOSED' "
                    "WHERE document_id=1")
        con.commit()
        con.close()
        status, body = self.arm()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "sprint_not_active")

    def test_arm_requires_mandatory_hooks(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE interface_sessions SET harness='codex', "
                    "cli_version='codex-cli 0.100.0' WHERE session_id=?",
                    (self.sid,))
        con.commit()
        con.close()
        status, body = self.arm()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "hooks_unsupported")

    def test_shell_actor_arms_only_itself(self):
        status, body = self.arm(headers=(SHELL1,))
        self.assertEqual(status, 201, body)
        # shell 1 arming planner 2 → refused before any state check
        status, body = self.arm(headers=(SHELL1,), planner=2, key="k-arm-3")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "not_the_planner")

    def test_shell_actor_cannot_reach_session_routes(self):
        status, body = self.call("GET", "/api/interface/shells", (SHELL1,))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "shell_scope")

    def test_release_cancels_queued_work_messages_stay_unread(self):
        status, body = self.arm()
        self.assertEqual(status, 201)
        binding_id = body["binding_id"]
        con = sqlite3.connect(self.db_path)
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'x','task',1)").lastrowid
        import interface_wake
        interface_wake.maybe_create_wake_item(con, mid)
        con.commit()
        con.close()
        status, body = self.call(
            "DELETE", f"/api/interface/sprint-bindings/{binding_id}",
            (OP, "Idempotency-Key: k-rel"), {"reason": "sprint closed"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cancelled_items"], 1)
        con = sqlite3.connect(self.db_path)
        item = con.execute(
            "SELECT state, error FROM planner_wake_items").fetchone()
        self.assertEqual(item[0], "cancelled")
        self.assertIn("sprint closed", item[1])
        read = con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=?",
            (mid,)).fetchone()[0]
        self.assertIsNone(read, "release must leave messages unread")
        released = con.execute(
            "SELECT released_at, release_reason FROM sprint_planner_bindings"
        ).fetchone()
        self.assertIsNotNone(released[0])
        self.assertEqual(released[1], "sprint closed")
        con.close()

    # -- action receipts -----------------------------------------------------------

    def test_receipt_lifecycle(self):
        con = sqlite3.connect(self.db_path)
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'x','task',1)").lastrowid
        con.commit()
        con.close()
        status, body = self.call(
            "POST", "/api/planner-action-receipts",
            (SHELL1, "Idempotency-Key: k-rc1"),
            {"message_id": mid, "operation": "merge", "target": "#42"})
        self.assertEqual(status, 201, body)
        rid = body["receipt_id"]
        self.assertEqual(body["state"], "intent")
        # Same key → the original receipt, no twin.
        status, body = self.call(
            "POST", "/api/planner-action-receipts",
            (SHELL1, "Idempotency-Key: k-rc1b"),
            {"message_id": mid, "operation": "merge", "target": "#42"})
        self.assertEqual(status, 200)
        self.assertEqual(body["receipt_id"], rid)
        self.assertTrue(body["duplicate"])
        # complete → suppresses a later duplicate begin.
        status, body = self.call(
            "PATCH", f"/api/planner-action-receipts/{rid}",
            (SHELL1, "Idempotency-Key: k-rc2"), {"state": "complete"})
        self.assertEqual(status, 200)
        status, body = self.call(
            "POST", "/api/planner-action-receipts",
            (SHELL1, "Idempotency-Key: k-rc3"),
            {"message_id": mid, "operation": "merge", "target": "#42"})
        self.assertTrue(body["suppressed"])
        # complete → complete is a same-state no-op; unknown from complete is
        # an illegal edge.
        status, body = self.call(
            "PATCH", f"/api/planner-action-receipts/{rid}",
            (SHELL1, "Idempotency-Key: k-rc4"), {"state": "unknown"})
        self.assertEqual(status, 409)

    def test_receipt_unknown_then_reconciled(self):
        status, body = self.call(
            "POST", "/api/planner-action-receipts",
            (SHELL1, "Idempotency-Key: k-rc5"),
            {"operation": "push", "target": "main"})
        rid = body["receipt_id"]
        status, _ = self.call(
            "PATCH", f"/api/planner-action-receipts/{rid}",
            (SHELL1, "Idempotency-Key: k-rc6"), {"state": "unknown"})
        self.assertEqual(status, 200)
        status, body = self.call(
            "PATCH", f"/api/planner-action-receipts/{rid}",
            (SHELL1, "Idempotency-Key: k-rc7"),
            {"state": "reconciled", "result_detail": "operator verified"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "reconciled")

    # -- flag #51: every hook rejection path is audited ------------------------------

    def _hook_gen(self):
        """A generation whose hook token is known, with no live session."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_generations SET hook_token_hash=? "
            "WHERE shell_id=1 AND generation=1",
            (hashlib.sha256(b"hooktok").hexdigest(),))
        con.commit()
        con.close()

    def _post_hook(self, body, token="hooktok"):
        with mock.patch.object(routes, "_log") as log:
            status, resp = self.call(
                "POST", "/api/interface/hook-callbacks",
                (f"Authorization: Bearer {token}",), body)
        return status, resp, log

    def test_audit_missing_fields(self):
        status, _, log = self._post_hook({"event": "turn_stop"})
        self.assertEqual(status, 422)
        self.assertTrue(log.called, "flag #51: missing-fields rejection "
                                    "must be audited")

    def test_audit_unknown_fields(self):
        status, _, log = self._post_hook(
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "turn_stop", "prompt": "stolen"})
        self.assertEqual(status, 422)
        self.assertTrue(log.called)

    def test_audit_unknown_source(self):
        self._hook_gen()
        status, _, log = self._post_hook(
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "turn_stop", "source": "moon"})
        self.assertEqual(status, 422)
        self.assertTrue(log.called)

    def test_audit_no_session(self):
        self._hook_gen()
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE interface_sessions SET occupancy='ended' "
                    "WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        status, _, log = self._post_hook(
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "turn_stop", "pid": 4321})
        self.assertEqual(status, 404)
        self.assertTrue(log.called, "flag #51: no-session rejection must "
                                    "be audited")

    def test_audit_session_start_without_pid(self):
        self._hook_gen()
        status, _, log = self._post_hook(
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "provider"})
        self.assertEqual(status, 422)
        self.assertTrue(log.called)

    def test_audit_stale_hook_seq(self):
        self._hook_gen()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_sessions SET pane_pid=4321 WHERE session_id=?",
            (self.sid,))
        con.execute(
            "UPDATE interface_generations SET last_hook_seq=5 "
            "WHERE shell_id=1 AND generation=1")
        con.commit()
        con.close()
        status, _, log = self._post_hook(
            {"shell_id": 1, "generation": 1, "hook_seq": 3,
             "event": "turn_stop", "pid": 4321})
        self.assertEqual(status, 409)
        self.assertTrue(log.called, "flag #51: a replayed/stale hook_seq "
                                    "must be audited — it is the exact "
                                    "diagnostic #50 needs in production")
