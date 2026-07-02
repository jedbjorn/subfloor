#!/usr/bin/env python3
"""Windows VM Broker — the host-side authority that drives the test VM.

A fork's shells run in a sandbox container that cannot reach the VM (no route
across libvirt NAT), holds no ssh key, and has no `virsh`. This broker runs ON
THE HOST, where the key + libvirt live, and exposes the loop verbs over a unix
socket inside the bind-mounted engine dir (`.super-coder/run/vm-broker.sock`).
`windows_devkit` curls that socket; the key never enters the fork and `virsh`
runs where it works. It mirrors dos-arch's credential-broker precedent: one host
process holds the secret so nothing downstream needs it. Spec:
.super-coder/docs/windows-vm-broker.md.

Routes (all JSON `{ok, ...}`):

    GET  /health               liveness
    GET  /vm                   read the saved vm block
    PUT  /vm        {vm}        write the vm block
    POST /exec      {command}   ssh the guest -> {ok, exit, stdout, stderr}
    POST /reset                 virsh snapshot-revert <dom> <snap> --running
    POST /push      {src,dest?} stage a host-visible artifact into transfer_dir
    POST /capture   {command?}  optional exec + a virsh screenshot (base64)
    POST /validate/{check}      one live setup check against the body's candidate cfg

Verbs act on the SAVED `vm` block; `/validate` tests the CANDIDATE block in the
body (the wizard, before save). The socket is fs-perm gated (0600) — reachable
only by processes sharing the bind mount; no network surface, no auth token.

Run on the HOST (never in the sandbox):
    ./sc vm-broker        foreground
    ./sc vm-broker-up     background (pidfile) ; ./sc vm-broker-down to stop
"""
from __future__ import annotations

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vm  # noqa: E402  (config + checks + loop verbs + socket path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # AF_UNIX peers have no address — the default logger would IndexError on it.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[vm-broker] " + (fmt % args) + "\n")

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
            return self._send(200, {"ok": True, "service": "vm-broker"})
        if self.path == "/vm":
            return self._send(200, {"vm": vm.read()})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_PUT(self) -> None:
        if self.path == "/vm":
            block = self._body().get("vm")
            if block is not None and not isinstance(block, dict):
                return self._send(400, {"ok": False, "error": "vm must be an object"})
            return self._send(200, {"ok": True, "vm": vm.write(block)})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_POST(self) -> None:
        try:
            if self.path == "/exec":
                b = self._body()
                return self._send(200, vm.do_exec(b.get("command", ""),
                                                  int(b.get("timeout", 120))))
            if self.path == "/reset":
                # {"running": false} ends a run clean + powered OFF (frees host
                # RAM); default true boots a clean box to START a run.
                return self._send(200, vm.do_reset(self._body().get("running", True)))
            if self.path == "/push":
                b = self._body()
                return self._send(200, vm.do_push(b.get("src", ""), b.get("dest")))
            if self.path == "/capture":
                return self._send(200, vm.do_capture(self._body().get("command")))
            if self.path.startswith("/validate/"):
                r = vm.validate(self.path.rsplit("/", 1)[1], self._body().get("vm") or {})
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
        sys.exit("vm-broker must run on the HOST (virsh + the ssh key live there), "
                 "not inside the sandbox. Run `./sc vm-broker` on the host.")
    sock = vm.SOCKET
    sock.parent.mkdir(parents=True, exist_ok=True)
    srv = UnixHTTPServer(str(sock), Handler)
    sys.stderr.write(f"[vm-broker] listening on {sock}\n")
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
