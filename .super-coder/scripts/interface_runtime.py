#!/usr/bin/env python3
"""Interface runtime — the per-generation tmux broker, productionized
(spec #20, sprint 25 seq 5 vertical slice).

Adapts the proven spike broker (spikes/interface-stream/broker.py) onto the
durable seq-4 substrate: generations are keyed by the DB session_id and carry
(shell_id, generation) for fencing; the input path walks
interface_broker.accept_human_input's two-phase commit protocol in a worker
thread with the tmux send-keys write injected as `writer`; the output path
(private tmux server on a mode-0700 socket, FIFO pipe-pane pump, 8 MiB
bounded bridge, shadow sidecar fed before fanout) is ported unchanged.

The runtime owns processes, tmux, and volatile stream state; every
occupancy/lifecycle/composer transition stays in the DB and is the routes
layer's job. The one DB-write exception is writer-lease liveness (seq 6):
a writer's heartbeats stamp heartbeat_at, and a detached or heartbeat-silent
writer's lease row is revoked (revoke_reason='liveness'), fenced by lease
id/token/generation so a stale detach never clobbers a re-acquired lease.
On service restart the tmux server and FIFO files survive
(`.super-coder/run/interface/` is stable), so start() reattaches every
occupied session whose pane identity still verifies exactly.

No input bytes, terminal output, or token plaintext is ever logged or
stored — metadata only.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import db_driver
import interface_broker
import interface_wake

ENGINE = Path(__file__).resolve().parents[1]
SHADOW_DIR = ENGINE / "shadow"
SHADOW_NODE_PATH = "/opt/sc-shadow/node_modules"

BRIDGE_MAX = 8 * 1024 * 1024          # bounded pump→loop bridge (spike-proven)
SENDKEYS_CHUNK = 512                  # proven tmux -H chunk size
MAX_INPUT_PAYLOAD = 64 * 1024         # wire protocol limit
TMUX_MIN_VERSION = (3, 4)
LEASE_HEARTBEAT_TIMEOUT = 60.0        # 3 missed 20s heartbeats
LEASE_LIVENESS_TIMEOUT = 40.0         # dead-lease sweep bound (WS PING_TIMEOUT)
TICKET_TTL_S = 60
GRACEFUL_TERMINATE_S = 10.0
SHADOW_REQUEST_TIMEOUT_S = 10.0   # a sidecar that can't answer is dead
TMUX_SYNC_TIMEOUT_S = 10.0   # a wedged-but-alive tmux must never hang the
                             # broker worker thread (SC-013)

TMUX_SESSION = "sc-interface"


def _log(tag: str, msg: str) -> None:
    print(f"[runtime {tag} {time.strftime('%H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


class InterfaceUnavailable(RuntimeError):
    """tmux/node stack missing or too old — review UI keeps working."""


class SpawnAborted(RuntimeError):
    """The generation was abandoned (cancel start) while its spawn was
    still in flight — the spawn must not complete (SC-064)."""


class _Rejected(Exception):
    """Pre-broker rejection carrying a stable wire reason string."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _reject_reason(exc: interface_broker.BrokerError) -> str:
    """Map a broker refusal to a stable input_reject reason string."""
    msg = str(exc)
    if "sequence gap" in msg:
        return "seq_gap"
    if "has no writer" in msg or "writer held" in msg:
        return "writer_revoked"
    if "is pending" in msg:
        return "pending_unacked"
    if "payload" in msg and "bytes" in msg:
        return "payload_too_large"
    if "not occupied" in msg:
        return "stale_generation"
    if "input lock" in msg:
        return "input_locked"
    return msg


def _read_start_ticks(pid: int) -> int:
    """Field 22 of /proc/<pid>/stat. comm may contain spaces/parens, so
    split AFTER the last ')'."""
    with open(f"/proc/{pid}/stat") as fh:
        text = fh.read()
    rest = text[text.rindex(")") + 2:]
    return int(rest.split()[19])  # field 22 (starttime), zero-based 19 after comm


def _pid_alive(pid: int, start_ticks: int) -> bool:
    """True only if /proc/<pid> exists AND its start ticks match — a recycled
    PID occupied by a different process is NOT our process."""
    try:
        return _read_start_ticks(pid) == start_ticks
    except Exception:
        return False


def _tmux_version() -> tuple[int, int] | None:
    try:
        out = subprocess.run(["tmux", "-V"], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    except Exception:
        return None
    m = re.search(r"tmux\s+(\d+)(?:\.(\d+))?", out)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


# ---------------------------------------------------------------- queue items

class HumanInput:
    def __init__(self, client, seq: int, payload: bytes):
        self.client, self.seq, self.payload = client, seq, payload


class Resize:
    def __init__(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols


# ---------------------------------------------------------------- shadow sidecar

class ShadowSidecar:
    """One Node process per service; multiplexes generations over stdio.

    @xterm/headless resolves via NODE_PATH: the image-wide install first
    (/opt/sc-shadow/node_modules, from the Dockerfile), then a local dev
    install at .super-coder/shadow/node_modules. Node ignores missing
    entries."""

    def __init__(self, script: str):
        self._script = script
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        env = dict(os.environ)
        node_path = f"{SHADOW_NODE_PATH}:{SHADOW_DIR / 'node_modules'}"
        if env.get("NODE_PATH"):
            node_path += ":" + env["NODE_PATH"]
        env["NODE_PATH"] = node_path
        self._proc = await asyncio.create_subprocess_exec(
            "node", self._script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            env=env,
        )
        asyncio.create_task(self._reader())
        # Liveness proof before the runtime declares itself available: a
        # sidecar that dies on require (e.g. @xterm/headless missing) never
        # answers, and every later request would wedge on a future nothing
        # resolves. Fail fast here instead.
        await self._request({"op": "ping"})

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
        try:
            return await asyncio.wait_for(fut, SHADOW_REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending.pop(op["id"], None)
            raise RuntimeError(
                f"shadow sidecar request {op['op']!r} timed out after "
                f"{SHADOW_REQUEST_TIMEOUT_S}s — sidecar dead or wedged"
            ) from None

    def create(self, gen: str, rows: int, cols: int) -> None:
        self._write({"op": "create", "gen": gen, "rows": rows, "cols": cols})

    def feed(self, gen: str, data: bytes) -> None:
        self._write({"op": "feed", "gen": gen,
                     "data": base64.b64encode(data).decode()})

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


# ---------------------------------------------------------------- generation

class Generation:
    """One interactive chat generation: tmux window, FIFO pump, bounded
    bridge, ordered input queue, attached clients. Keyed by DB session_id."""

    def __init__(self, runtime: "InterfaceRuntime", session_id: int,
                 shell_id: int, generation: int, rows: int, cols: int):
        self.runtime = runtime
        self.session_id = session_id
        self.shell_id = shell_id
        self.generation = generation
        self.sid = f"s{session_id}"  # tmux window name / log tag / shadow key
        self.rows, self.cols = rows, cols

        self.clients: set = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.continuity_broken = False

        self._bridge: collections.deque[bytes] = collections.deque()
        self._bridge_bytes = 0
        self._bridge_event = asyncio.Event()

        self.pane_id = ""
        self.pane_pid = 0
        self.pane_start_ticks = 0
        self.dbg_pump_bytes = 0
        self.dbg_fanout_bytes = 0
        self._fifo_keep_fd = -1
        self._pump_fd = -1
        self._pump_thread: threading.Thread | None = None
        self._tasks: list[asyncio.Task] = []
        self.terminating = False
        self.terminated = False

    # -- fanout -------------------------------------------------------------

    def broadcast_control(self, msg: dict) -> None:
        for client in list(self.clients):
            client.send_control(dict(msg))

    # -- pump / bridge (ported verbatim in shape from the spike) ------------

    def _pump_loop(self) -> None:
        """Blocking FIFO reader thread; never blocks on client I/O."""
        loop = self.runtime.loop
        while True:
            try:
                chunk = os.read(self._pump_fd, 65536)
            except OSError as exc:
                _log(self.sid, f"pump EXIT on OSError: {exc!r} "
                               f"(read={self.dbg_pump_bytes})")
                self._notify_exit(loop)
                return
            if not chunk:  # all writers gone
                _log(self.sid, f"pump EXIT on EOF (read={self.dbg_pump_bytes})")
                self._notify_exit(loop)
                return
            self.dbg_pump_bytes += len(chunk)
            try:
                loop.call_soon_threadsafe(self._on_pump_bytes, chunk)
            except RuntimeError:
                return  # loop closed (service shutting down)

    def _notify_exit(self, loop) -> None:
        # run_coroutine_threadsafe, not call_soon_threadsafe: the latter
        # would CALL the async def and drop the coroutine un-awaited —
        # the pump's pane-death signal never fired (real-tmux finding,
        # sprint 25 seq 11).
        coro = self.runtime._on_pump_exit(self)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()  # loop closed before it could be scheduled

    def _on_pump_bytes(self, chunk: bytes) -> None:
        if self.continuity_broken:
            return  # resync in progress; drop
        if self._bridge_bytes + len(chunk) > BRIDGE_MAX:
            self.continuity_broken = True
            self._bridge.clear()
            self._bridge_bytes = 0
            _log(self.sid, "ALERT continuity_broken: bridge overflow, "
                           "dropping queued bytes, resyncing")
            asyncio.create_task(self.runtime._resync_all(self))
            return
        self._bridge.append(chunk)
        self._bridge_bytes += len(chunk)
        self._bridge_event.set()

    async def _output_consumer(self) -> None:
        """Fanout only — real lifecycle/composer state lives in the DB
        (hooks), not in output-timing heuristics."""
        try:
            while True:
                await self._bridge_event.wait()
                while self._bridge:
                    chunk = self._bridge.popleft()
                    self._bridge_bytes -= len(chunk)
                    self.runtime.shadow.feed(self.sid, chunk)  # before fanout
                    for client in list(self.clients):
                        client.send_output(chunk)
                    self.dbg_fanout_bytes += len(chunk)
                self._bridge_event.clear()
        except asyncio.CancelledError:
            pass

    # -- input consumer -------------------------------------------------------

    async def _input_consumer(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if self.terminated:
                    continue
                if isinstance(item, HumanInput):
                    await self.runtime._do_human_input(self, item)
                elif isinstance(item, Resize):
                    await self.runtime._do_resize(self, item)
        except asyncio.CancelledError:
            pass

    # -- clients ---------------------------------------------------------------

    async def attach(self, client) -> None:
        # Snapshot covers every byte fed to the shadow so far; the client is
        # added to the fanout set only afterwards, so no byte is lost or
        # delivered twice.
        try:
            redraw = await self.runtime.shadow.snapshot(self.sid)
        except Exception as exc:
            _log(self.sid, f"attach: snapshot failed ({exc!r}), "
                           "falling back to capture-pane")
            self.runtime.shadow.create(self.sid, self.rows, self.cols)
            capture = await self.runtime.capture_pane(self.pane_id)
            self.runtime.shadow.feed(self.sid, capture)
            redraw = await self.runtime.shadow.snapshot(self.sid)
        client.send_redraw(redraw)
        self.clients.add(client)
        lifecycle, composer = await self.runtime.db_state(self.session_id)
        client.send_control({"type": "lifecycle", "lifecycle": lifecycle,
                             "composer": composer})
        client.send_control(
            await self.runtime.writer_control(self, client))

    def detach(self, client) -> None:
        self.clients.discard(client)

    # -- lifecycle -------------------------------------------------------------

    async def teardown(self, *, kill_window: bool) -> None:
        """Release runtime resources. kill_window=True also removes the tmux
        window (termination); False leaves the pane alive for reattach."""
        if self.terminated:
            return
        self.terminating = True
        self.terminated = True
        _log(self.sid, "tearing down generation")
        for task in self._tasks:
            task.cancel()
        if kill_window:
            for client in list(self.clients):
                client.send_control({"type": "error", "code": "terminated"})
                client.close(1000, "generation terminated")
            if self.pane_id:
                # A generation abandoned mid-spawn can have no pane yet —
                # never hand tmux an empty target (SC-064).
                try:
                    await self.runtime.tmux("kill-window", "-t", self.pane_id)
                except Exception:
                    pass
        self.runtime.shadow.dispose(self.sid)
        if self._pump_fd >= 0:
            try:
                os.close(self._pump_fd)
            except OSError:
                pass
            self._pump_fd = -1
        self.runtime._close_fifo_keep(self)


# ---------------------------------------------------------------- runtime

class InterfaceRuntime:
    """The broker facade the API layers drive. All async methods run on the
    single asyncio loop captured by start(); the sync HTTP layer uses call().

    Clients are duck-typed objects (see api/interface_ws.py) with:
        session_id, role ("viewer"|"writer"), client_id,
        lease_id, lease_token, last_hb, hb_stale
        send_control(dict) / send_output(bytes) / send_redraw(bytes)
        close(code, reason)
    """

    def __init__(self, db_path: str, run_dir: str | None = None,
                 shadow_script: str | None = None):
        self.db_path = str(db_path)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.run_dir = run_dir or str(Path(self.db_path).parent / "run"
                                        / "interface")
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        os.chmod(self.run_dir, stat.S_IRWXU)  # private tmux server dir
        self.sock = os.path.join(self.run_dir, "tmux.sock")
        self.shadow = ShadowSidecar(
            shadow_script or str(SHADOW_DIR / "sidecar.js"))
        self.generations: dict[int, Generation] = {}
        self.available = False
        self.unavailable_reason = "start() not called"
        self.on_unexpected_exit = None  # sync callable(session_id), set by routes
        self.wake_coordinator = None    # interface_wake.WakeCoordinator (start())
        self._tmux_session_started = False
        self._reaper: asyncio.Task | None = None
        self._tickets: dict[str, dict] = {}
        self._tickets_lock = threading.Lock()

    # -- availability -----------------------------------------------------------

    def _check_available(self) -> str | None:
        """None if the tmux/node stack is usable, else the reason string."""
        if shutil.which("tmux") is None:
            return "tmux not found on PATH"
        if shutil.which("node") is None:
            return "node not found on PATH (shadow sidecar)"
        version = _tmux_version()
        if version is None:
            return "could not parse `tmux -V`"
        if version < TMUX_MIN_VERSION:
            need = ".".join(map(str, TMUX_MIN_VERSION))
            got = ".".join(map(str, version))
            return f"tmux {got} < required {need}"
        return None

    def _require_available(self) -> None:
        if not self.available:
            raise InterfaceUnavailable(
                f"interface runtime unavailable: {self.unavailable_reason}")

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        reason = self._check_available()
        if reason is not None:
            self.available = False
            self.unavailable_reason = reason
            _log("boot", f"UNAVAILABLE: {reason} — review UI only")
            return
        try:
            await self.shadow.start()
        except Exception as exc:  # noqa: BLE001 — any boot failure = unavailable
            self.available = False
            self.unavailable_reason = f"shadow sidecar did not answer: {exc}"
            _log("boot", f"UNAVAILABLE: {self.unavailable_reason} — "
                         "review UI only")
            return
        self.available = True
        self.unavailable_reason = ""
        self._reaper = asyncio.create_task(self._heartbeat_reaper())
        # Reattach generations that survived a service restart (the tmux
        # server and FIFOs are ours, stable under run_dir). Lost sessions get
        # the SAME DB transition as a live-detected pane death (occupied →
        # lost/unreconciled) via the routes layer's callback — the rail must
        # never show a dead harness as occupied.
        sessions = await asyncio.to_thread(self._occupied_sessions)
        result = await self.reattach_all(sessions)
        if result["reattached"] or result["lost"]:
            _log("boot", f"reattach: {len(result['reattached'])} ok, "
                         f"lost {result['lost']}")
        callback = self.on_unexpected_exit
        for session_id in result["lost"]:
            if callback is None:
                _log("boot", f"s{session_id} lost on reattach but no "
                             "on_unexpected_exit callback is bound")
                continue
            try:
                await asyncio.to_thread(callback, session_id)
            except Exception as exc:  # noqa: BLE001 — keep booting
                _log("boot", f"lost-transition for s{session_id} failed: "
                             f"{exc!r}")
        # Wake coordinator (sprint 25 seq 8): the event-driven drain of
        # queued sprint wake work through the broker-owned input path —
        # never a direct tmux send. Signals arrive from message ingress,
        # hook callbacks, certifications, and binding arms; startup_pass is
        # the ONE reconciliation scan (spec Event Ingress: no interval model
        # scan, no steady wake timer).
        self.wake_coordinator = interface_wake.WakeCoordinator(
            self.db_path, writer_factory=self.wake_writer,
            unmanaged_probe=self.unmanaged_writable_client)
        self.wake_coordinator.start(self.loop)
        interface_wake.bind(self.wake_coordinator)
        self.wake_coordinator.startup_pass()

    # -- wake submission (sprint 25 seq 8 — the API-owned input path) -----------

    def wake_writer(self, session_id: int):
        """The broker-owned writer for one wake submission: preflight the
        pane (a failure PROVES no byte moved → PreSendError — the definite
        pre-send failure that rides the bounded 1s/5s/30s retries), then one
        indivisible send-keys of the fixed prompt + Enter. This is the ONLY
        path a wake reaches tmux; the crash-window parking in the broker is
        unbypassable from here."""
        payload = interface_broker.WAKE_PROMPT.encode() + b"\r"

        def writer(n: int) -> None:
            assert n == len(payload)
            gen = self.generations.get(session_id)
            if gen is None or gen.terminated:
                raise interface_broker.PreSendError(
                    f"session {session_id} generation not live in runtime")
            try:
                subprocess.run(
                    ["tmux", "-S", self.sock, "display-message", "-p",
                     "-t", gen.pane_id, "#{pane_id}"],
                    capture_output=True, check=True,
                    timeout=TMUX_SYNC_TIMEOUT_S)
            except Exception as exc:
                raise interface_broker.PreSendError(
                    f"wake preflight failed for {gen.pane_id}: "
                    f"{exc!r}") from exc
            self._send_keys_sync(gen.pane_id, payload)

        return writer

    def unmanaged_writable_client(self, session_id: int) -> bool:
        """Decision #15 probe: any READ-WRITE tmux client attached to our
        private server is unmanaged — the broker never attaches a client,
        and a read-only diagnostic client (spec Tmux Runtime) is tolerated.
        tmux itself being unreachable — or wedged and never answering — is
        NOT reported here (the wake writer's preflight owns that failure, as
        definite pre-send); the timeout keeps a wedged server from hanging
        the drain thread (SC-013)."""
        if session_id not in self.generations:
            return False
        try:
            out = subprocess.run(
                ["tmux", "-S", self.sock, "list-clients",
                 "-F", "#{client_readonly}"],
                capture_output=True, check=True, text=True,
                timeout=TMUX_SYNC_TIMEOUT_S).stdout
        except Exception:  # noqa: BLE001
            return False
        return any(line.strip() == "0" for line in out.splitlines())

    async def stop(self) -> None:
        """Release runtime resources; panes stay alive for reattach."""
        interface_wake.bind(None)
        self.wake_coordinator = None
        if self._reaper:
            self._reaper.cancel()
        for gen in list(self.generations.values()):
            await gen.teardown(kill_window=False)
        self.generations.clear()
        await self.shadow.stop()

    def call(self, coro):
        """Thread-safe facade for the sync HTTP layer."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(
            timeout=30)

    def _occupied_sessions(self) -> list[dict]:
        con = db_driver.connect(self.db_path)
        try:
            rows = con.execute(
                "SELECT session_id, shell_id, generation, tmux_pane_id, "
                "pane_pid, pane_start_ticks FROM interface_sessions "
                "WHERE occupancy='occupied' AND tmux_pane_id IS NOT NULL "
                "AND pane_pid IS NOT NULL AND pane_start_ticks IS NOT NULL"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # -- tickets (single-use stream upgrade credentials) --------------------------

    def mint_ticket(self, *, session_id: int, role: str, client_id: str,
                    lease_id: int | None = None,
                    lease_token: str | None = None) -> dict:
        """Mint a single-use, 60s stream ticket bound to
        session/role/client/lease. Safe to call from any thread."""
        if role not in ("viewer", "writer"):
            raise ValueError("role must be viewer|writer")
        ticket = secrets.token_hex(24)
        now = time.monotonic()
        with self._tickets_lock:
            expired = [t for t, v in self._tickets.items()
                       if v["expires"] < now]
            for t in expired:
                self._tickets.pop(t, None)
            self._tickets[ticket] = {
                "session_id": session_id, "role": role,
                "client_id": client_id, "lease_id": lease_id,
                "lease_token": lease_token if role == "writer" else None,
                "expires": now + TICKET_TTL_S,
            }
        return {"ticket": ticket, "expires_in": TICKET_TTL_S}

    def consume_ticket(self, ticket_id: str, session_id: int) -> dict | None:
        """Validate + consume (single-use). None on unknown, expired, or
        wrong-session ticket."""
        with self._tickets_lock:
            ticket = self._tickets.pop(ticket_id, None)
        if ticket is None or ticket["expires"] < time.monotonic():
            return None
        if ticket["session_id"] != session_id:
            return None
        return ticket

    # -- tmux primitives ----------------------------------------------------------

    async def tmux(self, *args: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-S", self.sock, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux {' '.join(args)} failed: "
                f"{err.decode(errors='replace').strip()}")
        return out

    def _send_keys_sync(self, pane_id: str, payload: bytes) -> None:
        """The injected broker writer: chunked send-keys -H, ≤512 bytes per
        invocation, called exactly once per accepted frame (as many tmux
        calls as chunks). Runs in the broker worker thread. Every call is
        timeout-bounded: a wedged server raises (TimeoutExpired — ambiguous,
        the caller parks delivery_unknown) instead of hanging the thread
        (SC-013)."""
        for off in range(0, len(payload), SENDKEYS_CHUNK):
            chunk = payload[off:off + SENDKEYS_CHUNK]
            subprocess.run(
                ["tmux", "-S", self.sock, "send-keys", "-t", pane_id, "-H",
                 *[f"{b:02x}" for b in chunk]],
                capture_output=True, check=True,
                timeout=TMUX_SYNC_TIMEOUT_S)

    async def send_keys(self, pane_id: str, payload: bytes) -> None:
        for off in range(0, len(payload), SENDKEYS_CHUNK):
            chunk = payload[off:off + SENDKEYS_CHUNK]
            await self.tmux("send-keys", "-t", pane_id, "-H",
                            *[f"{b:02x}" for b in chunk])

    async def capture_pane(self, pane_id: str) -> bytes:
        """Screen text for a shadow rebuild. tmux 3.5a emits a trailing
        newline after the last row; replayed as a stream into a same-sized
        zero-scrollback shadow terminal that extra newline scrolls the top
        row off the grid (real-tmux finding, sprint 25 seq 11 — a pane whose
        content sat on row 1 rebuilt as a blank screen). Strip exactly one
        trailing newline: 24 rows replay into 24 rows with no scroll."""
        out = await self.tmux("capture-pane", "-epN", "-t", pane_id)
        if out.endswith(b"\n"):
            out = out[:-1]
        return out

    # -- spawn -----------------------------------------------------------------

    def _fifo_path(self, session_id: int) -> str:
        return os.path.join(self.run_dir, f"fifo-s{session_id}")

    def _sentinel_path(self, session_id: int) -> str:
        return os.path.join(self.run_dir, f"ready-s{session_id}")

    def _open_fifo(self, gen: Generation) -> None:
        """Open the blocking pump reader. A FIFO O_RDONLY open blocks until a
        writer exists, so pre-open RDWR|NONBLOCK as the stand-in writer. The
        stand-in is held until _pipe_pane PROVES tmux's writer attached (the
        pump thread starts immediately after; a closed stand-in plus a
        still-starting `cat` reads as an instant false EOF — the pane-death
        signal — before any byte could flow, real-tmux finding seq 11). A
        stand-in held FOREVER would be just as wrong: the pump's read must
        return EOF when the pane dies and tmux's `cat` writer exits, and
        that EOF is the pump's pane-death signal."""
        gen._fifo_keep_fd = os.open(  # noqa: SLF001
            self._fifo_path(gen.session_id), os.O_RDWR | os.O_NONBLOCK)
        gen._pump_fd = os.open(  # noqa: SLF001
            self._fifo_path(gen.session_id), os.O_RDONLY)

    def _close_fifo_keep(self, gen: Generation) -> None:
        if gen._fifo_keep_fd >= 0:  # noqa: SLF001
            try:
                os.close(gen._fifo_keep_fd)  # noqa: SLF001
            except OSError:
                pass
            gen._fifo_keep_fd = -1  # noqa: SLF001

    async def _pipe_pane(self, gen: Generation) -> None:
        """Attach the pane's output pipe and drop the stand-in writer only
        once the pipe writer is PROVEN attached: the pipe command holds its
        own write fd (9) across the marker touch, so the marker means a real
        writer owns the FIFO. Without this handshake the pump's first read
        races tmux's fork of `cat` and loses (EOF with zero bytes read)."""
        fifo = self._fifo_path(gen.session_id)
        marker = fifo + ".pipeup"
        if os.path.exists(marker):
            os.unlink(marker)
        await self.tmux(
            "pipe-pane", "-t", gen.pane_id,
            f"exec 9>{shlex.quote(fifo)} && touch {shlex.quote(marker)} "
            f"&& exec cat >&9")
        deadline = time.monotonic() + TMUX_SYNC_TIMEOUT_S
        while not os.path.exists(marker):
            if gen.terminated:
                # Abandoned mid-spawn (SC-064) — the writer will never
                # attach; let spawn's abort check raise SpawnAborted.
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"pipe-pane writer never attached for {fifo}")
            await asyncio.sleep(0.02)
        os.unlink(marker)
        self._close_fifo_keep(gen)

    def _start_consumers(self, gen: Generation) -> None:
        gen._pump_thread = threading.Thread(  # noqa: SLF001
            target=gen._pump_loop, name=f"pump-{gen.sid}", daemon=True)
        gen._pump_thread.start()  # noqa: SLF001
        gen._tasks = [  # noqa: SLF001
            asyncio.create_task(gen._output_consumer()),
            asyncio.create_task(gen._input_consumer()),
        ]

    async def _abort_if_torn_down(self, gen: Generation) -> None:
        """Cancel-during-spawn convergence (SC-064): abandon() popped and
        tore down this generation while its spawn was in flight. Kill the
        just-created pane by its exact identity — provably ours, created
        seconds ago — and refuse to complete the spawn: a completed spawn
        on an already-ended row is a live harness nobody can close."""
        if not gen.terminated:
            return
        if gen.pane_id:
            try:
                await self.tmux("kill-window", "-t", gen.pane_id)
            except Exception:  # noqa: BLE001,S110 — already dead is the goal
                pass
        raise SpawnAborted(
            f"session {gen.session_id} abandoned during spawn")

    async def spawn(self, *, session_id: int, shell_id: int, generation: int,
                    worktree: str, sc_path: str, token_path: str,
                    rows: int, cols: int,
                    command: list[str] | None = None) -> dict:
        """Create the tmux window + FIFO pump + shadow for one generation.
        `command` overrides the exec line — tests only. Returns the pane
        identity the caller persists to interface_sessions.

        The generation is registered BEFORE the tmux work so a cancel
        start landing mid-spawn (SC-064) can see and tear it down through
        abandon(); the abort checks then stop the spawn before the harness
        ever boots and SpawnAborted tells the caller the session was
        cancelled — never a completed spawn on an ended row."""
        self._require_available()
        gen = Generation(self, session_id, shell_id, generation, rows, cols)
        self.generations[session_id] = gen
        try:
            fifo = self._fifo_path(session_id)
            sentinel = self._sentinel_path(session_id)
            for stale in (fifo, sentinel):
                if os.path.exists(stale):
                    os.unlink(stale)
            os.mkfifo(fifo)
            self._open_fifo(gen)

            if command is not None:
                exec_line = shlex.join(command)
            else:
                exec_line = (f"{shlex.quote(sc_path)} interface-exec "
                             f"{shlex.quote(token_path)}")
            shell_line = (
                f"cd {shlex.quote(worktree)} && "
                f"while [ ! -f {shlex.quote(sentinel)} ]; do sleep 0.02; done; "
                f"exec {exec_line}")
            window = gen.sid
            if not self._tmux_session_started:
                await self.tmux("new-session", "-d", "-s", TMUX_SESSION,
                                "-n", window, "-x", str(cols), "-y", str(rows),
                                shell_line)
                self._tmux_session_started = True
            else:
                try:
                    await self.tmux("new-window", "-d", "-t", f"{TMUX_SESSION}:",
                                    "-n", window, shell_line)
                except RuntimeError as exc:
                    if "no server running" not in str(exc):
                        raise
                    # last kill-window took the session (and server) down with it
                    self._tmux_session_started = False
                    await self.tmux("new-session", "-d", "-s", TMUX_SESSION,
                                    "-n", window, "-x", str(cols), "-y",
                                    str(rows), shell_line)
                    self._tmux_session_started = True
                else:
                    await self.tmux("resize-window", "-t",
                                    f"{TMUX_SESSION}:{window}",
                                    "-x", str(cols), "-y", str(rows))
            out = await self.tmux("display-message", "-p", "-t",
                                  f"{TMUX_SESSION}:{window}",
                                  "#{pane_id} #{pane_pid}")
            pane_id, pane_pid = out.decode().split()
            gen.pane_id, gen.pane_pid = pane_id, int(pane_pid)
            gen.pane_start_ticks = await asyncio.to_thread(
                _read_start_ticks, gen.pane_pid)
            await self._abort_if_torn_down(gen)

            self.shadow.create(gen.sid, rows, cols)
            await self._pipe_pane(gen)
            # boot the harness only when the spawn was not cancelled: zero
            # lost boot bytes, and never a live harness on an ended row.
            await self._abort_if_torn_down(gen)
            open(sentinel, "w").close()

            self._start_consumers(gen)
            _log(gen.sid, f"session spawned: shell={shell_id} gen={generation} "
                          f"pane={pane_id} pid={pane_pid} {cols}x{rows}")
            return {"pane_id": pane_id, "pane_pid": gen.pane_pid,
                    "pane_start_ticks": gen.pane_start_ticks,
                    "tmux_socket": self.sock, "tmux_session": TMUX_SESSION,
                    "tmux_window": window}
        except Exception:
            # A failed spawn leaves NO generation registered (the pre-SC-064
            # shape): a later terminate/abandon must not mistake a
            # half-spawned entry for a live one. The window itself is NOT
            # killed here — an ambiguous tmux outcome stays for the
            # unreconciled path to judge (only a proven-abandoned spawn
            # kills, in _abort_if_torn_down, by exact identity).
            if self.generations.get(session_id) is gen:
                self.generations.pop(session_id, None)
            raise

    def get_generation(self, session_id: int) -> Generation | None:
        return self.generations.get(session_id)

    # -- reattach after service restart ---------------------------------------------

    async def _pane_exists(self, pane_id: str) -> bool:
        try:
            out = await self.tmux("display-message", "-p", "-t", pane_id,
                                  "#{pane_id}")
            return out.decode().strip() == pane_id
        except Exception:
            return False

    def _verify_identity(self, pane_id: str, pane_pid: int,
                         pane_start_ticks: int) -> bool:
        """Exact identity proof: the stored pane exists in OUR tmux server
        with the stored pid, and that pid's /proc start ticks match. Fail
        closed on any unreadable field."""
        try:
            out = subprocess.run(
                ["tmux", "-S", self.sock, "display-message", "-p", "-t",
                 pane_id, "#{pane_id} #{pane_pid}"],
                capture_output=True, text=True, check=True, timeout=10)
            got_pane, got_pid = out.stdout.split()
            if got_pane != pane_id or int(got_pid) != pane_pid:
                return False
            return _read_start_ticks(pane_pid) == pane_start_ticks
        except Exception:
            return False

    async def reattach_all(self, sessions: list[dict]) -> dict:
        """Rebind occupied sessions that survived a service restart. On ANY
        identity mismatch the DB row is left alone and the session reported
        lost; start() walks the lost list through the routes layer's
        on_unexpected_exit callback so it lands the same occupied →
        lost/unreconciled transition as a live-detected pane death."""
        self._require_available()
        reattached: list[int] = []
        lost: list[int] = []
        for row in sessions:
            session_id = row["session_id"]
            pane_id = row["tmux_pane_id"]
            ok = await asyncio.to_thread(
                self._verify_identity, pane_id, row["pane_pid"],
                row["pane_start_ticks"])
            if not ok:
                _log(f"s{session_id}", "reattach: identity mismatch — lost")
                lost.append(session_id)
                continue
            out = await self.tmux("display-message", "-p", "-t", pane_id,
                                  "#{pane_width} #{pane_height}")
            cols, rows = (int(v) for v in out.decode().split())
            gen = Generation(self, session_id, row["shell_id"],
                             row["generation"], rows, cols)
            gen.pane_id = pane_id
            gen.pane_pid = row["pane_pid"]
            gen.pane_start_ticks = row["pane_start_ticks"]
            fifo = self._fifo_path(session_id)
            if not os.path.exists(fifo):
                # FIFO lost with the restart: recreate and re-issue the pipe
                # (the old pipe-pane died with its writer).
                os.mkfifo(fifo)
                self._open_fifo(gen)
                await self._pipe_pane(gen)
            else:
                self._open_fifo(gen)
                # No new pipe is issued here: the surviving `cat` is already
                # the writer (or it died with the old service — then the
                # pump's instant EOF is exactly the stale-pipe signal the
                # exit path turns into an alert / lost transition).
                self._close_fifo_keep(gen)
            # The shadow is volatile: rebuild from the visible pane.
            self.shadow.create(gen.sid, rows, cols)
            capture = await self.capture_pane(pane_id)
            self.shadow.feed(gen.sid, capture)
            self.generations[session_id] = gen
            self._start_consumers(gen)
            _log(gen.sid, f"reattached: pane={pane_id} pid={gen.pane_pid}")
            reattached.append(session_id)
        return {"reattached": reattached, "lost": lost}

    # -- terminate ------------------------------------------------------------

    async def terminate(self, session_id: int, *, force: bool = False) -> dict:
        """Signal the pane process only after exact identity proof — never
        signal an uncertain process. DB occupancy/lifecycle transitions are
        the caller's job; this returns what it verified."""
        self._require_available()
        gen = self.generations.get(session_id)
        if gen is None:
            return {"terminated": False, "reason": "not_running"}
        row = await asyncio.to_thread(self._session_identity, session_id)
        if row is None:
            return {"terminated": False, "reason": "identity_mismatch"}
        pane_id, pane_pid, pane_ticks, generation = row
        verified = await asyncio.to_thread(
            self._verify_identity, pane_id, pane_pid, pane_ticks)
        if not verified:
            _log(gen.sid, "terminate: identity mismatch — refusing to signal")
            return {"terminated": False, "reason": "identity_mismatch"}

        gen.terminating = True
        os.kill(pane_pid, signal.SIGTERM)
        dead = await self._wait_gone(pane_id, pane_pid, pane_ticks,
                                     GRACEFUL_TERMINATE_S)
        if not dead and not force:
            gen.terminating = False
            return {"terminated": False, "reason": "graceful_timeout",
                    "pid": pane_pid, "generation": generation}
        if not dead:
            # Re-verify before SIGKILL: the grace window is long enough for
            # PID reuse, and the rule is never kill an uncertain process.
            verified = await asyncio.to_thread(
                self._verify_identity, pane_id, pane_pid, pane_ticks)
            if not verified:
                gen.terminating = False
                _log(gen.sid, "force: identity changed during grace window — "
                              "refusing SIGKILL")
                return {"terminated": False, "reason": "identity_mismatch"}
            os.kill(pane_pid, signal.SIGKILL)
            await self._wait_gone(pane_id, pane_pid, pane_ticks,
                                  GRACEFUL_TERMINATE_S)
        await gen.teardown(kill_window=True)
        self.generations.pop(session_id, None)
        _log(gen.sid, f"terminated (force={force})")
        return {"terminated": True, "generation": generation}

    async def verify_identity(self, session_id: int) -> bool:
        """Public exact-identity proof for the routes layer's reconcile
        route: the stored pane exists in OUR tmux server with the stored
        pid, and that pid's /proc start ticks match. Fail closed — False on
        any unreadable or mismatched field."""
        self._require_available()
        row = await asyncio.to_thread(self._session_identity, session_id)
        if row is None:
            return False
        pane_id, pane_pid, pane_ticks, _generation = row
        return await asyncio.to_thread(
            self._verify_identity, pane_id, pane_pid, pane_ticks)

    async def prove_absence(self, session_id: int) -> bool:
        """Absence proof for the operator close path (spec Occupancy Model:
        the operator closes an unreconciled session only after absence is
        proved). False if the pane still exists in our tmux server OR a
        process with the recorded exact identity still sits at the pid —
        anything uncertain refuses the close."""
        self._require_available()
        row = await asyncio.to_thread(self._session_identity, session_id)
        if row is None:
            return True  # no recorded identity — nothing live to disprove
        pane_id, pane_pid, pane_ticks, _generation = row
        if await self._pane_exists(pane_id):
            return False
        if await asyncio.to_thread(_pid_alive, pane_pid, pane_ticks):
            return False  # the process lives on outside our tmux server
        return True

    async def abandon(self, session_id: int) -> None:
        """Drop a generation's runtime resources WITHOUT signaling anything
        (operator close after proved absence). The tmux window is already
        gone — kill-window is best-effort cleanup of any remnant."""
        gen = self.generations.pop(session_id, None)
        if gen is not None:
            await gen.teardown(kill_window=True)

    def _session_identity(self, session_id: int):
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT tmux_pane_id, pane_pid, pane_start_ticks, generation "
                "FROM interface_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            if row is None or any(v is None for v in row[:3]):
                return None
            return (row[0], row[1], row[2], row[3])
        finally:
            con.close()

    async def _wait_gone(self, pane_id: str, pid: int, start_ticks: int,
                         timeout: float) -> bool:
        """Gone = the pane is gone from our tmux server AND no process with
        our exact identity sits at the pid. A recycled PID (ticks differ)
        counts as gone — it is not our process."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid_gone = not await asyncio.to_thread(
                _pid_alive, pid, start_ticks)
            pane_gone = not await self._pane_exists(pane_id)
            if pid_gone and pane_gone:
                return True
            await asyncio.sleep(0.1)
        return False

    # -- unexpected pane death -------------------------------------------------

    async def _on_pump_exit(self, gen: Generation) -> None:
        """Pump EOF/OSError: if the pane is really gone and we weren't
        tearing it down, hand off to the routes layer (lifecycle lost /
        occupancy unreconciled)."""
        if gen.terminating or gen.session_id not in self.generations:
            return
        if await self._pane_exists(gen.pane_id):
            # The pane outlived its pipe writer (someone killed tmux's `cat`):
            # output no longer flows. Not a death transition — but never
            # silent.
            _log(gen.sid, "pump EOF with a live pane — pipe writer lost")
            await asyncio.to_thread(self._alert, gen.session_id,
                                    "interface_pump_lost", "critical")
            return
        _log(gen.sid, "pane died unexpectedly")
        callback = self.on_unexpected_exit
        if callback is not None:
            await asyncio.to_thread(callback, gen.session_id)

    # -- output resync -------------------------------------------------------------

    async def _resync_all(self, gen: Generation) -> None:
        """continuity_broken recovery: rebuild shadow from capture-pane,
        resync clients, raise the deduped alert."""
        try:
            await asyncio.to_thread(self._alert, gen.session_id,
                                    "interface_continuity_broken", "warning")
            self.shadow.create(gen.sid, gen.rows, gen.cols)
            capture = await self.capture_pane(gen.pane_id)
            self.shadow.feed(gen.sid, capture)
            redraw = await self.shadow.snapshot(gen.sid)
            for client in list(gen.clients):
                client.send_control({"type": "resync",
                                     "reason": "continuity_broken"})
                client.send_redraw(redraw)
            gen.continuity_broken = False
            _log(gen.sid, f"resync complete, {len(gen.clients)} client(s)")
        except Exception as exc:  # pane died etc.
            _log(gen.sid, f"resync failed: {exc!r}")
            gen.broadcast_control({"type": "error", "code": "resync_failed"})

    def _alert(self, session_id: int, reason: str, severity: str) -> None:
        """INSERT OR IGNORE shape replicated from interface_broker._alert
        (private there): deduplicated while open via the partial unique
        index on planner_alerts."""
        con = db_driver.connect(self.db_path)
        try:
            con.execute(
                "INSERT OR IGNORE INTO planner_alerts "
                "(session_id, severity, reason, dedupe_key) "
                "VALUES (?,?,?,?)",
                (session_id, severity, reason, f"{session_id}|-|{reason}"))
            con.commit()
        finally:
            con.close()

    # -- durable input path ----------------------------------------------------

    def _accept_input(self, gen: Generation, client, seq: int,
                      payload: bytes):
        """The whole broker protocol in one worker-thread transaction scope.
        Returns (result, lifecycle, composer)."""
        con = db_driver.connect(self.db_path)
        try:
            # This client's lease token must be the session's CURRENT lease,
            # fenced to this generation (current_writer doesn't select
            # token_hash/generation, so the query is extended inline here).
            lease = con.execute(
                "SELECT lease_id, generation, token_hash "
                "FROM interface_writer_leases "
                "WHERE session_id=? AND revoked_at IS NULL",
                (gen.session_id,)).fetchone()
            token_hash = hashlib.sha256(
                (client.lease_token or "").encode()).hexdigest()
            if (lease is None or lease["generation"] != gen.generation
                    or lease["token_hash"] != token_hash):
                raise _Rejected("writer_revoked")

            def writer(n):
                assert n == len(payload)
                self._send_keys_sync(gen.pane_id, payload)

            result = interface_broker.accept_human_input(
                con, gen.session_id, seq, len(payload), writer)
            state = con.execute(
                "SELECT s.lifecycle, i.composer FROM interface_sessions s "
                "LEFT JOIN interface_input_state i "
                "ON i.session_id=s.session_id WHERE s.session_id=?",
                (gen.session_id,)).fetchone()
            return result, state[0], state[1]
        finally:
            con.close()

    async def _do_human_input(self, gen: Generation, item: HumanInput) -> None:
        client, seq, payload = item.client, item.seq, item.payload
        try:
            result, lifecycle, composer = await asyncio.to_thread(
                self._accept_input, gen, client, seq, payload)
        except _Rejected as exc:
            _log(gen.sid, f"reject seq={seq}: {exc.reason}")
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": exc.reason})
            return
        except interface_broker.BrokerError as exc:
            reason = _reject_reason(exc)
            _log(gen.sid, f"reject seq={seq}: {reason}")
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": reason})
            return
        except Exception as exc:  # noqa: BLE001 — writer() re-raise
            # tmux write failed: accept_human_input already parked
            # delivery_unknown, revoked the writer, alerted, and committed.
            _log(gen.sid, f"seq={seq} write failed: {exc!r} — parked "
                          "delivery_unknown")
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": "delivery_unknown"})
            await self._broadcast_state(gen)
            return
        if result["duplicate"]:
            client.send_control({"type": "input_ack", "seq": result["ack"],
                                 "replayed": True})
            return
        _log(gen.sid, f"forwarded seq={seq} ({len(payload)} bytes)")
        client.send_control({"type": "input_ack", "seq": result["ack"]})
        # The pending commit dirtied the composer; broadcast DB state.
        gen.broadcast_control({"type": "lifecycle", "lifecycle": lifecycle,
                               "composer": composer})

    async def _broadcast_state(self, gen: Generation) -> None:
        lifecycle, composer = await self.db_state(gen.session_id)
        gen.broadcast_control({"type": "lifecycle", "lifecycle": lifecycle,
                               "composer": composer})

    async def _do_resize(self, gen: Generation, item: Resize) -> None:
        gen.rows, gen.cols = item.rows, item.cols
        await self.tmux("resize-window", "-t", gen.pane_id,
                        "-x", str(item.cols), "-y", str(item.rows))
        self.shadow.resize(gen.sid, item.rows, item.cols)
        _log(gen.sid, f"resize {item.cols}x{item.rows}")

    # -- frame entry points (called by the WS layer, on this loop) ------------

    def enqueue_input(self, client, seq: int, payload: bytes) -> None:
        gen = self.generations.get(client.session_id)
        if gen is None or gen.terminated:
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": "stale_generation"})
            return
        gen.queue.put_nowait(HumanInput(client, seq, payload))

    def enqueue_resize(self, client, rows: int, cols: int) -> None:
        gen = self.generations.get(client.session_id)
        if gen is None or gen.terminated:
            return
        gen.queue.put_nowait(Resize(rows, cols))

    # -- attach / detach --------------------------------------------------------

    async def attach(self, client) -> None:
        gen = self.generations[client.session_id]
        client.last_hb = time.monotonic()
        client.hb_stale = False
        await gen.attach(client)

    def detach(self, client) -> None:
        gen = self.generations.get(client.session_id)
        if gen is not None:
            gen.detach(client)
        if client.role == "writer" and client.lease_id is not None:
            # A detached writer's DB lease dies with it (seq 6 liveness);
            # the revoke is fenced so a stale detach never clobbers a lease
            # the client re-acquired. A viewer detaches nothing.
            generation = gen.generation if gen is not None else None
            asyncio.create_task(self._revoke_writer_lease(client, generation))

    # -- DB state reads (for controls) ---------------------------------------------

    def _db_state_sync(self, session_id: int) -> tuple[str, str]:
        con = db_driver.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT s.lifecycle, i.composer FROM interface_sessions s "
                "LEFT JOIN interface_input_state i "
                "ON i.session_id=s.session_id WHERE s.session_id=?",
                (session_id,)).fetchone()
            if row is None:
                return "starting", "unknown"
            return row[0] or "starting", row[1] or "unknown"
        finally:
            con.close()

    async def db_state(self, session_id: int) -> tuple[str, str]:
        return await asyncio.to_thread(self._db_state_sync, session_id)

    def _writer_lease_sync(self, session_id: int):
        con = db_driver.connect(self.db_path)
        try:
            return con.execute(
                "SELECT lease_id, client_id, token_hash "
                "FROM interface_writer_leases "
                "WHERE session_id=? AND revoked_at IS NULL",
                (session_id,)).fetchone()
        finally:
            con.close()

    async def writer_control(self, gen: Generation, client) -> dict:
        """The {"type":"writer",...} control for one client: active (this
        client's lease is current), held (someone else's), or none."""
        lease = await asyncio.to_thread(self._writer_lease_sync,
                                        gen.session_id)
        if lease is None:
            state = "none"
        elif (client.role == "writer" and client.lease_id == lease["lease_id"]
                and hashlib.sha256((client.lease_token or "").encode())
                .hexdigest() == lease["token_hash"]):
            state = "active"
        else:
            state = "held"
        msg = {"type": "writer", "state": state}
        if getattr(client, "hb_stale", False):
            msg["stale"] = True
        return msg

    # -- writer lease liveness (seq 6): the DB lease tracks the client -------

    def heartbeat(self, client) -> None:
        gen = self.generations.get(client.session_id)
        if gen is None or client.role != "writer":
            return
        was_stale = getattr(client, "hb_stale", False)
        client.last_hb = time.monotonic()
        if client.lease_id is not None:
            # Durable heartbeat: the dead-lease sweep reads heartbeat_at.
            asyncio.create_task(self._touch_writer_lease(client))
        if was_stale:
            client.hb_stale = False
            asyncio.create_task(self._broadcast_writer_state(gen))

    def _touch_lease_sync(self, lease_id, lease_token) -> None:
        token_hash = hashlib.sha256((lease_token or "").encode()).hexdigest()
        con = db_driver.connect(self.db_path)
        try:
            con.execute(
                "UPDATE interface_writer_leases SET heartbeat_at=datetime('now')"
                " WHERE lease_id=? AND token_hash=? AND revoked_at IS NULL",
                (lease_id, token_hash))
            con.commit()
        finally:
            con.close()

    async def _touch_writer_lease(self, client) -> None:
        try:
            await asyncio.to_thread(self._touch_lease_sync, client.lease_id,
                                    client.lease_token)
        except Exception as exc:  # noqa: BLE001 — a missed touch is retried
            _log(f"s{client.session_id}", f"lease heartbeat failed: {exc!r}")

    def _revoke_lease_sync(self, lease_id, lease_token, generation) -> bool:
        """Liveness revoke, fenced by lease id + token (+ generation when
        known): a late detach of a dead client must never clobber the lease
        it re-acquired. True iff this call did the revoking."""
        token_hash = hashlib.sha256((lease_token or "").encode()).hexdigest()
        sql = ("UPDATE interface_writer_leases SET revoked_at=datetime('now'),"
               " revoke_reason='liveness' WHERE lease_id=? AND token_hash=?"
               " AND revoked_at IS NULL")
        args: list = [lease_id, token_hash]
        if generation is not None:
            sql += " AND generation=?"
            args.append(generation)
        con = db_driver.connect(self.db_path)
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    async def _revoke_writer_lease(self, client, generation) -> None:
        try:
            revoked = await asyncio.to_thread(
                self._revoke_lease_sync, client.lease_id, client.lease_token,
                generation)
        except Exception as exc:  # noqa: BLE001 — never break the detach path
            _log(f"s{client.session_id}", f"lease revoke failed: {exc!r}")
            return
        if not revoked:
            return  # already released / taken over / ended — nothing to say
        _log(f"s{client.session_id}", f"writer lease {client.lease_id} revoked"
                                     " (liveness: client detached)")
        gen = self.generations.get(client.session_id)
        if gen is not None:
            await self._broadcast_writer_state(gen)

    def _sweep_dead_leases_sync(self, targets) -> list:
        """Revoke live leases whose durable heartbeat has gone silent past
        the liveness bound, scoped to the (session, generation) pairs this
        runtime owns. Returns the [(session_id, lease_id)] it revoked."""
        con = db_driver.connect(self.db_path)
        revoked = []
        try:
            for session_id, generation in targets:
                row = con.execute(
                    "SELECT lease_id FROM interface_writer_leases "
                    "WHERE session_id=? AND generation=? AND revoked_at IS NULL"
                    " AND (heartbeat_at IS NULL OR heartbeat_at < "
                    "datetime('now', ?))",
                    (session_id, generation,
                     f"-{int(LEASE_LIVENESS_TIMEOUT)} seconds")).fetchone()
                if row is None:
                    continue
                cur = con.execute(
                    "UPDATE interface_writer_leases SET "
                    "revoked_at=datetime('now'), revoke_reason='liveness' "
                    "WHERE lease_id=? AND revoked_at IS NULL", (row[0],))
                if cur.rowcount > 0:
                    revoked.append((session_id, row[0]))
            con.commit()
            return revoked
        finally:
            con.close()

    async def _sweep_dead_leases(self) -> None:
        targets = [(gen.session_id, gen.generation)
                   for gen in list(self.generations.values())]
        if not targets:
            return
        try:
            revoked = await asyncio.to_thread(self._sweep_dead_leases_sync,
                                              targets)
        except Exception as exc:  # noqa: BLE001 — the reaper must not die
            _log("sweep", f"stale-lease sweep failed: {exc!r}")
            return
        for session_id, lease_id in revoked:
            _log(f"s{session_id}", f"stale writer lease {lease_id} swept "
                                   "(liveness: heartbeat silent)")
            gen = self.generations.get(session_id)
            if gen is not None:
                await self._broadcast_writer_state(gen)

    async def _broadcast_writer_state(self, gen: Generation) -> None:
        for client in list(gen.clients):
            client.send_control(await self.writer_control(gen, client))

    async def _heartbeat_reaper(self) -> None:
        """Mark heartbeat-silent writers stale, broadcast writer state, and
        sweep writer leases whose durable heartbeat has gone silent past the
        liveness bound — a dead writer's lease must not outlive it."""
        try:
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                for gen in list(self.generations.values()):
                    changed = False
                    for client in list(gen.clients):
                        if (client.role == "writer"
                                and not getattr(client, "hb_stale", False)
                                and now - client.last_hb
                                > LEASE_HEARTBEAT_TIMEOUT):
                            _log(gen.sid, "writer heartbeat timeout — stale")
                            client.hb_stale = True
                            changed = True
                    if changed:
                        await self._broadcast_writer_state(gen)
                await self._sweep_dead_leases()
        except asyncio.CancelledError:
            pass

    # -- introspection -------------------------------------------------------------

    def runtime_state(self, session_id: int) -> dict | None:
        gen = self.generations.get(session_id)
        if gen is None or gen.terminated:
            return None
        return {"attached_clients": len(gen.clients),
                "continuity_broken": gen.continuity_broken,
                "pump_bytes": gen.dbg_pump_bytes,
                "fanout_bytes": gen.dbg_fanout_bytes}

    # -- WebSocket entry (implemented in api/interface_ws.py) ---------------------

    async def handle_ws(self, reader, writer, head_raw: bytes) -> None:
        import interface_ws  # late import: api/ is the caller's sys.path entry
        await interface_ws.handle_ws(self, reader, writer, head_raw)
