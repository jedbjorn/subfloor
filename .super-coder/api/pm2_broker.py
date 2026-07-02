#!/usr/bin/env python3
"""pm2 Broker — the host-side authority that observes + manages the host's
pm2-supervised app stack.

A fork's shells run in a sandbox container that has no pm2 binary, no route to
the host's 127.0.0.1-bound ports, and no way to watch a host-run `make deploy`.
This broker runs ON THE HOST, where pm2 and the app live, and exposes narrow
verbs over a unix socket inside the bind-mounted engine dir
(`.super-coder/run/pm2-broker.sock`). The `pm2` skill curls that socket; the
sandbox names verbs and holds nothing. It is the third sibling of the Windows
VM broker (api/vm_broker.py) and the tailnet broker (api/ts_broker.py): one
host process holds the capability so nothing downstream needs it. Spec:
.super-coder/docs/pm2-broker.md.

Routes (all JSON `{ok, ...}`):

    GET  /health               liveness (of the broker, not the app)
    GET  /pm2                  read the saved pm2 block
    PUT  /pm2       {pm2}       write the pm2 block
    GET  /status                parsed `pm2 jlist`, declared processes only
    GET  /app-health            curl the app's local health_url, host-side
    POST /logs      {proc, lines?}  tail one process's out+err logs
    POST /restart   {proc}      pm2 restart (allowlisted procs)
    POST /stop      {proc}      pm2 stop  (+ allow_lifecycle gate)
    POST /start     {proc}      pm2 start (+ allow_lifecycle gate)
    POST /validate/{check}      one live setup check against the body's candidate cfg

Verbs act on the SAVED `pm2` block; `/validate` tests the CANDIDATE block in
the body (before save). Every verb is fail-closed on the block's `processes`
allowlist — the sandbox can only see + bounce what the fork declared. The
socket is fs-perm gated (0600) — reachable only by processes sharing the bind
mount; no network surface, no auth token.

Run on the HOST (never in the sandbox):
    ./sc pm2-broker        foreground
    ./sc pm2-broker-up     background (pidfile) ; ./sc pm2-broker-down to stop
"""
from __future__ import annotations

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pm2  # noqa: E402  (config + checks + loop verbs + socket path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # AF_UNIX peers have no address — the default logger would IndexError on it.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[pm2-broker] " + (fmt % args) + "\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                return self._send(200, {"ok": True, "service": "pm2-broker"})
            if self.path == "/pm2":
                return self._send(200, {"pm2": pm2.read()})
            if self.path == "/status":
                return self._send(200, pm2.do_status())
            if self.path == "/app-health":
                return self._send(200, pm2.do_health())
        except Exception as e:  # never let one bad call kill a worker thread
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_PUT(self) -> None:
        if self.path == "/pm2":
            block = self._body().get("pm2")
            if block is not None and not isinstance(block, dict):
                return self._send(400, {"ok": False, "error": "pm2 must be an object"})
            return self._send(200, {"ok": True, "pm2": pm2.write(block)})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_POST(self) -> None:
        try:
            if self.path == "/logs":
                b = self._body()
                return self._send(200, pm2.do_logs(b.get("proc", ""),
                                                   int(b.get("lines", 100))))
            if self.path in ("/restart", "/stop", "/start"):
                return self._send(200, pm2.do_lifecycle(self.path.lstrip("/"),
                                                        self._body().get("proc", "")))
            if self.path.startswith("/validate/"):
                r = pm2.validate(self.path.rsplit("/", 1)[1],
                                 self._body().get("pm2") or {})
                if r is None:
                    return self._send(404, {"ok": False, "error": "no such check"})
                return self._send(200, r)
        except Exception as e:  # never let one bad call kill a worker thread
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"ok": False, "error": "no such route"})


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """HTTP over a unix socket. Clears a stale socket from a crashed prior run
    (else bind fails EADDRINUSE) and locks the socket to the owner (0600)."""
    daemon_threads = True

    def server_bind(self) -> None:
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        super().server_bind()
        os.chmod(self.server_address, 0o600)


def main(argv: list[str]) -> int:
    if os.environ.get("SC_SANDBOX"):
        sys.exit("pm2-broker must run on the HOST (pm2 + the app live there), "
                 "not inside the sandbox. Run `./sc pm2-broker` on the host.")
    sock = pm2.SOCKET
    sock.parent.mkdir(parents=True, exist_ok=True)
    srv = UnixHTTPServer(str(sock), Handler)
    sys.stderr.write(f"[pm2-broker] listening on {sock}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            os.unlink(sock)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
