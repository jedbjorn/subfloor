"""Test helpers: HTTP API client, WS client, tmux access, replay comparison."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

SPIKE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "programs")
PY = sys.executable


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class Api:
    def __init__(self, port: int, token: str):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def __call__(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class WSClient:
    """Synchronous test WS client with a background receive pump."""

    def __init__(self, api: Api, sid: str, role: str = "viewer",
                 lease_token: str | None = None, port: int | None = None):
        st, body = api("POST", "/api/interface/stream-tickets",
                       {"session_id": sid, "role": role, "lease_token": lease_token})
        assert st == 201, f"ticket mint failed: {st} {body}"
        port = port or int(api.base.rsplit(":", 1)[1])
        self.ws = connect(
            f"ws://127.0.0.1:{port}/api/interface/session-streams/{sid}"
            f"?ticket={body['ticket']}",
            subprotocols=["sc-term.v1"], open_timeout=10)
        self.sid = sid
        self.out = bytearray()
        self.redraws: list[bytes] = []
        self.controls: list[dict] = []
        self._ctrl_cursor = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.close_code: int | None = None
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for message in self.ws:
                with self.lock:
                    if isinstance(message, bytes):
                        if message[:1] == b"\x00":
                            self.out += message[1:]
                        elif message[:1] == b"\x04":
                            self.redraws.append(bytes(message[1:]))
                    else:
                        self.controls.append(json.loads(message))
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
        finally:
            self.closed.set()

    def send_input(self, seq: int, payload: bytes) -> None:
        self.ws.send(b"\x01" + seq.to_bytes(8, "big") + payload)

    def send_resize(self, rows: int, cols: int) -> None:
        self.ws.send(b"\x03" + rows.to_bytes(2, "big") + cols.to_bytes(2, "big"))

    def send_wake(self) -> None:
        self.ws.send(json.dumps({"type": "wake"}))

    def send_heartbeat(self) -> None:
        self.ws.send(json.dumps({"type": "heartbeat"}))

    def output(self) -> bytes:
        with self.lock:
            return bytes(self.out)

    def control(self, pred, timeout: float = 10.0) -> dict:
        """Wait for the NEXT control message matching pred (consuming cursor)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                while self._ctrl_cursor < len(self.controls):
                    msg = self.controls[self._ctrl_cursor]
                    self._ctrl_cursor += 1
                    if pred(msg):
                        return msg
            time.sleep(0.01)
        raise TimeoutError("no matching control message")

    def wait_output(self, pred, timeout: float = 30.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.output()
            if pred(data):
                return data
            time.sleep(0.02)
        raise TimeoutError(f"output predicate not met (have {len(self.output())} bytes)")

    def out_len(self) -> int:
        """Length-only check: no whole-buffer copy (matters at MB scale)."""
        with self.lock:
            return len(self.out)

    def wait_len(self, n: int, timeout: float = 30.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.out_len() >= n:
                return self.output()
            time.sleep(0.05)
        raise TimeoutError(f"length predicate not met (have {self.out_len()} bytes)")

    def wait_redraw(self, timeout: float = 10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.redraws:
                    return self.redraws[0]
            time.sleep(0.01)
        raise TimeoutError("no redraw received")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        self.closed.wait(5)


def tmux(sock: str, *args: str) -> bytes:
    res = subprocess.run(["tmux", "-S", sock, *args],
                         capture_output=True, timeout=20)
    if res.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {res.stderr.decode(errors='replace')}")
    return res.stdout


def capture_pane(sock: str, pane_id: str) -> bytes:
    return tmux(sock, "capture-pane", "-epN", "-t", pane_id)


def capture_text(sock: str, pane_id: str) -> str:
    return tmux(sock, "capture-pane", "-p", "-t", pane_id).decode(errors="replace")


def pane_fmt(sock: str, pane_id: str, fmt: str) -> str:
    return tmux(sock, "display-message", "-p", "-t", pane_id, fmt).decode().strip()


def replay_dump(cols: int, rows: int, data: bytes) -> dict:
    """Replay a byte stream into a fresh @xterm/headless terminal; semantic dump."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
        # capture-pane style replays need CR before each LF row separator
        fh.write(data)
        path = fh.name
    try:
        res = subprocess.run(
            ["node", os.path.join(SPIKE_ROOT, "shadow", "dump.js"),
             str(cols), str(rows), path],
            capture_output=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"dump.js failed: {res.stderr.decode(errors='replace')[:500]}")
        return json.loads(res.stdout)
    finally:
        os.unlink(path)


def replay_capture(cols: int, rows: int, capture: bytes) -> dict:
    """Replay a capture-pane -epN dump. Rows are LF-separated; emitting bare
    LFs would scroll at the bottom edge and double-step after full-width
    rows, so position each row with an absolute CUP instead."""
    lines = capture.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    data = b"".join(b"\x1b[%d;1H" % (i + 1) + line for i, line in enumerate(lines))
    return replay_dump(cols, rows, data)


def grids_equal(a: dict, b: dict) -> tuple[bool, str]:
    if (a["cols"], a["rows"]) != (b["cols"], b["rows"]):
        return False, f"geometry {a['cols']}x{a['rows']} vs {b['cols']}x{b['rows']}"
    for y, (ra, rb) in enumerate(zip(a["grid"], b["grid"])):
        for x, (ca, cb) in enumerate(zip(ra, rb)):
            if ca != cb:
                return False, f"cell ({x},{y}): {ca!r} vs {cb!r}"
    return True, "grids identical"


def wait_file(path: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.02)
    raise TimeoutError(f"no file {path}")


def build_corpus() -> bytes:
    """All 256 byte values, UTF-8, a bracketed-paste frame, 114 KiB pattern."""
    corpus = bytes(range(256))
    corpus += "héllo⛄".encode()
    corpus += b"\x1b[200~" + b"paste:" + bytes(range(256)) + b":end\x1b[201~"
    corpus += (b"0123456789abcdef" * (114 * 1024 // 16 + 1))[:114 * 1024]
    return corpus
