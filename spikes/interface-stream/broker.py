"""Per-generation tmux broker for the interface-stream spike.

Owns: the private tmux server, FIFO pump threads, the bounded asyncio
bridge, the generation-fenced input queue, writer leases, the simulated
lifecycle/composer state machine, and the shadow terminal sidecar.

All methods run on the single asyncio loop that created the Broker.
Clients are duck-typed server-side objects with:
    role: "viewer" | "writer"
    lease_token: str | None
    send_control(dict)   -- enqueue a text JSON control frame
    send_output(bytes)   -- enqueue a 0x00 binary frame (bounded per client)
    send_redraw(bytes)   -- enqueue a 0x04 binary frame
    close(code, reason)
    seq state: last_seq (int | None), forwarded (set[int])
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import os
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time

BRIDGE_MAX = 8 * 1024 * 1024          # bounded pump→loop bridge
SENDKEYS_CHUNK = 512                  # proven tmux -H chunk size
MAX_INPUT_PAYLOAD = 64 * 1024         # wire protocol limit
CLIENT_QUEUE_MAX = 2 * 1024 * 1024    # per-client outbound (enforced server-side)
LEASE_HEARTBEAT_TIMEOUT = 60.0        # 3 missed 20s heartbeats
IDLE_QUIET_S = 1.0                    # output-quiet time marking idle

HARNESS_COMMANDS = {"claude": "claude", "codex": "codex", "kimi": "kimi", "bash": "bash"}


def _log(tag: str, msg: str) -> None:
    print(f"[broker {tag} {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- queue items

class HumanInput:
    def __init__(self, client, seq: int, payload: bytes):
        self.client, self.seq, self.payload = client, seq, payload


class WakeSubmit:
    def __init__(self, client):
        self.client = client


class Resize:
    def __init__(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols


# ---------------------------------------------------------------- shadow sidecar

class ShadowSidecar:
    """One Node process per service; multiplexes generations over stdio."""

    def __init__(self, script: str):
        self._script = script
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "node", self._script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("shadow sidecar exited"))
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)

    def _write(self, op: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(op).encode() + b"\n")

    async def _request(self, op: dict) -> dict:
        self._next_id += 1
        op["id"] = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[op["id"]] = fut
        self._write(op)
        return await fut

    def create(self, gen: str, rows: int, cols: int) -> None:
        self._write({"op": "create", "gen": gen, "rows": rows, "cols": cols})

    def feed(self, gen: str, data: bytes) -> None:
        self._write({"op": "feed", "gen": gen, "data": base64.b64encode(data).decode()})

    def resize(self, gen: str, rows: int, cols: int) -> None:
        self._write({"op": "resize", "gen": gen, "rows": rows, "cols": cols})

    def dispose(self, gen: str) -> None:
        self._write({"op": "dispose", "gen": gen})

    async def snapshot(self, gen: str) -> bytes:
        msg = await self._request({"op": "snapshot", "gen": gen})
        if not msg.get("ok"):
            raise RuntimeError(f"shadow snapshot failed: {msg.get('error')}")
        return base64.b64decode(msg["redraw"])

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()


# ---------------------------------------------------------------- lease

class Lease:
    def __init__(self):
        self.id = secrets.token_hex(8)
        self.token = secrets.token_hex(16)
        self.last_hb = time.monotonic()
        self.client = None  # writer connection currently holding it, if any


# ---------------------------------------------------------------- generation

class Generation:
    def __init__(self, broker: "Broker", sid: str, harness: str, worktree: str,
                 command: str | None, rows: int, cols: int,
                 wake_prompt: bytes, quiet_s: float, idle_quiet_s: float = IDLE_QUIET_S):
        self.broker = broker
        self.sid = sid
        self.harness = harness
        self.worktree = worktree
        self.command = command or HARNESS_COMMANDS[harness]
        self.rows, self.cols = rows, cols
        self.wake_prompt = wake_prompt
        self.quiet_s = quiet_s
        self.idle_quiet_s = idle_quiet_s

        # simulated lifecycle state machine (spike scope):
        #   lifecycle: starting -> idle (sentinel + first output quiet 1s)
        #              idle -> busy (wake submitted) -> idle (output quiet 1s)
        #   composer:  clean -> dirty (accepted human input) -> clean (pane output)
        self.lifecycle = "starting"
        self.composer = "clean"
        self.last_human_ts = 0.0
        self.last_output_ts = 0.0
        self._idle_timer: asyncio.Task | None = None

        self.clients: set = set()
        self.lease: Lease | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.continuity_broken = False

        self._bridge: collections.deque[bytes] = collections.deque()
        self._bridge_bytes = 0
        self._bridge_event = asyncio.Event()

        self.pane_id = ""
        self.pane_pid = 0
        self.dbg_pump_bytes = 0
        self.dbg_fanout_bytes = 0
        self.dbg_bridge_hwm = 0
        self._fifo_keep_fd = -1
        self._pump_fd = -1
        self._pump_thread: threading.Thread | None = None
        self._tasks: list[asyncio.Task] = []
        self.terminated = False

    # -- state machine ----------------------------------------------------

    def _set_lifecycle(self, state: str) -> None:
        if self.lifecycle == state:
            return
        _log(self.sid, f"lifecycle {self.lifecycle} -> {state}")
        self.lifecycle = state
        self.broadcast_control({"type": "lifecycle", "state": state, "composer": self.composer})

    def _set_composer(self, state: str) -> None:
        if self.composer == state:
            return
        _log(self.sid, f"composer {self.composer} -> {state}")
        self.composer = state
        self.broadcast_control({"type": "lifecycle", "state": self.lifecycle, "composer": state})

    def _arm_idle_timer(self) -> None:
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = asyncio.create_task(self._idle_after_quiet())

    async def _idle_after_quiet(self) -> None:
        try:
            await asyncio.sleep(self.idle_quiet_s)
            if time.monotonic() - self.last_output_ts >= self.idle_quiet_s and not self.terminated:
                self._set_lifecycle("idle")
        except asyncio.CancelledError:
            pass

    # -- fanout -------------------------------------------------------------

    def broadcast_control(self, msg: dict) -> None:
        for client in list(self.clients):
            client.send_control(dict(msg))

    # -- pump / bridge --------------------------------------------------------

    def _pump_loop(self) -> None:
        """Blocking FIFO reader thread; never blocks on client I/O."""
        loop = self.broker.loop
        while True:
            try:
                chunk = os.read(self._pump_fd, 65536)
            except OSError as exc:
                _log(self.sid, f"pump EXIT on OSError: {exc!r} (read={self.dbg_pump_bytes})")
                return
            if not chunk:  # all writers gone (shutdown)
                _log(self.sid, f"pump EXIT on EOF (read={self.dbg_pump_bytes})")
                return
            self.dbg_pump_bytes += len(chunk)
            loop.call_soon_threadsafe(self._on_pump_bytes, chunk)

    def _on_pump_bytes(self, chunk: bytes) -> None:
        if self.continuity_broken:
            return  # resync in progress; drop
        if self._bridge_bytes + len(chunk) > BRIDGE_MAX:
            self.continuity_broken = True
            self._bridge.clear()
            self._bridge_bytes = 0
            _log(self.sid, "ALERT continuity_broken: bridge overflow, dropping queued bytes, resyncing")
            asyncio.create_task(self._resync_all("continuity_broken"))
            return
        self._bridge.append(chunk)
        self._bridge_bytes += len(chunk)
        if self._bridge_bytes > self.dbg_bridge_hwm:
            self.dbg_bridge_hwm = self._bridge_bytes
        self._bridge_event.set()

    async def _output_consumer(self) -> None:
        try:
            while True:
                await self._bridge_event.wait()
                while self._bridge:
                    chunk = self._bridge.popleft()
                    self._bridge_bytes -= len(chunk)
                    self.broker.shadow.feed(self.sid, chunk)  # before client fanout
                    self.last_output_ts = time.monotonic()
                    if self.composer == "dirty":
                        self._set_composer("clean")
                    if self.lifecycle in ("starting", "busy"):
                        self._arm_idle_timer()
                    for client in list(self.clients):
                        client.send_output(chunk)
                    self.dbg_fanout_bytes += len(chunk)
                self._bridge_event.clear()
        except asyncio.CancelledError:
            pass

    async def _resync_all(self, reason: str) -> None:
        """continuity_broken recovery: rebuild shadow from capture-pane, resync clients."""
        try:
            self.broker.shadow.create(self.sid, self.rows, self.cols)
            capture = await self.broker.capture_pane(self.pane_id)
            self.broker.shadow.feed(self.sid, capture)
            redraw = await self.broker.shadow.snapshot(self.sid)
            for client in list(self.clients):
                client.send_control({"type": "resync", "reason": reason})
                client.send_redraw(redraw)
            self.continuity_broken = False
            _log(self.sid, f"resync complete ({reason}), {len(self.clients)} client(s)")
        except Exception as exc:  # pane died etc.
            _log(self.sid, f"resync failed: {exc!r}")
            self.broadcast_control({"type": "error", "code": "resync_failed"})

    # -- input consumer ------------------------------------------------------

    async def _input_consumer(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if self.terminated:
                    continue
                if isinstance(item, HumanInput):
                    await self._do_human(item)
                elif isinstance(item, WakeSubmit):
                    await self._do_wake(item)
                elif isinstance(item, Resize):
                    await self._do_resize(item)
        except asyncio.CancelledError:
            pass

    async def _do_human(self, item: HumanInput) -> None:
        client, seq, payload = item.client, item.seq, item.payload
        # validate at dequeue: generation current (implicit), lease, seq
        if self.lease is None or client.lease_token != self.lease.token:
            _log(self.sid, f"reject seq={seq}: writer_revoked (lease mismatch)")
            client.send_control({"type": "input_reject", "seq": seq, "reason": "writer_revoked"})
            return
        if client.last_seq is not None:
            if seq in client.forwarded:
                _log(self.sid, f"duplicate seq={seq}: replaying prior ack, no forward")
                client.send_control({"type": "input_ack", "seq": seq, "replayed": True})
                return
            if seq != client.last_seq + 1:
                _log(self.sid, f"reject seq={seq}: seq_gap (last={client.last_seq})")
                client.send_control({"type": "input_reject", "seq": seq, "reason": "seq_gap"})
                return
        # durable state change BEFORE forwarding
        self._set_composer("dirty")
        await self.broker.send_keys(self.pane_id, payload)
        client.forwarded.add(seq)
        client.last_seq = seq
        self.last_human_ts = time.monotonic()
        client.send_control({"type": "input_ack", "seq": seq})

    async def _do_wake(self, item: WakeSubmit) -> None:
        client = item.client
        now = time.monotonic()
        human_pending = any(isinstance(i, HumanInput) for i in self.queue._queue)  # noqa: SLF001
        reason = None
        if self.lifecycle != "idle":
            reason = f"not_idle:{self.lifecycle}"
        elif self.composer != "clean":
            reason = "composer_dirty"
        elif now - self.last_human_ts < self.quiet_s:
            reason = "quiet_window"
        elif human_pending:
            reason = "human_pending"
        if reason:
            _log(self.sid, f"wake CANCELLED ({reason}) — zero bytes sent")
            client.send_control({"type": "wake", "state": "cancelled", "reason": reason})
            return
        _log(self.sid, f"wake SUBMITTED ({len(self.wake_prompt)} bytes)")
        await self.broker.send_keys(self.pane_id, self.wake_prompt)
        self._set_lifecycle("busy")
        client.send_control({"type": "wake", "state": "submitted"})

    async def _do_resize(self, item: Resize) -> None:
        self.rows, self.cols = item.rows, item.cols
        await self.broker.tmux("resize-window", "-t", self.pane_id,
                               "-x", str(item.cols), "-y", str(item.rows))
        self.broker.shadow.resize(self.sid, item.rows, item.cols)
        _log(self.sid, f"resize {item.cols}x{item.rows}")

    # -- clients ---------------------------------------------------------------

    async def attach(self, client) -> None:
        # Snapshot covers every byte fed to the shadow so far; the client is
        # added to the fanout set only afterwards, so no byte is lost or
        # delivered twice.
        try:
            redraw = await self.broker.shadow.snapshot(self.sid)
        except Exception as exc:
            _log(self.sid, f"attach: snapshot failed ({exc!r}), falling back to capture-pane")
            self.broker.shadow.create(self.sid, self.rows, self.cols)
            capture = await self.broker.capture_pane(self.pane_id)
            self.broker.shadow.feed(self.sid, capture)
            redraw = await self.broker.shadow.snapshot(self.sid)
        client.send_redraw(redraw)
        self.clients.add(client)
        if (client.role == "writer" and self.lease
                and client.lease_token == self.lease.token):
            self.lease.client = client
        client.send_control({"type": "lifecycle", "state": self.lifecycle, "composer": self.composer})
        client.send_control({"type": "writer", "state": self.writer_state_for(client)})

    def detach(self, client) -> None:
        self.clients.discard(client)
        if self.lease and self.lease.client is client:
            self.lease.client = None

    def writer_state_for(self, client) -> str:
        if self.lease is None:
            return "none"
        if client.lease_token == self.lease.token:
            return "active"
        return "held"

    # -- lifecycle -------------------------------------------------------------

    async def terminate(self) -> None:
        if self.terminated:
            return
        self.terminated = True
        _log(self.sid, "terminating generation")
        self._set_lifecycle("terminated")
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
        for task in self._tasks:
            task.cancel()
        for client in list(self.clients):
            client.send_control({"type": "error", "code": "terminated"})
            client.close(1000, "generation terminated")
        try:
            await self.broker.tmux("kill-window", "-t", self.pane_id)
        except Exception:
            pass
        self.broker.shadow.dispose(self.sid)
        for fd in (self._fifo_keep_fd, self._pump_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------- broker

class Broker:
    def __init__(self, run_dir: str | None = None):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.run_dir = run_dir or tempfile.mkdtemp(prefix="sc-ispike-")
        os.chmod(self.run_dir, stat.S_IRWXU)  # mode 0700, per design
        self.sock = os.path.join(self.run_dir, "tmux.sock")
        self.shadow = ShadowSidecar(os.path.join(os.path.dirname(__file__), "shadow", "sidecar.js"))
        self.generations: dict[str, Generation] = {}
        self.leases: dict[str, Generation] = {}  # lease_id -> generation
        self._tmux_session_started = False
        self._counter = 0
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.shadow.start()
        self._reaper = asyncio.create_task(self._lease_reaper())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for gen in list(self.generations.values()):
            await gen.terminate()
        await self.shadow.stop()
        try:
            await self.tmux("kill-server")
        except Exception:
            pass

    # -- tmux primitives ------------------------------------------------------

    async def tmux(self, *args: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-S", self.sock, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)} failed: {err.decode(errors='replace').strip()}")
        return out

    async def send_keys(self, pane_id: str, payload: bytes) -> None:
        for off in range(0, len(payload), SENDKEYS_CHUNK):
            chunk = payload[off:off + SENDKEYS_CHUNK]
            await self.tmux("send-keys", "-t", pane_id, "-H",
                            *[f"{b:02x}" for b in chunk])

    async def capture_pane(self, pane_id: str) -> bytes:
        return await self.tmux("capture-pane", "-epN", "-t", pane_id)

    # -- sessions ---------------------------------------------------------------

    async def create_session(self, harness: str, worktree: str,
                             command: str | None = None,
                             rows: int = 24, cols: int = 80,
                             wake_prompt: bytes = b"WAKEPROMPT\n",
                             quiet_s: float = 3.0,
                             idle_quiet_s: float = IDLE_QUIET_S) -> Generation:
        if harness not in HARNESS_COMMANDS:
            raise ValueError(f"unknown harness {harness!r}")
        self._counter += 1
        sid = f"s{self._counter}-{secrets.token_hex(3)}"
        gen = Generation(self, sid, harness, worktree, command, rows, cols,
                         wake_prompt, quiet_s, idle_quiet_s)
        self.generations[sid] = gen

        sentinel = os.path.join(self.run_dir, f"ready-{sid}")
        fifo = os.path.join(self.run_dir, f"fifo-{sid}")
        os.mkfifo(fifo)
        # pre-open RDWR|NONBLOCK so tmux's `cat > fifo` writer never blocks
        gen._fifo_keep_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # noqa: SLF001
        gen._pump_fd = os.open(fifo, os.O_RDONLY)  # noqa: SLF001

        cmdline = gen.command
        shell_line = (
            f"while [ ! -f {shlex.quote(sentinel)} ]; do sleep 0.02; done; "
            f"cd {shlex.quote(worktree)} && exec {cmdline}"
        )
        if not self._tmux_session_started:
            await self.tmux("new-session", "-d", "-s", "spike", "-n", sid,
                            "-x", str(cols), "-y", str(rows), shell_line)
            self._tmux_session_started = True
        else:
            try:
                await self.tmux("new-window", "-d", "-t", "spike:", "-n", sid, shell_line)
            except RuntimeError as exc:
                if "no server running" not in str(exc):
                    raise
                # last kill-window took the session (and server) down with it
                self._tmux_session_started = False
                await self.tmux("new-session", "-d", "-s", "spike", "-n", sid,
                                "-x", str(cols), "-y", str(rows), shell_line)
                self._tmux_session_started = True
            else:
                await self.tmux("resize-window", "-t", f"spike:{sid}",
                                "-x", str(cols), "-y", str(rows))
        out = await self.tmux("display-message", "-p", "-t", f"spike:{sid}",
                              "#{pane_id} #{pane_pid}")
        pane_id, pane_pid = out.decode().split()
        gen.pane_id, gen.pane_pid = pane_id, int(pane_pid)

        self.shadow.create(sid, rows, cols)
        await self.tmux("pipe-pane", "-t", pane_id, f"cat > {shlex.quote(fifo)}")
        # boot the harness only now: zero lost boot bytes
        open(sentinel, "w").close()

        gen._pump_thread = threading.Thread(  # noqa: SLF001
            target=gen._pump_loop, name=f"pump-{sid}", daemon=True)  # noqa: SLF001
        gen._pump_thread.start()  # noqa: SLF001
        gen._tasks = [  # noqa: SLF001
            asyncio.create_task(gen._output_consumer()),
            asyncio.create_task(gen._input_consumer()),
        ]
        _log(sid, f"session created: harness={harness} pane={pane_id} pid={pane_pid} {cols}x{rows}")
        return gen

    def get_session(self, sid: str) -> Generation | None:
        return self.generations.get(sid)

    async def terminate_session(self, sid: str) -> bool:
        gen = self.generations.pop(sid, None)
        if not gen:
            return False
        self.leases = {k: v for k, v in self.leases.items() if v is not gen}
        await gen.terminate()
        return True

    # -- writer leases -----------------------------------------------------------

    def acquire_lease(self, sid: str, takeover: bool = False) -> Lease | None:
        gen = self.generations.get(sid)
        if not gen or gen.terminated:
            return None
        old = gen.lease
        if old is not None and not takeover:
            return None
        lease = Lease()
        old_client = old.client if old is not None else None
        if old is not None:
            _log(sid, f"lease takeover: revoking {old.id}, issuing {lease.id}")
            self.leases.pop(old.id, None)
        else:
            _log(sid, f"lease issued: {lease.id}")
        gen.lease = lease
        self.leases[lease.id] = gen
        for client in list(gen.clients):
            if client is old_client:
                client.send_control({"type": "writer", "state": "revoked"})
            else:
                client.send_control({"type": "writer", "state": gen.writer_state_for(client)})
        return lease

    def release_lease(self, lease_id: str) -> bool:
        gen = self.leases.pop(lease_id, None)
        if not gen or gen.lease is None or gen.lease.id != lease_id:
            return False
        _log(gen.sid, f"lease released: {lease_id}")
        gen.lease = None
        for client in list(gen.clients):
            client.send_control({"type": "writer", "state": "none"})
        return True

    def heartbeat(self, client) -> None:
        gen = self.generations.get(client.sid)
        if gen and gen.lease and client.lease_token == gen.lease.token:
            gen.lease.last_hb = time.monotonic()
            if gen.lease.client is None:
                gen.lease.client = client

    async def _lease_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                for gen in list(self.generations.values()):
                    lease = gen.lease
                    if lease and now - lease.last_hb > LEASE_HEARTBEAT_TIMEOUT:
                        _log(gen.sid, f"lease {lease.id} heartbeat timeout — revoking")
                        self.release_lease(lease.id)
        except asyncio.CancelledError:
            pass

    # -- frame entry points (called by the WS layer) --------------------------------

    def enqueue_human(self, client, seq: int, payload: bytes) -> None:
        gen = self.generations.get(client.sid)
        if gen is None or gen.terminated:
            client.send_control({"type": "input_reject", "seq": seq, "reason": "stale_generation"})
            return
        gen.queue.put_nowait(HumanInput(client, seq, payload))

    def enqueue_wake(self, client) -> None:
        gen = self.generations.get(client.sid)
        if gen is None or gen.terminated:
            client.send_control({"type": "wake", "state": "cancelled", "reason": "stale_generation"})
            return
        gen.queue.put_nowait(WakeSubmit(client))

    def enqueue_resize(self, client, rows: int, cols: int) -> None:
        gen = self.generations.get(client.sid)
        if gen is None or gen.terminated:
            return
        gen.queue.put_nowait(Resize(rows, cols))

    # -- introspection ----------------------------------------------------------------

    def session_info(self, gen: Generation) -> dict:
        return {
            "session_id": gen.sid,
            "generation": 1,
            "harness": gen.harness,
            "worktree": gen.worktree,
            "lifecycle": gen.lifecycle,
            "composer": gen.composer,
            "rows": gen.rows,
            "cols": gen.cols,
            "pane_id": gen.pane_id,
            "pane_pid": gen.pane_pid,
            "continuity_broken": gen.continuity_broken,
            "dbg": {"pump_bytes": gen.dbg_pump_bytes,
                    "fanout_bytes": gen.dbg_fanout_bytes,
                    "bridge_hwm": gen.dbg_bridge_hwm},
            "writer_lease": gen.lease.id if gen.lease else None,
            "clients": len(gen.clients),
        }
