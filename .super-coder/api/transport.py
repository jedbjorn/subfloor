"""One-port asyncio HTTP + WebSocket transport (spec #20, sprint 25 seq 5).

Replaces the stdlib ThreadingHTTPServer request loop with the spike-proven
multiplex (spikes/interface-stream/DESIGN.md): plain HTTP and WebSocket
upgrades share one loopback port, one supervised process. WebSocket framing
is driven through the maintained `websockets` sans-io ServerProtocol by the
CALLER (the interface WS module) — this module only demuxes connections and
speaks minimal HTTP/1.1; the terminal/stream protocol is never reimplemented
here.

Contract:
  http_handler(method, path, headers_raw, body) -> (status, headers, body)
      sync callable; invoked on a thread-pool executor (engine DB access is
      blocking sqlite, same as the old threaded server). `headers_raw` is the
      verbatim header block (CRLF-joined, no trailing blank line) so the
      caller can parse it exactly like BaseHTTPRequestHandler did.
      Returns (status int, [(name, value)], body bytes).
  ws_handler(reader, writer, head_raw) -> None
      async callable taking full ownership of an upgraded connection.
      `head_raw` is the verbatim request head (ending CRLFCRLF) plus any
      already-read leftover bytes, ready to feed a sans-io protocol.

Limits: 64 KiB request head, 8 MiB body. Every HTTP response is
`Connection: close` (the multiplex reads exactly one request per connection;
browsers and fetch() transparently open the next).
"""
from __future__ import annotations

import asyncio

MAX_HEAD = 65536
MAX_BODY = 8 * 1024 * 1024

_PHRASES = {
    200: "200 OK", 201: "201 Created", 202: "202 Accepted",
    204: "204 No Content", 302: "302 Found",
    400: "400 Bad Request", 401: "401 Unauthorized", 403: "403 Forbidden",
    404: "404 Not Found", 405: "405 Method Not Allowed",
    409: "409 Conflict", 413: "413 Payload Too Large",
    422: "422 Unprocessable Entity", 500: "500 Internal Server Error",
    501: "501 Not Implemented", 503: "503 Service Unavailable",
}


def phrase(status: int) -> str:
    return _PHRASES.get(status, f"{status} Status")


async def _read_head(reader: asyncio.StreamReader):
    """Read one request head. Returns (request_line, headers_raw, raw+leftover)
    or None on EOF/oversize."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        raw += chunk
        if len(raw) > MAX_HEAD:
            return None
    head_raw, leftover = raw.split(b"\r\n\r\n", 1)
    head_raw += b"\r\n\r\n"
    lines = head_raw[:-4].decode("latin-1").split("\r\n")
    return lines[0], "\r\n".join(lines[1:]), head_raw + leftover


class Transport:
    def __init__(self, host: str, port: int, http_handler, ws_handler,
                 log=print):
        self.host = host
        self.port = port
        self.http_handler = http_handler
        self.ws_handler = ws_handler
        self._log = log
        self._tcp: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._tcp = await asyncio.start_server(
            self._on_connection, self.host, self.port)
        self.port = self._tcp.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._tcp:
            self._tcp.close()
            await self._tcp.wait_closed()

    async def _on_connection(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
            if head is None:
                writer.close()
                return
            request_line, headers_raw, raw = head
            headers = {}
            for line in headers_raw.split("\r\n"):
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            if "upgrade" in headers.get("connection", "").lower():
                await self.ws_handler(reader, writer, raw)
            else:
                await self._handle_http(reader, writer, request_line,
                                        headers, headers_raw, raw)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001 — one bad connection must not kill the server
            self._log(f"transport: connection error: {exc!r}")
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_http(self, reader, writer, request_line: str,
                           headers: dict, headers_raw: str,
                           raw: bytes) -> None:
        parts = (request_line.split(" ", 2) + ["", ""])[:3]
        method, path = parts[0].upper(), parts[1]
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            await self._respond(writer, 413, [],
                                b'{"error":"request body too large"}')
            return
        head_len = raw.find(b"\r\n\r\n") + 4
        body = raw[head_len:head_len + length]
        while len(body) < length:
            chunk = await reader.read(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        loop = asyncio.get_running_loop()
        try:
            status, resp_headers, resp_body = await loop.run_in_executor(
                None, self.http_handler, method, path, headers_raw, body)
        except Exception as exc:  # noqa: BLE001 — defense in depth; handlers catch their own
            self._log(f"transport: handler error {method} {path}: {exc!r}")
            status, resp_headers, resp_body = 500, [], b'{"error":"internal"}'
        await self._respond(writer, status, resp_headers, resp_body)

    async def _respond(self, writer, status: int,
                       headers: list, body: bytes) -> None:
        if not isinstance(body, (bytes, bytearray)):
            body = str(body).encode()
        lines = [f"HTTP/1.1 {phrase(status)}"]
        names = {k.lower() for k, _ in headers}
        for k, v in headers:
            lines.append(f"{k}: {v}")
        if "content-length" not in names:
            lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        writer.write(head + bytes(body))
        try:
            await writer.drain()
        finally:
            writer.close()


async def serve(host: str, port: int, http_handler, ws_handler,
                log=print) -> int:
    """Start the multiplex and block forever (until cancelled). Returns the
    bound port after start for callers that passed port=0 — via `log` line and
    the Transport object; this coroutine simply runs until shutdown."""
    transport = Transport(host, port, http_handler, ws_handler, log=log)
    await transport.start()
    log(f"transport: listening on {host}:{transport.port}")
    try:
        await asyncio.Event().wait()
    finally:
        await transport.stop()
