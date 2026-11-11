#!/usr/bin/env python3
"""Engine-ops service — the host-side authority that runs engine update,
restart, and harness update for the review GUI.

On the docker runtime the review API runs inside the sandbox and cannot drive
the host's docker, so it cannot run these verbs; on the host runtime it would
be stopping itself. This service runs ON THE HOST under both runtimes and owns
them. It is a sibling of the vm, ts, pm2, and db brokers (host process, fixed
verbs, refuses to run in the sandbox, started and stopped with `./sc launch`
and `./sc down`), with one deliberate difference in its boundary:

It does NOT listen on a unix socket in the bind-mounted engine directory.
Every sandboxed shell can reach that directory, and these verbs replace the
engine. It listens on its own host loopback TCP port (127.0.0.1 only, one per
fork, `./sc engine-ops-port`), which a sandbox has no route to. The browser
calls it directly. Spec: .super-coder/docs/engine-ops.md.

Routes (all JSON):

    GET     /health        liveness of this service
    GET     /status        runtime, per-op facts, current or last operation,
                           bounded log tail, engine ref, live sessions
    POST    /ops/<op>      start update | restart | update-harnesses
    OPTIONS *              CORS preflight, for the GUI origin only

Every request must name a loopback Host on this port and must carry no
Authorization header: a shell credential is refused, and the browser operator
has none. A POST must carry an Origin equal to this install's GUI origin; a
GET may omit Origin, but a foreign one is refused. CORS headers are sent for
the GUI origin and for nothing else.

The verb table is fixed (`engine_ops.VERBS`). The path segment selects a row;
the request body and query string are never read. An operation runs detached
from this service (`engine_ops.start`), because `./sc restart` and `./sc down`
stop it along with the brokers.

Run on the HOST (never in the sandbox):
    ./sc engine-ops         foreground
    ./sc engine-ops-up      background (pidfile) ; ./sc engine-ops-down to stop
"""
from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_ops  # noqa: E402  (verb table, status file, detached runner)

# The only address this service binds. Not configurable: a wider bind would
# put engine replacement on the network.
BIND = engine_ops.BIND
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
OPS_PREFIX = "/ops/"


def gui_origins() -> frozenset[str]:
    """The origins the review GUI of THIS install is served from."""
    import ports  # noqa: PLC0415  (scripts/, on sys.path)

    port = ports.resolve(persist=False).get("port")
    if type(port) is not int:
        return frozenset()
    return frozenset(f"http://{host}:{port}" for host in LOOPBACK_HOSTS)


def in_sandbox() -> bool:
    return bool(os.environ.get("SC_SANDBOX")) or any(
        os.path.exists(marker) for marker in _CONTAINER_MARKERS)


def _error(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: one request per connection. Request bodies are never read, and
    # an unread body must not be parsed as the next request.
    protocol_version = "HTTP/1.0"
    server_version = "sc-engine-ops"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[engine-ops] " + (fmt % args) + "\n")

    # ── responses ────────────────────────────────────────────────────────────

    def _cors(self) -> list[tuple[str, str]]:
        """CORS headers, only when the request's Origin is the GUI's."""
        origin = self.headers.get("Origin")
        if not origin or origin not in gui_origins():
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
        ]

    def _send(self, code: int, payload: dict | None,
              extra: list[tuple[str, str]] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in self._cors() + (extra or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ── gates ────────────────────────────────────────────────────────────────

    def _gate(self, *, mutation: bool) -> bool:
        """Loopback Host, no credential, and the GUI origin. False = answered."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in {f"{name}:{port}" for name in LOOPBACK_HOSTS}:
            self._send(403, _error(
                "loopback_host_required",
                "the engine-ops service answers only its own loopback address"))
            return False
        if self.headers.get("Authorization") is not None:
            self._send(403, _error(
                "fnb_operator_required",
                "engine operations are owned by the browser FnB operator; "
                "a shell credential cannot start or read them"))
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            if mutation:
                self._send(403, _error(
                    "same_origin_required",
                    "engine operations require the review GUI's origin"))
                return False
        elif origin not in gui_origins():
            self._send(403, _error(
                "same_origin_required",
                "engine operations require the review GUI's origin"))
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, _error(
                "same_origin_required",
                "engine operations require the review GUI's origin"))
            return False
        return True

    # ── routes ───────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        if not self._gate(mutation=True):
            return
        extra = [
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Access-Control-Max-Age", "600"),
        ]
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            extra.append(("Access-Control-Allow-Private-Network", "true"))
        self._send(204, None, extra)

    def do_GET(self) -> None:
        if not self._gate(mutation=False):
            return
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "service": "engine-ops"})
            if path == "/status":
                return self._send(200, engine_ops.status())
            return self._send(404, _error("not_found", "not found"))
        except Exception as exc:  # noqa: BLE001 — one request must not end the service
            return self._send(500, _error("internal_error", str(exc)))

    def do_POST(self) -> None:
        if not self._gate(mutation=True):
            return
        path = urlparse(self.path).path
        if not path.startswith(OPS_PREFIX):
            return self._send(404, _error("not_found", "not found"))
        op = path[len(OPS_PREFIX):]
        try:
            operation = engine_ops.start(op)
        except engine_ops.UnknownOp:
            return self._send(404, _error(
                "unknown_engine_op", "no such engine operation",
                {"ops": sorted(engine_ops.VERBS)}))
        except engine_ops.Busy as exc:
            return self._send(409, {
                "operation": exc.operation,
                **_error("engine_op_running", str(exc),
                         {"op": exc.operation.get("op")}),
            })
        except engine_ops.StartFailed as exc:
            return self._send(500, _error("engine_op_start_failed", str(exc)))
        except Exception as exc:  # noqa: BLE001 — one request must not end the service
            return self._send(500, _error("internal_error", str(exc)))
        return self._send(202, {"started": True, "operation": operation})

    def _method_not_allowed(self) -> None:
        self._send(405, _error("method_not_allowed", "method not allowed"))

    do_PUT = do_PATCH = do_DELETE = do_HEAD = _method_not_allowed


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port: int) -> Server:
    """The service's listener, bound to loopback and nothing else."""
    return Server((BIND, port), Handler)


def _write_state(port: int) -> None:
    engine_ops.write_status(engine_ops.STATE_FILE, {
        "service": "engine-ops",
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def _clear_state() -> None:
    state = engine_ops.read_status(engine_ops.STATE_FILE) or {}
    if state.get("pid") == os.getpid():
        try:
            engine_ops.STATE_FILE.unlink()
        except OSError:
            pass


def serve(port: int) -> int:
    """Listen until stopped. `port` 0 asks the kernel for one (tests)."""
    srv = make_server(port)
    actual = srv.server_address[1]

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    _write_state(actual)
    sys.stderr.write(f"[engine-ops] listening on http://{BIND}:{actual}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _clear_state()
        srv.server_close()
    return 0


def main(argv: list[str]) -> int:
    if in_sandbox():
        sys.exit("engine-ops must run on the HOST (it runs ./sc update, restart, "
                 "and update-harnesses there), not inside the sandbox. "
                 "Run `./sc engine-ops` on the host.")
    if argv:
        sys.exit("usage: engine_ops_service.py   (no arguments; the port is "
                 "this install's `./sc engine-ops-port`)")
    import ports  # noqa: PLC0415  (scripts/, on sys.path)

    return serve(ports.ensure_engine_ops_port())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
