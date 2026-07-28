#!/usr/bin/env python3
"""Deterministic submit — spec #62 Stage 0, units U1 (delivery) + U2 (watch).

The defect under test is a coin-flip, not a crash: `body + \\r` sent as one
rapid tmux burst is classified as a PASTE by the harness TUI, and
paste-classified input bypasses keybinding resolution — so the `\\r` inserts a
newline instead of submitting, and the engine's `input_ack` ("bytes committed
to tmux") cannot tell the difference. Every test here therefore asserts on the
SHAPE OF THE DELIVERY (what bytes went out, in how many calls, in what order,
with what between them) rather than on a return value, because the return value
was never wrong.

Layout:
  DeliveryShapeTest      — the three frame shapes, wrap/no-wrap, settle ordering
  SubmitWatchTest        — arm, confirm, bounded retries, exhaustion surface
  CapabilityGateTest     — seats that can never confirm never get a watch
  ConstantOrderingTest   — the load-bearing constant order (decision #107)

Run:
    python3 -m pytest tests/test_deterministic_submit.py
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
TESTS = Path(__file__).resolve().parent

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(TESTS))
import interface_broker  # noqa: E402
import interface_runtime  # noqa: E402
from test_interface_crash_window import build_engine_db  # noqa: E402


class _Recorder:
    """Interleaved log of what the writer did, in the order it did it.

    Records the tmux byte-sends AND the settle sleeps into ONE list, because
    every property in DeliveryShapeTest is about their relative order: "the CR
    left in its own call, after a wait" is a statement about interleaving. A
    test that recorded only the sends could not tell a settled submit from a
    burst, which is precisely the bug.
    """

    def __init__(self):
        self.log: list[tuple] = []

    def send(self, pane_id: str, payload: bytes) -> None:
        self.log.append(("send", payload))

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))

    @property
    def sends(self) -> list[bytes]:
        return [p for kind, p in self.log if kind == "send"]

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.log]


def _runtime(tmp: Path):
    """A runtime with no tmux, no sidecar and no DB rows — this file's unit
    tests inject at `_send_keys_sync` and at the mode read, which is every
    external edge `_deliver_frame_sync` touches."""
    return interface_runtime.InterfaceRuntime(
        str(tmp / "shell_db.db"), run_dir=str(tmp / "run"))


class DeliveryShapeTest(unittest.TestCase):
    """D1: mode-aware two-phase delivery."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = _runtime(self.tmp)
        self.gen = interface_runtime.Generation(self.rt, 1, 1, 1, 24, 80)
        self.gen.pane_id = "%0"
        # Capability is pre-resolved so these tests are about DELIVERY SHAPE
        # only; the gate itself is CapabilityGateTest's subject. There is no
        # loop here either, so arming is a no-op — deliberately, since the
        # shape must not depend on whether a watch could be armed.
        self.gen.submit_confirmable = True
        self.rec = _Recorder()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _deliver(self, payload: bytes, mode: str = "off"):
        with (
            mock.patch.object(self.rt, "_send_keys_sync", self.rec.send),
            mock.patch.object(self.rt, "_bracketed_paste_sync",
                              return_value=mode),
            mock.patch.object(interface_runtime.time, "sleep", self.rec.sleep),
        ):
            self.rt._deliver_frame_sync(self.gen, payload)

    # -- the split itself ---------------------------------------------------

    def test_body_and_submit_are_separate_calls_with_a_settle_between(self):
        self._deliver(b"hello\r")
        self.assertEqual(self.rec.kinds, ["send", "sleep", "send"],
                         "body and submit must not ride one burst")
        self.assertEqual(self.rec.sends, [b"hello", b"\r"])

    def test_settle_uses_the_configured_delay(self):
        with mock.patch.object(interface_runtime, "SUBMIT_SETTLE_MS", 400):
            self._deliver(b"hello\r")
        self.assertEqual(
            [s for k, s in self.rec.log if k == "sleep"], [0.4],
            "SUBMIT_SETTLE_MS must be read at call time, not bound at import")

    def test_no_trailing_cr_is_forwarded_verbatim(self):
        # A raw terminal keystroke. Splitting or wrapping it would change what
        # the operator typed, and there is no submit of ours to settle for.
        self._deliver(b"abc")
        self.assertEqual(self.rec.kinds, ["send"])
        self.assertEqual(self.rec.sends, [b"abc"])

    def test_bare_cr_is_forwarded_verbatim(self):
        # The operator tapping Enter to answer a TUI dialog: body is empty, so
        # there is nothing to wrap and nothing to wait for. Wrapping an empty
        # body would send paste markers around nothing, and settling would
        # delay a keystroke the operator is watching for.
        self._deliver(b"\r")
        self.assertEqual(self.rec.kinds, ["send"])
        self.assertEqual(self.rec.sends, [b"\r"])

    def test_bare_cr_never_reads_the_mode(self):
        # Guards the shape, not just the output: an implementation that asked
        # the sidecar first and then discarded the answer would pass the test
        # above while putting a blocking round-trip in front of every dialog
        # keystroke.
        with (
            mock.patch.object(self.rt, "_send_keys_sync", self.rec.send),
            mock.patch.object(self.rt, "_bracketed_paste_sync") as mode,
            mock.patch.object(interface_runtime.time, "sleep", self.rec.sleep),
        ):
            self.rt._deliver_frame_sync(self.gen, b"\r")
            self.rt._deliver_frame_sync(self.gen, b"abc")
        mode.assert_not_called()

    def test_empty_payload_sends_nothing_new(self):
        # Not a shape the composer produces, but the verbatim branch must not
        # invent bytes for it either.
        self._deliver(b"")
        self.assertEqual(self.rec.sends, [b""])

    # -- the wrap -----------------------------------------------------------

    def test_wraps_body_when_bracketed_paste_is_active(self):
        self._deliver(b"hello\r", mode="on")
        self.assertEqual(
            self.rec.sends,
            [b"\x1b[200~hello\x1b[201~", b"\r"],
            "an active 2004 pane must receive real paste markers")

    def test_does_not_wrap_when_inactive(self):
        self._deliver(b"hello\r", mode="off")
        self.assertEqual(self.rec.sends, [b"hello", b"\r"])

    def test_unknown_mode_degrades_to_no_wrap(self):
        # A shadow that is dead, restarting, or behind reads 'unknown'. The
        # separate-CR phase alone is still strictly better than today's single
        # burst, so 'unknown' must deliver — never refuse, never guess 'on'
        # (wrapping a pane that is NOT in paste mode would put literal
        # `ESC[200~` into the prompt).
        self._deliver(b"hello\r", mode="unknown")
        self.assertEqual(self.rec.sends, [b"hello", b"\r"])

    def test_multiline_body_keeps_its_newlines_inside_the_wrap(self):
        # The multiline-composer fix: under a wrap every byte of the body is
        # DATA, so the interior newlines cannot submit early. Exactly one CR
        # leaves the writer, and it leaves alone.
        self._deliver(b"line one\nline two\nline three\r", mode="on")
        body, submit = self.rec.sends
        self.assertEqual(
            body, b"\x1b[200~line one\nline two\nline three\x1b[201~")
        self.assertEqual(submit, b"\r")
        self.assertEqual(self.rec.sends.count(b"\r"), 1,
                         "interior newlines must not become extra submits")

    def test_only_the_trailing_cr_is_split_off(self):
        # A body that itself ends in CRLF-ish content: exactly one byte comes
        # off the end, and the rest is delivered untouched.
        self._deliver(b"keep\r\r", mode="off")
        self.assertEqual(self.rec.sends, [b"keep\r", b"\r"])


class _WriterWiringTest(unittest.TestCase):
    """Both injected writers route through the two-phase path.

    The point is coverage of the ROUTE, not of the split: a fix applied to one
    writer and not the other leaves the wake path — the path this sprint's
    success criterion is about — on the old burst delivery, and every
    delivery-shape test above would still pass.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO interface_generations (shell_id, generation) "
                    "VALUES (1,1)")
        self.sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (1,1,'occupied','idle')").lastrowid
        con.execute("INSERT INTO interface_input_state (session_id, shell_id,"
                    " generation, composer) VALUES (?,1,1,'clean')",
                    (self.sid,))
        con.commit()
        self.lease_id = interface_broker.acquire_writer(
            con, self.sid, "tab-1", "tok-1")
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_human_writer_delivers_in_two_phases(self):
        async def flow():
            rt = _runtime(self.tmp)
            rt.loop = asyncio.get_running_loop()
            gen = interface_runtime.Generation(rt, self.sid, 1, 1, 24, 80)
            gen.pane_id = "%0"
            rt.generations[self.sid] = gen
            rec = _Recorder()

            client = mock.Mock(session_id=self.sid, role="writer",
                              client_id="tab-1", lease_id=self.lease_id,
                              lease_token="tok-1")
            with (
                mock.patch.object(rt, "_send_keys_sync", rec.send),
                mock.patch.object(rt, "_bracketed_paste_sync",
                                  return_value="off"),
                mock.patch.object(interface_runtime.time, "sleep", rec.sleep),
            ):
                await rt._do_human_input(
                    gen, interface_runtime.HumanInput(
                        client, 1, b"composed\r"))
            # The real broker accepted it (ack sent), and the bytes left in
            # two phases rather than one.
            self.assertEqual(rec.kinds, ["send", "sleep", "send"])
            self.assertEqual(rec.sends, [b"composed", b"\r"])
        asyncio.run(flow())

    def test_wake_writer_delivers_in_two_phases(self):
        async def flow():
            rt = _runtime(self.tmp)
            rt.loop = asyncio.get_running_loop()
            gen = interface_runtime.Generation(rt, self.sid, 1, 1, 24, 80)
            gen.pane_id = "%0"
            rt.generations[self.sid] = gen
            rec = _Recorder()

            payload = interface_broker.WAKE_PROMPT.encode() + b"\r"
            writer = rt.wake_writer(self.sid)
            with (
                mock.patch.object(rt, "_send_keys_sync", rec.send),
                mock.patch.object(rt, "_bracketed_paste_sync",
                                  return_value="on"),
                mock.patch.object(interface_runtime.time, "sleep", rec.sleep),
                # The wake writer's own preflight shells out to tmux; this
                # test is about delivery shape, not about the preflight.
                mock.patch.object(interface_runtime.subprocess, "run"),
            ):
                await asyncio.to_thread(writer, len(payload))
            self.assertEqual(rec.kinds, ["send", "sleep", "send"])
            self.assertEqual(
                rec.sends,
                [interface_runtime.PASTE_START
                 + interface_broker.WAKE_PROMPT.encode()
                 + interface_runtime.PASTE_END,
                 b"\r"],
                "the wake writer must adopt the identical delivery")
        asyncio.run(flow())


class _WatchFixture(unittest.TestCase):
    """A live runtime + generation on a real loop, with tmux stubbed.

    The watch is loop state driven by a hook that arrives from another thread,
    so these run the real task against a real event loop and a real
    asyncio.Event. Time is compressed by patching the thresholds rather than
    by sleeping through 3s waits — the property is the SEQUENCE of what
    happens per elapsed window, never the wall-clock size of the window.
    """

    HARNESS = "claude"
    CLI_VERSION = "2.1.217"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO interface_generations (shell_id, generation) "
                    "VALUES (1,1)")
        self.sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle, harness, cli_version) "
            "VALUES (1,1,'occupied','idle',?,?)",
            (self.HARNESS, self.CLI_VERSION)).lastrowid
        con.execute("INSERT INTO interface_input_state (session_id, shell_id,"
                    " generation, composer) VALUES (?,1,1,'clean')",
                    (self.sid,))
        con.commit()
        con.close()
        self.rec = _Recorder()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self):
        rt = _runtime(self.tmp)
        rt.db_path = str(self.db)
        rt.loop = asyncio.get_running_loop()
        gen = interface_runtime.Generation(rt, self.sid, 1, 1, 24, 80)
        gen.pane_id = "%0"
        rt.generations[self.sid] = gen
        client = mock.Mock()
        client.controls = []
        client.send_control = client.controls.append
        gen.clients.add(client)

        # Started here and stopped at CLEANUP, not scoped to the delivery
        # call: the watch is a task that OUTLIVES the send, and its retries
        # fire seconds later. A `with` block around delivery alone expires
        # before then and the retries reach the real tmux — which is how this
        # fixture was wrong the first time, quietly shelling out per retry.
        for patcher in (
            mock.patch.object(rt, "_send_keys_sync", self.rec.send),
            mock.patch.object(rt, "_bracketed_paste_sync",
                              return_value="off"),
            mock.patch.object(interface_runtime.time, "sleep", self.rec.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return rt, gen, client

    async def _deliver(self, rt, gen, payload=b"hello\r", seq=1):
        """Deliver a frame through the real writer path, off the loop thread
        exactly as the broker does."""
        await asyncio.to_thread(rt._deliver_frame_sync, gen, payload, seq=seq)

    @staticmethod
    def _types(client):
        return [c.get("type") for c in client.controls]


class SubmitWatchTest(_WatchFixture):
    """D2: the watch, its bounded retries, and the two outcomes."""

    def test_confirmation_ends_the_watch_with_one_message(self):
        async def flow():
            rt, gen, client = self._make()
            with mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 5):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                self.assertIsNotNone(watch, "watch must be armed")
                # Held in a local: a resolving watch clears gen.submit_watch
                # in its own `finally`, so re-reading it here would race the
                # very completion this test is waiting on.
                # The hook route's notify, from a non-loop thread as in life.
                await asyncio.to_thread(rt.notify_submit, self.sid)
                await asyncio.wait_for(watch.task, timeout=5)
            self.assertEqual(self._types(client), ["submit_confirmed"])
            self.assertEqual(client.controls[0]["seq"], 1)
            self.assertEqual(
                self.rec.sends, [b"hello", b"\r"],
                "a confirmed submit must not press Enter again")
        asyncio.run(flow())

    def test_retries_then_reports_pending_when_no_hook_arrives(self):
        async def flow():
            rt, gen, client = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 2),
            ):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                await asyncio.wait_for(watch.task, timeout=5)
            # Exactly the budget: the frame's own CR, then two bare-CR
            # retries, and no more.
            self.assertEqual(self.rec.sends, [b"hello", b"\r", b"\r", b"\r"])
            self.assertEqual(self._types(client), ["submit_pending"])
            self.assertEqual(client.controls[0]["seq"], 1)
        asyncio.run(flow())

    def test_retry_budget_is_honoured_exactly(self):
        # Distinguishes "bounded" from "bounded at the number we meant": a
        # loop with an off-by-one still terminates and still reports pending.
        for budget in (0, 1, 3):
            with self.subTest(retries=budget):
                self.rec = _Recorder()

                async def flow():
                    rt, gen, client = self._make()
                    with (
                        mock.patch.object(
                            interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                        mock.patch.object(
                            interface_runtime, "SUBMIT_RETRIES", budget),
                    ):
                        await self._deliver(rt, gen)
                        watch = gen.submit_watch
                        await asyncio.wait_for(watch.task, timeout=5)
                    bare = self.rec.sends[1:]
                    self.assertEqual(bare, [b"\r"] * (budget + 1),
                                     "one submit CR plus exactly `budget` "
                                     "retries")
                asyncio.run(flow())

    def test_late_hook_during_the_retry_window_still_confirms(self):
        # The spec's "retry lands after a real-but-late hook" case, from the
        # other side: a hook that arrives mid-budget must stop the retries and
        # confirm — not be ignored because retrying had already begun.
        async def flow():
            rt, gen, client = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.2),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 5),
            ):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                await asyncio.sleep(0.3)     # at least one retry has fired
                self.assertGreaterEqual(watch.attempts, 1,
                                        "test did not reach the retry window")
                await asyncio.to_thread(rt.notify_submit, self.sid)
                await asyncio.wait_for(watch.task, timeout=5)
            self.assertEqual(self._types(client), ["submit_confirmed"])
            self.assertLess(self.rec.sends.count(b"\r"), 6,
                            "retries must stop at the hook, not run the "
                            "budget out anyway")
        asyncio.run(flow())

    def test_exhaustion_records_its_own_row_for_a_human_frame(self):
        # No wake batch is in flight, so this is NOT H-27's condition and must
        # not be written into H-27's reason (decision #107's second half).
        async def flow():
            rt, gen, _ = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 1),
            ):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                await asyncio.wait_for(watch.task, timeout=5)
        asyncio.run(flow())
        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT reason, severity, detail FROM planner_alerts "
            "WHERE session_id=? AND resolved_at IS NULL", (self.sid,)
        ).fetchall()
        con.close()
        self.assertEqual([r[0] for r in rows], ["submit_unconfirmed"])
        self.assertEqual(rows[0][1], "info")
        self.assertIn("prompt_submit", rows[0][2])
        self.assertIn("retry", rows[0][2])

    def test_a_delivered_frame_is_never_parked_by_the_watch(self):
        # The whole point of submit_pending over a halt: the bytes ARE on
        # screen. Nothing about exhausting the retries may park the session or
        # revoke the writer.
        async def flow():
            rt, gen, _ = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 1),
            ):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                await asyncio.wait_for(watch.task, timeout=5)
        asyncio.run(flow())
        con = sqlite3.connect(self.db)
        lifecycle, = con.execute(
            "SELECT lifecycle FROM interface_sessions WHERE session_id=?",
            (self.sid,)).fetchone()
        revoked = con.execute(
            "SELECT COUNT(*) FROM interface_writer_leases "
            "WHERE session_id=? AND revoked_at IS NOT NULL",
            (self.sid,)).fetchone()[0]
        con.close()
        self.assertNotEqual(lifecycle, "lost")
        self.assertEqual(revoked, 0)

    def test_a_failed_retry_write_does_not_raise_or_add_a_surface(self):
        # tmux refuses the retry (a window killed mid-watch, say). The frame's
        # own bytes already landed, so the honest end state is unchanged:
        # one submit_pending, no exception escaping the task.
        async def flow():
            rt, gen, client = self._make()

            def refuse(pane_id, payload):
                raise RuntimeError("no such window")

            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 2),
            ):
                await self._deliver(rt, gen)
                # Only the RETRIES refuse; the frame's own bytes already
                # landed above, which is the state this test is about.
                watch = gen.submit_watch
                with mock.patch.object(rt, "_send_keys_sync", refuse):
                    await asyncio.wait_for(watch.task, timeout=5)
            self.assertEqual(self._types(client), ["submit_pending"])
        asyncio.run(flow())

    def test_a_superseded_frame_does_not_report_pending(self):
        # A second frame arrives while the first is still unconfirmed. Only
        # the live frame's outcome is reported; the abandoned watch must go
        # quietly rather than emit a submit_pending the operator has already
        # moved past.
        async def flow():
            rt, gen, client = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 10),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 0),
            ):
                await self._deliver(rt, gen, b"first\r", seq=1)
                first = gen.submit_watch
                await self._deliver(rt, gen, b"second\r", seq=2)
                second = gen.submit_watch
                self.assertIsNot(first, second)
                await asyncio.sleep(0)
                self.assertTrue(first.task.cancelled()
                                or first.task.done())
                await asyncio.to_thread(rt.notify_submit, self.sid)
                await asyncio.wait_for(second.task, timeout=5)
            self.assertEqual(self._types(client), ["submit_confirmed"])
            self.assertEqual(client.controls[0]["seq"], 2)
        asyncio.run(flow())

    def test_teardown_cancels_a_live_watch(self):
        # A pane that is going away cannot submit, and a retry into a killed
        # window is a tmux error nobody asked for.
        async def flow():
            rt, gen, _ = self._make()
            # teardown also disposes the shadow generation; there is no
            # sidecar process here and that is not this test's subject.
            rt.shadow = mock.Mock()
            with mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 10):
                await self._deliver(rt, gen)
                task = gen.submit_watch.task
                await gen.teardown(kill_window=False)
                await asyncio.sleep(0)
                self.assertTrue(task.cancelled() or task.done())
                self.assertIsNone(gen.submit_watch)
        asyncio.run(flow())


class WakeBatchExhaustionTest(_WatchFixture):
    """Decision #107: exhaustion under a live wake batch feeds H-27's ROW.

    One condition must not produce two alerts. The watch observes it at ~6s
    and H-27 at 60s, so if these two wrote different reasons — or the same
    reason with different refs — the operator would see the same fault
    reported twice, which is exactly the class decision #106 forbids.
    """

    def _arm_batch(self) -> int:
        con = sqlite3.connect(self.db)
        doc_id = con.execute(
            "INSERT INTO documents (title, kind, body) "
            "VALUES ('sprint','spec','x')").lastrowid
        binding_id = con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
            " generation) VALUES (?,1,?,1,1)",
            (doc_id, self.sid)).lastrowid
        # submitting_at, NOT submitted_at: H-27 measures the wait that STARTS
        # at the submitting commit (migration 0117), and treats a NULL there
        # as unmeasured rather than as zero seconds ago. Seeding the wrong
        # column makes H-27 silently skip the batch, which reads exactly like
        # "it deduped correctly".
        batch_id = con.execute(
            "INSERT INTO planner_wake_batches "
            "(binding_id, shell_id, generation, state, submitting_at) "
            "VALUES (?,1,1,'submitting',datetime('now'))",
            (binding_id,)).lastrowid
        con.commit()
        con.close()
        return batch_id

    def _open_alerts(self):
        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT reason, batch_id, dedupe_key, detail FROM planner_alerts "
            "WHERE resolved_at IS NULL ORDER BY alert_id").fetchall()
        con.close()
        return rows

    def _exhaust(self):
        async def flow():
            rt, gen, client = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 2),
            ):
                await self._deliver(rt, gen)
                watch = gen.submit_watch
                await asyncio.wait_for(watch.task, timeout=5)
            return client
        return asyncio.run(flow())

    def test_exhaustion_opens_h27s_reason_on_h27s_batch(self):
        batch_id = self._arm_batch()
        self._exhaust()
        rows = self._open_alerts()
        self.assertEqual(len(rows), 1, f"expected one row, got {rows}")
        reason, row_batch, _, detail = rows[0]
        self.assertEqual(reason, "hooks_declared_but_silent")
        self.assertEqual(row_batch, batch_id,
                         "the row must be scoped to the batch, so H-27's own "
                         "later observation lands on it")
        self.assertIn("retries", detail)

    def test_h27_running_afterwards_refreshes_rather_than_duplicates(self):
        # The real sequence in a dead-hook seat: the watch exhausts at ~6s,
        # then H-27's 60s threshold falls due on the same batch. The second
        # observation must find the first row — one condition, one row.
        batch_id = self._arm_batch()
        self._exhaust()
        con = sqlite3.connect(self.db)
        binding_id, = con.execute(
            "SELECT binding_id FROM planner_wake_batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        interface_broker.hooks_silence_alert(con, binding_id, submit_s=0.0)
        con.commit()
        con.close()

        rows = self._open_alerts()
        self.assertEqual(
            len(rows), 1,
            f"H-27 minted a second row for one condition: {rows}")
        self.assertIn("submitted", rows[0][3],
                      "the surviving row's detail should have refreshed to "
                      "H-27's later measurement")

    def test_the_two_measurements_agree_on_the_dedupe_key(self):
        # The structural version of the test above: if these keys ever differ,
        # the dedupe index cannot collapse them no matter what order they run
        # in. Asserted directly so a refactor that inlines one of the two
        # _alert calls fails here rather than in production.
        batch_id = self._arm_batch()
        self._exhaust()
        watch_key = self._open_alerts()[0][2]

        con = sqlite3.connect(self.db)
        con.execute("UPDATE planner_alerts SET resolved_at=datetime('now')")
        binding_id, = con.execute(
            "SELECT binding_id FROM planner_wake_batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        con.commit()
        interface_broker.hooks_silence_alert(con, binding_id, submit_s=0.0)
        con.commit()
        con.close()

        h27_key = self._open_alerts()[0][2]
        self.assertEqual(watch_key, h27_key,
                         "the repair and the backstop must key the same row")


class CapabilityGateTest(_WatchFixture):
    """D2 gating: a seat that can never confirm never gets a watch.

    vibe and opencode have no hook adapter at all. Arming there would press
    Enter twice into a healthy pane and then report every correct send as
    delivered-not-submitted — a monitor that lies on the happy path.
    """

    HARNESS = "vibe"
    CLI_VERSION = None

    def test_no_watch_is_armed_without_a_prompt_submit_hook(self):
        async def flow():
            rt, gen, client = self._make()
            with (
                mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S", 0.05),
                mock.patch.object(interface_runtime, "SUBMIT_RETRIES", 2),
            ):
                await self._deliver(rt, gen)
                self.assertIsNone(gen.submit_watch)
                await asyncio.sleep(0.3)   # past the whole retry budget
            # Delivery is unchanged; nothing pressed Enter again and no
            # message claimed anything about submission either way.
            self.assertEqual(self.rec.sends, [b"hello", b"\r"])
            self.assertEqual(self._types(client), [])
        asyncio.run(flow())

    def test_unconfirmable_seat_raises_no_alert(self):
        async def flow():
            rt, gen, _ = self._make()
            with mock.patch.object(interface_runtime, "SUBMIT_CONFIRM_S",
                                   0.05):
                await self._deliver(rt, gen)
                await asyncio.sleep(0.3)
        asyncio.run(flow())
        con = sqlite3.connect(self.db)
        count = con.execute("SELECT COUNT(*) FROM planner_alerts").fetchone()[0]
        con.close()
        self.assertEqual(count, 0,
                         "a seat with no hook adapter is not a fault")

    def test_capability_is_resolved_once_per_generation(self):
        # The read sits inside the writer, between the submit CR and the
        # caller's ack. Doing it per frame would put a DB round trip on every
        # message for an answer that cannot change under a live generation.
        async def flow():
            rt, gen, _ = self._make()
            with mock.patch.object(
                    rt, "_read_submit_capability",
                    wraps=rt._read_submit_capability) as read:
                await self._deliver(rt, gen, seq=1)
                await self._deliver(rt, gen, seq=2)
                await self._deliver(rt, gen, seq=3)
            self.assertEqual(read.call_count, 1)
        asyncio.run(flow())


class ArmingIsNeverFatalTest(_WatchFixture):
    """Arming runs AFTER the frame's bytes are all on the wire.

    An exception escaping it would propagate through the broker's two-phase
    commit, which reads any writer exception as an ambiguous write and parks
    the session `delivery_unknown` — recording a frame that demonstrably
    delivered as maybe-delivered, over an observability lookup.
    """

    def test_capability_lookup_failure_does_not_fail_the_frame(self):
        async def flow():
            rt, gen, client = self._make()

            def boom(session_id):
                raise sqlite3.OperationalError("no such table")

            with mock.patch.object(rt, "_read_submit_capability", boom):
                # Must not raise: the bytes below already went out.
                await self._deliver(rt, gen)
            self.assertEqual(self.rec.sends, [b"hello", b"\r"])
            self.assertIsNone(gen.submit_watch)
            self.assertEqual(self._types(client), [])
        asyncio.run(flow())


class ConstantOrderingTest(unittest.TestCase):
    """Decision #107: the submit watch is the REPAIR, H-27 is the BACKSTOP.

    Two measurements of ONE condition ("the engine pressed Enter and no
    prompt_submit answered") at two timescales. The order is load-bearing: the
    watch must have spent all its retries BEFORE H-27's batch-silence
    threshold falls due, or the backstop reports a condition the repair is
    still working on and the operator sees one fault twice.

    This is a pin, not a calculation — it fails on a config that reorders them,
    which is the only way that invariant can break.
    """

    def test_retries_finish_before_the_backstop_reports(self):
        watch_window = (interface_runtime.SUBMIT_RETRIES
                        * interface_runtime.SUBMIT_CONFIRM_S)
        self.assertLess(
            watch_window, interface_broker.HOOKS_SUBMIT_SILENT_S,
            f"the submit watch spends up to {watch_window}s but H-27 reports "
            f"batch silence at {interface_broker.HOOKS_SUBMIT_SILENT_S}s — the "
            "backstop would alert on a condition the repair has not finished "
            "attempting (decision #107)")

    def test_shipped_defaults_are_the_ruled_ones(self):
        # The FnB's QAQC ruling (spec #62 open question 1) settled these
        # numbers explicitly: ON by default, 2 retries x 3s. A silent change
        # to the defaults is a change to a ruled decision.
        self.assertEqual(interface_runtime.SUBMIT_RETRIES, 2)
        self.assertEqual(interface_runtime.SUBMIT_CONFIRM_S, 3.0)
        self.assertEqual(interface_runtime.SUBMIT_SETTLE_MS, 250.0)


if __name__ == "__main__":
    unittest.main()
