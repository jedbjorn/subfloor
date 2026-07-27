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
  Flag #303 adds the one exception and it is not a loophole: on a
  'first_turn_gated' harness the provider stamp cannot arrive unbidden, so
  the baseline is the weaker process_ready_at stamp — a separately aged
  column, still owed in full, and only for a seat whose hooks installed.
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
import shutil
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
import interface_hooks  # noqa: E402
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
    # A declared board is what makes the sprint LIVE (H-1); the body's
    # `status:` line above is display prose that nothing reads.
    con.execute(
        "INSERT INTO sprint_units (sprint_doc_id, seq, unit_title) "
        "VALUES (1,'U1','the unit')")
    con.commit()
    con.close()


def _age(con, table, col, row_id, seconds, pk):
    """Backdate a timestamp column so quiet-debounce arithmetic is exact."""
    con.execute(
        f"UPDATE {table} SET {col}=datetime('now', ?) WHERE {pk}=?",
        (f"-{seconds} seconds", row_id))


class WakeFixture(unittest.TestCase):
    """An armed sprint: occupied+idle+clean planner session (kimi, full
    hooks), an ACTIVE sprint doc, an unreleased binding.

    The seat is built from the four class attributes below rather than
    mutated afterwards: the lifecycle trigger legitimately refuses an
    idle -> starting rewind, so a test that needs a pre-readiness seat must
    INSERT one in that state."""

    HARNESS = "kimi"
    CLI_VERSION = "kimi-code 0.27.0"
    LIFECYCLE = "idle"
    COMPOSER = "clean"

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
            " lifecycle, harness, cli_version) VALUES (1,1,'occupied',?,?,?)",
            (self.LIFECYCLE, self.HARNESS, self.CLI_VERSION)).lastrowid
        self.con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,?)",
            (self.sid, self.COMPOSER))
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
        # glob-then-unlink races SQLite's WAL sidecars: a -wal/-shm listed by
        # glob can be gone by the time unlink reaches it, and the test fails in
        # cleanup having already passed. Same tolerant rmtree the routes case
        # below already uses.
        shutil.rmtree(self.tmp, ignore_errors=True)

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

    def test_a_closed_status_line_alone_still_wakes(self):
        """H-1's delivered half at the ingress gate: the body's `status:` line
        is display prose. It said CLOSED here and the wake item is created
        anyway, because closing a sprint is freezing its doc — see
        test_frozen_sprint_never_wakes for the structural half. Before H-1 a
        planner who reformatted this one line went silently deaf."""
        self.con.execute(
            "UPDATE documents SET body='# SPRINT: test\nstatus: CLOSED' "
            "WHERE document_id=1")
        mid = self.add_message("task")
        self.assertIsNotNone(
            interface_wake.maybe_create_wake_item(self.con, mid))

    def test_undeclared_board_never_wakes(self):
        """The unit-count operand on its own: unfrozen, correctly titled, and
        not live because no board has been declared yet."""
        self.con.execute("DELETE FROM sprint_units WHERE sprint_doc_id=1")
        mid = self.add_message("task")
        self.assertIsNone(interface_wake.maybe_create_wake_item(self.con, mid))

    def test_retitled_sprint_never_wakes(self):
        """The title operand on its own — the field `doc edit` now refuses to
        change on a doc holding a board, for exactly this reason.

        The lower-case case pins that `LIKE` is ASCII-case-insensitive in
        SQLite, which is the inherited behaviour: swapping the predicate to
        GLOB or a case-sensitive compare would silently un-live every sprint
        whose title was typed in mixed case, and nothing else would notice.
        """
        for title, wakes in (("Retro notes", False),
                             ("sprint: test", True)):
            with self.subTest(title=title):
                self.con.execute(
                    "UPDATE documents SET title=? WHERE document_id=1",
                    (title,))
                mid = self.add_message("task")
                item = interface_wake.maybe_create_wake_item(self.con, mid)
                self.assertIs(item is not None, wakes)

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


# ── Flag #303: a first-turn-gated harness (codex) arms on the weaker proof ───

class CodexFirstTurnGatedTest(WakeFixture):
    """Codex 0.145.0 does not fire SessionStart until a human submits the
    first turn (measured on a live TUI seat). Waiting for it deadlocks the
    seat that exists to BE woken: no readiness -> lifecycle stays 'starting'
    -> the gate never sees occupied+idle -> the submit that would have
    triggered the hook never goes out. Decisions #98/#99."""

    # The state interface_exec's pre-exec claim arrives in: promoted to
    # occupied, but lifecycle still 'starting' and the composer not yet
    # proven — and running codex, not kimi.
    HARNESS = "codex"
    CLI_VERSION = "codex-cli 0.145.0"
    LIFECYCLE = "starting"
    COMPOSER = "unknown"

    def seat(self):
        return self.con.execute(
            "SELECT lifecycle, provider_ready_at, process_ready_at "
            "FROM interface_sessions WHERE session_id=?",
            (self.sid,)).fetchone()

    def composer(self):
        return self.con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=?",
            (self.sid,)).fetchone()[0]

    def entrypoint_claim(self, seq=1, hooks_installed=True):
        interface_broker.record_hook(self.con, 1, 1, seq, "session_start",
                                     source="entrypoint",
                                     hooks_installed=hooks_installed)
        self.con.commit()

    def test_entrypoint_claim_promotes_a_first_turn_gated_seat(self):
        self.entrypoint_claim()
        lifecycle, provider, process = self.seat()
        with self.subTest("lifecycle"):
            self.assertEqual(lifecycle, "idle")
        with self.subTest("composer"):
            self.assertEqual(self.composer(), "clean")
        with self.subTest("weak proof recorded"):
            self.assertIsNotNone(process)
        with self.subTest("strong proof not claimed"):
            self.assertIsNone(
                provider,
                "the pre-exec claim proves the PROCESS is up, never that "
                "the provider handshaked — aliasing it into "
                "provider_ready_at makes that column mean two different "
                "things by harness and silently breaks every reader")

    def test_turnless_codex_seat_can_be_woken(self):
        """The flag #303 deadlock, end to end: nobody ever types, so no
        provider session_start arrives — the wake must still submit."""
        self.entrypoint_claim()
        _age(self.con, "interface_sessions", "process_ready_at", self.sid,
             60, "session_id")
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        batch_id = self.form()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertTrue(out["submitted"], out)
        self.assertEqual(writes, [len(interface_broker.WAKE_PROMPT) + 1])

    def test_debounce_is_owed_from_the_process_stamp(self):
        """Proceeding on weak proof does NOT skip the fence. occupied_at and
        created_at are 60s old, but the process claim landed just now — the
        gate must still owe the full debounce, or flag #49's defect returns
        through the new column."""
        self.entrypoint_claim()
        mid = self.add_message("task")
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        batch_id = self.form()
        out = self.submit(batch_id, lambda n: None, quiet_s=3.0)
        self.assertFalse(out["submitted"])
        self.assertIn("quiet", out["reason"])
        self.assertEqual(self.batch_state(batch_id), "queued")

    def test_a_failed_hook_install_neither_promotes_nor_arms(self):
        """Flag #366 (SC-354): promotion on the entrypoint's claim is only
        sound while the seat really holds the hooks its capability
        advertises. `capability()` is a STATIC version lookup — a codex seat
        whose `.codex/hooks.json` is unparseable installs nothing and still
        reads mandatory_ok=True — so without the install report the claim
        would arm a seat with ZERO lifecycle hooks: no prompt_submit fence,
        no turn_stop. That configuration was fail-CLOSED before this unit and
        must stay that way.

        The install result is CHAINED from the real installer rather than
        written in as a literal False: a change that made the corrupt file
        install cleanly has to turn this red too."""
        work = self.tmp / "corrupt-seat"
        (work / ".codex").mkdir(parents=True)
        (work / ".codex" / "hooks.json").write_text("{ not json")
        out = interface_hooks.install("codex", work, run_dir=self.tmp / "run",
                                      session_id=self.sid,
                                      cli_version=self.CLI_VERSION)
        with self.subTest("install refuses the corrupt file"):
            self.assertFalse(out["installed"])
        with self.subTest("capability alone would have promoted"):
            self.assertTrue(
                out["capability"]["mandatory_ok"],
                "the static table is exactly why the install report is "
                "needed — if it ever fails closed on its own, this test is "
                "no longer measuring the gap it was written for")

        self.entrypoint_claim(hooks_installed=out["installed"])
        lifecycle, provider, process = self.seat()
        with self.subTest("not promoted"):
            self.assertEqual(lifecycle, "starting")
        with self.subTest("composer not certified"):
            self.assertEqual(self.composer(), "unknown")
        with self.subTest("provider stamp"):
            self.assertIsNone(provider)
        with self.subTest("process stamp is still recorded"):
            self.assertIsNotNone(
                process, "the process IS up — the weak proof is true and "
                         "stays recorded; it is the PROMOTION that is "
                         "withheld")

        # ...and the wake gate refuses. The item and batch still form: the
        # ingress check reads the same static capability, so the ONLY thing
        # standing between this seat and a submit is the withheld promotion.
        _age(self.con, "interface_sessions", "process_ready_at", self.sid,
             60, "session_id")
        mid = self.add_message("task")
        self.assertIsNotNone(
            interface_wake.maybe_create_wake_item(self.con, mid))
        self.con.commit()
        writes = []
        out = self.submit(self.form(), writes.append)
        with self.subTest("no submit"):
            self.assertFalse(out["submitted"], out)
        with self.subTest("refused on the unpromoted lifecycle"):
            self.assertIn("starting", out["reason"])
        with self.subTest("nothing was written to the pane"):
            self.assertEqual(writes, [])

    def test_first_real_turn_upgrades_to_provider_readiness(self):
        """'Proceed on weak proof, upgrade to strong proof when it arrives.'
        The deferred provider hook still stamps the real column, and BOTH
        stamps survive — that is what lets a reader tell 'process ready,
        provider unproven' from 'provider handshaked'."""
        self.entrypoint_claim()
        _, provider, process = self.seat()
        self.assertIsNone(provider)
        interface_broker.record_hook(self.con, 1, 1, 2, "session_start",
                                     source="provider")
        self.con.commit()
        lifecycle, provider, process_after = self.seat()
        with self.subTest("provider stamp arrives"):
            self.assertIsNotNone(provider)
        with self.subTest("process stamp survives"):
            self.assertEqual(process_after, process)
        with self.subTest("still idle"):
            self.assertEqual(lifecycle, "idle")


class NativeReadinessSeatTest(WakeFixture):
    """The counterpart scope proof: a harness whose readiness signal DOES
    arrive unbidden must not be armed by the weaker claim."""

    LIFECYCLE = "starting"
    COMPOSER = "unknown"

    def test_entrypoint_claim_does_not_promote_a_native_readiness_harness(self):
        """kimi awaits SessionStart as the final step of session creation,
        so it has a real signal coming and nothing is deadlocked. The
        process stamp is still recorded (it is true of every harness); only
        the PROMOTION is scoped to first_turn_gated.

        The claim reports hooks_installed=True so that what is proved here is
        the readiness-class scoping and nothing else — with the operand false
        this seat would stay 'starting' for the other reason and the test
        would pass while measuring nothing."""
        interface_broker.record_hook(self.con, 1, 1, 1, "session_start",
                                     source="entrypoint",
                                     hooks_installed=True)
        self.con.commit()
        lifecycle, provider, process = self.con.execute(
            "SELECT lifecycle, provider_ready_at, process_ready_at "
            "FROM interface_sessions WHERE session_id=?",
            (self.sid,)).fetchone()
        with self.subTest("lifecycle"):
            self.assertEqual(lifecycle, "starting",
                             "kimi still owes its native readiness hook")
        with self.subTest("composer"):
            self.assertEqual(
                self.con.execute(
                    "SELECT composer FROM interface_input_state "
                    "WHERE session_id=?", (self.sid,)).fetchone()[0],
                "unknown")
        with self.subTest("provider stamp"):
            self.assertIsNone(provider)
        with self.subTest("process stamp"):
            self.assertIsNotNone(process)


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

    def test_nonempty_browser_composer_blocks_same_wake_gate(self):
        batch_id = self._armed_batch()
        self.con.execute(
            "UPDATE interface_input_state SET browser_composer='dirty' "
            "WHERE session_id=?", (self.sid,))
        self.con.commit()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertFalse(out["submitted"])
        self.assertEqual(out["reason"], "browser composer is dirty")
        self.assertEqual(writes, [], "a browser draft must block every wake byte")
        self.assertEqual(self.batch_state(batch_id), "queued")

        self.con.execute(
            "UPDATE interface_input_state SET browser_composer='clean' "
            "WHERE session_id=?", (self.sid,))
        self.con.commit()
        out = self.submit(batch_id, writes.append)
        self.assertTrue(out["submitted"])
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.batch_state(batch_id), "submitting")

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

    def test_ended_session_gate_fails_alerts_never_crashes(self):
        # SC-011: End chat does NOT release the binding or cancel queued
        # wake work — armed binding + queued batch + ended session must
        # gate_fail WITH an alert (spec Retry Policy: session loss queues
        # AND alerts), never die on a None dereference.
        batch_id = self._armed_batch()
        self.con.execute(
            "UPDATE interface_sessions SET occupancy='ended' "
            "WHERE session_id=?", (self.sid,))
        self.con.commit()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertFalse(out["submitted"])
        self.assertIn("session ended", out["reason"])
        self.assertEqual(writes, [], "no byte may move")
        self.assertEqual(self.batch_state(batch_id), "queued",
                         "the batch waits for a future generation")
        alert = self.con.execute(
            "SELECT severity FROM planner_alerts "
            "WHERE reason='wake_session_ended' AND resolved_at IS NULL"
        ).fetchone()
        self.assertIsNotNone(alert, "session loss must alert, not stall")
        self.assertEqual(alert[0], "critical")
        # Idempotent: every drain / startup_pass re-attempt gate-fails
        # cleanly — no re-crash, and the open alert dedupes (no spam).
        out2 = self.submit(batch_id, writes.append)
        self.assertFalse(out2["submitted"])
        self.assertEqual(self.batch_state(batch_id), "queued")
        count = self.con.execute(
            "SELECT COUNT(*) FROM planner_alerts "
            "WHERE reason='wake_session_ended' AND resolved_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_frozen_active_sprint_blocks_submit(self):
        # SC-012: spec Sprint Scope — a wake is eligible only while the doc
        # is unfrozen AND ACTIVE. Freeze between form and submit revokes
        # sprint authority exactly like a close: the batch cancels, no byte.
        batch_id = self._armed_batch()
        self.con.execute("UPDATE documents SET frozen=1 WHERE document_id=1")
        self.con.commit()
        writes = []
        out = self.submit(batch_id, writes.append)
        self.assertFalse(out["submitted"])
        self.assertTrue(out["cancelled"])
        self.assertEqual(writes, [], "a post-freeze wake must never fire")
        self.assertEqual(self.batch_state(batch_id), "complete")
        self.assertEqual(self.item_states()[0][2], "cancelled")

    def test_probe_runs_outside_the_write_transaction(self):
        # SC-013: the unmanaged-client probe shells out to tmux; run inside
        # BEGIN IMMEDIATE, a wedged server would hang the drain thread WHILE
        # HOLDING the SQLite write lock — an engine-wide write stall.
        batch_id = self._armed_batch()
        seen = {}

        def probe():
            seen["in_txn"] = self.con.in_transaction
            return False

        writes = []
        out = self.submit(batch_id, writes.append, probe=probe)
        self.assertTrue(out["submitted"], out)
        self.assertFalse(seen["in_txn"],
                         "the probe must not run inside the write txn")


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


# ── H-4: a row the planner already read wakes nobody ─────────────────────────

class AlreadyReadNeverWakesTest(WakeFixture):
    """The planner drained its inbox by hand (or a faster channel woke it
    first) BEFORE the batch formed. Delivering those rows costs a no-op
    planner turn and trains the planner to dismiss wake prompts."""

    def _queued(self, read=False):
        mid = self.add_message("task", read=read)
        interface_wake.maybe_create_wake_item(self.con, mid)
        self.con.commit()
        return mid

    def test_already_read_row_never_joins_a_batch(self):
        mid = self._queued(read=True)
        self.form()
        states = {r[1]: (r[2], r[4]) for r in self.item_states()}
        self.assertEqual(states[mid][0], "queued",
                         "form_batch must leave it alone, not batch it")
        self.assertIsNone(states[mid][1], "it must carry no batch_id")

    def test_sweep_completes_an_already_read_row_without_a_batch(self):
        mid = self._queued(read=True)
        swept = interface_broker.sweep_read_queued(self.con, self.binding)
        self.con.commit()
        self.assertEqual(swept, 1)
        states = {r[1]: r[2] for r in self.item_states()}
        self.assertEqual(states[mid], "done")
        self.assertIsNone(
            self.con.execute(
                "SELECT batch_id FROM planner_wake_batches").fetchone(),
            "no batch may be formed for work that needs no wake")

    def test_sweep_leaves_an_unread_row_queued(self):
        mid = self._queued(read=False)
        self.assertEqual(
            interface_broker.sweep_read_queued(self.con, self.binding), 0)
        self.con.commit()
        self.assertEqual({r[1]: r[2] for r in self.item_states()}[mid],
                         "queued")

    def test_unread_sibling_still_batches_when_one_row_was_read(self):
        read_mid = self._queued(read=True)
        unread_mid = self._queued(read=False)
        interface_broker.sweep_read_queued(self.con, self.binding)
        batch_id = self.form()
        states = {r[1]: (r[2], r[4]) for r in self.item_states()}
        self.assertEqual(states[read_mid][0], "done")
        self.assertEqual(states[unread_mid], ("batched", batch_id),
                         "a drained sibling must not suppress live work")


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
        # The coordinator has no stop(), so a timer armed by the test can still
        # fire a drain — opening and closing a WAL connection — while cleanup
        # runs. Deleting the tree tolerantly is what makes that harmless.
        shutil.rmtree(self.tmp, ignore_errors=True)

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

    async def test_drained_inbox_forms_no_batch_and_writes_nothing(self):
        """H-4 end to end: every queued row read before the drain → no
        batch, no keystroke, and the items retire as done."""
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task", read=True)
        self.add_message("result", read=True)
        self.coord.notify_binding(self.binding)
        done = await self.wait_for(
            lambda: self.one("SELECT COUNT(*) FROM planner_wake_items "
                             "WHERE state='done'") == 2)
        self.assertTrue(done, "read rows must complete, not sit queued")
        self.assertEqual(self.writes, [], "a drained inbox costs no wake turn")
        self.assertIsNone(self.one("SELECT batch_id FROM planner_wake_batches"))

    async def test_unread_row_still_drains_when_a_read_row_precedes_it(self):
        """The suppression is per-row: it must not swallow live work."""
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task", read=True)
        unread = self.add_message("result", read=False)
        self.coord.notify_binding(self.binding)
        ok = await self.wait_for(lambda: len(self.writes) == 1)
        self.assertTrue(ok, "the unread row must still drive a submission")
        batched = self.one(
            "SELECT COUNT(*) FROM planner_wake_items WHERE batch_id IS NOT NULL")
        self.assertEqual(batched, 1, "only the unread row rides the batch")
        self.assertEqual(
            self.one("SELECT state FROM planner_wake_items WHERE message_id=?",
                     (unread,)), "submitting")

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

    # The ONE test here gated on the debounce itself, so the ONE that has to
    # respect the clock the gate is measured with. `quiet` is
    # julianday(now) - julianday(baseline) with BOTH ends from `datetime('now')`
    # (interface_broker._now / WakeCoordinator._drain) — whole seconds. So it
    # moves in 1s steps and a sub-second debounce is unobservable: at QUIET=0.2
    # the byte was held or sent purely on whether a second boundary happened to
    # fall between the fixture write and the gate, which failed ~1 run in 25
    # locally and more under CI load.
    #
    # Fixed by out-scaling the clock, not by retrying: with a debounce above 1s,
    # quiet ∈ {0, 1} is inside it and quiet = 2 is past it, for every wall-clock
    # phase. Do NOT lower this to the module's QUIET — that reintroduces the
    # flake. The other QUIET sleeps in this file are safe because they assert
    # absence for lifecycle/parking reasons, not because of the debounce.
    DEBOUNCE_S = 1.5

    async def test_quiet_debounce_reschedules_at_the_deadline(self):
        self.coord.quiet_s = self.DEBOUNCE_S
        con = self.connect()
        con.execute("UPDATE interface_input_state SET last_human_input_at="
                    "datetime('now') WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        self.coord.start(asyncio.get_running_loop())
        self.add_message("task")
        self.coord.notify_binding(self.binding)
        await asyncio.sleep(self.DEBOUNCE_S / 2)
        self.assertEqual(self.writes, [], "inside the debounce: no byte")
        ok = await self.wait_for(lambda: len(self.writes) == 1,
                                 timeout=self.DEBOUNCE_S * 5)
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
        self._seed()

    def _seed(self):
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

    def _rebuild(self):
        """A fresh DB at the same path, so one test can run several
        independent gate cases without their mutations composing."""
        self.db_path.unlink()
        build_engine_db(self.db_path)
        self._seed()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        # rmtree alone; the glob-unlink pass that used to precede it was both
        # redundant and racy in the same way (is_file then unlink).
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

    def test_arm_requires_a_live_sprint(self):
        """H-1 at the arm gate: each operand refuses ALONE, and the prose line
        refuses nothing. The undeclared-board case is the ordering the spec
        creates — declare the board, then arm — and it was previously armable,
        which is how a planner ended up bound to a sprint with no units.
        """
        cases = (
            ("frozen", "UPDATE documents SET frozen=1 WHERE document_id=1",
             False),
            ("retitled", "UPDATE documents SET title='Retro' "
                         "WHERE document_id=1", False),
            ("no board declared", "DELETE FROM sprint_units "
                                  "WHERE sprint_doc_id=1", False),
            ("status line says CLOSED",
             "UPDATE documents SET body='# S\nstatus: CLOSED' "
             "WHERE document_id=1", True),
        )
        for i, (label, mutation, arms) in enumerate(cases):
            with self.subTest(case=label):
                self._rebuild()
                con = sqlite3.connect(self.db_path)
                con.execute(mutation)
                con.commit()
                con.close()
                status, body = self.arm(key=f"k-live-{i}")
                if arms:
                    self.assertEqual(status, 201, body)
                else:
                    self.assertEqual(status, 409, body)
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
