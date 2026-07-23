#!/usr/bin/env python3
"""Interface snapshot-projection tests (spec #20 task #80).

The snapshot contract: it may run while a chat is live because it omits all
volatile state, and rebuild/update refuse while live state exists — so
content.sql carries only durable audit. These tests pin:

- volatile tables (interface_writer_leases, interface_input_state,
  pr_poll_runs) are NOT in PER_INSTANCE_TABLES;
- volatile columns (tmux socket, PIDs/start ticks, hook token hash) never
  appear in a dump even on preserved rows;
- row filters keep live rows out: non-ended sessions, armed bindings,
  nonterminal batches/items, and no-transition observations are excluded
  while their terminal counterparts are preserved;
- durable guard tables (action receipts, idempotency keys, alerts) dump
  whole.

Run:
    python3 tests/test_interface_snapshot.py
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
import snapshot  # noqa: E402

SECRET_SOCKET = "/run/sc/SECRET-tmux-socket-0700"
SECRET_HOOK_HASH = "hookhash_SECRET_must_not_ship_to_git_00000000"


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


class InterfaceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.con = sqlite3.connect(self.db)
        # Generations: gen 1 live (secret hook hash), gen 2 ended.
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation,"
            " hook_token_hash) VALUES (1,1,?)", (SECRET_HOOK_HASH,))
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation,"
            " ended_at) VALUES (2,2,datetime('now'))")
        # Sessions: one live with volatile identity, one ended audit row.
        self.con.execute(
            "INSERT INTO interface_sessions (session_id, shell_id,"
            " generation, occupancy, tmux_socket, tmux_session, tmux_pane_id,"
            " pane_pid, pane_start_ticks, harness_pid, harness_start_ticks) "
            "VALUES (1,1,1,'occupied',?, 's','%1',111,222,333,444)",
            (SECRET_SOCKET,))
        self.con.execute(
            "INSERT INTO interface_sessions (session_id, shell_id,"
            " generation, occupancy, lifecycle, ended_at, end_reason,"
            " tmux_socket, pane_pid) "
            "VALUES (2,2,2,'ended','ended',datetime('now'),'operator_end',"
            " ?, 999)", (SECRET_SOCKET,))
        # Bindings: one armed, one released.
        self.con.execute(
            "INSERT INTO sprint_planner_bindings (binding_id, sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,1,1,1,1)")
        self.con.execute(
            "INSERT INTO sprint_planner_bindings (binding_id, sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation,"
            " released_at, release_reason) "
            "VALUES (2,1,2,2,2,2,datetime('now'),'sprint_closed')")
        # Batches: complete (audit) + queued (live).
        self.con.execute(
            "INSERT INTO planner_wake_batches (batch_id, binding_id,"
            " shell_id, generation, state) VALUES (1,2,2,2,'complete')")
        self.con.execute(
            "INSERT INTO planner_wake_batches (batch_id, binding_id,"
            " shell_id, generation, state) VALUES (2,1,1,1,'queued')")
        # Items: done (audit) + queued (live).
        self.con.execute(
            "INSERT INTO shell_messages (message_id, from_shell_id,"
            " to_shell_id, body) VALUES (1,1,2,'a')")
        self.con.execute(
            "INSERT INTO shell_messages (message_id, from_shell_id,"
            " to_shell_id, body) VALUES (2,1,2,'b')")
        self.con.execute(
            "INSERT INTO planner_wake_items (item_id, binding_id, message_id,"
            " batch_id, state) VALUES (1,2,1,1,'done')")
        self.con.execute(
            "INSERT INTO planner_wake_items (item_id, binding_id, message_id,"
            " state) VALUES (2,1,2,'queued')")
        # Observations: transition + blind-window (audit), plain (noise).
        self.con.execute(
            "INSERT INTO watched_prs (watch_id, repo, pr_number, shell_id) "
            "VALUES (1,'o/r',5,2)")
        self.con.execute(
            "INSERT INTO pr_poll_observations (observation_id, watch_id,"
            " transition) VALUES (1,1,'checks_green')")
        self.con.execute(
            "INSERT INTO pr_poll_observations (observation_id, watch_id,"
            " blind_window) VALUES (2,1,1)")
        self.con.execute(
            "INSERT INTO pr_poll_observations (observation_id, watch_id)"
            " VALUES (3,1)")
        # Durable guard tables.
        self.con.execute(
            "INSERT INTO planner_action_receipts (operation, target,"
            " idem_key) VALUES ('op','tgt','k1')")
        self.con.execute(
            "INSERT INTO interface_idempotency_keys (actor_scope, operation,"
            " idem_key, request_hash, expires_at) "
            "VALUES ('operator','sessions.create','k','h','2030-01-01')")
        self.con.execute(
            "INSERT INTO planner_alerts (severity, reason, dedupe_key) "
            "VALUES ('critical','crash','-|-|crash')")
        self.con.execute(
            "INSERT INTO planner_alerts (session_id, severity, reason, dedupe_key) "
            "VALUES (1,'warning','live-session','1|-|-|-|live')")
        self.con.execute(
            "INSERT INTO planner_alerts (session_id, severity, reason, dedupe_key) "
            "VALUES (2,'warning','ended-session','2|-|-|-|ended')")
        self.con.execute(
            "INSERT INTO planner_alerts (binding_id, severity, reason, dedupe_key) "
            "VALUES (1,'warning','live-binding','-|1|-|-|live')")
        self.con.execute(
            "INSERT INTO planner_alerts (binding_id, severity, reason, dedupe_key) "
            "VALUES (2,'warning','ended-binding','-|2|-|-|ended')")
        self.con.execute(
            "INSERT INTO planner_alerts (session_id, binding_id, severity, "
            "reason, dedupe_key) "
            "VALUES (2,1,'warning','mixed-live-parent','2|1|-|-|mixed')")
        self.con.execute(
            "INSERT INTO planner_alerts (session_id, severity, reason, dedupe_key) "
            "VALUES (999,'warning','orphan-session','999|-|-|-|orphan')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for p in self.tmp.glob("*"):
            p.unlink()
        self.tmp.rmdir()

    def _dump(self, table):
        return "\n".join(snapshot.dump_table(self.con, table))

    def test_volatile_tables_not_snapshotted(self):
        for table in ("interface_writer_leases", "interface_input_state",
                      "pr_poll_runs"):
            self.assertNotIn(table, snapshot.PER_INSTANCE_TABLES,
                             f"{table} must never reach content.sql")

    def test_session_rows_filtered_and_redacted(self):
        out = self._dump("interface_sessions")
        self.assertIn("session_id", out)
        self.assertNotIn("'occupied'", out, "live session leaked")
        self.assertIn("'ended'", out, "closed-session audit must survive")
        self.assertIn("'operator_end'", out)
        for col in ("tmux_socket", "pane_pid", "pane_start_ticks",
                    "harness_pid", "harness_start_ticks"):
            self.assertNotIn(col, out, f"volatile column {col} leaked")
        self.assertNotIn(SECRET_SOCKET, out)
        self.assertNotIn("999", out, "volatile PID value leaked")

    def test_generation_hook_hash_redacted(self):
        out = self._dump("interface_generations")
        self.assertNotIn(SECRET_HOOK_HASH, out)
        self.assertNotIn("hook_token_hash", out)

    def test_generations_keep_ended_only(self):
        # A LIVE generation must never serialize: a rebuild would restore it
        # and idx_interface_generations_live would brick that shell's next
        # New chat (flag #36). Ended generations are durable audit.
        out = self._dump("interface_generations")
        self.assertNotIn("VALUES (1, 1,", out, "live generation leaked")
        self.assertIn("VALUES (2, 2,", out, "ended generation audit dropped")

    def test_bindings_keep_released_only(self):
        out = self._dump("sprint_planner_bindings")
        self.assertIn("'sprint_closed'", out)
        # binding 1 (armed) must not appear; binding 2 (released) must.
        self.assertNotIn("VALUES (1,", out)
        self.assertIn("VALUES (2,", out)

    def test_wake_batches_keep_terminal_only(self):
        out = self._dump("planner_wake_batches")
        self.assertIn("'complete'", out)
        self.assertNotIn("'queued'", out)

    def test_wake_items_keep_terminal_only(self):
        out = self._dump("planner_wake_items")
        self.assertIn("'done'", out)
        self.assertNotIn("'queued'", out)

    def test_observations_keep_transitions_and_blind_windows(self):
        out = self._dump("pr_poll_observations")
        self.assertIn("'checks_green'", out)
        self.assertIn("VALUES (2,", out, "blind-window observation dropped")
        self.assertNotIn("VALUES (3,", out, "no-transition observation kept")

    def test_durable_guard_tables_preserve_closed_projection(self):
        self.assertIn("'k1'", self._dump("planner_action_receipts"))
        self.assertIn("'operator'", self._dump("interface_idempotency_keys"))
        self.assertIn("'crash'", self._dump("planner_alerts"))

    def test_alerts_require_every_referenced_parent_in_projection(self):
        out = self._dump("planner_alerts")
        self.assertIn("'ended-session'", out)
        self.assertIn("'ended-binding'", out)
        self.assertNotIn("'live-session'", out)
        self.assertNotIn("'live-binding'", out)
        self.assertNotIn("'mixed-live-parent'", out)
        self.assertNotIn("'orphan-session'", out)

    def test_row_filters_reference_live_columns(self):
        # Drift guard: each filter must parse and execute against the live
        # schema — a renamed column would otherwise silently turn the filter
        # into a SQL error (or worse, get "fixed" by dropping the filter and
        # widening the dump to all rows).
        for table, filt in snapshot.SNAPSHOT_ROW_FILTERS.items():
            try:
                self.con.execute(
                    f"SELECT 1 FROM {table} {filt} LIMIT 1").fetchall()
            except sqlite3.OperationalError as e:
                self.fail(f"{table}: row filter broken against schema "
                          f"({filt!r}): {e}")

    def test_complete_snapshot_projection_is_foreign_key_closed(self):
        target = self.tmp / "rebuilt.db"
        build_engine_db(target)
        lines = ["PRAGMA foreign_keys=OFF;", "BEGIN;"]
        for table in snapshot.PER_INSTANCE_TABLES:
            if snapshot.table_exists(self.con, table):
                lines.extend(snapshot.dump_table(self.con, table))
        lines.extend(["COMMIT;", "PRAGMA foreign_keys=ON;"])

        rebuilt = sqlite3.connect(target)
        try:
            rebuilt.executescript("\n".join(lines))
            violations = rebuilt.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            rebuilt.close()
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
