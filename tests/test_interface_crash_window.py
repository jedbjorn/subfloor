#!/usr/bin/env python3
"""Crash-window / delivery_unknown parking proofs (decision #22, spec #20).

The seq-3 gate spike DEFERRED this proof to seq 4 with a hard condition: no
wake/retry unit may ship until parking is implemented AND proven. These are
those proofs, hermetic against a real engine DB (schema.sql + all migrations)
with the byte transport injected as a recording fake:

1. Broker crash BEFORE the tmux write with a pending unacknowledged human
   sequence → composer unknown, delivery delivery_unknown, writer revoked,
   alert raised, ZERO bytes ever written, and no auto-replay — the pending
   frame is never re-forwarded by any recovery path.
2. Broker crash AFTER the tmux write (bytes landed, forward-commit lost) →
   indistinguishable from (1): identical park, and the write count stays 1
   across every reconciliation — never replayed.
3. Operator reconciliation is the only way out: 'delivered' folds the
   pending sequence into forwarded (no resend), 'not_delivered' drops the
   reservation so the client can resend under a fresh lease.
4. A submitting/running wake batch at restart → delivery_unknown UNLESS
   durable hook-sequence evidence proves the transition (submit stamp →
   running; stop stamp → complete with items reconciled from message read
   state). A parked batch is never blindly resubmitted; resolve_batch
   requeues its items without sending anything.

Run:
    python3 tests/test_interface_crash_window.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_broker  # noqa: E402
import interface_reconcile  # noqa: E402


class Crash(Exception):
    """Simulated broker process death at an exact point."""


class Recorder:
    """The injected tmux write. `mode` decides where the process dies."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.writes = []

    def __call__(self, payload_len: int) -> None:
        if self.mode == "crash_before_write":
            raise Crash("died before the tmux write")
        self.writes.append(payload_len)
        if self.mode == "crash_after_write":
            raise Crash("died after the tmux write, before forward-commit")


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


class CrashWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)
        # One occupied shell-1 session (generation 1), clean composer,
        # writer lease held by client 'tab-1'.
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
        interface_broker.acquire_writer(self.con, self.sid, "tab-1", "tok-1")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _reconnect(self):
        """Simulate process death + service restart: drop the connection."""
        self.con.close()
        self.con = sqlite3.connect(self.db)

    def _input(self):
        cols = ("composer", "delivery", "pending_seq", "forwarded_seq")
        row = self.con.execute(
            f"SELECT {', '.join(cols)} FROM interface_input_state "
            "WHERE session_id=?", (self.sid,)).fetchone()
        return dict(zip(cols, row))

    def _assert_parked(self, writes):
        ist = self._input()
        self.assertEqual(ist["composer"], "unknown")
        self.assertEqual(ist["delivery"], "delivery_unknown")
        self.assertEqual(ist["pending_seq"], 1, "evidence must survive")
        self.assertEqual(ist["forwarded_seq"], 0)
        lease = self.con.execute(
            "SELECT revoked_at, revoke_reason FROM interface_writer_leases "
            "WHERE session_id=?", (self.sid,)).fetchone()
        self.assertIsNotNone(lease[0], "writer must be revoked")
        alert = self.con.execute(
            "SELECT severity FROM planner_alerts "
            "WHERE reason='crash_window_delivery_unknown' AND resolved_at IS "
            "NULL").fetchone()
        self.assertIsNotNone(alert, "park must alert")

    # ── 1 & 2: crash before vs after the tmux write ─────────────────────
    def _crash_case(self, mode, expect_writes):
        rec = Recorder(mode)
        with self.assertRaises(Crash):
            interface_broker.accept_human_input(
                self.con, self.sid, client_seq=1, payload_len=10, writer=rec)
        self.assertEqual(rec.writes, expect_writes)
        # The pending reservation committed before the crash.
        self.assertEqual(self._input()["pending_seq"], 1)

        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        self._assert_parked(rec.writes)

        # Reconciliation is idempotent and NEVER replays: run it twice more.
        interface_reconcile.startup_reconcile(self.con)
        interface_reconcile.startup_reconcile(self.con)
        self.assertEqual(rec.writes, expect_writes,
                         "recovery must never re-forward the pending frame")
        self.assertEqual(self._input()["pending_seq"], 1)
        return rec

    def test_crash_before_write_parks_without_replay(self):
        self._crash_case("crash_before_write", [])

    def test_crash_after_write_parks_without_replay(self):
        self._crash_case("crash_after_write", [10])

    # ── 3: operator reconciliation paths ────────────────────────────────
    def test_reconcile_delivered_folds_sequence_no_resend(self):
        rec = Recorder("crash_after_write")
        with self.assertRaises(Crash):
            interface_broker.accept_human_input(
                self.con, self.sid, 1, 10, writer=rec)
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        interface_broker.reconcile_input(self.con, self.sid, "delivered")
        self.con.commit()
        ist = self._input()
        self.assertEqual(ist["delivery"], "normal")
        self.assertIsNone(ist["pending_seq"])
        self.assertEqual(ist["forwarded_seq"], 1,
                         "proven-delivered frame folds into forwarded_seq")
        self.assertEqual(rec.writes, [10], "folding must not resend")

    def test_reconcile_not_delivered_allows_client_resend(self):
        rec = Recorder("crash_before_write")
        with self.assertRaises(Crash):
            interface_broker.accept_human_input(
                self.con, self.sid, 1, 10, writer=rec)
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        interface_broker.reconcile_input(self.con, self.sid, "not_delivered")
        self.con.commit()
        self.assertEqual(self._input()["forwarded_seq"], 0)
        # The operator certifies the composer; the client re-acquires the
        # writer and resends sequence 1 — accepted exactly once.
        interface_broker.certify_clean(self.con, self.sid, "op", 0)
        lease = interface_broker.acquire_writer(
            self.con, self.sid, "tab-2", "tok-2")
        self.assertIsNotNone(lease)
        rec2 = Recorder("ok")
        ack = interface_broker.accept_human_input(
            self.con, self.sid, 1, 10, writer=rec2)
        self.assertEqual(ack, {"ack": 1, "duplicate": False})
        self.assertEqual(rec2.writes, [10])
        self.assertEqual(self._input()["forwarded_seq"], 1)

    # ── duplicate / gap discipline (no double-forward, no out-of-order) ──
    def test_duplicate_seq_replays_ack_never_bytes(self):
        rec = Recorder("ok")
        interface_broker.accept_human_input(
            self.con, self.sid, 1, 10, writer=rec)
        dup = interface_broker.accept_human_input(
            self.con, self.sid, 1, 10, writer=rec)
        self.assertEqual(dup, {"ack": 1, "duplicate": True})
        self.assertEqual(rec.writes, [10], "duplicate must not re-forward")

    def test_sequence_gap_rejected_before_any_write(self):
        rec = Recorder("ok")
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.accept_human_input(
                self.con, self.sid, 5, 10, writer=rec)
        self.assertEqual(rec.writes, [])
        self.assertEqual(self._input()["composer"], "clean")

    # ── 4: wake-batch restart recovery from hook-sequence evidence ───────
    def _arm_and_submit(self):
        """A binding, one wake item, a submitted batch. Returns batch_id."""
        self.con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,?,1,1)", (self.sid,))
        bid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'wake','task',1)")
        mid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.con.execute(
            "INSERT INTO planner_wake_items (binding_id, message_id) "
            "VALUES (?,?)", (bid, mid))
        batch = interface_broker.form_batch(self.con, bid)
        self.con.commit()
        rec = Recorder("ok")
        out = interface_broker.submit_wake_batch(
            self.con, batch, writer=rec, now_iso="2030-01-01 00:00:10")
        self.assertTrue(out["submitted"], "gates were clean+idle+quiet")
        self.assertEqual(len(rec.writes), 1)
        self.con.commit()
        return batch, rec

    def test_submitting_batch_without_hook_evidence_parks(self):
        batch, rec = self._arm_and_submit()
        # Broker dies before the submit hook lands — no durable evidence.
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        state = self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch,)).fetchone()[0]
        self.assertEqual(state, "delivery_unknown")
        alert = self.con.execute(
            "SELECT 1 FROM planner_alerts "
            "WHERE reason='wake_batch_delivery_unknown' AND resolved_at IS "
            "NULL").fetchone()
        self.assertIsNotNone(alert)
        # Never blindly resubmitted: the only byte-write was the original.
        self.assertEqual(len(rec.writes), 1)
        # Operator resolution requeues the item WITHOUT sending anything.
        interface_broker.resolve_batch(self.con, batch)
        item = self.con.execute(
            "SELECT state, batch_id FROM planner_wake_items").fetchone()
        self.assertEqual(item, ("queued", None))
        bstate = self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch,)).fetchone()[0]
        self.assertEqual(bstate, "complete")

    def test_submitting_batch_with_submit_hook_proven_running(self):
        batch, _ = self._arm_and_submit()
        # The submit hook landed durably before the crash.
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        self._reconnect()
        counts = interface_reconcile.startup_reconcile(self.con)
        state = self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch,)).fetchone()[0]
        self.assertEqual(state, "running",
                         "durable submit evidence proves the transition")
        self.assertEqual(counts["batches_delivery_unknown"], 0)

    def test_running_batch_with_stop_evidence_completes(self):
        batch, _ = self._arm_and_submit()
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        # Crash between recording the stop hook and finishing the batch:
        # stop evidence exists but the batch is still 'running'.
        self.con.execute(
            "UPDATE interface_generations SET last_hook_seq=2 "
            "WHERE shell_id=1 AND generation=1")
        self.con.execute(
            "UPDATE planner_wake_batches SET stop_hook_seq=2 WHERE batch_id=?",
            (batch,))
        # The planner read the message during the turn (durable read state).
        self.con.execute(
            "UPDATE shell_messages SET read_at=datetime('now')")
        self.con.commit()
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        bstate = self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch,)).fetchone()[0]
        self.assertEqual(bstate, "complete")
        istate = self.con.execute(
            "SELECT state FROM planner_wake_items").fetchone()[0]
        self.assertEqual(istate, "done",
                         "read message → item done on proven stop")

    def test_running_batch_unread_item_requeues_with_wake_count(self):
        batch, _ = self._arm_and_submit()
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        self.con.execute(
            "UPDATE planner_wake_batches SET stop_hook_seq=2 WHERE batch_id=?",
            (batch,))
        self.con.commit()
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        item = self.con.execute(
            "SELECT state, completed_wakes, batch_id FROM planner_wake_items"
        ).fetchone()
        self.assertEqual(item, ("queued", 1, None),
                         "unread without ambiguity → queued, wake count +1")

    def test_stale_hook_sequence_rejected(self):
        self._arm_and_submit()
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.record_hook(self.con, 1, 1, 1, "turn_stop")
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.record_hook(self.con, 1, 1, 0, "turn_stop")

    # ── fresh-lease sequence continuity (flag #34) ──────────────────────
    def test_fresh_lease_continues_session_sequence(self):
        rec = Recorder("ok")
        interface_broker.accept_human_input(self.con, self.sid, 1, 10, writer=rec)
        interface_broker.accept_human_input(self.con, self.sid, 2, 10, writer=rec)
        # Service restart: the lease dies, the session's forwarded_seq is 2.
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        lease = interface_broker.acquire_writer(self.con, self.sid, "tab-2", "tok-2")
        next_seq = self.con.execute(
            "SELECT next_input_seq FROM interface_writer_leases WHERE lease_id=?",
            (lease,)).fetchone()[0]
        self.assertEqual(next_seq, 3,
                         "a fresh lease must continue the session sequence, "
                         "not reseed to 1")
        rec2 = Recorder("ok")
        # The client's legitimate next frame is accepted (no gap-wedge)...
        ack = interface_broker.accept_human_input(
            self.con, self.sid, 3, 10, writer=rec2)
        self.assertEqual(ack, {"ack": 3, "duplicate": False})
        self.assertEqual(rec2.writes, [10])
        # ...and an already-forwarded sequence is a duplicate ack, never a
        # false acceptance of new bytes.
        dup = interface_broker.accept_human_input(
            self.con, self.sid, 2, 10, writer=rec2)
        self.assertEqual(dup, {"ack": 2, "duplicate": True})
        self.assertEqual(rec2.writes, [10])

    def test_not_delivered_resend_with_forwarded_seq_nonzero(self):
        # forwarded_seq=1 when the crash wedges seq 2: the not_delivered
        # resend path must work past the degenerate forwarded_seq=0 case.
        rec = Recorder("ok")
        interface_broker.accept_human_input(self.con, self.sid, 1, 10, writer=rec)
        crash = Recorder("crash_before_write")
        with self.assertRaises(Crash):
            interface_broker.accept_human_input(
                self.con, self.sid, 2, 20, writer=crash)
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        interface_broker.reconcile_input(self.con, self.sid, "not_delivered")
        self.con.commit()
        self.assertEqual(self._input()["forwarded_seq"], 1)
        interface_broker.certify_clean(self.con, self.sid, "op", 1)
        interface_broker.acquire_writer(self.con, self.sid, "tab-2", "tok-2")
        rec2 = Recorder("ok")
        ack = interface_broker.accept_human_input(
            self.con, self.sid, 2, 20, writer=rec2)
        self.assertEqual(ack, {"ack": 2, "duplicate": False})
        self.assertEqual(rec2.writes, [20], "the resent frame forwards once")
        self.assertEqual(self._input()["forwarded_seq"], 2)

    # ── live park on write failure without process death (flag #35) ─────
    def test_write_failure_without_crash_parks_live(self):
        class TmuxError(Exception):
            pass

        def failing_writer(_len):
            raise TmuxError("tmux send-keys failed")

        with self.assertRaises(TmuxError):
            interface_broker.accept_human_input(
                self.con, self.sid, 1, 10, writer=failing_writer)
        # No restart, no reconciliation yet — the park is immediate.
        ist = self._input()
        self.assertEqual(ist["composer"], "unknown")
        self.assertEqual(ist["delivery"], "delivery_unknown")
        self.assertEqual(ist["pending_seq"], 1, "evidence must survive")
        lease = self.con.execute(
            "SELECT revoked_at, revoke_reason FROM interface_writer_leases "
            "WHERE session_id=?", (self.sid,)).fetchone()
        self.assertIsNotNone(lease[0], "writer must be revoked live")
        self.assertEqual(lease[1], "write_failure")
        alert = self.con.execute(
            "SELECT 1 FROM planner_alerts "
            "WHERE reason='crash_window_delivery_unknown' AND resolved_at IS "
            "NULL").fetchone()
        self.assertIsNotNone(alert, "a live write failure must alert")
        # The way out is the same operator reconciliation as the crash window.
        interface_broker.reconcile_input(self.con, self.sid, "not_delivered")
        self.con.commit()
        interface_broker.certify_clean(self.con, self.sid, "op", 0)
        interface_broker.acquire_writer(self.con, self.sid, "tab-2", "tok-2")
        rec = Recorder("ok")
        ack = interface_broker.accept_human_input(
            self.con, self.sid, 1, 10, writer=rec)
        self.assertEqual(ack, {"ack": 1, "duplicate": False})

    # ── fenced submit hook + input lock (flag #33) ──────────────────────
    def test_input_lock_rejects_human_frame_during_submit(self):
        self._arm_and_submit()
        rec = Recorder("ok")
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.accept_human_input(
                self.con, self.sid, 1, 10, writer=rec)
        self.assertEqual(rec.writes, [], "locked frame must never be written")
        ist = self._input()
        self.assertIsNone(ist["pending_seq"])
        self.assertEqual(ist["composer"], "clean")

    def test_submit_hook_fenced_against_later_human_input(self):
        batch, _ = self._arm_and_submit()  # fence = forwarded_seq+1 = 1
        # A human frame slipped in before the lock engaged (the race the
        # fence exists for): seq 1 was accepted after the wake submitted.
        self.con.execute(
            "UPDATE interface_input_state SET forwarded_seq=1, composer='dirty'"
            " WHERE session_id=?", (self.sid,))
        self.con.commit()
        # The Enter that fires this hook is the human's — it must NOT
        # manufacture the batch's durable submit evidence.
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        row = self.con.execute(
            "SELECT state, submit_hook_seq FROM planner_wake_batches "
            "WHERE batch_id=?", (batch,)).fetchone()
        self.assertEqual(row[0], "delivery_unknown",
                         "an unfenced hook parks the batch, never promotes it")
        self.assertIsNone(row[1], "no submit evidence may be stamped")
        self.assertEqual(self._input()["composer"], "dirty",
                         "a later human sequence keeps the composer dirty")
        alert = self.con.execute(
            "SELECT 1 FROM planner_alerts "
            "WHERE reason='wake_batch_delivery_unknown' AND resolved_at IS "
            "NULL").fetchone()
        self.assertIsNotNone(alert)
        # Decision #22 re-proof under the fixed fence: recovery finds no
        # manufactured evidence and keeps the park — never a blind resubmit.
        self._reconnect()
        interface_reconcile.startup_reconcile(self.con)
        state = self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?",
            (batch,)).fetchone()[0]
        self.assertEqual(state, "delivery_unknown")

    def test_unfenced_hook_cannot_clear_unknown_composer(self):
        self._arm_and_submit()
        self.con.execute(
            "UPDATE interface_input_state SET composer='unknown' "
            "WHERE session_id=?", (self.sid,))
        self.con.commit()
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        self.assertEqual(self._input()["composer"], "unknown",
                         "only exact recovery + certification clears unknown")

    def test_session_end_hook_ends_generation(self):
        self._arm_and_submit()
        interface_broker.record_hook(self.con, 1, 1, 1, "prompt_submit")
        interface_broker.record_hook(self.con, 1, 1, 2, "turn_stop")
        # lifecycle walks busy -> idle -> stopping before the end hook.
        self.con.execute(
            "UPDATE interface_sessions SET lifecycle='stopping' "
            "WHERE session_id=?", (self.sid,))
        self.con.commit()
        interface_broker.record_hook(self.con, 1, 1, 3, "session_end")
        ended = self.con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0]
        self.assertIsNotNone(ended,
                             "a proven session end must end the generation — "
                             "else a rebuild resurrects it and bricks New chat")
        # Hooks for the ended generation are rejected from here on.
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.record_hook(self.con, 1, 1, 4, "prompt_submit")


if __name__ == "__main__":
    unittest.main()
