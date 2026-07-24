#!/usr/bin/env python3
"""Interface wake e2e on REAL tmux (sprint 25 seq 11, task #87) — the three
integration paths earlier units deferred to the real-tmux gate:

- wake-into-fresh: a >3s-boot harness receives its wake only after REAL
  provider readiness + the quiet debounce, never at the pre-exec
  occupied_at (flag #49 end-to-end: the bytes land, exactly once, in a
  live pane — on the runtime's real writer path through private tmux).
- out-of-order hook injection under input load: hook sequence fencing
  holds (out-of-order and replayed seqs rejected, last_hook_seq monotonic)
  while ordered input streams through real tmux — no lost frames, no
  interleaved bytes.
- composer submit: one frame containing text plus terminal Enter advances a
  real tmux command past its line read exactly once.
- parking-under-crash: a broker crash mid-write (the private tmux server
  dies between the wake preflight and send-keys) parks the batch
  delivery_unknown with an alert, and no drain, startup pass, or notify
  ever replays it (decision #22 on the real writer path, not a stub).

Gated on the shadow stack (tmux + node + @xterm/headless), like
TmuxIntegrationTest. Hermetic coverage of the same invariants lives in
test_interface_wake.py and test_interface_crash_window.py.

Run:
    python3 tests/test_interface_wake_tmux.py
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
TESTS = Path(__file__).resolve().parent

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(TESTS))
import interface_broker  # noqa: E402
import interface_runtime  # noqa: E402
import interface_wake  # noqa: E402
from test_interface_crash_window import build_engine_db  # noqa: E402
from test_interface_runtime import (  # noqa: E402
    HAS_SHADOW_STACK, FakeClient, wait_for)


@unittest.skipUnless(HAS_SHADOW_STACK,
                     "needs tmux + node + @xterm/headless (shadow sidecar)")
class WakeTmuxE2ETest(unittest.TestCase):
    """One armed sprint binding over a real private-tmux generation."""

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
            " lifecycle, harness, cli_version) VALUES (1,1,'occupied','idle',"
            "'kimi','kimi-code 0.27.0')").lastrowid
        con.execute(
            "INSERT INTO interface_input_state (session_id, shell_id,"
            " generation, composer) VALUES (?,1,1,'clean')", (self.sid,))
        self.binding = con.execute(
            "INSERT INTO sprint_planner_bindings (sprint_doc_id,"
            " planner_shell_id, session_id, shell_id, generation) "
            "VALUES (1,1,?,1,1)", (self.sid,)).lastrowid
        con.commit()
        con.close()
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.db), run_dir=str(self.tmp / "run"))

    def tearDown(self):
        subprocess.run(
            ["tmux", "-S", str(self.tmp / "run" / "tmux.sock"),
             "kill-server"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def _one(self, sql, params=()):
        con = sqlite3.connect(self.db)
        row = con.execute(sql, params).fetchone()
        con.close()
        return row[0] if row else None

    def _age(self, col, seconds):
        con = sqlite3.connect(self.db)
        con.execute(
            f"UPDATE interface_sessions SET {col}=datetime('now', ?) "
            f"WHERE session_id=?", (f"-{seconds} seconds", self.sid))
        con.commit()
        con.close()

    def _record_hook(self, seq, event):
        con = sqlite3.connect(self.db)
        interface_broker.record_hook(con, 1, 1, seq, event)
        con.commit()
        con.close()

    def _add_message(self):
        con = sqlite3.connect(self.db)
        mid = con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body,"
            " kind, sprint_doc_id) VALUES (2,1,'evt','task',1)").lastrowid
        interface_wake.maybe_create_wake_item(con, mid)
        con.commit()
        con.close()
        return mid

    def _batch_state(self):
        return self._one(
            "SELECT state FROM planner_wake_batches WHERE binding_id=? "
            "ORDER BY batch_id DESC LIMIT 1", (self.binding,))

    def _capture(self, info):
        return subprocess.run(
            ["tmux", "-S", info["tmux_socket"], "capture-pane", "-epN",
             "-t", info["pane_id"]], capture_output=True).stdout

    def _acquire_writer(self):
        con = sqlite3.connect(self.db)
        lease_id = interface_broker.acquire_writer(con, self.sid, "tab-1",
                                                   "tok-1")
        con.commit()
        con.close()
        return lease_id

    async def _spawn(self, command):
        await self.rt.start()
        self.assertTrue(self.rt.available, self.rt.unavailable_reason)
        info = await self.rt.spawn(
            session_id=self.sid, shell_id=1, generation=1,
            worktree=str(self.tmp), sc_path="/bin/sc",
            token_path="/tmp/tok", rows=24, cols=80, command=command)
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
        gen = self.rt.generations[self.sid]
        await wait_for(lambda: gen.dbg_fanout_bytes >= 5, timeout=15,
                       what="pane READY marker")
        return info

    # -- e2e 1: wake-into-fresh -------------------------------------------------

    def test_wake_into_fresh_waits_for_real_readiness(self):
        asyncio.run(self._flow_wake_into_fresh())

    async def _flow_wake_into_fresh(self):
        # A >3s boot: the pane is unpainted for 4s, then becomes ready —
        # and readiness arrives as the provider session_start hook AFTER
        # the boot, exactly like a real slow harness (flag #49's shape).
        info = await self._spawn(
            ["/bin/sh", "-c", "sleep 4; stty raw -echo; printf READY; cat"])
        # The session row claims occupation long ago (the defect shape):
        # without the readiness stamp the gate would fire immediately.
        self._age("occupied_at", 60)
        self._age("created_at", 60)
        self._record_hook(1, "session_start")   # real readiness, stamped now
        hook_at = time.monotonic()
        self._add_message()
        self.rt.wake_coordinator.notify_binding(self.binding)

        quiet = interface_broker.DEFAULT_QUIET_S
        # The debounce is owed from REAL readiness: no byte may move first.
        await asyncio.sleep(quiet * 0.6)
        self.assertNotEqual(self._batch_state(), "submitting")
        self.assertNotIn(interface_broker.WAKE_PROMPT.encode(),
                         self._capture(info))
        # Then exactly one submission lands in the live pane.
        await wait_for(lambda: self._batch_state() == "submitting",
                       timeout=quiet * 4,
                       what="wake submission after readiness + quiet")
        self.assertGreaterEqual(time.monotonic() - hook_at, quiet * 0.8,
                                "submission came before the readiness "
                                "debounce was owed")
        prompt = interface_broker.WAKE_PROMPT.encode()
        await wait_for(lambda: prompt in self._capture(info),
                       what="wake prompt visible in pane")
        await asyncio.sleep(0.5)  # let any stray duplicate land
        self.assertEqual(self._capture(info).count(prompt), 1,
                         "the wake must land exactly once")
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM planner_wake_batches"), 1)
        await self.rt.stop()

    # -- e2e 2: out-of-order hook injection under input load ---------------------

    def test_out_of_order_hooks_under_input_load(self):
        asyncio.run(self._flow_hooks_under_load())

    async def _flow_hooks_under_load(self):
        await self._spawn(["/bin/sh", "-c",
                           "stty raw -echo; printf READY; cat"])
        self._age("occupied_at", 60)
        self._age("created_at", 60)
        lease_id = self._acquire_writer()
        writer = FakeClient(self.sid, role="writer", client_id="tab-1",
                            lease_id=lease_id, lease_token="tok-1")
        await self.rt.attach(writer)

        # Hooks arrive OUT OF ORDER from a second thread while input
        # streams: seqs 2 and 4 arrive after higher seqs committed and
        # must be fenced as stale; replays must be rejected outright.
        rejected = []
        surprises = []

        def hook_worker():
            con = sqlite3.connect(self.db)
            try:
                for seq, event in [(1, "session_start"), (3, "turn_stop"),
                                   (2, "prompt_submit"), (5, "turn_stop"),
                                   (4, "prompt_submit")]:
                    try:
                        interface_broker.record_hook(con, 1, 1, seq, event)
                        con.commit()
                    except interface_broker.BrokerError:
                        con.rollback()
                        rejected.append(seq)
                for replay in (3, 5):
                    try:
                        interface_broker.record_hook(con, 1, 1, replay,
                                                     "turn_stop")
                        con.commit()
                        surprises.append(f"replay {replay} ACCEPTED")
                    except interface_broker.BrokerError:
                        con.rollback()
            finally:
                con.close()

        worker = threading.Thread(target=hook_worker)
        worker.start()

        frames = [f"frame-{i:02d}".encode() for i in range(1, 21)]
        for i, payload in enumerate(frames, 1):
            self.rt.enqueue_input(writer, i, payload)
            await wait_for(
                lambda i=i: any(a.get("seq") == i and not a.get("replayed")
                                for a in writer.by_type("input_ack")),
                what=f"input_ack seq={i}")
        worker.join(5)

        # Fencing: the out-of-order seqs were fenced, the replays rejected,
        # and the durable sequence only ever moved forward.
        self.assertEqual(surprises, [])
        self.assertEqual(sorted(rejected), [2, 4],
                         "out-of-order seqs must be fenced as stale")
        self.assertEqual(
            self._one("SELECT last_hook_seq FROM interface_generations "
                      "WHERE shell_id=1 AND generation=1"), 5)
        self.assertEqual(
            self._one("SELECT lifecycle FROM interface_sessions "
                      "WHERE session_id=?", (self.sid,)), "idle")
        # No lost frames, no interleaved bytes: the echo stream IS the
        # frame stream, in order, byte-exact.
        await wait_for(lambda: b"".join(frames) in b"".join(writer.outputs),
                       what="full echo stream")
        await asyncio.sleep(0.3)
        self.assertEqual(b"".join(writer.outputs), b"".join(frames))
        self.assertFalse(
            self.rt.runtime_state(self.sid)["continuity_broken"])
        await self.rt.stop()

    # -- e2e 3: one composed frame starts one real terminal turn ------------------

    def test_composed_text_and_submit_start_one_real_turn(self):
        asyncio.run(self._flow_composed_turn())

    async def _flow_composed_turn(self):
        await self._spawn([
            "/bin/sh",
            "-c",
            "printf READY; IFS= read -r line; "
            "printf '\\nTURN_STARTED:%s\\n' \"$line\"; sleep 5",
        ])
        lease_id = self._acquire_writer()
        writer = FakeClient(
            self.sid,
            role="writer",
            client_id="tab-1",
            lease_id=lease_id,
            lease_token="tok-1",
        )
        await self.rt.attach(writer)

        # This is the browser composer's one generation-fenced acceptance:
        # text and terminal submit share one sequence and one broker frame.
        self.rt.enqueue_input(writer, 1, b"one composed turn\r")
        await wait_for(
            lambda: len([
                ack for ack in writer.by_type("input_ack")
                if ack.get("seq") == 1 and not ack.get("replayed")
            ]) == 1,
            what="one composer input acknowledgement",
        )
        marker = b"TURN_STARTED:one composed turn"
        await wait_for(
            lambda: marker in b"".join(writer.outputs),
            what="real terminal command advanced past its line read",
        )
        await asyncio.sleep(0.3)
        self.assertEqual(
            b"".join(writer.outputs).count(marker),
            1,
            "one composed acceptance must start exactly one turn",
        )
        self.assertEqual(
            [ack["seq"] for ack in writer.by_type("input_ack")],
            [1],
        )
        await self.rt.stop()

    # -- e2e 4: parking under a mid-write broker crash ----------------------------

    def test_broker_crash_mid_write_parks_delivery_unknown(self):
        asyncio.run(self._flow_parking_under_crash())

    async def _flow_parking_under_crash(self):
        await self._spawn(["/bin/sh", "-c",
                           "stty raw -echo; printf READY; cat"])
        self._age("occupied_at", 60)
        self._age("created_at", 60)

        attempts = {"n": 0}
        original = self.rt._send_keys_sync

        def crashing_write(pane_id, payload):
            attempts["n"] += 1
            # The broker's tmux dies MID-WRITE: the preflight already
            # passed, so whether any byte moved is unprovable — the crash
            # window decision #22 parks for.
            subprocess.run(["tmux", "-S", self.rt.sock, "kill-server"],
                           capture_output=True)
            original(pane_id, payload)  # raises: the server is gone

        self.rt._send_keys_sync = crashing_write
        self._add_message()
        self.rt.wake_coordinator.notify_binding(self.binding)

        await wait_for(lambda: self._batch_state() == "delivery_unknown",
                       timeout=10, what="batch parked delivery_unknown")
        self.assertEqual(attempts["n"], 1, "exactly one write attempt")
        self.assertEqual(
            self._one("SELECT reason FROM planner_alerts "
                      "WHERE reason='wake_batch_delivery_unknown'"),
            "wake_batch_delivery_unknown")

        # No wake path may replay the parked submission: not a fresh
        # notify, not the startup reconciliation pass.
        self.rt.wake_coordinator.notify_binding(self.binding)
        self.rt.wake_coordinator.startup_pass()
        await asyncio.sleep(2.0)
        self.assertEqual(attempts["n"], 1,
                         "a parked batch must never be auto-replayed")
        self.assertEqual(self._batch_state(), "delivery_unknown")
        # The tmux server is already gone; stop() only releases runtime
        # resources (it never touches a dead server).
        await self.rt.stop()


if __name__ == "__main__":
    unittest.main()
