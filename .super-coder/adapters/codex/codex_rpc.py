#!/usr/bin/env python3
"""Minimal JSON-RPC client for Codex app-server Unix sockets.

Codex speaks WebSocket frames over the Unix socket, not newline JSON.  Keeping
this client in the adapter avoids adding an engine dependency for one provider.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable


class CodexProtocolError(RuntimeError):
    """The app-server transport or JSON-RPC response violated its contract."""


class CodexRpcError(RuntimeError):
    """The app-server returned a JSON-RPC error."""


SUPPORTED_MAJOR = 0
SUPPORTED_MINOR = 144


def probe_codex(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """Probe the installed CLI and fail active control closed on unknown versions."""
    version_call = run(
        ["codex", "--version"], capture_output=True, text=True, timeout=5, check=False
    )
    version_text = (version_call.stdout or version_call.stderr or "").strip()
    match = re.search(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)", version_text)
    version = match.group(0).removeprefix("codex-cli ") if match else None
    app_help = run(
        ["codex", "app-server", "--help"], capture_output=True, text=True,
        timeout=5, check=False,
    )
    resume_help = run(
        ["codex", "exec", "resume", "--help"], capture_output=True, text=True,
        timeout=5, check=False,
    )
    app_text = (app_help.stdout or "") + (app_help.stderr or "")
    resume_text = (resume_help.stdout or "") + (resume_help.stderr or "")
    known_version = bool(
        match and int(match.group(1)) == SUPPORTED_MAJOR
        and int(match.group(2)) == SUPPORTED_MINOR
    )
    unix_transport = app_help.returncode == 0 and "unix://" in app_text and "--listen" in app_text
    resume = resume_help.returncode == 0 and "SESSION_ID" in resume_text
    return {
        "cli_version": version,
        "active_delivery": bool(known_version and unix_transport),
        "create": bool(known_version and unix_transport),
        "deliver": bool(known_version and unix_transport),
        "resume": resume,
        "status": bool(known_version and unix_transport),
        "transport": "unix-websocket-v2" if unix_transport else None,
        "normal_steer": False,
    }


def unix_socket_path(endpoint: str) -> Path:
    if not endpoint.startswith("unix://"):
        raise ValueError("Codex control endpoint must use unix://")
    path = Path(endpoint[len("unix://"):])
    if not path.is_absolute():
        raise ValueError("Codex control socket path must be absolute")
    return path


def encode_frame(payload: bytes, *, opcode: int = 1, mask: bytes | None = None) -> bytes:
    """Encode one client WebSocket frame; client frames are always masked."""
    mask = mask or os.urandom(4)
    if len(mask) != 4:
        raise ValueError("WebSocket mask must contain four bytes")
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise CodexProtocolError("Codex app-server closed the control socket")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    """Decode one WebSocket frame and return ``(final, opcode, payload)``."""
    first, second = _recv_exact(sock, 2)
    final = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return final, opcode, payload


class AppServerClient:
    """Synchronous initialized app-server connection over a Unix socket."""

    def __init__(self, endpoint: str, *, timeout: float = 5.0,
                 socket_factory: Callable[..., socket.socket] = socket.socket):
        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.sock: socket.socket | None = None
        self.next_id = 1
        self.notifications: list[dict] = []

    def __enter__(self) -> "AppServerClient":
        path = unix_socket_path(self.endpoint)
        sock = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(path))
        self.sock = sock
        self._handshake()
        self.request("initialize", {
            "clientInfo": {
                "name": "super_coder",
                "title": "super-coder session dispatcher",
                "version": "1",
            }
        })
        self.notify("initialized", {})
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(encode_frame(b"", opcode=8))
        except OSError:
            pass
        self.sock.close()
        self.sock = None

    def _handshake(self) -> None:
        assert self.sock is not None
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self.sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk or len(response) + len(chunk) > 65536:
                raise CodexProtocolError("invalid Codex WebSocket handshake")
            response.extend(chunk)
        header = bytes(response).split(b"\r\n\r\n", 1)[0]
        lines = header.split(b"\r\n")
        if not lines or b" 101 " not in lines[0] + b" ":
            raise CodexProtocolError("Codex app-server refused WebSocket upgrade")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest())
        if headers.get(b"sec-websocket-accept") != expected:
            raise CodexProtocolError("Codex WebSocket accept key did not match")

    def _send_json(self, payload: dict) -> None:
        if self.sock is None:
            raise CodexProtocolError("Codex app-server client is not connected")
        self.sock.sendall(encode_frame(
            json.dumps(payload, separators=(",", ":")).encode()
        ))

    def _recv_json(self) -> dict:
        if self.sock is None:
            raise CodexProtocolError("Codex app-server client is not connected")
        fragments: list[bytes] = []
        text_started = False
        while True:
            final, opcode, payload = decode_frame(self.sock)
            if opcode == 8:
                raise CodexProtocolError("Codex app-server closed the control socket")
            if opcode == 9:
                self.sock.sendall(encode_frame(payload, opcode=10))
                continue
            if opcode == 1:
                fragments = [payload]
                text_started = True
            elif opcode == 0 and text_started:
                fragments.append(payload)
            else:
                continue
            if not final:
                continue
            try:
                message = json.loads(b"".join(fragments))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CodexProtocolError("Codex app-server sent invalid JSON") from exc
            if not isinstance(message, dict):
                raise CodexProtocolError("Codex app-server message is not an object")
            return message

    def notify(self, method: str, params: dict) -> None:
        self._send_json({"method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self._send_json({"method": method, "id": request_id, "params": params})
        while True:
            message = self._recv_json()
            if "id" not in message:
                self.notifications.append(message)
                continue
            if message.get("id") != request_id:
                method_name = message.get("method") or "unknown"
                raise CodexProtocolError(
                    f"unsupported Codex server request while awaiting {method}: "
                    f"{method_name}"
                )
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CodexRpcError(f"Codex {method} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexProtocolError(f"Codex {method} returned no result object")
            return result

    def wait_notification(self, method: str, predicate: Callable[[dict], bool],
                          *, timeout: float = 3600.0) -> dict:
        if self.sock is None:
            raise CodexProtocolError("Codex app-server client is not connected")
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.notifications):
                if message.get("method") == method and predicate(message):
                    return self.notifications.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for Codex {method}")
            self.sock.settimeout(remaining)
            message = self._recv_json()
            if "id" in message:
                raise CodexProtocolError(
                    "unsupported Codex server request during wake turn: "
                    f"{message.get('method') or 'unknown'}"
                )
            if message.get("method") == method and predicate(message):
                return message
            self.notifications.append(message)
