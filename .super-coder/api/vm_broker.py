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
    GET  /status               read-only domain, SSH, and tunnel state
    GET  /vm                   read the saved vm block
    PUT  /vm        {vm}        write the vm block
    POST /exec      {command}   ssh the guest -> {ok, exit, stdout, stderr}
    POST /start                 start only if off, then wait for SSH readiness
    POST /reset                 virsh snapshot-revert <dom> <snap> --running
    POST /push      {src,dest?} stage a host-visible artifact into transfer_dir
    POST /capture   {command?}  optional exec + a virsh screenshot (base64)
    POST /validate/{check}      one live setup check against the body's candidate cfg
    POST /mcp/up                open the GUI seam: ssh-forward run/vm-mcp.sock
                                to the guest's Windows-MCP port (idempotent)
    POST /mcp/down              close it (idempotent)
    GET  /mcp/status            {ok, running, pid, socket}

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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vm  # noqa: E402  (config + checks + loop verbs + socket path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        self._response_started = False
        self._request_id = uuid.uuid4().hex[:12]
        self._request_started = time.monotonic()
        super().handle_one_request()

    # AF_UNIX peers have no address — the default logger would IndexError on it.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[vm-broker] " + (fmt % args) + "\n")

    def _send(self, code: int, payload: dict) -> None:
        if self._response_started:
            self._log("response_suppressed", code=code)
            return
        body = json.dumps(payload).encode()
        self._response_started = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._log("response", code=code, bytes=len(body))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
            # The VM operation may already have completed. A disconnected caller
            # gets one attempted response and one bounded server-side fact; never
            # retry a write on the same socket or leak request payloads to logs.
            self._log("response_lost", code=code, error=type(exc).__name__)

    def _log(self, event: str, **fields: object) -> None:
        elapsed_ms = int((time.monotonic() - self._request_started) * 1000)
        safe = " ".join(f"{key}={str(value)[:120]}" for key, value in fields.items())
        suffix = f" {safe}" if safe else ""
        sys.stderr.write(
            f"[vm-broker] request_id={self._request_id} method={self.command} "
            f"path={self.path[:160]} event={event} elapsed_ms={elapsed_ms}{suffix}\n"
        )

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            value = json.loads(self.rfile.read(n).decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "vm-broker"})
        if self.path == "/status":
            return self._send(200, vm.do_status())
        if self.path == "/vm":
            return self._send(200, {"vm": vm.read()})
        if self.path == "/mcp/status":
            return self._send(200, vm.mcp_status())
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
            if self.path == "/start":
                with self.server.vm_mutation_lock:
                    return self._send(200, vm.do_start())
            if self.path == "/reset":
                # {"running": false} ends a run clean + powered OFF (frees host
                # RAM); default true boots a clean box to START a run.
                with self.server.vm_mutation_lock:
                    return self._send(200, vm.do_reset(self._body().get("running", True)))
            if self.path == "/push":
                b = self._body()
                return self._send(200, vm.do_push(b.get("src", ""), b.get("dest")))
            if self.path == "/capture":
                return self._send(200, vm.do_capture(self._body().get("command")))
            if self.path == "/mcp/up":
                # The GUI seam (#263): forward run/vm-mcp.sock to the guest's
                # Windows-MCP. Target port comes from the SAVED block, never
                # the caller — the sandbox names an action, not a destination.
                return self._send(200, vm.do_mcp_up())
            if self.path == "/mcp/down":
                return self._send(200, vm.do_mcp_down())
            if self.path.startswith("/validate/"):
                r = vm.validate(self.path.rsplit("/", 1)[1], self._body().get("vm") or {})
                if r is None:
                    return self._send(404, {"ok": False, "error": "no such check"})
                return self._send(200, r)
        except Exception as e:  # noqa: BLE001 — broker boundary must survive verb faults
            self._log("handler_error", error=type(e).__name__)
            return self._send(500, {"ok": False, "error": "broker request failed"})
        return self._send(404, {"ok": False, "error": "no such route"})


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """HTTP over a unix socket. Clears a stale socket from a crashed prior run
    (else bind fails EADDRINUSE) and locks the socket to the owner (0600)."""
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        self.vm_mutation_lock = threading.Lock()
        super().__init__(*args, **kwargs)

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
