#!/usr/bin/env python3
"""In-sandbox TCP→unix relay — the last inch of the Windows-MCP GUI seam (#263).

The broker's `/mcp/up` forwards `run/vm-mcp.sock` (visible in the sandbox via
the bind mount) to the guest's localhost-bound Windows-MCP server. But
`claude mcp add --transport http` only speaks TCP URLs, and a host-side TCP
tunnel would be invisible to the container (and was rejected by the broker
spec as a network surface). So this relay runs INSIDE the container: it
listens on 127.0.0.1:<port> (default 18000) and pipes each connection's bytes
to the socket, both directions. A raw byte pipe — SSE/chunked streaming pass
through untouched, no HTTP parsing anywhere.

Stdlib only (the sandbox has python3 and nothing else). Run IN the sandbox:

    ./sc vm-mcp-relay up [port]     background (pidfile); idempotent
    ./sc vm-mcp-relay down          stop it
    ./sc vm-mcp-relay status        {ok, running, pid, port, upstream}
    ./sc vm-mcp-relay fg [port]     foreground (what `up` daemonizes)

Then connect the harness:

    claude mcp add --transport http windows-mcp http://127.0.0.1:18000/mcp

The pidfile lives in run/ next to the sockets, but pids are namespace-local:
this relay is started, inspected, and stopped from inside the container only.
The broker never touches it.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import vm  # single source for RUN_DIR / MCP_SOCKET

DEFAULT_PORT = 18000
PIDFILE = vm.RUN_DIR / "vm-mcp-relay.pid"
PORTFILE = vm.RUN_DIR / "vm-mcp-relay.port"
LOG = vm.RUN_DIR / "vm-mcp-relay.log"


# -- the pipe -----------------------------------------------------------------

def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src→dst until EOF, then half-close dst so the peer sees the
    EOF too (streamable-http holds long-lived SSE responses — a full close
    here would cut the other direction mid-stream)."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(str(vm.MCP_SOCKET))
    except OSError as e:
        sys.stderr.write(f"[vm-mcp-relay] upstream connect failed: {e} — "
                         "is the broker tunnel up? (POST /mcp/up)\n")
        client.close()
        upstream.close()
        return
    t = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    t.start()
    _pump(client, upstream)
    t.join()
    client.close()
    upstream.close()


def make_server(port: int) -> socket.socket:
    """Bind the loopback listener (port 0 → ephemeral, for tests)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    return srv


def run(srv: socket.socket) -> None:
    """Accept loop — one thread per connection; MCP sessions are few."""
    while True:
        try:
            client, _ = srv.accept()
        except OSError:  # listener closed — shutting down
            return
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


# -- supervision (nohup-style, mirroring the sc broker pattern) ---------------

def _pid_alive() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def status() -> dict:
    pid = _pid_alive()
    port = None
    try:
        port = int(PORTFILE.read_text().strip())
    except (OSError, ValueError):
        pass
    return {"ok": True, "running": pid is not None, "pid": pid, "port": port,
            # upstream = the broker's tunnel socket; the relay works only when
            # both halves are up, so surface the other half here too
            "upstream": vm.MCP_SOCKET.exists()}


def up(port: int) -> dict:
    if pid := _pid_alive():
        return {"ok": True, "output": f"relay already up (pid {pid})", **status()}
    vm.RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "wb") as log:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "fg", str(port)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    PIDFILE.write_text(str(p.pid))
    PORTFILE.write_text(str(port))
    # verify the listener answers before reporting success
    for _ in range(25):
        if p.poll() is not None:
            err = LOG.read_text(errors="replace").strip()[-500:]
            PIDFILE.unlink(missing_ok=True)
            return {"ok": False, "output": f"relay exited (rc {p.returncode}): {err or '(no output)'}"}
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            r = {"ok": True, "pid": p.pid, "port": port,
                 "url": f"http://127.0.0.1:{port}/mcp",
                 "upstream": vm.MCP_SOCKET.exists()}
            if not r["upstream"]:
                r["output"] = ("relay up, but the broker tunnel socket is absent — "
                               "connections will fail until POST /mcp/up on the vm-broker")
            return r
        except OSError:
            pass
    p.terminate()
    PIDFILE.unlink(missing_ok=True)
    return {"ok": False, "output": f"relay did not start listening on 127.0.0.1:{port}"}


def down() -> dict:
    pid = _pid_alive()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    PIDFILE.unlink(missing_ok=True)
    PORTFILE.unlink(missing_ok=True)
    return {"ok": True,
            "output": f"relay stopped (pid {pid})" if pid else "relay not running"}


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "status"
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    if mode == "fg":
        srv = make_server(port)
        sys.stderr.write(f"[vm-mcp-relay] 127.0.0.1:{port} -> {vm.MCP_SOCKET}\n")
        run(srv)
    elif mode == "up":
        r = up(port)
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    elif mode == "down":
        print(json.dumps(down()))
    elif mode == "status":
        print(json.dumps(status()))
    else:
        sys.exit("usage: vm_mcp_relay.py [up [port]|down|status|fg [port]]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
