#!/usr/bin/env python3
"""Interface transition-validator tests (spec #20 task #80).

Drift guard between the two enforcement layers: the SQL triggers in
migrations/0078_interface_sessions.sql (backstop) and the app-level edge
maps in scripts/interface_state.py (friendly errors). For EVERY state
machine this walks every (old, new) pair and asserts:

- the DB trigger allows exactly the pairs interface_state calls legal
  (a same-state no-op is legal in both layers), and
- interface_state.transition raises InterfaceTransitionError on illegal
  pairs and never on legal ones.

Any drift between the SQL and the Python fails this file.

Run:
    python3 tests/test_interface_transitions.py
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
import interface_state  # noqa: E402


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


class TransitionMatrixTest(unittest.TestCase):
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

    # ── row factories: INSERT bypasses the UPDATE triggers, so any state ──
    def _session_row(self, occupancy, lifecycle):
        self.con.execute("DELETE FROM interface_sessions")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (1,1)")
        cur = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (1,1,?,?)", (occupancy, lifecycle))
        self.con.commit()
        return cur.lastrowid

    def _input_row(self, composer, delivery):
        self.con.execute("DELETE FROM interface_input_state")
        self.con.execute("DELETE FROM interface_sessions")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (1,1)")
        sid = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy)"
            " VALUES (1,1,'occupied')").lastrowid
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer, delivery) VALUES (?,1,1,?,?)",
            (sid, composer, delivery))
        self.con.commit()
        return sid

    def _item_row(self, state):
        self.con.execute("DELETE FROM planner_wake_items")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (2,1)")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_sessions (session_id, shell_id,"
            " generation, occupancy) VALUES (1,2,1,'occupied')")
        self.con.execute(
            "INSERT OR IGNORE INTO sprint_planner_bindings (binding_id,"
            " sprint_doc_id, planner_shell_id, session_id, shell_id,"
            " generation) VALUES (1,1,2,1,2,1)")
        self.con.execute(
            "INSERT OR IGNORE INTO shell_messages (message_id, from_shell_id,"
            " to_shell_id, body) VALUES (1,1,2,'wake')")
        cur = self.con.execute(
            "INSERT INTO planner_wake_items (binding_id, message_id, state)"
            " VALUES (1,1,?)", (state,))
        self.con.commit()
        return cur.lastrowid

    def _batch_row(self, state):
        self.con.execute("DELETE FROM planner_wake_batches")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (2,1)")
        self.con.execute(
            "INSERT OR IGNORE INTO interface_sessions (session_id, shell_id,"
            " generation, occupancy) VALUES (1,2,1,'occupied')")
        self.con.execute(
            "INSERT OR IGNORE INTO sprint_planner_bindings (binding_id,"
            " sprint_doc_id, planner_shell_id, session_id, shell_id,"
            " generation) VALUES (1,1,2,1,2,1)")
        cur = self.con.execute(
            "INSERT INTO planner_wake_batches (binding_id, shell_id,"
            " generation, state) VALUES (1,2,1,?)", (state,))
        self.con.commit()
        return cur.lastrowid

    def _receipt_row(self, state, n):
        self.con.execute("DELETE FROM planner_action_receipts")
        cur = self.con.execute(
            "INSERT INTO planner_action_receipts (operation, target, idem_key,"
            " state) VALUES ('op','tgt',?,?)", (f"k{n}", state))
        self.con.commit()
        return cur.lastrowid

    # ── the matrix walker ────────────────────────────────────────────────
    def _walk(self, machine, row_factory):
        table, pk, col, edges = interface_state.MACHINES[machine]
        states = set(edges) | {s for targets in edges.values() for s in targets}
        for old in sorted(states):
            for new in sorted(states):
                row_id = row_factory(old, new)
                legal = (new == old) or (new in edges.get(old, ()))
                # app layer
                if legal:
                    prior = interface_state.transition(
                        self.con, machine, row_id, new)
                    self.assertEqual(prior, old, f"{machine}: {old}->{new}")
                    self.con.commit()
                else:
                    with self.assertRaises(
                            interface_state.InterfaceTransitionError,
                            msg=f"{machine}: {old}->{new} must refuse"):
                        interface_state.transition(
                            self.con, machine, row_id, new)
                # DB layer: replay the same pair on a fresh row and ask the
                # trigger directly (bypassing the app pre-check).
                row_id = row_factory(old, new)
                try:
                    self.con.execute(
                        f"UPDATE {table} SET {col}=? WHERE {pk}=?",
                        (new, row_id))
                    self.con.commit()
                    db_allowed = True
                except sqlite3.IntegrityError:
                    self.con.rollback()
                    db_allowed = False
                self.assertEqual(
                    db_allowed, legal,
                    f"{machine}: trigger/app drift on {old}->{new}")

    def test_occupancy_machine(self):
        self._walk("occupancy",
                   lambda old, _n: self._session_row(old, "idle"))

    def test_lifecycle_machine(self):
        self._walk("lifecycle",
                   lambda old, _n: self._session_row("occupied", old))

    def test_composer_machine(self):
        self._walk("composer",
                   lambda old, _n: self._input_row(old, "normal"))

    def test_delivery_machine(self):
        self._walk("delivery",
                   lambda old, _n: self._input_row("clean", old))

    def test_wake_item_machine(self):
        self._walk("wake_item", lambda old, _n: self._item_row(old))

    def test_wake_batch_machine(self):
        self._walk("wake_batch", lambda old, _n: self._batch_row(old))

    def test_receipt_machine(self):
        n = [0]

        def factory(old, _new):
            n[0] += 1
            return self._receipt_row(old, n[0])

        self._walk("receipt", factory)


if __name__ == "__main__":
    unittest.main()
