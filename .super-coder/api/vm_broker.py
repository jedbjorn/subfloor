#!/usr/bin/env python3
"""VM and remote broker — host-side authority for libvirt and named SSH targets.

A fork's shells run in a sandbox container that cannot reach the VM (no route
across libvirt NAT), holds no ssh key, and has no `virsh`. This broker runs ON
THE HOST, where the key + libvirt live, and exposes the loop verbs over a unix
socket inside the bind-mounted engine dir (`.super-coder/run/vm-broker.sock`).
Shell clients call that socket; keys never enter the sandbox and `virsh` runs
where it works. One host process holds the secret so nothing downstream needs
it. The socket path is shared with sandboxes through the engine bind mount.

Routes (all JSON `{ok, ...}`):

    GET  /health               liveness
    GET  /status               read-only domain, SSH, and tunnel state
    GET  /vm                   read the saved vm block
    PUT  /vm        {vm}        write the vm block
    POST /exec      {command}   ssh the guest -> {ok, exit, stdout, stderr}
    POST /start                 start only if off, then wait for SSH readiness
    POST /stop       {force?}   shut down, or destroy only with force=true
    POST /restart               graceful stop followed by start readiness
    GET  /snapshot/list         list snapshots with current marker
    POST /snapshot/create       create one offline snapshot
    POST /snapshot/delete       delete one non-configured snapshot
    POST /reset      {snapshot?} revert a named/default snapshot, powered off
    POST /push      {src,dest?} stage a host-visible artifact into transfer_dir
    POST /capture   {command?}  optional exec + a virsh screenshot (base64)
    POST /validate/{check}      one live setup check against the body's candidate cfg
    POST /mcp/up                open the GUI seam: ssh-forward run/vm-mcp.sock
                                to the guest's Windows-MCP port (idempotent)
    POST /mcp/down              close it (idempotent)
    GET  /mcp/status            {ok, running, pid, socket}
    GET  /remote/<name>/status  broker-held-key SSH readiness
    POST /remote/<name>/exec    execute one remote command
    POST /remote/<name>/push    copy one repo-contained file to the remote
    POST /remote/<name>/pull    copy into the repo or .sc-state/local

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
import remote  # noqa: E402  (named remote host verbs share this broker)
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
        return

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

    def _guard(self, action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — broker boundary survives verb faults
            self._log("handler_error", error=type(exc).__name__)
            self._send(500, {"ok": False, "error": "broker request failed"})

    def _mutate(self, action) -> None:
        lock = self.server.vm_mutation_lock
        if not lock.acquire(timeout=vm.MUTATION_LOCK_TIMEOUT):
            return self._send(409, {
                "ok": False,
                "error": "vm_busy",
                "output": "another VM mutation is still running",
                "wait_seconds": vm.MUTATION_LOCK_TIMEOUT,
            })
        try:
            result = action()
        finally:
            lock.release()
        return self._send(200, result)

    def do_GET(self) -> None:
        self._guard(self._do_get)

    def _remote_route(self) -> tuple[str, str] | None:
        parts = self.path.split("/")
        if len(parts) == 4 and parts[1] == "remote" and parts[2] and parts[3]:
            return parts[2], parts[3]
        return None

    def _do_get(self) -> None:
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "vm-broker"})
        if self.path == "/status":
            return self._send(200, vm.do_status())
        if self.path == "/snapshot/list":
            return self._send(200, vm.do_snapshot_list())
        if self.path == "/vm":
            return self._send(200, {"vm": vm.read()})
        if self.path == "/mcp/status":
            return self._send(200, vm.mcp_status())
        remote_route = self._remote_route()
        if remote_route and remote_route[1] == "status":
            return self._send(200, remote.do_status(remote_route[0]))
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_PUT(self) -> None:
        self._guard(self._do_put)

    def _do_put(self) -> None:
        if self.path == "/vm":
            block = self._body().get("vm")
            if block is not None and not isinstance(block, dict):
                return self._send(400, {"ok": False, "error": "vm must be an object"})
            return self._send(200, {"ok": True, "vm": vm.write(block)})
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_POST(self) -> None:
        self._guard(self._do_post)

    def _do_post(self) -> None:
        remote_route = self._remote_route()
        if remote_route and remote_route[1] in {"exec", "push", "pull"}:
            name, operation = remote_route
            body = self._body()
            if operation == "exec":
                return self._mutate(
                    lambda: remote.do_exec(name, body.get("command", ""))
                )
            if operation == "push":
                return self._mutate(
                    lambda: remote.do_push(name, body.get("src"), body.get("dest"))
                )
            return self._mutate(
                lambda: remote.do_pull(name, body.get("src"), body.get("dest"))
            )
        if self.path == "/exec":
            b = self._body()
            return self._mutate(
                lambda: vm.do_exec(b.get("command", ""), int(b.get("timeout", 120)))
            )
        if self.path == "/start":
            return self._mutate(vm.do_start)
        if self.path == "/stop":
            force = self._body().get("force", False)
            if not isinstance(force, bool):
                return self._send(400, {"ok": False, "error": "force must be boolean"})
            return self._mutate(lambda: vm.do_stop(force=force))
        if self.path == "/restart":
            return self._mutate(vm.do_restart)
        if self.path == "/snapshot/create":
            name = self._body().get("name", "")
            return self._mutate(lambda: vm.do_snapshot_create(name))
        if self.path == "/snapshot/delete":
            name = self._body().get("name", "")
            return self._mutate(lambda: vm.do_snapshot_delete(name))
        if self.path == "/reset":
            # {"running": false} ends a run clean + powered OFF (frees host
            # RAM); default true boots a clean box to START a run.
            body = self._body()
            running = body.get("running", True)
            return self._mutate(
                lambda: vm.do_reset(running=running, snapshot=body.get("snapshot"))
            )
        if self.path == "/push":
            b = self._body()
            return self._mutate(
                lambda: vm.do_push(b.get("src", ""), b.get("dest"))
            )
        if self.path == "/capture":
            command = self._body().get("command")
            return self._mutate(lambda: vm.do_capture(command))
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
