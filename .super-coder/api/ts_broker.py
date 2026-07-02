#!/usr/bin/env python3
"""Tailnet Broker — the host-side authority that drives the tailnet.

A fork's shells run in a sandbox container that cannot join the tailnet (no
route, no TUN, no NET_ADMIN) and must not hold a tailnet credential. This broker
runs ON THE HOST, where the node is already `tailscale up`, and exposes the loop
verbs over a unix socket inside the bind-mounted engine dir
(`.super-coder/run/ts-broker.sock`). The `tailscale` skill curls that socket; the
tailnet identity never enters the fork. It is the sibling of the Windows VM
broker (api/vm_broker.py): one host process holds the credential so nothing
downstream needs it. Spec: .super-coder/docs/tailscale-broker.md.

Routes (all JSON `{ok, ...}`):

    GET  /health               liveness
    GET  /ts                   read the saved ts block
    PUT  /ts        {ts}        write the ts block
    GET  /status               `tailscale status --json` -> self + peers summary
    POST /exec      {host,command,timeout?}  tailscale ssh -> {ok, exit, stdout, stderr}
    POST /validate/{check}     one live setup check against the body's candidate cfg

Verbs act on the SAVED `ts` block + a caller-named host; `/validate` tests the
CANDIDATE block in the body (the form, before save). The socket is fs-perm gated
(0600) — reachable only by processes sharing the bind mount; no network surface,
no auth token.

Run on the HOST (never in the sandbox):
    ./sc ts-broker        foreground
    ./sc ts-broker-up     background (pidfile) ; ./sc ts-broker-down to stop
"""
from __future__ import annotations

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ts  # noqa: E402  (config + checks + loop verbs + socket path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # AF_UNIX peers have no address — the default logger would IndexError on it.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[ts-broker] " + (fmt % args) + "\n")

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
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "ts-broker"})
        if self.path == "/ts":
            return self._send(200, {"ts": ts.read()})
        if self.path == "/status":
            return self._send(200, ts.do_status())
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_PUT(self) -> None:
        if self.path == "/ts":
            block = self._body().get("ts")
            if block is not None and not isinstance(block, dict):
                return self._send(400, {"ok": False, "error": "ts must be an object"})
            return self._send(200, {"ok": True, "ts": ts.write(block)})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_POST(self) -> None:
        try:
            if self.path == "/exec":
                b = self._body()
                return self._send(200, ts.do_exec(b.get("host", ""),
                                                  b.get("command", ""),
                                                  int(b.get("timeout", 120))))
            if self.path.startswith("/validate/"):
                r = ts.validate(self.path.rsplit("/", 1)[1], self._body().get("ts") or {})
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
        sys.exit("ts-broker must run on the HOST (the tailnet node + tailscale "
                 "CLI live there), not inside the sandbox. Run `./sc ts-broker` "
                 "on the host.")
    sock = ts.SOCKET
    sock.parent.mkdir(parents=True, exist_ok=True)
    srv = UnixHTTPServer(str(sock), Handler)
    sys.stderr.write(f"[ts-broker] listening on {sock}\n")
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
