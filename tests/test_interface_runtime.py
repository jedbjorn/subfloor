#!/usr/bin/env python3
"""Interface runtime tests (spec #20, sprint 25 seq 5 vertical slice).

Unit tests run hermetic WITHOUT tmux/node: availability gating, ticket
mint/consume discipline, reject-reason mapping, PID-reuse identity, the
reattach-lost callback wiring, and writer-lease liveness (seq 6: fenced
detach revoke, dead-lease sweep, durable heartbeat stamps). The sidecar tests need node only (a dead or
silent sidecar must fail fast, never hang — sprint 25 flag #45).
Integration tests are gated on tmux + node + the @xterm/headless module
(tmux+node alone is NOT sufficient — the sidecar dies on require without
it); they drive a real private tmux server against a stub command and prove
the durable input path end to end: ordered human input → byte-exact echo,
duplicate replay, seq-gap rejection, reconnect redraw, reattach-after-
restart, graceful terminate, and real pane death → lost/unreconciled.

Run:
    python3 tests/test_interface_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
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

HAS_TMUX = shutil.which("tmux") is not None
HAS_NODE = shutil.which("node") is not None


def _shadow_module_present() -> bool:
    """@xterm/headless must resolve for the sidecar — tmux+node alone is NOT
    enough (CI runners carry both but not the module, and a sidecar that
    dies on require used to hang the whole suite: sprint 25 flag #45)."""
    for base in (interface_runtime.SHADOW_NODE_PATH,
                 str(interface_runtime.SHADOW_DIR / "node_modules")):
        if (Path(base) / "@xterm" / "headless").is_dir():
            return True
    return False


HAS_SHADOW_STACK = HAS_TMUX and HAS_NODE and _shadow_module_present()


class FakeClient:
    """The runtime's client duck type, capturing everything sent."""

    def __init__(self, session_id, role="viewer", client_id="c-1",
                 lease_id=None, lease_token=None):
        self.session_id = session_id
        self.role = role
        self.client_id = client_id
        self.lease_id = lease_id
        self.lease_token = lease_token
        self.last_hb = time.monotonic()
        self.hb_stale = False
        self.controls = []
        self.outputs = []
        self.redraws = []
        self.closed = None

    def send_control(self, msg):
        self.controls.append(msg)

    def send_output(self, data):
        self.outputs.append(data)

    def send_redraw(self, data):
        self.redraws.append(data)

    def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def by_type(self, mtype):
        return [m for m in self.controls if m.get("type") == mtype]


async def wait_for(pred, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class WedgedTmuxTimeoutTest(unittest.TestCase):
    """SC-013 (sprint 25 seq 8): every wake-path SYNC tmux call — the
    unmanaged-client probe, the writer preflight, _send_keys_sync — is
    timeout-bounded. A wedged-but-alive tmux (socket accepts, never
    answers) must raise / fail closed fast, never hang the broker drain
    thread (a hang strands the batch and, worse, used to stall it while
    the gate held the SQLite write lock). Hermetic: a stub `tmux` that
    sleeps forever, first on PATH, with the timeout constant patched
    down for speed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        stub = bindir / "tmux"
        stub.write_text("#!/bin/sh\nexec sleep 3600\n")
        stub.chmod(0o755)
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))
        self.rt.sock = str(self.tmp / "tmux.sock")
        gen = mock.Mock()
        gen.terminated = False
        gen.pane_id = "%1"
        self.rt.generations = {1: gen}
        self._env = mock.patch.dict(
            os.environ,
            {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
        self._env.start()
        self._tmo = mock.patch.object(
            interface_runtime, "TMUX_SYNC_TIMEOUT_S", 0.5)
        self._tmo.start()

    def tearDown(self):
        self._tmo.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_timeout_fails_closed_never_hangs(self):
        start = time.monotonic()
        # Unreachable/wedged tmux is NOT 'unmanaged' — the writer preflight
        # owns that failure as definite pre-send (decision #32) — but the
        # call must RETURN, not hang.
        self.assertFalse(self.rt.unmanaged_writable_client(1))
        self.assertLess(time.monotonic() - start, 5)

    def test_wake_preflight_timeout_is_definite_pre_send(self):
        writer = self.rt.wake_writer(1)
        with self.assertRaises(interface_broker.PreSendError):
            writer(len(interface_broker.WAKE_PROMPT) + 1)

    def test_send_keys_timeout_raises_never_hangs(self):
        # send-keys hangs AFTER bytes may have moved: TimeoutExpired must
        # propagate (ambiguous → the broker parks delivery_unknown + alerts),
        # never hang the worker thread.
        with self.assertRaises(subprocess.TimeoutExpired):
            self.rt._send_keys_sync("%1", b"x")


# ------------------------------------------------------------------ unit (no tmux)

class AvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runtime(self, shadow_script=None):
        return interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"),
            shadow_script=shadow_script)

    def test_late_runtime_alert_keeps_ended_session_audit_resolved(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations "
            "(shell_id, generation, ended_at) "
            "VALUES (1,1,'2026-07-20 00:00:00')")
        sid = con.execute(
            "INSERT INTO interface_sessions "
            "(shell_id, generation, occupancy, lifecycle, ended_at) "
            "VALUES (1,1,'ended','ended','2026-07-20 00:00:00')"
        ).lastrowid
        con.execute(
            "INSERT INTO planner_alerts "
            "(alert_id, session_id, severity, reason, dedupe_key) "
            "VALUES (42,?,'warning','interface_continuity_broken',"
            "? || '|-|-|interface_continuity_broken')", (sid, sid))
        con.commit()
        con.close()

        rt = self._runtime()
        rt._alert(sid, "interface_continuity_broken", "warning")
        rt._alert(sid, "interface_continuity_broken", "warning")

        con = sqlite3.connect(self.db)
        try:
            alerts = con.execute(
                "SELECT alert_id, reason, resolved_at FROM planner_alerts "
                "WHERE session_id=?", (sid,)).fetchall()
        finally:
            con.close()
        self.assertEqual(alerts, [
            (42, "interface_continuity_broken", "2026-07-20 00:00:00")
        ])

    def test_no_tmux_marks_unavailable(self):
        with mock.patch.object(interface_runtime.shutil, "which",
                               return_value=None):
            rt = self._runtime()

            async def flow():
                await rt.start()
                self.assertFalse(rt.available)
                self.assertIn("tmux", rt.unavailable_reason)
                with self.assertRaises(interface_runtime.InterfaceUnavailable):
                    await rt.spawn(
                        session_id=1, shell_id=1, generation=1,
                        worktree=str(self.tmp), sc_path="/bin/sc",
                        token_path="/tmp/tok", rows=24, cols=80)
                with self.assertRaises(interface_runtime.InterfaceUnavailable):
                    await rt.terminate(1)
                with self.assertRaises(interface_runtime.InterfaceUnavailable):
                    await rt.reattach_all([])

            asyncio.run(flow())

    def test_old_tmux_rejected(self):
        sidecar = self.tmp / "sidecar.js"
        sidecar.touch()
        rt = self._runtime(shadow_script=str(sidecar))
        with mock.patch.object(interface_runtime.shutil, "which",
                               return_value="/usr/bin/x"), \
                mock.patch.object(interface_runtime, "_tmux_version",
                                  return_value=(3, 3)):
            reason = rt._check_available()
        self.assertIsNotNone(reason)
        self.assertIn("3.3", reason)

    def test_missing_sidecar_names_incomplete_materialize(self):
        sidecar = self.tmp / "missing-sidecar.js"
        rt = self._runtime(shadow_script=str(sidecar))
        with mock.patch.object(interface_runtime.shutil, "which",
                               return_value="/usr/bin/x"):
            reason = rt._check_available()
        self.assertIn(str(sidecar), reason)
        self.assertIn("engine materialize is incomplete", reason)

    def test_present_sidecar_keeps_runtime_available(self):
        sidecar = self.tmp / "sidecar.js"
        sidecar.touch()
        rt = self._runtime(shadow_script=str(sidecar))
        with mock.patch.object(interface_runtime.shutil, "which",
                               return_value="/usr/bin/x"), \
                mock.patch.object(interface_runtime, "_tmux_version",
                                  return_value=(3, 4)):
            self.assertIsNone(rt._check_available())

    def test_tmux_version_parse(self):
        cases = [("tmux 3.5a\n", (3, 5)), ("tmux 3.4\n", (3, 4)),
                 ("tmux next-3.6\n", None), ("garbage\n", None)]
        for text, expect in cases:
            with mock.patch.object(interface_runtime.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout=text)
                self.assertEqual(interface_runtime._tmux_version(), expect,
                                 f"parse of {text!r}")

    def test_start_ticks_parse(self):
        # comm may contain spaces and parens; field 22 follows the last ')'.
        stat_text = ("123 (weird ) name) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 "
                     "15 16 17 18 999888 20\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=stat_text)):
            self.assertEqual(interface_runtime._read_start_ticks(123), 999888)

    def test_pid_alive_requires_exact_ticks(self):
        # PID reuse: a live pid with DIFFERENT start ticks is not our process.
        stat_text = ("123 (x) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 "
                     "999888 20\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=stat_text)):
            self.assertTrue(interface_runtime._pid_alive(123, 999888))
            self.assertFalse(interface_runtime._pid_alive(123, 111))
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertFalse(interface_runtime._pid_alive(123, 999888))

    def test_start_walks_lost_reattach_through_callback(self):
        # An occupied session whose pane identity cannot verify is lost on
        # reattach; start() must hand it to the on_unexpected_exit callback
        # (the routes layer's occupied → lost/unreconciled transition), not
        # just log it (sprint 25 flag #40).
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle, tmux_pane_id, pane_pid, pane_start_ticks) VALUES "
            "(1,1,'occupied','idle','%999',424242,1)").lastrowid
        con.commit()
        con.close()
        rt = self._runtime()
        called = []
        rt.on_unexpected_exit = called.append
        with mock.patch.object(rt, "_check_available", return_value=None), \
                mock.patch.object(rt.shadow, "start", new=mock.AsyncMock()):
            async def flow():
                await rt.start()
                self.assertTrue(rt.available)
                self.assertEqual(called, [sid],
                                 "a lost reattach must fire the callback")
                await rt.stop()
            asyncio.run(flow())


# ------------------------------------------------------------------ shadow sidecar

@unittest.skipUnless(HAS_NODE, "node not installed")
class ShadowSidecarTest(unittest.TestCase):
    """Sidecar liveness (sprint 25 flag #45): a sidecar that dies on require
    or wedges mid-session must fail requests fast — never hang a caller on a
    future nothing resolves."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _script(self, text: str) -> str:
        p = self.tmp / "stub.js"
        p.write_text(text)
        return str(p)

    def test_silent_sidecar_times_out(self):
        # Answers nothing — requests must raise after the timeout, not hang.
        script = self._script(
            "require('readline').createInterface"
            "({input: process.stdin}).on('line', () => {});\n")

        # Same flag #106 correction as its dead-sidecar sibling below, applied
        # to the whole class rather than only the instance that reddened: the
        # `elapsed < 5` line was a wall-clock budget with a quiet-machine
        # assumption baked in. The property is that an unanswering sidecar
        # RAISES rather than hanging a caller forever, and the injected 0.5s
        # timeout — not the wall clock — is what bounds this test's runtime.
        # Asserting the timeout's own message proves the raise came from
        # wait_for expiring, which is the path this test exists to cover.
        async def flow():
            sidecar = interface_runtime.ShadowSidecar(script)
            with (
                mock.patch.object(
                    interface_runtime, "SHADOW_REQUEST_TIMEOUT_S", 0.5),
                self.assertRaises(RuntimeError) as caught,
            ):
                await sidecar.start()   # the boot probe times out
            self.assertIn("timed out", str(caught.exception))
            await sidecar.stop()
        asyncio.run(flow())

    def test_dead_sidecar_fails_probe_fast(self):
        # Dies instantly (what a missing @xterm/headless require does).
        script = self._script("process.exit(1)\n")

        # Flag #106: this test used to inject a 5s timeout and assert the probe
        # returned in under 5s — the budget and the timeout were the SAME
        # number, so it carried no headroom and reddened twice under sprint
        # load at 5.027s while the code was correct. "Fast" here does not mean
        # "under N seconds on a quiet machine"; it means the failure comes from
        # NOTICING THE PROCESS DIED rather than from waiting the request out.
        # Those are two different code paths raising two different messages, so
        # the discriminator is the message, and no clock enters the assertion:
        #   dead     -> _reader hits EOF -> "shadow sidecar exited"
        #   wedged   -> asyncio.wait_for -> "... timed out after Ns ..."
        # The injected timeout is deliberately far LARGER than any plausible
        # death-detection time, which is what makes the two outcomes
        # unmistakable: correct code never reaches it under any load, and a
        # regression that waits it out is reported as a wrong-path failure
        # rather than a slow pass.
        async def flow():
            sidecar = interface_runtime.ShadowSidecar(script)
            with (
                mock.patch.object(
                    interface_runtime, "SHADOW_REQUEST_TIMEOUT_S", 30),
                self.assertRaises(RuntimeError) as caught,
            ):
                await sidecar.start()
            self.assertIn("exited", str(caught.exception))
            self.assertNotIn("timed out", str(caught.exception),
                             "the probe waited out the request timeout instead "
                             "of failing on the sidecar's death")
            await sidecar.stop()
        asyncio.run(flow())

    def test_dead_sidecar_marks_runtime_unavailable(self):
        tmp_db = self.tmp / "shell_db.db"
        build_engine_db(tmp_db)
        script = self._script("process.exit(1)\n")
        rt = interface_runtime.InterfaceRuntime(
            str(tmp_db), run_dir=str(self.tmp / "run"), shadow_script=script)

        async def flow():
            with mock.patch.object(rt, "_check_available", return_value=None):
                await rt.start()
            self.assertFalse(rt.available)
            self.assertIn("sidecar", rt.unavailable_reason)
        asyncio.run(flow())


@unittest.skipUnless(HAS_SHADOW_STACK,
                     "needs node + @xterm/headless (shadow sidecar)")
class ShadowBracketedPasteTest(unittest.TestCase):
    """The `modes` op (spec #62 D1 step 4): the writer's ONLY route to the
    pane's DECSET 2004 state, and the one input to whether a body is wrapped
    in real paste markers.

    Driven against the REAL sidecar, not a stub: the whole point of the op is
    that @xterm/headless tracks a mode our own code does not model, so a stub
    that returns what we expect would prove only that we can spell the field
    name. Every case here feeds actual escape bytes and reads back what the
    terminal made of them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sidecar = interface_runtime.ShadowSidecar(
            str(interface_runtime.SHADOW_DIR / "sidecar.js"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, coro_fn):
        async def flow():
            await self.sidecar.start()
            try:
                return await coro_fn()
            finally:
                await self.sidecar.stop()
        return asyncio.run(flow())

    def test_tracks_mode_set_and_reset(self):
        # One generation, both transitions, in the order a real harness makes
        # them: a TUI enables 2004 when it takes the prompt and disables it on
        # the way out. Asserting 'on' alone would pass against a stub wired to
        # a constant.
        async def flow():
            self.sidecar.create("g1", 24, 80)
            self.assertEqual(await self.sidecar.bracketed_paste("g1"), "off",
                             "a fresh terminal has 2004 inactive")
            self.sidecar.feed("g1", b"\x1b[?2004h")
            self.assertEqual(await self.sidecar.bracketed_paste("g1"), "on")
            self.sidecar.feed("g1", b"\x1b[?2004l")
            self.assertEqual(await self.sidecar.bracketed_paste("g1"), "off")
        self._run(flow)

    def test_answer_reflects_bytes_fed_before_the_ask(self):
        # feed is fire-and-forget; the ask must ride the same per-generation
        # chain. If it did not, this read could answer from the state BEFORE
        # the mode-setting bytes were parsed — a wrap decision racing the
        # redraw that justifies it. The mode bytes are deliberately trailed by
        # a payload large enough that parsing cannot plausibly be instant.
        async def flow():
            self.sidecar.create("g2", 24, 80)
            self.sidecar.feed("g2", b"\x1b[?2004h" + b"x" * 200_000)
            self.assertEqual(await self.sidecar.bracketed_paste("g2"), "on")
        self._run(flow)

    def test_isolated_per_generation(self):
        # The sidecar multiplexes every session in one process; a mode read is
        # meaningless if it can answer from a neighbour's terminal.
        async def flow():
            self.sidecar.create("a", 24, 80)
            self.sidecar.create("b", 24, 80)
            self.sidecar.feed("a", b"\x1b[?2004h")
            self.assertEqual(await self.sidecar.bracketed_paste("a"), "on")
            self.assertEqual(await self.sidecar.bracketed_paste("b"), "off")
        self._run(flow)

    def test_unknown_generation_reads_unknown_without_raising(self):
        # The caller is a writer mid-frame: 'unknown' degrades it to no-wrap,
        # an exception would fail a send whose bytes are fine.
        async def flow():
            self.assertEqual(await self.sidecar.bracketed_paste("nope"),
                             "unknown")
        self._run(flow)

    def test_dead_sidecar_reads_unknown_without_raising(self):
        # Same contract at the harshest failure: the process is gone, so the
        # request cannot be answered at all. snapshot() RAISES here by design
        # (attach has a capture-pane fallback to fall into); this one must not.
        script = self.tmp / "dead.js"
        script.write_text("process.exit(1)\n")
        dead = interface_runtime.ShadowSidecar(str(script))

        async def flow():
            with self.assertRaises(RuntimeError):
                await dead.start()          # boot probe sees the death
            self.assertEqual(await dead.bracketed_paste("g1"), "unknown")
            await dead.stop()
        asyncio.run(flow())

    def test_silent_sidecar_reads_unknown_without_raising(self):
        # A wedged-but-alive sidecar: the request times out rather than
        # failing on EOF. Same degradation, different path — and this is the
        # one that would otherwise stall a writer thread on a future nothing
        # resolves.
        script = self.tmp / "silent.js"
        script.write_text("require('readline').createInterface"
                          "({input: process.stdin}).on('line', () => {});\n")
        silent = interface_runtime.ShadowSidecar(str(script))

        async def flow():
            with mock.patch.object(
                    interface_runtime, "SHADOW_REQUEST_TIMEOUT_S", 0.3):
                with self.assertRaises(RuntimeError):
                    await silent.start()
                self.assertEqual(await silent.bracketed_paste("g1"), "unknown")
            await silent.stop()
        asyncio.run(flow())


class ServerOptionPinTest(unittest.TestCase):
    """_pin_server_options is ADVISORY at every step (spec #62 D4).

    A server option the runtime could not pin is not a reason to deny the
    operator a chat session, so each of the three things that can go wrong —
    the set is refused, the readback fails, the effective value disagrees —
    must be logged and swallowed. They are three separate code paths, and an
    `except` that covers the first says nothing about the other two, so each
    gets its own case."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.tmp / "shell_db.db"), run_dir=str(self.tmp / "run"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pin_with(self, fake_tmux):
        calls: list[tuple] = []

        async def tmux(*args):
            calls.append(args)
            return fake_tmux(*args)

        with mock.patch.object(self.rt, "tmux", new=tmux):
            asyncio.run(self.rt._pin_server_options())
        return calls

    def test_set_refused_does_not_raise(self):
        def fake(*args):
            raise RuntimeError("invalid option: extended-keys")
        calls = self._pin_with(fake)
        self.assertEqual([c[0] for c in calls], ["set"],
                         "a refused set must not be followed by a readback")

    def test_readback_failure_does_not_raise(self):
        def fake(*args):
            if args[0] == "show":
                raise RuntimeError("no server running")
            return b""
        calls = self._pin_with(fake)
        self.assertEqual([c[0] for c in calls], ["set", "show"])

    def test_disagreeing_effective_value_does_not_raise(self):
        # tmux accepted the set and still reports something else. The pin
        # cannot fix that; it must report it and let the spawn proceed.
        def fake(*args):
            return b"on\n" if args[0] == "show" else b""
        calls = self._pin_with(fake)
        self.assertEqual([c[0] for c in calls], ["set", "show"])

    def test_pins_the_server_scope_off(self):
        # The exact argv matters: `-s` is server scope (one server, set at
        # creation). A session- or window-scoped set would silently not apply
        # to panes created later, which is every pane we care about.
        def fake(*args):
            return b"off\n" if args[0] == "show" else b""
        calls = self._pin_with(fake)
        self.assertEqual(calls[0], ("set", "-s", "extended-keys", "off"))
        self.assertEqual(calls[1], ("show", "-sv", "extended-keys"))


class TicketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.tmp / "shell_db.db"), run_dir=str(self.tmp / "run"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_use(self):
        minted = self.rt.mint_ticket(session_id=7, role="viewer",
                                     client_id="tab-1")
        ticket = self.rt.consume_ticket(minted["ticket"], 7)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["role"], "viewer")
        self.assertEqual(ticket["client_id"], "tab-1")
        self.assertIsNone(self.rt.consume_ticket(minted["ticket"], 7),
                          "a consumed ticket must be gone")

    def test_wrong_session_rejected(self):
        minted = self.rt.mint_ticket(session_id=7, role="viewer",
                                     client_id="tab-1")
        self.assertIsNone(self.rt.consume_ticket(minted["ticket"], 8))

    def test_expiry(self):
        minted = self.rt.mint_ticket(session_id=7, role="writer",
                                     client_id="tab-1", lease_id=3,
                                     lease_token="tok")
        self.assertEqual(minted["expires_in"], 60)
        self.rt._tickets[minted["ticket"]]["expires"] = time.monotonic() - 1
        self.assertIsNone(self.rt.consume_ticket(minted["ticket"], 7))

    def test_writer_ticket_binds_lease(self):
        minted = self.rt.mint_ticket(session_id=7, role="writer",
                                     client_id="tab-1", lease_id=3,
                                     lease_token="tok")
        ticket = self.rt.consume_ticket(minted["ticket"], 7)
        self.assertEqual(ticket["lease_id"], 3)
        self.assertEqual(ticket["lease_token"], "tok")

    def test_viewer_ticket_drops_lease(self):
        minted = self.rt.mint_ticket(session_id=7, role="viewer",
                                     client_id="tab-1", lease_id=3,
                                     lease_token="tok")
        ticket = self.rt.consume_ticket(minted["ticket"], 7)
        self.assertIsNone(ticket["lease_token"])

    def test_bad_role_rejected(self):
        with self.assertRaises(ValueError):
            self.rt.mint_ticket(session_id=7, role="admin", client_id="t")

    def test_unknown_ticket(self):
        self.assertIsNone(self.rt.consume_ticket("nope", 7))


class RejectReasonTest(unittest.TestCase):
    def test_stable_reasons(self):
        cases = [
            ("sequence gap: expected 3, got 5 — rejected, no bytes forwarded",
             "seq_gap"),
            ("session 1 has no writer", "writer_revoked"),
            ("session 1 writer held by tab-2 — explicit takeover required",
             "writer_revoked"),
            ("sequence 4 is pending — wait for its ack", "pending_unacked"),
            ("payload 70000 > 65536 bytes", "payload_too_large"),
            ("session 1 is ended, not occupied", "stale_generation"),
            ("a wake submission holds the input lock — this frame is ordered "
             "after it; retry once the wake is acknowledged", "input_locked"),
            ("something else entirely", "something else entirely"),
        ]
        for msg, expect in cases:
            reason = interface_runtime._reject_reason(
                interface_broker.BrokerError(msg))
            self.assertEqual(reason, expect, f"mapping of {msg!r}")


# ------------------------------------------------------------- lease liveness (seq 6)

class LeaseLivenessTest(unittest.TestCase):
    """Hermetic (no tmux/node): a dead writer's DB lease must not outlive
    it — detach revokes fenced by lease id/token/generation, the reaper's
    sweep revokes heartbeat-silent leases, and neither path can clobber a
    re-acquired lease."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, shell_id=1, generation=1):
        """One occupied session + input state + a runtime-owned Generation."""
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (?,?)", (shell_id, generation))
        sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (?,?,'occupied','idle')",
            (shell_id, generation)).lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,?,?,'clean')",
            (sid, shell_id, generation))
        con.commit()
        con.close()
        gen = interface_runtime.Generation(self.rt, sid, shell_id, generation,
                                           24, 80)
        self.rt.generations[sid] = gen
        return sid, gen

    def _lease(self, sid, client_id="tab-1", token="tok-1", takeover=False):
        con = sqlite3.connect(self.db)
        lease_id = interface_broker.acquire_writer(con, sid, client_id, token,
                                                   takeover=takeover)
        con.commit()
        con.close()
        return lease_id

    def _lease_row(self, lease_id):
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT revoked_at, revoke_reason FROM interface_writer_leases "
            "WHERE lease_id=?", (lease_id,)).fetchone()
        con.close()
        return row

    def _stale_heartbeat(self, lease_id):
        con = sqlite3.connect(self.db)
        con.execute(
            "UPDATE interface_writer_leases SET "
            "heartbeat_at=datetime('now','-120 seconds') WHERE lease_id=?",
            (lease_id,))
        con.commit()
        con.close()

    def _heartbeat_fresh(self, lease_id):
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT heartbeat_at > datetime('now','-5 seconds') "
            "FROM interface_writer_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        con.close()
        return row[0] == 1

    def test_writer_detach_revokes_its_lease(self):
        sid, gen = self._session()
        lease_id = self._lease(sid)
        writer = FakeClient(sid, role="writer", client_id="tab-1",
                            lease_id=lease_id, lease_token="tok-1")
        viewer = FakeClient(sid, role="viewer", client_id="tab-2")
        gen.clients.update({writer, viewer})

        async def flow():
            self.rt.detach(writer)
            await wait_for(lambda: self._lease_row(lease_id)[0] is not None,
                           what="liveness revoke")
        asyncio.run(flow())
        self.assertEqual(self._lease_row(lease_id)[1], "liveness")
        # The remaining client is told the writer lease is gone.
        wstates = viewer.by_type("writer")
        self.assertTrue(wstates)
        self.assertEqual(wstates[-1]["state"], "none")

    def test_viewer_detach_revokes_nothing(self):
        sid, gen = self._session()
        lease_id = self._lease(sid)
        viewer = FakeClient(sid, role="viewer", client_id="tab-2")
        gen.clients.add(viewer)

        async def flow():
            self.rt.detach(viewer)
            await asyncio.sleep(0.2)  # any stray revoke task would land
        asyncio.run(flow())
        self.assertIsNone(self._lease_row(lease_id)[0])

    def test_late_detach_never_clobbers_reacquired_lease(self):
        sid, gen = self._session()
        old_lease = self._lease(sid, token="tok-old")
        old = FakeClient(sid, role="writer", client_id="tab-1",
                         lease_id=old_lease, lease_token="tok-old")
        gen.clients.add(old)

        async def flow():
            self.rt.detach(old)
            await wait_for(lambda: self._lease_row(old_lease)[0] is not None,
                           what="first liveness revoke")
        asyncio.run(flow())

        # The client re-acquires (new lease id, new token); then the OLD
        # client object's detach fires again — a late close echo.
        new_lease = self._lease(sid, token="tok-new")
        stale = FakeClient(sid, role="writer", client_id="tab-1",
                           lease_id=old_lease, lease_token="tok-old")

        async def flow2():
            self.rt.detach(stale)
            await asyncio.sleep(0.2)
        asyncio.run(flow2())
        self.assertIsNone(self._lease_row(new_lease)[0],
                          "a stale detach must not touch the new lease")

    def test_revoke_fence_requires_token_and_generation(self):
        sid, _gen = self._session()
        lease_id = self._lease(sid, token="tok-1")
        self.assertFalse(self.rt._revoke_lease_sync(lease_id, "wrong", 1))
        self.assertFalse(self.rt._revoke_lease_sync(lease_id, "tok-1", 2))
        self.assertIsNone(self._lease_row(lease_id)[0])
        self.assertTrue(self.rt._revoke_lease_sync(lease_id, "tok-1", 1))
        self.assertEqual(self._lease_row(lease_id)[1], "liveness")
        # A double revoke is a no-op (the revoked_at IS NULL fence).
        self.assertFalse(self.rt._revoke_lease_sync(lease_id, "tok-1", 1))

    def test_sweep_revokes_silent_lease_keeps_fresh_one(self):
        sid, _gen = self._session()
        stale_lease = self._lease(sid, client_id="tab-dead", token="tok-d")
        self._stale_heartbeat(stale_lease)

        async def flow():
            await self.rt._sweep_dead_leases()
        asyncio.run(flow())
        self.assertEqual(self._lease_row(stale_lease)[1], "liveness")

        # A lease with a fresh durable heartbeat survives the sweep.
        fresh_lease = self._lease(sid, client_id="tab-live", token="tok-l")
        asyncio.run(flow())
        self.assertIsNone(self._lease_row(fresh_lease)[0])

    def test_acquire_after_sweep_needs_no_takeover(self):
        sid, _gen = self._session()
        dead_lease = self._lease(sid, client_id="tab-dead", token="tok-d")
        self._stale_heartbeat(dead_lease)
        # While the dead writer's lease is live, a plain acquire refuses.
        con = sqlite3.connect(self.db)
        with self.assertRaises(interface_broker.BrokerError):
            interface_broker.acquire_writer(con, sid, "tab-2", "tok-2")
        con.close()

        async def flow():
            await self.rt._sweep_dead_leases()
        asyncio.run(flow())
        # After the sweep the lease is free — no takeover needed.
        new_lease = self._lease(sid, client_id="tab-2", token="tok-2")
        self.assertIsNone(self._lease_row(new_lease)[0])

    def test_sweep_scopes_to_owned_generations(self):
        sid1, _gen1 = self._session(shell_id=1, generation=1)
        lease1 = self._lease(sid1, client_id="tab-1", token="tok-1")
        # A second occupied session this runtime does NOT manage.
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (2,1)")
        sid2 = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (2,1,'occupied','idle')").lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,2,1,'clean')", (sid2,))
        con.commit()
        con.close()
        lease2 = self._lease(sid2, client_id="tab-x", token="tok-x")
        self._stale_heartbeat(lease1)
        self._stale_heartbeat(lease2)

        async def flow():
            await self.rt._sweep_dead_leases()
        asyncio.run(flow())
        self.assertEqual(self._lease_row(lease1)[1], "liveness")
        self.assertIsNone(self._lease_row(lease2)[0],
                          "the sweep must not touch foreign generations")

    def test_heartbeat_stamps_lease_fenced_by_token(self):
        sid, _gen = self._session()
        lease_id = self._lease(sid, token="tok-1")
        self._stale_heartbeat(lease_id)
        writer = FakeClient(sid, role="writer", client_id="tab-1",
                            lease_id=lease_id, lease_token="tok-1")

        async def flow():
            self.rt.heartbeat(writer)
            await wait_for(lambda: self._heartbeat_fresh(lease_id),
                           what="durable heartbeat stamp")
        asyncio.run(flow())

        # A writer frame with the wrong token stamps nothing.
        self._stale_heartbeat(lease_id)
        impostor = FakeClient(sid, role="writer", client_id="tab-9",
                              lease_id=lease_id, lease_token="nope")

        async def flow2():
            self.rt.heartbeat(impostor)
            await asyncio.sleep(0.3)
        asyncio.run(flow2())
        self.assertFalse(self._heartbeat_fresh(lease_id))


# ------------------------------------------------------------- integration (tmux)

@unittest.skipUnless(HAS_SHADOW_STACK,
                     "needs tmux + node + @xterm/headless (shadow sidecar)")
class TmuxIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (1,1)")
        self.sid = con.execute(
            "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
            " lifecycle) VALUES (1,1,'occupied','idle')").lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'clean')", (self.sid,))
        con.commit()
        con.close()
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))

    def tearDown(self):
        # The runtime's stop() deliberately leaves the private tmux server
        # alive (reattach is its whole point) — tests own killing it.
        subprocess.run(
            ["tmux", "-S", str(self.tmp / "run" / "tmux.sock"),
             "kill-server"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _persist_identity(self, info):
        con = sqlite3.connect(self.db)
        con.execute(
            "UPDATE interface_sessions SET tmux_socket=?, tmux_session=?, "
            "tmux_window=?, tmux_pane_id=?, pane_pid=?, pane_start_ticks=? "
            "WHERE session_id=?",
            (info["tmux_socket"], info["tmux_session"], info["tmux_window"],
             info["pane_id"], info["pane_pid"], info["pane_start_ticks"],
             self.sid))
        con.commit()
        con.close()

    def _acquire_writer(self, client_id="tab-1", token="tok-1"):
        con = sqlite3.connect(self.db)
        lease_id = interface_broker.acquire_writer(con, self.sid, client_id,
                                                   token)
        con.commit()
        con.close()
        return lease_id

    async def _spawn_stub(self):
        await self.rt.start()
        self.assertTrue(self.rt.available, self.rt.unavailable_reason)
        info = await self.rt.spawn(
            session_id=self.sid, shell_id=1, generation=1,
            worktree=str(self.tmp), sc_path="/bin/sc",
            token_path="/tmp/tok", rows=24, cols=80,
            # raw: the spike-proven mode (reader.py setraw). Plain
            # `stty -echo` leaves canonical mode on — a newline-free frame
            # never reaches cat, so no echo can ever come back. The READY
            # marker is the pane's first output: it proves stty already ran,
            # so no input can land while tty echo is still on (which would
            # double the echo).
            command=["/bin/sh", "-c", "stty raw -echo; printf READY; cat"])
        self._persist_identity(info)
        gen = self.rt.generations[self.sid]
        await wait_for(lambda: gen.dbg_fanout_bytes >= 5,
                       what="pane raw-mode READY marker")
        return info

    def test_extended_keys_pinned_at_server_creation(self):
        """The runtime PINS extended-keys off; it does not inherit the default.

        tmux 3.5a already defaults this off, so asserting `off` after a plain
        spawn is a test that cannot come back red — it passes with
        _pin_server_options deleted. So the private server is pre-seeded with
        `extended-keys on` (exactly what a tmux config we do not own could do)
        and the spawn has to have flipped it. The seed uses a DIFFERENT session
        name so the runtime's own `new-session -d -s sc-interface` still
        succeeds against the now-existing server.

        Why it matters: `on` makes tmux re-encode keys in CSI-u form, which
        rewrites the 0x0D this writer sends as its separate submit phase
        (claude-code #43169) — the byte the whole two-phase protocol exists to
        deliver intact.
        """
        async def flow():
            sock = self.rt.sock
            subprocess.run(["tmux", "-S", sock, "new-session", "-d",
                            "-s", "seed-not-ours", "sleep 60"],
                           check=True, capture_output=True)
            subprocess.run(["tmux", "-S", sock, "set", "-s",
                            "extended-keys", "on"],
                           check=True, capture_output=True)
            seeded = subprocess.run(
                ["tmux", "-S", sock, "show", "-sv", "extended-keys"],
                check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(seeded, "on",
                             "seeding failed, so the assertion below could "
                             "not have failed either — test is vacuous")

            await self._spawn_stub()
            try:
                out = await self.rt.tmux("show", "-sv", "extended-keys")
                self.assertEqual(
                    out.decode().strip(), "off",
                    "the runtime did not pin extended-keys at server creation "
                    "— CSI-u re-encoding can still corrupt the submit CR")
            finally:
                await self.rt.stop()   # the sidecar is a child process
        asyncio.run(flow())

    def test_input_echo_redraw_terminate(self):
        asyncio.run(self._flow_input_echo_redraw_terminate())

    async def _flow_input_echo_redraw_terminate(self):
        info = await self._spawn_stub()
        self.assertTrue(info["pane_id"].startswith("%"))
        self.assertEqual(info["tmux_session"], "sc-interface")

        lease_id = self._acquire_writer()
        writer = FakeClient(self.sid, role="writer", client_id="tab-1",
                            lease_id=lease_id, lease_token="tok-1")
        await self.rt.attach(writer)
        self.assertTrue(writer.redraws, "attach must send a redraw")
        states = {m["type"]: m for m in writer.controls}
        self.assertEqual(states["lifecycle"]["lifecycle"], "idle")
        self.assertEqual(states["writer"]["state"], "active")

        # Ordered human input → ack → byte-exact echo via the FIFO pump.
        payload = b"echo-me-exactly"
        self.rt.enqueue_input(writer, 1, payload)
        await wait_for(lambda: writer.by_type("input_ack"),
                       what="input_ack seq=1")
        self.assertEqual(writer.by_type("input_ack")[0]["seq"], 1)
        await wait_for(
            lambda: payload in b"".join(writer.outputs), what="echo output")
        await asyncio.sleep(0.3)  # let any strays land
        self.assertEqual(b"".join(writer.outputs), payload,
                         "stty -echo + cat must echo byte-exactly")
        # The pending commit dirtied the composer; the broadcast came from DB.
        lifecycle = writer.by_type("lifecycle")[-1]
        self.assertEqual(lifecycle["composer"], "dirty")
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT composer, forwarded_seq FROM interface_input_state "
            "WHERE session_id=?", (self.sid,)).fetchone()
        con.close()
        self.assertEqual(row, ("dirty", 1))

        # A duplicate replays the ack and forwards nothing new.
        self.rt.enqueue_input(writer, 1, payload)
        await wait_for(
            lambda: any(a.get("replayed") for a in writer.by_type("input_ack")),
            what="replayed ack")
        # A gap is rejected before any bytes move.
        self.rt.enqueue_input(writer, 5, b"nope")
        await wait_for(lambda: writer.by_type("input_reject"),
                       what="seq_gap reject")
        self.assertEqual(writer.by_type("input_reject")[-1]["reason"],
                         "seq_gap")
        # In-order continuation still works.
        self.rt.enqueue_input(writer, 2, b"-second")
        await wait_for(
            lambda: len([a for a in writer.by_type("input_ack")
                         if not a.get("replayed")]) >= 2,
            what="input_ack seq=2")

        # Reconnect: a fresh viewer attach gets a redraw of the session.
        viewer = FakeClient(self.sid, role="viewer", client_id="tab-2")
        await self.rt.attach(viewer)
        self.assertTrue(viewer.redraws)
        self.assertIn(b"echo-me-exactly", viewer.redraws[0])
        wstate = {m["type"]: m for m in viewer.controls}["writer"]
        self.assertEqual(wstate["state"], "held")

        state = self.rt.runtime_state(self.sid)
        self.assertEqual(state["attached_clients"], 2)
        self.assertFalse(state["continuity_broken"])
        self.assertGreater(state["pump_bytes"], 0)

        # Graceful terminate: SIGTERM kills cat, pane + pid verified gone.
        result = await self.rt.terminate(self.sid)
        self.assertEqual(result, {"terminated": True, "generation": 1})
        self.assertIsNone(self.rt.runtime_state(self.sid))
        self.assertEqual(writer.closed[0], 1000)
        self.assertIn({"type": "error", "code": "terminated"},
                      writer.controls)
        await self.rt.stop()

    def test_reattach_after_service_restart(self):
        asyncio.run(self._flow_reattach())

    async def _flow_reattach(self):
        info = await self._spawn_stub()
        lease_id = self._acquire_writer()
        writer = FakeClient(self.sid, role="writer", client_id="tab-1",
                            lease_id=lease_id, lease_token="tok-1")
        await self.rt.attach(writer)
        self.rt.enqueue_input(writer, 1, b"before-restart")
        await wait_for(lambda: writer.by_type("input_ack"), what="ack")
        await wait_for(lambda: b"before-restart" in b"".join(writer.outputs),
                       what="echo")
        # Service stops: panes stay alive, runtime state is torn down.
        await self.rt.stop()

        # A new runtime process on the same run_dir + DB reattaches.
        rt2 = interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))
        self.rt = rt2
        await rt2.start()
        gen = rt2.get_generation(self.sid)
        self.assertIsNotNone(gen, "occupied session must reattach")
        self.assertEqual(gen.pane_id, info["pane_id"])
        viewer = FakeClient(self.sid, role="viewer", client_id="tab-3")
        await rt2.attach(viewer)
        self.assertIn(b"before-restart", viewer.redraws[0],
                      "shadow rebuilt from capture-pane keeps the screen")
        result = await rt2.terminate(self.sid)
        self.assertTrue(result["terminated"])
        await rt2.stop()

    def test_terminate_identity_mismatch_fails_closed(self):
        asyncio.run(self._flow_identity_mismatch())

    async def _flow_identity_mismatch(self):
        await self._spawn_stub()
        # Corrupt the stored pid: terminate must refuse to signal anything.
        con = sqlite3.connect(self.db)
        con.execute("UPDATE interface_sessions SET pane_pid=-1 "
                    "WHERE session_id=?", (self.sid,))
        con.commit()
        con.close()
        result = await self.rt.terminate(self.sid)
        self.assertEqual(result["terminated"], False)
        self.assertEqual(result["reason"], "identity_mismatch")
        gen = self.rt.get_generation(self.sid)
        self.assertIsNotNone(gen, "generation must survive a refused kill")
        await self.rt.stop()

    def test_abandon_during_spawn_aborts_and_kills_pane(self):
        asyncio.run(self._flow_abandon_during_spawn())

    async def _flow_abandon_during_spawn(self):
        """SC-064: cancel start lands mid-spawn (the pane created, the
        spawn not yet complete). abandon() — the cancel path's teardown —
        must see the in-flight generation, spawn must refuse to complete
        (SpawnAborted) before the harness boots, and the just-created pane
        must be killed by exact identity — never left live on a cancelled
        session."""
        await self.rt.start()
        self.assertTrue(self.rt.available, self.rt.unavailable_reason)
        piped = asyncio.Event()
        release = asyncio.Event()
        orig_pipe = self.rt._pipe_pane

        async def held_pipe(gen):
            piped.set()
            await release.wait()

        self.rt._pipe_pane = held_pipe
        try:
            spawn_task = asyncio.create_task(self.rt.spawn(
                session_id=self.sid, shell_id=1, generation=1,
                worktree=str(self.tmp), sc_path="/bin/sc",
                token_path="/tmp/tok", rows=24, cols=80,
                command=["/bin/sh", "-c",
                         "stty raw -echo; printf READY; cat"]))
            await asyncio.wait_for(piped.wait(), 10)
            gen = self.rt.generations.get(self.sid)
            self.assertIsNotNone(
                gen, "an in-flight spawn is visible to the cancel path")
            pane_id = gen.pane_id
            self.assertTrue(pane_id.startswith("%"))
            # The cancel start's teardown, mid-spawn.
            await self.rt.abandon(self.sid)
            self.assertNotIn(self.sid, self.rt.generations)
            release.set()
            with self.assertRaises(interface_runtime.SpawnAborted):
                await asyncio.wait_for(spawn_task, 10)
            self.assertFalse(
                await self.rt._pane_exists(pane_id),
                "the just-created pane is killed — never live on an "
                "ended session")
        finally:
            self.rt._pipe_pane = orig_pipe
        await self.rt.stop()

    def test_pane_death_drives_real_lost_transition(self):
        asyncio.run(self._flow_pane_death())

    async def _flow_pane_death(self):
        """The REAL trigger (sprint 25 flag #40): kill the pane's process and
        watch the whole chain fire — tmux's pipe writer exits → pump FIFO EOF
        → _on_pump_exit → the routes callback → DB occupied →
        lost/unreconciled. No callback invoked directly."""
        info = await self._spawn_stub()
        sys.path.insert(0, str(ENGINE / "api"))
        import interface_routes as routes
        with mock.patch.object(routes, "DB_PATH", self.db):
            routes.bind_runtime(self.rt)   # as the server does, pre-start
            os.kill(info["pane_pid"], signal.SIGKILL)

            def lost():
                con = sqlite3.connect(self.db)
                row = con.execute(
                    "SELECT occupancy, lifecycle FROM interface_sessions "
                    "WHERE session_id=?", (self.sid,)).fetchone()
                con.close()
                return row == ("unreconciled", "lost")
            await wait_for(lost, what="pane death → lost/unreconciled")
            con = sqlite3.connect(self.db)
            alert = con.execute(
                "SELECT reason FROM planner_alerts WHERE session_id=?",
                (self.sid,)).fetchone()
            con.close()
            self.assertIsNotNone(alert)
            self.assertEqual(alert[0], "session_lost")
        await self.rt.stop()


if __name__ == "__main__":
    unittest.main()
