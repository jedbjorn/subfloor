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
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import vm  # single source for RUN_DIR / MCP_SOCKET

DEFAULT_PORT = 18000
PIDFILE = vm.RUN_DIR / "vm-mcp-relay.pid"
# Kept only so new commands remove pre-hardening split state safely.
PORTFILE = vm.RUN_DIR / "vm-mcp-relay.port"
LOCKFILE = vm.RUN_DIR / "vm-mcp-relay.lock"
LOG = vm.RUN_DIR / "vm-mcp-relay.log"
READY_TIMEOUT = 5.0
READY_INTERVAL = 0.2


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

def _relay_executable() -> str:
    return sys.executable


def _relay_token() -> str:
    return str(Path(__file__).resolve())


def _relay_process() -> dict | None:
    return vm._owned_process(
        PIDFILE,
        kind="vm-mcp-relay",
        expected_executable=_relay_executable(),
        required_token=_relay_token(),
    )


def _listener_ready(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def _cleanup_state() -> None:
    PIDFILE.unlink(missing_ok=True)
    PORTFILE.unlink(missing_ok=True)


def status() -> dict:
    state = _relay_process()
    port = state.get("port") if state else None
    listening = isinstance(port, int) and _listener_ready(port)
    return {"ok": True, "running": state is not None and listening,
            "pid": state["pid"] if state else None, "port": port,
            "listening": listening,
            # upstream = the broker's tunnel socket; the relay works only when
            # both halves are up, so surface the other half here too
            "upstream": vm.MCP_SOCKET.exists()}


def up(port: int, wait: float = READY_TIMEOUT) -> dict:
    with vm._process_state_lock(LOCKFILE):
        state = _relay_process()
        if state and state.get("port") == port and _listener_ready(port):
            return {
                "ok": True,
                "output": f"relay already up (pid {state['pid']})",
                **status(),
            }
        if state and not vm._terminate_owned_process(
            state,
            expected_executable=_relay_executable(),
            required_token=_relay_token(),
        ):
            return {
                "ok": False,
                "output": f"verified stale relay did not stop (pid {state['pid']})",
            }
        _cleanup_state()
        vm.RUN_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "wb") as log:
            p = subprocess.Popen(
                [sys.executable, _relay_token(), "fg", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        state = vm._new_process_state(
            p.pid,
            kind="vm-mcp-relay",
            expected_executable=_relay_executable(),
            required_token=_relay_token(),
            port=port,
        )
        if state is None:
            if p.poll() is None:
                p.terminate()
            return {"ok": False, "output": "could not verify the new relay process"}
        vm._atomic_write_process_state(PIDFILE, state)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if p.poll() is not None:
                err = vm._bounded_log_tail(LOG)
                _cleanup_state()
                return {
                    "ok": False,
                    "output": f"relay exited (rc {p.returncode}): {err or '(no output)'}",
                }
            if _listener_ready(port):
                result = {
                    "ok": True,
                    "running": True,
                    "listening": True,
                    "pid": p.pid,
                    "port": port,
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "upstream": vm.MCP_SOCKET.exists(),
                }
                if not result["upstream"]:
                    result["output"] = (
                        "relay up, but the broker tunnel socket is absent — "
                        "connections will fail until POST /mcp/up on the vm-broker"
                    )
                return result
            time.sleep(READY_INTERVAL)
        stopped = vm._terminate_owned_process(
            state,
            expected_executable=_relay_executable(),
            required_token=_relay_token(),
        )
        err = vm._bounded_log_tail(LOG)
        if stopped:
            _cleanup_state()
        detail = f": {err}" if err else ""
        return {
            "ok": False,
            "output": (
                f"relay did not start listening on 127.0.0.1:{port} "
                f"within {wait}s{detail}"
            ),
        }


def down() -> dict:
    with vm._process_state_lock(LOCKFILE):
        state = _relay_process()
        stale = (PIDFILE.exists() or PORTFILE.exists()) and state is None
        if state and not vm._terminate_owned_process(
            state,
            expected_executable=_relay_executable(),
            required_token=_relay_token(),
        ):
            return {
                "ok": False,
                "output": f"verified relay did not stop (pid {state['pid']})",
            }
        _cleanup_state()
        if state:
            output = f"relay stopped (pid {state['pid']})"
        elif stale:
            output = "relay not running (stale state removed)"
        else:
            output = "relay not running"
        return {"ok": True, "output": output}


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
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
