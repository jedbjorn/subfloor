#!/usr/bin/env python3
"""Stdlib loopback MCP gate; each URL and transport session belongs to one shell."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import browser

MAX_BODY = 4 * 1024 * 1024
DISARMED = "Browser is disarmed; ask the FnB to arm it in Scripts → Browser."
DISCONNECTED = (
    "extension not connected: open Subfloor and ask the FnB to approve this connection"
)


def rpc_result(body: bytes) -> dict:
    """Extract the response from JSON or the finite SSE response to an RPC POST."""
    try:
        return json.loads(body)
    except ValueError:
        for line in reversed(body.splitlines()):
            if line.startswith(b"data:"):
                try:
                    return json.loads(line[5:])
                except ValueError:
                    continue
    return {}


class Gate(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address, config, *, state=None, window_check=None, timeout=browser.TIMEOUT
    ):
        super().__init__(address, Handler)
        self.config = config
        self.state = state or browser.read
        self.window_check = window_check or browser.guard.check
        self.timeout_seconds = timeout
        self.sessions = {}
        self.lock = threading.RLock()
        self.tools = []
        self.audit_path = browser.private() / "audit.jsonl"

    def audit(self, shell, message, result):
        arguments = message.get("params", {}).get("arguments", {})
        arguments = arguments if isinstance(arguments, dict) else {}
        target = arguments.get("url") or arguments.get("ref") or ""
        if isinstance(target, str) and "://" in target:
            try:
                url = urlsplit(target)
                target = urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
            except ValueError:
                target = "[invalid URL]"
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shell": shell,
            "tool": message.get("params", {}).get("name"),
            "target": str(target)[:500],
            "result": result,
        }
        data = (json.dumps(row) + "\n").encode()
        with self.lock:
            fd = os.open(
                self.audit_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, data)
            finally:
                os.close(fd)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # request URLs and page data never enter an HTTP access log

    def send_json(self, value, status=200, session=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(data)

    def error(self, message, text, status=200):
        self.send_json(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32000, "message": text},
            },
            status,
        )

    def identity(self):
        match = re.fullmatch(r"/mcp/([A-Za-z0-9_-]{1,64})", self.path)
        return match[1].upper() if match else None

    def session(self, shell):
        sid = self.headers.get("Mcp-Session-Id")
        with self.server.lock:
            session = self.server.sessions.get(sid)
        return (sid, session) if session and session["shell"] == shell else (None, None)

    def do_GET(self):
        if self.path == "/status":
            with self.server.lock:
                active = sorted(
                    {
                        s["shell"]
                        for s in self.server.sessions.values()
                        if s.get("connected")
                    }
                )
                errors = [
                    s["protocol_error"]
                    for s in self.server.sessions.values()
                    if s.get("protocol_error")
                ]
            return self.send_json(
                {
                    "active_shells": active,
                    "extension": "connected" if active else "not connected",
                    "protocol_errors": errors,
                }
            )
        shell = self.identity()
        sid, session = self.session(shell)
        if not session:
            return self.send_json({"error": "unknown shell session"}, 404)
        # A GET event stream is long lived; only RPC actions have a deadline.
        self.forward("GET", shell, sid, session, None)

    def do_DELETE(self):
        shell = self.identity()
        sid, session = self.session(shell)
        if not session:
            return self.send_json({"error": "unknown shell session"}, 404)
        try:
            self.forward("DELETE", shell, sid, session, None)
        finally:
            with self.server.lock:
                self.server.sessions.pop(sid, None)

    def do_POST(self):
        shell = self.identity()
        if not shell:
            return self.send_json({"error": "expected /mcp/<shortname>"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid request length")
            self.connection.settimeout(self.server.timeout_seconds)
            message = json.loads(self.rfile.read(length))
            if (
                not isinstance(message, dict)
                or message.get("jsonrpc") != "2.0"
                or not (
                    isinstance(message.get("method"), str)
                    or ("id" in message and ("result" in message or "error" in message))
                )
            ):
                raise ValueError("one JSON-RPC request is required")
            if not isinstance(message.get("params", {}), dict):
                raise TypeError("params must be an object")
        except (ValueError, TypeError, OSError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        method = message.get("method")
        if method == "initialize":
            params = message.setdefault("params", {})
            client_info = params.get("clientInfo", {})
            if not isinstance(client_info, dict):
                return self.error(message, "clientInfo must be an object", 400)
            params["clientInfo"] = {**client_info, "name": f"Subfloor {shell}"}
            params["clientInfo"].setdefault("version", "1")
            sid = uuid.uuid4().hex
            with self.server.lock:
                self.server.sessions[sid] = {
                    "shell": shell,
                    "initialize": message,
                    "upstream": None,
                    "connected": False,
                    "lock": threading.RLock(),
                    "connection_lock": threading.RLock(),
                }
            return self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "Subfloor browser", "version": "1"},
                    },
                },
                session=sid,
            )
        sid, session = self.session(shell)
        if not session:
            return self.error(
                message, "unknown shell session; initialize this shell first", 404
            )
        if method is None:
            # Replies to server requests (notably roots/list) must flow while
            # the initiating tool is waiting; they are never browser actions.
            if not session.get("upstream"):
                return self.error(message, "upstream session is not initialized", 404)
            return self.forward("POST", shell, sid, session, message)
        config = self.server.state()
        if not config or not config["armed"]:
            session["connected"] = False
            session["upstream"] = None
            if method == "tools/call":
                self.server.audit(shell, message, "disarmed")
                return self.error(message, DISARMED)
            if method == "tools/list":
                return self.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {"tools": self.server.tools},
                    }
                )
            return self.send_json({}, 202)
        if method == "tools/call":
            try:
                arguments = message.get("params", {}).get("arguments", {})
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
                self.server.window_check(config)
            except (ValueError, TypeError) as exc:
                session["connected"] = False
                self.server.audit(shell, message, "extension not connected")
                return self.error(message, str(exc))
        if not session["lock"].acquire(blocking=False):
            if method == "tools/call":
                self.server.audit(shell, message, "busy; not queued")
            return self.error(
                message,
                "This shell already has an active browser request; action was not queued.",
            )
        try:
            self.forward("POST", shell, sid, session, message)
        finally:
            session["lock"].release()

    def upstream(self, method, session, body, timeout):
        config = self.server.config
        connection = http.client.HTTPConnection(
            "127.0.0.1", config["browser_port"], timeout=timeout
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": session["initialize"]["params"].get(
                "protocolVersion", "2025-03-26"
            ),
        }
        if session.get("upstream"):
            headers["Mcp-Session-Id"] = session["upstream"]
        if self.headers.get("Last-Event-ID"):
            headers["Last-Event-ID"] = self.headers["Last-Event-ID"]
        connection.request(
            method,
            "/mcp",
            body=json.dumps(body).encode() if body else None,
            headers=headers,
        )
        return connection, connection.getresponse()

    def connect_session(self, session, deadline):
        if not session["connection_lock"].acquire(
            timeout=max(0.01, deadline - time.monotonic())
        ):
            raise TimeoutError("initialization is busy")
        try:
            self._connect_session(session, deadline)
        finally:
            session["connection_lock"].release()

    def _connect_session(self, session, deadline):
        # Backend restarts invalidate old transport sessions; never replay a tool.
        receipt = browser.process_receipt("server")
        generation = (receipt or {}).get("start")
        if session.get("upstream") and session.get("generation") == generation:
            return
        session["upstream"] = None
        connection, response = self.upstream(
            "POST",
            session,
            session["initialize"],
            max(0.01, deadline - time.monotonic()),
        )
        try:
            payload = response.read(MAX_BODY)
            if response.status != 200 or "error" in rpc_result(payload):
                raise ValueError("browser initialization failed")
            session["upstream"] = response.getheader("Mcp-Session-Id")
            if not session["upstream"]:
                raise ValueError("browser omitted MCP session identity")
            session["generation"] = generation
        finally:
            connection.close()
        connection, response = self.upstream(
            "POST",
            session,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            max(0.01, deadline - time.monotonic()),
        )
        response.close()
        connection.close()

    def forward(self, method, shell, sid, session, message):
        deadline = time.monotonic() + self.server.timeout_seconds
        connection = None
        sent = False
        is_tool = bool(message and message.get("method") == "tools/call")
        outcome = "failed"
        try:
            self.connect_session(session, deadline)
            connection, response = self.upstream(
                method, session, message, max(0.01, deadline - time.monotonic())
            )
            # Stream SSE without buffering page content or unbounded waits.
            chunks = []
            self.send_response(response.status)
            content_type = response.getheader("Content-Type", "application/json")
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent = True
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if method != "GET" and remaining <= 0:
                    raise TimeoutError()
                if connection.sock:
                    connection.sock.settimeout(None if method == "GET" else remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                size += len(chunk)
                if size <= MAX_BODY:
                    chunks.append(chunk)
            payload = rpc_result(b"".join(chunks))
            success = (
                response.status < 400
                and "result" in payload
                and "error" not in payload
                and not payload.get("result", {}).get("isError")
            )
            outcome = "ok" if success else "upstream error"
            if is_tool:
                session["connected"] = success
                session.pop("protocol_error", None)
                texts = [payload.get("error", {}).get("message", "")]
                texts.extend(
                    item.get("text", "")
                    for item in payload.get("result", {}).get("content", [])
                    if isinstance(item, dict)
                )
                for text in texts:
                    if "unsupported protocol version" in text.lower():
                        session["protocol_error"] = text
            if message and message.get("method") == "tools/list" and success:
                self.server.tools = payload.get("result", {}).get("tools", [])
        except (OSError, ValueError, http.client.HTTPException) as exc:
            session["connected"] = False
            if session.get("upstream"):
                try:
                    cancel, canceled = self.upstream("DELETE", session, None, 1)
                    canceled.close()
                    cancel.close()
                except (OSError, http.client.HTTPException):
                    pass
            session["upstream"] = None
            outcome = (
                "extension not connected"
                if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError))
                else "upstream error"
            )
            if not sent:
                self.error(message or {}, DISCONNECTED)
            elif method == "POST":
                error = {
                    "jsonrpc": "2.0",
                    "id": (message or {}).get("id"),
                    "error": {"code": -32000, "message": DISCONNECTED},
                }
                wire = json.dumps(error).encode()
                if "text/event-stream" in content_type:
                    wire = b"event: message\ndata: " + wire + b"\n\n"
                try:
                    self.wfile.write(wire)
                except OSError:
                    pass
        finally:
            if connection:
                connection.close()
            if is_tool:
                self.server.audit(shell, message, outcome)


def main() -> int:
    config = browser.read()
    if not config:
        raise ValueError("browser is not configured")
    server = Gate(("127.0.0.1", config["proxy_port"]), config)
    ready = os.environ.get("SC_BROWSER_READY_FD")
    if ready:
        fd = int(ready)
        os.write(fd, b"1")
        os.close(fd)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
