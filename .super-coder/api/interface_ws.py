#!/usr/bin/env python3
"""Interface WebSocket protocol — sc-term.v1 (spec #20, sprint 25 seq 5).

The terminal stream half of the one-port multiplex, adapted from the proven
spike (spikes/interface-stream/server.py): the sans-io websockets 16
ServerProtocol drives framing on connections transport.py has already
demuxed as upgrades. The durable broker mechanics live in
scripts/interface_runtime.py; this module is the wire protocol only.

Wire protocol (subprotocol sc-term.v1):
  client -> server binary: 0x01|seq:u64be|payload (human input, <=64 KiB)
                           0x03|rows:u16|cols:u16 (resize)
  server -> client binary: 0x00|payload (output)   0x04|payload (redraw)
  text JSON control both ways: input_ack, input_reject, writer, lifecycle,
  resync, heartbeat, error. There is NO client "wake" frame — production
  wake submissions are the coordinator's (seq 8).

Upgrade contract: GET /api/interface/session-streams/<session_id>?ticket=T
with subprotocol sc-term.v1; the ticket is single-use, minted via
runtime.mint_ticket by the HTTP layer. Origin is checked exactly: only
http(s)://<request Host> or no Origin header (non-browser CLI clients).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.parse
from pathlib import Path

from websockets.frames import Frame, Opcode
from websockets.http11 import Request, Response
from websockets.server import ServerProtocol

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import interface_runtime  # noqa: E402

SUBPROTOCOL = "sc-term.v1"
MAX_SIZE = 1 << 20               # 1 MiB WS message bound
CLIENT_QUEUE_MAX = 2 * 1024 * 1024   # per-client outbound bound (1011 past it)
MAX_INPUT_PAYLOAD = 64 * 1024
PING_INTERVAL_S = 20.0
PING_TIMEOUT_S = 40.0


def _log(msg: str) -> None:
    print(f"[ws {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr,
          flush=True)


def build_runtime(db_path: str) -> "interface_runtime.InterfaceRuntime":
    """The server.py entry: construct the Interface runtime for one engine
    DB. start()/stop() and handle_ws are the runtime's own methods."""
    return interface_runtime.InterfaceRuntime(db_path)


# ------------------------------------------------------------------ WS client

class Client:
    """One attached WS client; implements the runtime's client duck type."""

    def __init__(self, proto: ServerProtocol, writer: asyncio.StreamWriter,
                 ticket: dict):
        self.proto = proto
        self.writer = writer
        self.transport = writer.transport
        self.session_id = ticket["session_id"]
        self.sid = f"s{ticket['session_id']}"  # log tag
        self.role = ticket["role"]
        self.client_id = ticket["client_id"]
        self.lease_id = ticket.get("lease_id")
        self.lease_token = ticket.get("lease_token")
        self.alive = True
        self.last_recv = time.monotonic()
        self.last_hb = time.monotonic()
        self.hb_stale = False
        self._last_ping = time.monotonic()
        self._out: asyncio.Queue = asyncio.Queue()
        self._out_bytes = 0

    # -- runtime-facing API (called from runtime tasks) -------------------------

    def send_control(self, msg: dict) -> None:
        self._enqueue("text", json.dumps(msg).encode())

    def send_output(self, data: bytes) -> None:
        self._enqueue("bin", b"\x00" + data)

    def send_redraw(self, data: bytes) -> None:
        self._enqueue("bin", b"\x04" + data)

    def _enqueue(self, kind: str, payload: bytes) -> None:
        if not self.alive:
            return
        if self._out_bytes + len(payload) > CLIENT_QUEUE_MAX:
            _log(f"{self.sid}: client over 2 MiB outbound — closing 1011 "
                 "slow consumer")
            self.close(1011, "slow consumer")
            return
        self._out.put_nowait((kind, payload))
        self._out_bytes += len(payload)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.alive:
            self.alive = False
            self._out.put_nowait(("close", (code, reason)))

    # -- connection tasks ------------------------------------------------------

    def _flush(self) -> None:
        data = b"".join(self.proto.data_to_send())
        if data:
            self.transport.write(data)

    async def writer_loop(self) -> None:
        try:
            while True:
                kind, payload = await self._out.get()
                if kind == "close":
                    code, reason = payload
                    try:
                        self.proto.send_close(code, reason)
                        self._flush()
                    except Exception:
                        pass
                    return
                if kind == "bin":
                    self.proto.send_binary(payload)
                else:
                    self.proto.send_text(payload)  # sans-io send_text takes bytes
                self._out_bytes -= len(payload)
                self._flush()
                # Real backpressure: drain blocks while the client stalls, the
                # 2 MiB queue above fills, and the producer closes us (1011).
                await self.writer.drain()
                # A client we are successfully writing to is alive; refresh
                # liveness so a long output flood can't keepalive-kill it.
                self.last_recv = time.monotonic()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log(f"{self.sid}: writer_loop died: {exc!r}")
            self.alive = False

    async def keepalive_loop(self) -> None:
        try:
            while self.alive:
                await asyncio.sleep(10)
                now = time.monotonic()
                if now - self.last_recv > PING_TIMEOUT_S:
                    _log(f"{self.sid}: keepalive timeout — closing client")
                    self.alive = False
                    self.transport.close()
                    return
                if now - self._last_ping > PING_INTERVAL_S:
                    self._last_ping = now
                    try:
                        self.proto.send_ping(b"hb")
                        self._flush()
                    except Exception:
                        return
        except asyncio.CancelledError:
            pass


# ------------------------------------------------------------------ WS handler

async def handle_ws(runtime: "interface_runtime.InterfaceRuntime", reader,
                    writer, head_raw: bytes) -> None:
    """Take ownership of one upgraded connection (transport.py demux)."""
    transport = writer.transport
    proto = ServerProtocol(subprotocols=[SUBPROTOCOL], max_size=MAX_SIZE)
    proto.receive_data(head_raw)
    events = proto.events_received()
    request = next((e for e in events if isinstance(e, Request)), None)
    if request is None:
        transport.close()
        return

    def reject(status: int, text: str) -> None:
        resp = proto.reject(status, text)
        proto.send_response(resp)
        transport.write(b"".join(proto.data_to_send()))
        transport.close()

    parsed = urllib.parse.urlparse(request.path)
    parts = parsed.path.strip("/").split("/")
    if parts[:3] != ["api", "interface", "session-streams"] or len(parts) != 4:
        reject(404, "no such stream")
        return
    try:
        session_id = int(parts[3])
    except ValueError:
        reject(404, "no such stream")
        return
    # Exact Origin check: same-host http(s), or no Origin (CLI clients).
    host = request.headers.get("Host", "")
    origin = request.headers.get("Origin")
    if origin not in (f"http://{host}", f"https://{host}", None):
        reject(403, "origin not allowed")
        return
    offered = request.headers.get("Sec-WebSocket-Protocol", "")
    if SUBPROTOCOL not in [s.strip() for s in offered.split(",")]:
        reject(400, "subprotocol sc-term.v1 required")
        return
    query = urllib.parse.parse_qs(parsed.query)
    ticket_id = (query.get("ticket") or [""])[0]
    ticket = runtime.consume_ticket(ticket_id, session_id)
    if ticket is None:
        reject(403, "invalid or expired ticket")
        return
    gen = runtime.get_generation(session_id)
    if gen is None or gen.terminated:
        reject(404, "no such generation")
        return

    resp = proto.accept(request)
    proto.send_response(resp)
    if not isinstance(resp, Response) or resp.status_code != 101:
        transport.write(b"".join(proto.data_to_send()))
        transport.close()
        return
    transport.write(b"".join(proto.data_to_send()))

    client = Client(proto, writer, ticket)
    _log(f"{client.sid}: {client.role} client {client.client_id} attached")
    writer_task = asyncio.create_task(client.writer_loop())
    keepalive_task = asyncio.create_task(client.keepalive_loop())
    await runtime.attach(client)
    try:
        await _ws_read_loop(runtime, reader, client)
    finally:
        runtime.detach(client)
        client.alive = False
        writer_task.cancel()
        keepalive_task.cancel()
        try:
            transport.close()
        except Exception:
            pass
        _log(f"{client.sid}: client detached")


async def _ws_read_loop(runtime, reader, client: Client) -> None:
    frag_op = None
    frag = bytearray()
    while True:
        data = await reader.read(65536)
        if not data:
            return
        client.last_recv = time.monotonic()
        client.proto.receive_data(data)
        # Flush whatever the sans-io layer queued while parsing (notably
        # automatic pong replies) before handling events.
        out = b"".join(client.proto.data_to_send())
        if out:
            client.transport.write(out)
        for event in client.proto.events_received():
            if not isinstance(event, Frame):
                continue
            if event.opcode is Opcode.CLOSE:
                try:
                    client.proto.send_close()
                    client.transport.write(
                        b"".join(client.proto.data_to_send()))
                except Exception:
                    pass
                return
            if event.opcode in (Opcode.PING, Opcode.PONG):
                continue  # ping auto-ponged by the sans-io layer
            if event.opcode in (Opcode.TEXT, Opcode.BINARY):
                frag_op = event.opcode
                frag = bytearray(event.data)
                if not event.fin:
                    continue
            elif event.opcode is Opcode.CONT:
                if frag_op is None:
                    continue
                frag += event.data
                if not event.fin:
                    continue
            else:
                continue
            op, payload = frag_op, bytes(frag)
            frag_op, frag = None, bytearray()
            if op is Opcode.BINARY:
                _on_binary(runtime, client, payload)
            else:
                _on_text(runtime, client, payload)


def _on_binary(runtime, client: Client, payload: bytes) -> None:
    if not payload:
        return
    kind = payload[0]
    if kind == 0x01:
        if len(payload) < 9:
            client.send_control({"type": "error", "code": "malformed_frame"})
            return
        seq = int.from_bytes(payload[1:9], "big")
        data = payload[9:]
        if len(data) > MAX_INPUT_PAYLOAD:
            _log(f"{client.sid}: reject seq={seq} payload_too_large "
                 f"({len(data)} bytes)")
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": "payload_too_large"})
            return
        if client.role != "writer":
            client.send_control({"type": "input_reject", "seq": seq,
                                 "reason": "viewer_read_only"})
            return
        runtime.enqueue_input(client, seq, data)
    elif kind == 0x03:
        if len(payload) < 5:
            client.send_control({"type": "error", "code": "malformed_frame"})
            return
        rows = int.from_bytes(payload[1:3], "big")
        cols = int.from_bytes(payload[3:5], "big")
        if not (1 <= rows <= 500 and 1 <= cols <= 500):
            client.send_control({"type": "error", "code": "bad_geometry"})
            return
        runtime.enqueue_resize(client, rows, cols)


def _on_text(runtime, client: Client, payload: bytes) -> None:
    try:
        msg = json.loads(payload)
    except ValueError:
        client.send_control({"type": "error", "code": "bad_control"})
        return
    mtype = msg.get("type")
    if mtype == "heartbeat":
        runtime.heartbeat(client)
        client.send_control({"type": "heartbeat"})
    elif mtype == "wake":
        # Production wake submissions are the coordinator's via the API
        # (seq 8), never a client frame.
        client.send_control({"type": "error", "code": "unsupported"})
