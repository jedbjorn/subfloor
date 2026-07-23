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

    def _runtime(self):
        return interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))

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
        rt = self._runtime()
        with mock.patch.object(interface_runtime.shutil, "which",
                               return_value="/usr/bin/x"), \
                mock.patch.object(interface_runtime, "_tmux_version",
                                  return_value=(3, 3)):
            reason = rt._check_available()
        self.assertIsNotNone(reason)
        self.assertIn("3.3", reason)

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

        async def flow():
            sidecar = interface_runtime.ShadowSidecar(script)
            with mock.patch.object(interface_runtime,
                                   "SHADOW_REQUEST_TIMEOUT_S", 0.5):
                t0 = time.monotonic()
                with self.assertRaises(RuntimeError):
                    await sidecar.start()   # the boot probe times out
                self.assertLess(time.monotonic() - t0, 5)
            await sidecar.stop()
        asyncio.run(flow())

    def test_dead_sidecar_fails_probe_fast(self):
        # Dies instantly (what a missing @xterm/headless require does).
        script = self._script("process.exit(1)\n")

        async def flow():
            sidecar = interface_runtime.ShadowSidecar(script)
            with mock.patch.object(interface_runtime,
                                   "SHADOW_REQUEST_TIMEOUT_S", 5):
                t0 = time.monotonic()
                with self.assertRaises(RuntimeError):
                    await sidecar.start()
                self.assertLess(time.monotonic() - t0, 5)
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
