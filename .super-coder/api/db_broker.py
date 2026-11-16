#!/usr/bin/env python3
"""Host broker for application-DB diagnosis and repair.

GET /health tests the actual DB connection. POST /query accepts SQL and an
optional autocommit boolean. Credentials stay on the host; SQL permissions are
selected by instance configuration and the connected PostgreSQL role.
"""
from __future__ import annotations

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dbq  # noqa: E402  (config + validation + the query verb + socket path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # AF_UNIX peers have no address — the default logger would IndexError on it.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[db-broker] " + (fmt % args) + "\n")

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
            result = dbq.health()
            return self._send(200 if result["ok"] else 503, result)
        return self._send(404, {"ok": False, "error": "no such route"})

    def do_POST(self) -> None:
        try:
            if self.path == "/query":
                body = self._body()
                if not isinstance(body, dict):
                    return self._send(400, {"ok": False, "error": "body must be a JSON object"})
                sql = body.get("sql", "")
                if not isinstance(sql, str):
                    return self._send(400, {"ok": False, "error": "sql must be a string"})
                autocommit = body.get("autocommit", False)
                if not isinstance(autocommit, bool):
                    return self._send(400, {"ok": False, "error": "autocommit must be a boolean"})
                result = dbq.do_query(sql, autocommit=autocommit)
                _audit(sql, result)
                return self._send(200, result)
        except Exception:  # unexpected driver errors must not expose credentials
            return self._send(500, {"ok": False, "error": "internal database broker error"})
        return self._send(404, {"ok": False, "error": "no such route"})


def _audit(sql: str, result: dict) -> None:
    """Append one line per brokered query to the host-side audit log — a read
    path to live data must be legible after the fact. Best-effort: an audit
    write failure must never fail the query."""
    try:
        dbq.RUN_DIR.mkdir(parents=True, exist_ok=True)
        verdict = "ok" if result.get("ok") else "deny"
        row = {
            "verdict": verdict,
            "rows": result.get("row_count"),
            "truncated": result.get("truncated"),
            "error": result.get("error"),
            "sql": sql,
            "autocommit": result.get("autocommit"),
            "status": result.get("status"),
            "affected_rows": result.get("affected_rows"),
        }
        with open(dbq.RUN_DIR / "db-broker.audit.log", "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


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
        sys.exit("db-broker must run on the HOST (the live DSN + route live "
                 "there), not inside the sandbox. Run `./sc db-broker` on the host.")
    sock = dbq.SOCKET
    sock.parent.mkdir(parents=True, exist_ok=True)
    srv = UnixHTTPServer(str(sock), Handler)
    sys.stderr.write(f"[db-broker] listening on {sock}\n")
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
