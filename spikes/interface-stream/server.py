"""Interface-stream spike service: one asyncio process, one loopback port.

Multiplexes plain HTTP (JSON API + static UI) and WebSocket upgrades on one
port by driving the websockets 16 SANS-IO ServerProtocol for upgraded
connections. (websockets 16's process_request hook cannot read HTTP request
bodies — Request has no body support — so POST+JSON requires multiplexing
below the handshake; the sans-io protocol is the maintained embedding API.)

Wire protocol (subprotocol sc-term.v1):
  client -> server binary: 0x01|seq:u64be|payload (human input, <=64 KiB)
                           0x03|rows:u16|cols:u16 (resize)
  server -> client binary: 0x00|payload (output)   0x04|payload (redraw)
  text JSON control both ways: input_ack, input_reject, writer, lifecycle,
  wake, resync, heartbeat, error.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import urllib.parse

from websockets.frames import Frame, Opcode
from websockets.http11 import Request, Response
from websockets.server import ServerProtocol

from broker import Broker, CLIENT_QUEUE_MAX, MAX_INPUT_PAYLOAD

SUBPROTOCOL = "sc-term.v1"
TICKET_TTL_S = 60.0
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".map": "application/json"}


def _log(msg: str) -> None:
    print(f"[server {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ WS client

class Client:
    """One attached WS client; implements the broker's client duck type."""

    def __init__(self, server: "Server", proto: ServerProtocol,
                 writer: asyncio.StreamWriter, ticket: dict):
        self.server = server
        self.proto = proto
        self.writer = writer
        self.transport = writer.transport
        self.sid = ticket["session_id"]
        self.role = ticket["role"]
        self.lease_token = ticket.get("lease_token")
        self.last_seq: int | None = None
        self.forwarded: set[int] = set()
        self.alive = True
        self.last_recv = time.monotonic()
        self._last_ping = time.monotonic()
        self._out: asyncio.Queue = asyncio.Queue()
        self._out_bytes = 0

    # -- broker-facing API (called from broker tasks) -------------------------

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
            _log(f"{self.sid}: client over 2 MiB outbound — closing 1011 slow consumer")
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
                if now - self.last_recv > 40:
                    _log(f"{self.sid}: keepalive timeout — closing client")
                    self.alive = False
                    self.transport.close()
                    return
                if now - self._last_ping > 20:
                    self._last_ping = now
                    try:
                        self.proto.send_ping(b"hb")
                        self._flush()
                    except Exception:
                        return
        except asyncio.CancelledError:
            pass


# ------------------------------------------------------------------ server

class Server:
    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token
        self.broker = Broker()
        self.tickets: dict[str, dict] = {}
        self._tcp: asyncio.AbstractServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.broker.start()
        self._tcp = await asyncio.start_server(self._on_connection, "127.0.0.1", self.port)
        self.port = self._tcp.sockets[0].getsockname()[1]
        _log(f"listening on 127.0.0.1:{self.port} run_dir={self.broker.run_dir}")

    async def stop(self) -> None:
        if self._tcp:
            self._tcp.close()
            await self._tcp.wait_closed()
        await self.broker.stop()

    # -- connection entry --------------------------------------------------------

    async def _on_connection(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            head = await self._read_head(reader)
            if head is None:
                writer.close()
                return
            request_line, headers, raw, leftover = head
            if "upgrade" in headers.get("connection", "").lower():
                await self._handle_ws(reader, writer, raw + leftover)
            else:
                await self._handle_http(reader, writer, request_line, headers, leftover)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001
            _log(f"connection error: {exc!r}")
            try:
                writer.close()
            except Exception:
                pass

    async def _read_head(self, reader: asyncio.StreamReader):
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await reader.read(4096)
            if not chunk:
                return None
            raw += chunk
            if len(raw) > 65536:
                return None
        head_raw, leftover = raw.split(b"\r\n\r\n", 1)
        head_raw += b"\r\n\r\n"
        lines = head_raw[:-4].decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return lines[0], headers, head_raw, leftover

    # -- plain HTTP -------------------------------------------------------------

    async def _handle_http(self, reader, writer, request_line: str, headers: dict,
                           leftover: bytes) -> None:
        method, path, _ = (request_line.split(" ", 2) + ["", ""])[:3]
        length = int(headers.get("content-length") or 0)
        body = leftover[:length]
        while len(body) < length:
            chunk = await reader.read(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        status, payload, ctype = await self._route(method.upper(), path, headers, body)
        resp = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
        writer.write(resp)
        await writer.drain()
        writer.close()

    def _json(self, status: int, obj) -> tuple[str, bytes, str]:
        phrase = {200: "200 OK", 201: "201 Created", 202: "202 Accepted",
                  204: "204 No Content", 400: "400 Bad Request", 401: "401 Unauthorized",
                  403: "403 Forbidden", 404: "404 Not Found", 409: "409 Conflict",
                  405: "405 Method Not Allowed", 500: "500 Internal Server Error"}[status]
        return phrase, json.dumps(obj).encode(), "application/json"

    async def _route(self, method: str, path: str, headers: dict, body: bytes):
        parsed = urllib.parse.urlparse(path)
        p = parsed.path
        if p.startswith("/api/"):
            if headers.get("authorization") != f"Bearer {self.token}":
                return self._json(401, {"error": "unauthorized"})
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                return self._json(400, {"error": "bad json"})
            try:
                return await self._route_api(method, p, data)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                _log(f"api error {method} {p}: {exc!r}")
                return self._json(500, {"error": str(exc)})
        if method != "GET":
            return self._json(405, {"error": "method not allowed"})
        return self._static(p)

    async def _route_api(self, method: str, p: str, data: dict):
        if p == "/api/interface/sessions" and method == "POST":
            worktree = data.get("worktree") or ""
            if not os.path.isdir(worktree):
                raise ValueError(f"worktree not a directory: {worktree!r}")
            gen = await self.broker.create_session(
                harness=data.get("harness", "bash"),
                worktree=worktree,
                command=data.get("command"),
                rows=int(data.get("rows", 24)), cols=int(data.get("cols", 80)),
                wake_prompt=str(data.get("wake_prompt", "WAKEPROMPT\n")).encode(),
                quiet_s=float(data.get("quiet_ms", 3000)) / 1000.0,
                idle_quiet_s=float(data.get("idle_quiet_ms", 1000)) / 1000.0)
            return self._json(201, self.broker.session_info(gen))
        if p.startswith("/api/interface/sessions/") and method == "GET":
            gen = self.broker.get_session(p.rsplit("/", 1)[1])
            if not gen:
                return self._json(404, {"error": "no such session"})
            return self._json(200, self.broker.session_info(gen))
        if p == "/api/interface/stream-tickets" and method == "POST":
            gen = self.broker.get_session(data.get("session_id", ""))
            if not gen:
                return self._json(404, {"error": "no such session"})
            role = data.get("role", "viewer")
            if role not in ("viewer", "writer"):
                raise ValueError("role must be viewer|writer")
            if role == "writer":
                if not gen.lease or data.get("lease_token") != gen.lease.token:
                    return self._json(403, {"error": "writer ticket requires the current lease_token"})
            ticket = secrets.token_hex(24)
            self.tickets[ticket] = {
                "session_id": gen.sid, "role": role,
                "lease_token": data.get("lease_token") if role == "writer" else None,
                "expires": time.monotonic() + TICKET_TTL_S,
            }
            return self._json(201, {"ticket": ticket, "expires_in": TICKET_TTL_S})
        if p == "/api/interface/writer-leases" and method == "POST":
            lease = self.broker.acquire_lease(data.get("session_id", ""),
                                              takeover=bool(data.get("takeover")))
            if lease is None:
                if self.broker.get_session(data.get("session_id", "")) is None:
                    return self._json(404, {"error": "no such session"})
                return self._json(409, {"error": "writer lease held; pass takeover=true"})
            return self._json(201, {"lease_id": lease.id, "lease_token": lease.token})
        if p.startswith("/api/interface/writer-leases/") and method == "DELETE":
            if self.broker.release_lease(p.rsplit("/", 1)[1]):
                return self._json(204, {})
            return self._json(404, {"error": "no such lease"})
        if p == "/api/interface/termination-requests" and method == "POST":
            if await self.broker.terminate_session(data.get("session_id", "")):
                return self._json(202, {"terminated": True})
            return self._json(404, {"error": "no such session"})
        return self._json(404, {"error": "no such route"})

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return "404 Not Found", b"not found", "text/plain"
        with open(full, "rb") as fh:
            payload = fh.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        return "200 OK", payload, ctype

    # -- WebSocket ---------------------------------------------------------------

    async def _handle_ws(self, reader, writer, head_raw: bytes) -> None:
        transport = writer.transport
        proto = ServerProtocol(
            subprotocols=[SUBPROTOCOL],
            origins=[f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", None],
            max_size=1 << 20,
        )
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
        sid = parts[3]
        offered = request.headers.get("Sec-WebSocket-Protocol", "")
        if SUBPROTOCOL not in [s.strip() for s in offered.split(",")]:
            reject(400, "subprotocol sc-term.v1 required")
            return
        query = urllib.parse.parse_qs(parsed.query)
        ticket_id = (query.get("ticket") or [""])[0]
        ticket = self.tickets.pop(ticket_id, None)  # single-use: consumed on upgrade
        if ticket is None or ticket["expires"] < time.monotonic() or ticket["session_id"] != sid:
            reject(403, "invalid or expired ticket")
            return
        gen = self.broker.get_session(sid)
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

        client = Client(self, proto, writer, ticket)
        _log(f"{sid}: {client.role} client attached")
        writer_task = asyncio.create_task(client.writer_loop())
        keepalive_task = asyncio.create_task(client.keepalive_loop())
        await gen.attach(client)
        try:
            await self._ws_read_loop(reader, client, gen)
        finally:
            gen.detach(client)
            client.alive = False
            writer_task.cancel()
            keepalive_task.cancel()
            try:
                transport.close()
            except Exception:
                pass
            _log(f"{sid}: client detached")

    async def _ws_read_loop(self, reader, client: Client, gen) -> None:
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
                        client.transport.write(b"".join(client.proto.data_to_send()))
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
                    self._on_binary(client, payload)
                else:
                    self._on_text(client, payload)

    def _on_binary(self, client: Client, payload: bytes) -> None:
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
                _log(f"{client.sid}: reject seq={seq} payload_too_large ({len(data)} bytes)")
                client.send_control({"type": "input_reject", "seq": seq,
                                     "reason": "payload_too_large"})
                return
            if client.role != "writer":
                client.send_control({"type": "input_reject", "seq": seq,
                                     "reason": "viewer_read_only"})
                return
            self.broker.enqueue_human(client, seq, data)
        elif kind == 0x03:
            if len(payload) < 5:
                client.send_control({"type": "error", "code": "malformed_frame"})
                return
            rows = int.from_bytes(payload[1:3], "big")
            cols = int.from_bytes(payload[3:5], "big")
            if not (1 <= rows <= 500 and 1 <= cols <= 500):
                client.send_control({"type": "error", "code": "bad_geometry"})
                return
            self.broker.enqueue_resize(client, rows, cols)

    def _on_text(self, client: Client, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            client.send_control({"type": "error", "code": "bad_control"})
            return
        mtype = msg.get("type")
        if mtype == "heartbeat":
            self.broker.heartbeat(client)
            client.send_control({"type": "heartbeat"})
        elif mtype == "wake":
            # Spike: wake submissions ride the writer connection. Production
            # moves this to the coordinator via the API, not a client frame.
            if client.role != "writer":
                client.send_control({"type": "error", "code": "wake_requires_writer"})
                return
            self.broker.enqueue_wake(client)


# ------------------------------------------------------------------ main

async def _amain() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("SPIKE_PORT", 18777))
    server = Server(port, os.environ.get("SPIKE_TOKEN", "spike"))
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
