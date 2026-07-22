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

The runtime owns processes, tmux, and volatile stream state ONLY — every
occupancy/lifecycle/composer transition stays in the DB and is the routes
layer's job. On service restart the tmux server and FIFO files survive
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

ENGINE = Path(__file__).resolve().parents[1]
SHADOW_DIR = ENGINE / "shadow"
SHADOW_NODE_PATH = "/opt/sc-shadow/node_modules"

BRIDGE_MAX = 8 * 1024 * 1024          # bounded pump→loop bridge (spike-proven)
SENDKEYS_CHUNK = 512                  # proven tmux -H chunk size
MAX_INPUT_PAYLOAD = 64 * 1024         # wire protocol limit
TMUX_MIN_VERSION = (3, 4)
LEASE_HEARTBEAT_TIMEOUT = 60.0        # 3 missed 20s heartbeats
TICKET_TTL_S = 60
GRACEFUL_TERMINATE_S = 10.0

TMUX_SESSION = "sc-interface"


def _log(tag: str, msg: str) -> None:
    print(f"[runtime {tag} {time.strftime('%H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


class InterfaceUnavailable(RuntimeError):
    """tmux/node stack missing or too old — review UI keeps working."""


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
        try:
            loop.call_soon_threadsafe(self.runtime._on_pump_exit, self)
        except RuntimeError:
            pass  # loop closed

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
            try:
                await self.runtime.tmux("kill-window", "-t", self.pane_id)
            except Exception:
                pass
        self.runtime.shadow.dispose(self.sid)
        for fd in (self._fifo_keep_fd, self._pump_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fifo_keep_fd = self._pump_fd = -1


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
        self.available = True
        self.unavailable_reason = ""
        await self.shadow.start()
        self._reaper = asyncio.create_task(self._heartbeat_reaper())
        # Reattach generations that survived a service restart (the tmux
        # server and FIFOs are ours, stable under run_dir).
        sessions = await asyncio.to_thread(self._occupied_sessions)
        result = await self.reattach_all(sessions)
        if result["reattached"] or result["lost"]:
            _log("boot", f"reattach: {len(result['reattached'])} ok, "
                         f"lost {result['lost']}")

    async def stop(self) -> None:
        """Release runtime resources; panes stay alive for reattach."""
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
        calls as chunks). Runs in the broker worker thread."""
        for off in range(0, len(payload), SENDKEYS_CHUNK):
            chunk = payload[off:off + SENDKEYS_CHUNK]
            subprocess.run(
                ["tmux", "-S", self.sock, "send-keys", "-t", pane_id, "-H",
                 *[f"{b:02x}" for b in chunk]],
                capture_output=True, check=True)

    async def send_keys(self, pane_id: str, payload: bytes) -> None:
        for off in range(0, len(payload), SENDKEYS_CHUNK):
            chunk = payload[off:off + SENDKEYS_CHUNK]
            await self.tmux("send-keys", "-t", pane_id, "-H",
                            *[f"{b:02x}" for b in chunk])

    async def capture_pane(self, pane_id: str) -> bytes:
        return await self.tmux("capture-pane", "-epN", "-t", pane_id)

    # -- spawn -----------------------------------------------------------------

    def _fifo_path(self, session_id: int) -> str:
        return os.path.join(self.run_dir, f"fifo-s{session_id}")

    def _sentinel_path(self, session_id: int) -> str:
        return os.path.join(self.run_dir, f"ready-s{session_id}")

    def _open_fifo(self, gen: Generation) -> None:
        """Pre-open RDWR|NONBLOCK so tmux's `cat > fifo` writer never blocks,
        then the blocking pump reader."""
        gen._fifo_keep_fd = os.open(  # noqa: SLF001
            self._fifo_path(gen.session_id), os.O_RDWR | os.O_NONBLOCK)
        gen._pump_fd = os.open(  # noqa: SLF001
            self._fifo_path(gen.session_id), os.O_RDONLY)

    def _start_consumers(self, gen: Generation) -> None:
        gen._pump_thread = threading.Thread(  # noqa: SLF001
            target=gen._pump_loop, name=f"pump-{gen.sid}", daemon=True)
        gen._pump_thread.start()  # noqa: SLF001
        gen._tasks = [  # noqa: SLF001
            asyncio.create_task(gen._output_consumer()),
            asyncio.create_task(gen._input_consumer()),
        ]

    async def spawn(self, *, session_id: int, shell_id: int, generation: int,
                    worktree: str, sc_path: str, token_path: str,
                    rows: int, cols: int,
                    command: list[str] | None = None) -> dict:
        """Create the tmux window + FIFO pump + shadow for one generation.
        `command` overrides the exec line — tests only. Returns the pane
        identity the caller persists to interface_sessions."""
        self._require_available()
        gen = Generation(self, session_id, shell_id, generation, rows, cols)
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

        self.shadow.create(gen.sid, rows, cols)
        await self.tmux("pipe-pane", "-t", pane_id,
                        f"cat > {shlex.quote(fifo)}")
        # boot the harness only now: zero lost boot bytes
        open(sentinel, "w").close()

        self.generations[session_id] = gen
        self._start_consumers(gen)
        _log(gen.sid, f"session spawned: shell={shell_id} gen={generation} "
                      f"pane={pane_id} pid={pane_pid} {cols}x{rows}")
        return {"pane_id": pane_id, "pane_pid": gen.pane_pid,
                "pane_start_ticks": gen.pane_start_ticks,
                "tmux_socket": self.sock, "tmux_session": TMUX_SESSION,
                "tmux_window": window}

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
        lost (the caller marks it unreconciled+lost)."""
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
                await self.tmux("pipe-pane", "-t", pane_id,
                                f"cat > {shlex.quote(fifo)}")
            else:
                self._open_fifo(gen)
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
        dead = await self._wait_gone(pane_id, pane_pid, GRACEFUL_TERMINATE_S)
        if not dead and not force:
            gen.terminating = False
            return {"terminated": False, "reason": "graceful_timeout",
                    "pid": pane_pid, "generation": generation}
        if not dead:
            os.kill(pane_pid, signal.SIGKILL)
            await self._wait_gone(pane_id, pane_pid, GRACEFUL_TERMINATE_S)
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

    async def _wait_gone(self, pane_id: str, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid_gone = not os.path.exists(f"/proc/{pid}")
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
            return  # FIFO hiccup, pane alive — nothing to prove
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

    # -- writer heartbeat (WS layer heartbeats; DB revoke is seq 6+) ---------

    def heartbeat(self, client) -> None:
        gen = self.generations.get(client.session_id)
        if gen is None or client.role != "writer":
            return
        was_stale = getattr(client, "hb_stale", False)
        client.last_hb = time.monotonic()
        if was_stale:
            client.hb_stale = False
            asyncio.create_task(self._broadcast_writer_state(gen))

    async def _broadcast_writer_state(self, gen: Generation) -> None:
        for client in list(gen.clients):
            client.send_control(await self.writer_control(gen, client))

    async def _heartbeat_reaper(self) -> None:
        """Mark heartbeat-silent writers stale and broadcast writer state.
        The DB lease itself persists until takeover/termination/restart."""
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
