#!/usr/bin/env python3
"""Wake-submission gate tests (spec #20 Wake Delivery, flags #33/#37).

submit_wake_batch is the last checkpoint before a byte moves: it must
revalidate everything at SUBMIT time, not trust what form_batch saw:

- binding still armed + sprint doc still ACTIVE — a sprint close between
  form_batch and submit CANCELS the batch (no byte, no blind retry);
- a fresh full debounce after every service restart — a NULL
  last_human_input_at never skips the quiet check, and the restart stamp
  (service_restart lease revocation) floors the quiet baseline;
- quiet_s=0 is forbidden outright;
- a writer failure without process death parks the batch delivery_unknown
  (the prompt may have landed) and releases the input lock.

Run:
    python3 tests/test_interface_wake_submit.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_broker  # noqa: E402

ACTIVE_BODY = "# SPRINT: live\nstatus: ACTIVE"
CLOSED_BODY = "# SPRINT: done\nstatus: CLOSED"


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    for sid in (1, 2):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
            "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (?,?,?,'test','sp',1,0,1,1)",
            (sid, f"S{sid}", f"s{sid}"),
        )
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body) "
        "VALUES (1,'doc','SPRINT: live',?)",
        (ACTIVE_BODY,),
    )
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body) "
        "VALUES (2,'doc','SPRINT: done',?)",
        (CLOSED_BODY,),
    )
    con.commit()
    con.close()


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


class WakeSubmitGateTest(unittest.TestCase):
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

    def _arm(self, doc_id=1, shell_id=1):
        """Occupied+idle session, clean composer, binding, one wake item,
        one formed batch. Returns (session_id, binding_id, batch_id)."""
        self.con.execute(
            "INSERT OR IGNORE INTO interface_generations (shell_id, generation)"
            " VALUES (?,1)",
            (shell_id,),
        )
        sid = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (?,1,'occupied','idle')",
            (shell_id,),
        ).lastrowid
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,?,1,'clean')",
            (sid, shell_id),
        )
        bid = self.con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (?,?,?,?,1)",
            (doc_id, shell_id, sid, shell_id),
        ).lastrowid
        mid = self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,?,'wake','task',?)",
            (shell_id, doc_id),
        ).lastrowid
        self.con.execute(
            "INSERT INTO planner_wake_items (binding_id, message_id) VALUES (?,?)",
            (bid, mid),
        )
        self.con.commit()
        batch = interface_broker.form_batch(self.con, bid)
        self.con.commit()
        return sid, bid, batch

    def _submit(self, batch, now_iso, **kw):
        writes = []

        def rec(_len):
            writes.append(_len)

        out = interface_broker.submit_wake_batch(
            self.con, batch, writer=rec, now_iso=now_iso, **kw
        )
        return out, writes

    def _batch_state(self, batch):
        return self.con.execute(
            "SELECT state FROM planner_wake_batches WHERE batch_id=?", (batch,)
        ).fetchone()[0]

    # ── happy path ──────────────────────────────────────────────────────
    def test_clean_gates_submit(self):
        _, _, batch = self._arm()
        out, writes = self._submit(batch, "2030-01-01 00:00:10")
        self.assertTrue(out["submitted"])
        self.assertEqual(out["input_seq_fence"], 1)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self._batch_state(batch), "submitting")

    # ── armed/ACTIVE revalidation at submit (flag #37) ──────────────────
    def test_binding_released_after_form_cancels_batch(self):
        _, bid, batch = self._arm()
        self.con.execute(
            "UPDATE sprint_planner_bindings SET released_at=datetime('now')"
            " WHERE binding_id=?",
            (bid,),
        )
        self.con.commit()
        out, writes = self._submit(batch, "2030-01-01 00:00:10")
        self.assertFalse(out["submitted"])
        self.assertTrue(out["cancelled"])
        self.assertEqual(writes, [], "a cancelled batch never sends a byte")
        self.assertEqual(self._batch_state(batch), "complete")
        item = self.con.execute("SELECT state FROM planner_wake_items").fetchone()[0]
        self.assertEqual(item, "cancelled", "items must not fire for a disarmed sprint")

    def test_closed_sprint_cancels_batch(self):
        _, _, batch = self._arm(doc_id=2, shell_id=2)
        out, writes = self._submit(batch, "2030-01-01 00:00:10")
        self.assertFalse(out["submitted"])
        self.assertTrue(out["cancelled"])
        self.assertEqual(writes, [])
        self.assertEqual(self._batch_state(batch), "complete")

    # ── post-restart debounce (flag #37) ────────────────────────────────
    def test_null_last_human_input_still_owes_full_debounce(self):
        sid, _, batch = self._arm()
        created = self.con.execute(
            "SELECT created_at FROM interface_sessions WHERE session_id=?", (sid,)
        ).fetchone()[0]
        # NULL last_human_input_at must NOT skip the quiet check: 1s after
        # the session started is inside the debounce.
        soon = (parse(created) + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        out, writes = self._submit(batch, soon)
        self.assertFalse(out["submitted"])
        self.assertIn("quiet", out["reason"])
        self.assertEqual(writes, [])
        self.assertEqual(
            self._batch_state(batch),
            "queued",
            "a quiet refusal awaits a later event, no state change",
        )
        later = (parse(created) + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        out, writes = self._submit(batch, later)
        self.assertTrue(out["submitted"])
        self.assertEqual(len(writes), 1)

    def test_restart_floors_the_quiet_baseline(self):
        sid, _, batch = self._arm()
        # Human input long ago — would pass the old check — but a service
        # restart (lease revocation stamp) is 1s before NOW: a fresh full
        # debounce is owed.
        self.con.execute(
            "UPDATE interface_input_state SET last_human_input_at="
            "'2020-01-01 00:00:00' WHERE session_id=?",
            (sid,),
        )
        self.con.execute(
            "INSERT INTO interface_writer_leases (session_id, shell_id,"
            " generation, client_id, token_hash, revoked_at, revoke_reason) "
            "VALUES (?,1,1,'c','h','2030-01-01 00:00:00','service_restart')",
            (sid,),
        )
        self.con.commit()
        out, writes = self._submit(batch, "2030-01-01 00:00:01")
        self.assertFalse(out["submitted"])
        self.assertIn("quiet", out["reason"])
        self.assertEqual(writes, [])
        out, writes = self._submit(batch, "2030-01-01 00:00:10")
        self.assertTrue(out["submitted"])
        self.assertEqual(len(writes), 1)

    # ── zero debounce forbidden (flag #37) ──────────────────────────────
    def test_zero_quiet_s_rejected(self):
        _, _, batch = self._arm()
        with self.assertRaises(interface_broker.BrokerError):
            self._submit(batch, "2030-01-01 00:00:10", quiet_s=0)
        with self.assertRaises(interface_broker.BrokerError):
            self._submit(batch, "2030-01-01 00:00:10", quiet_s=-1)
        self.assertEqual(self._batch_state(batch), "queued")

    # ── writer failure parks the batch live (lock release, flag #33) ────
    def test_writer_failure_parks_batch_and_alerts(self):
        _, bid, batch = self._arm()

        class TmuxError(Exception):
            pass

        def failing_writer(_len):
            raise TmuxError("tmux send-keys failed")

        with self.assertRaises(TmuxError):
            interface_broker.submit_wake_batch(
                self.con, batch, writer=failing_writer, now_iso="2030-01-01 00:00:10"
            )
        row = self.con.execute(
            "SELECT state, submit_hook_seq FROM planner_wake_batches WHERE batch_id=?",
            (batch,),
        ).fetchone()
        self.assertEqual(
            row,
            ("delivery_unknown", None),
            "ambiguous delivery parks; no evidence may be stamped",
        )
        alert = self.con.execute(
            "SELECT 1 FROM planner_alerts "
            "WHERE reason='wake_batch_delivery_unknown' AND resolved_at IS "
            "NULL"
        ).fetchone()
        self.assertIsNotNone(alert)
        # The park releases the input lock: human input flows again.
        self.con.execute(
            "INSERT INTO interface_writer_leases (session_id, shell_id,"
            " generation, client_id, token_hash) "
            "SELECT session_id, shell_id, generation, 'tab', 'h' "
            "FROM interface_sessions"
        )
        self.con.commit()
        sid = self.con.execute("SELECT session_id FROM interface_sessions").fetchone()[
            0
        ]
        writes = []
        ack = interface_broker.accept_human_input(
            self.con, sid, 1, 10, writer=lambda n: writes.append(n)
        )
        self.assertEqual(ack, {"ack": 1, "duplicate": False})
        self.assertEqual(writes, [10])


if __name__ == "__main__":
    unittest.main()
