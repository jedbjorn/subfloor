#!/usr/bin/env python3
"""Interface startup reconciliation + rebuild/update refusal tests (spec #20).

Covers the non-crash-window halves of interface_reconcile: expired
reservation repair, writer-lease hygiene on restart (dirty state preserved),
the rebuild/update live-guard (live_refusal_reasons) across every blocking
shape and the fully-drained case, and the rebuild.py/update.py integration
that turns those reasons into a refusal.

Run:
    python3 tests/test_interface_reconcile_guard.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_reconcile  # noqa: E402
import interface_broker  # noqa: E402
import rebuild  # noqa: E402
import update  # noqa: E402


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
        "INSERT INTO documents (document_id, kind, title) "
        "VALUES (1,'doc','SPRINT: test')")
    con.commit()
    con.close()


class StartupReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _session(self, shell_id=1, generation=1, occupancy="occupied",
                 reservation_expires_at=None):
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (?,?)", (shell_id, generation))
        sid = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " reservation_expires_at) VALUES (?,?,?,?)",
            (shell_id, generation, occupancy, reservation_expires_at)
        ).lastrowid
        self.con.commit()
        return sid

    def test_expired_reservation_fails_closed(self):
        sid = self._session(occupancy="reserved",
                            reservation_expires_at="2020-01-01 00:00:00")
        counts = interface_reconcile.startup_reconcile(self.con)
        occ, detail = self.con.execute(
            "SELECT occupancy, error_detail FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(occ, "unreconciled",
                         "ambiguous spawn must fail closed, never auto-end")
        self.assertEqual(detail, "reservation expired at restart")
        self.assertEqual(counts["reservations_unreconciled"], 1)
        alert = self.con.execute(
            "SELECT 1 FROM planner_alerts WHERE reason='reservation_expired'"
        ).fetchone()
        self.assertIsNotNone(alert)

    def test_fresh_reservation_untouched(self):
        sid = self._session(occupancy="reserved",
                            reservation_expires_at="2999-01-01 00:00:00")
        interface_reconcile.startup_reconcile(self.con)
        occ = self.con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0]
        self.assertEqual(occ, "reserved")

    def test_restart_revokes_writers_preserves_dirty(self):
        sid = self._session()
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'dirty')", (sid,))
        self.con.execute(
            "INSERT INTO interface_writer_leases (session_id, shell_id,"
            " generation, client_id, token_hash) VALUES (?,1,1,'c','h')",
            (sid,))
        self.con.commit()
        counts = interface_reconcile.startup_reconcile(self.con)
        self.assertEqual(counts["leases_revoked"], 1)
        reason = self.con.execute(
            "SELECT revoke_reason FROM interface_writer_leases "
            "WHERE session_id=?", (sid,)).fetchone()[0]
        self.assertEqual(reason, "service_restart")
        composer = self.con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=?",
            (sid,)).fetchone()[0]
        self.assertEqual(composer, "dirty",
                         "dirty state survives disconnect per spec")
        # Idempotent: a second run revokes nothing.
        counts = interface_reconcile.startup_reconcile(self.con)
        self.assertEqual(counts["leases_revoked"], 0)

    def test_pre_0078_db_is_a_noop(self):
        bare = self.tmp / "bare.db"
        con = sqlite3.connect(bare)
        con.execute("CREATE TABLE t (x INTEGER)")
        con.commit()
        out = interface_reconcile.startup_reconcile(con)
        self.assertEqual(out, {"skipped": "pre-0078 DB"})
        con.close()
        self.assertEqual(interface_reconcile.live_refusal_reasons(bare), [])


class LiveRefusalGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _occupied_session(self):
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (1,1)")
        sid = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy)"
            " VALUES (1,1,'occupied')").lastrowid
        self.con.commit()
        return sid

    def test_clean_db_has_no_reasons(self):
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])
        self.assertEqual(
            interface_reconcile.live_refusal_reasons(self.tmp / "nope.db"),
            [])

    def test_live_session_blocks(self):
        self._occupied_session()
        reasons = interface_reconcile.live_refusal_reasons(self.db)
        self.assertTrue(any("occupied" in r for r in reasons))
        self.assertTrue(any("generation" in r for r in reasons),
                        "a live generation is itself live state")

    def test_live_generation_blocks_with_all_sessions_ended(self):
        sid = self._occupied_session()
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended', lifecycle="
            "'ended', ended_at=datetime('now') WHERE session_id=?", (sid,))
        self.con.commit()
        reasons = interface_reconcile.live_refusal_reasons(self.db)
        self.assertEqual(len(reasons), 1)
        self.assertIn("generation 1/1 is live", reasons[0])

    def test_armed_binding_blocks(self):
        sid = self._occupied_session()
        self.con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,2,?,2,1)", (sid,))
        self.con.commit()
        reasons = interface_reconcile.live_refusal_reasons(self.db)
        self.assertTrue(any("binding" in r for r in reasons))

    def test_nonterminal_batch_blocks(self):
        sid = self._occupied_session()
        self.con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,2,?,2,1)", (sid,))
        bid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        for state in ("queued", "submitting", "running", "delivery_unknown"):
            self.con.execute("DELETE FROM planner_wake_batches")
            self.con.execute(
                "INSERT INTO planner_wake_batches (binding_id, shell_id,"
                " generation, state) VALUES (?,2,1,?)", (bid, state))
            self.con.commit()
            reasons = interface_reconcile.live_refusal_reasons(self.db)
            self.assertTrue(
                any("batch" in r and state in r for r in reasons),
                f"batch state {state} must block: {reasons}")
        self.con.execute(
            "UPDATE planner_wake_batches SET state='complete'")
        self.con.execute(
            "UPDATE sprint_planner_bindings SET released_at=datetime('now')")
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended', lifecycle='ended', "
            "ended_at=datetime('now')")
        self.con.execute(
            "UPDATE interface_generations SET ended_at=datetime('now')")
        self.con.commit()
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])

    def test_input_ambiguity_blocks(self):
        sid = self._occupied_session()
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'unknown')", (sid,))
        self.con.commit()
        reasons = interface_reconcile.live_refusal_reasons(self.db)
        self.assertTrue(any("ambiguity" in r for r in reasons))

    def test_terminal_session_park_survives_restart_without_reopening_alert(self):
        sid = self._occupied_session()
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended', lifecycle='ended', "
            "ended_at=datetime('now') WHERE session_id=?", (sid,))
        self.con.execute(
            "UPDATE interface_generations SET ended_at=datetime('now')")
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id, generation, "
            "composer, delivery, pending_seq) "
            "VALUES (?,1,1,'unknown','delivery_unknown',9)", (sid,))
        self.con.execute(
            "INSERT INTO planner_alerts "
            "(alert_id, session_id, severity, reason, dedupe_key) "
            "VALUES (42,?,'critical','crash_window_delivery_unknown',"
            "? || '|-|-|crash_window_delivery_unknown')", (sid, sid))
        self.con.commit()
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])

        counts = interface_reconcile.startup_reconcile(self.con)
        self.assertEqual(counts["terminal_inputs_removed"], 0)
        self.assertEqual(counts["terminal_input_parks"], 1)
        self.assertEqual(counts["terminal_alerts_resolved"], 1)
        first_alert = self.con.execute(
            "SELECT alert_id, reason, resolved_at FROM planner_alerts "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(first_alert[:2], (
            42, "crash_window_delivery_unknown"))
        self.assertIsNotNone(first_alert[2])
        second = interface_reconcile.startup_reconcile(self.con)
        self.assertEqual(second["terminal_alerts_resolved"], 0)
        self.assertEqual(self.con.execute(
            "SELECT composer, delivery, pending_seq "
            "FROM interface_input_state WHERE session_id=?",
            (sid,)).fetchone(), ("unknown", "delivery_unknown", 9))
        self.assertEqual(self.con.execute(
            "SELECT alert_id, reason, resolved_at FROM planner_alerts "
            "WHERE session_id=?", (sid,)).fetchall(), [first_alert])
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM planner_alerts WHERE session_id=? "
            "AND resolved_at IS NULL", (sid,)).fetchone())

    def test_shared_close_revokes_writer_and_preserves_ambiguity(self):
        sid = self._occupied_session()
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id, generation, "
            "composer, pending_seq) VALUES (?,1,1,'unknown',4)", (sid,))
        self.con.execute(
            "INSERT INTO interface_writer_leases (session_id, shell_id, "
            "generation, client_id, token_hash) VALUES (?,1,1,'client','hash')",
            (sid,))
        interface_broker.close_session(self.con, sid, "operator_end")
        self.con.commit()

        self.assertEqual(self.con.execute(
            "SELECT composer, delivery, pending_seq "
            "FROM interface_input_state WHERE session_id=?",
            (sid,)).fetchone(), ("unknown", "delivery_unknown", 4))
        revoked_at, reason = self.con.execute(
            "SELECT revoked_at, revoke_reason FROM interface_writer_leases "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertIsNotNone(revoked_at)
        self.assertEqual(reason, "session_end")
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])

        # A repeated close preserves the same evidence without reopening or
        # restamping the terminal session.
        result = interface_broker.close_session(
            self.con, sid, "different_retry_reason")
        self.assertTrue(result["already_ended"])
        self.assertEqual(result["end_reason"], "operator_end")
        self.assertEqual(self.con.execute(
            "SELECT delivery, pending_seq FROM interface_input_state "
            "WHERE session_id=?", (sid,)).fetchone(),
            ("delivery_unknown", 4))

    def test_operator_approved_discard_terminalizes_all_live_state(self):
        sid = self._occupied_session()
        self.con.execute(
            "INSERT INTO shell_memory_archives "
            "(archive_id, shell_id, date, full_narrative) "
            "VALUES (10,1,'2026-07-24','unsaved turn')")
        self.con.execute(
            "UPDATE interface_sessions SET archive_id=10 WHERE session_id=?",
            (sid,))
        self.con.execute(
            "UPDATE shells SET active_archive_id=10 WHERE shell_id=1")
        self.con.execute(
            "INSERT INTO interface_input_state "
            "(session_id, shell_id, generation, composer, pending_seq) "
            "VALUES (?,1,1,'unknown',4)", (sid,))
        self.con.execute(
            "INSERT INTO interface_writer_leases "
            "(session_id, shell_id, generation, client_id, token_hash) "
            "VALUES (?,1,1,'client','hash')", (sid,))
        binding_id = self.con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,?,1,1)", (sid,)).lastrowid
        batch_id = self.con.execute(
            "INSERT INTO planner_wake_batches "
            "(binding_id, shell_id, generation, state) "
            "VALUES (?,1,1,'running')", (binding_id,)).lastrowid
        message_id = self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) "
            "VALUES (2,1,'must remain unread')").lastrowid
        item_id = self.con.execute(
            "INSERT INTO planner_wake_items "
            "(binding_id, message_id, batch_id, state) "
            "VALUES (?,?,?,'running')",
            (binding_id, message_id, batch_id)).lastrowid
        self.con.commit()

        before = interface_reconcile.live_refusal_reasons(self.db)
        self.assertTrue(any("session" in reason for reason in before))
        self.assertTrue(any("batch" in reason for reason in before))
        counts = interface_reconcile.discard_live_state(self.db)

        self.assertEqual(counts["sessions_ended"], 1)
        self.assertEqual(counts["generations_ended"], 0,
                         "close_session owns the matching generation")
        self.assertEqual(counts["archives_closed"], 1)
        self.assertEqual(counts["bindings_released"], 1)
        self.assertEqual(counts["batches_completed"], 1)
        self.assertEqual(counts["items_cancelled"], 1)
        self.assertEqual(
            self.con.execute(
                "SELECT occupancy, lifecycle, end_reason "
                "FROM interface_sessions WHERE session_id=?", (sid,)
            ).fetchone(),
            ("ended", "ended", "update_discard"),
        )
        self.assertIsNotNone(self.con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0])
        self.assertEqual(
            self.con.execute(
                "SELECT ended_at FROM shell_memory_archives "
                "WHERE archive_id=10").fetchone()[0] is not None,
            True,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_archive_id FROM shells WHERE shell_id=1"
        ).fetchone()[0])
        self.assertEqual(
            self.con.execute(
                "SELECT release_reason FROM sprint_planner_bindings "
                "WHERE binding_id=?", (binding_id,)).fetchone()[0],
            "update_discard",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state, completed_at FROM planner_wake_batches "
                "WHERE batch_id=?", (batch_id,)).fetchone()[0],
            "complete",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state, error FROM planner_wake_items WHERE item_id=?",
                (item_id,)).fetchone(),
            ("cancelled", "discarded by operator-approved update"),
        )
        self.assertIsNone(self.con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=?",
            (message_id,)).fetchone()[0])
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])

    def test_fully_drained_passes(self):
        sid = self._occupied_session()
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended', lifecycle="
            "'ended', ended_at=datetime('now') WHERE session_id=?", (sid,))
        self.con.execute(
            "UPDATE interface_generations SET ended_at=datetime('now')")
        self.con.commit()
        self.assertEqual(interface_reconcile.live_refusal_reasons(self.db), [])


class RefusalIntegrationTest(unittest.TestCase):
    """rebuild.main and update.migrate_or_rebuild turn reasons into refusal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy)"
            " VALUES (1,1,'occupied')")
        con.commit()
        con.close()

    def tearDown(self):
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def test_rebuild_refuses_with_live_session(self):
        with mock.patch.object(rebuild, "DB_PATH", self.db), \
             mock.patch.object(rebuild, "backup_existing"):
            with self.assertRaises(SystemExit) as ctx:
                rebuild.main(["--no-backup"])
        self.assertIn("refusing", str(ctx.exception))

    def test_update_refuses_with_live_session(self):
        with mock.patch.object(update, "DB_PATH", self.db):
            with self.assertRaises(SystemExit) as ctx:
                update.migrate_or_rebuild()
        self.assertIn("refusing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
