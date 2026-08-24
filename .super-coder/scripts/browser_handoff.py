#!/usr/bin/env python3
"""Open a one-shot local redirect without placing a Web capability in argv."""
from __future__ import annotations

import argparse
import http.server
import os
import re
import secrets
import socketserver
import sys
import threading
from pathlib import Path


CAPABILITY = re.compile(
    r"http://127\.0\.0\.1:\d+/\?sc_generation=[0-9a-f]{64}\Z"
)


def _read_capability(path: Path) -> str:
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o777 != 0o600:
        raise ValueError("browser handoff capability file is unsafe")
    value = path.read_text().strip()
    if CAPABILITY.fullmatch(value) is None:
        raise ValueError("browser handoff capability is invalid")
    return value


def _write_ready(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(url + "\n")


def handoff(capability_file: Path, ready_file: Path, ttl: float) -> int:
    capability = _read_capability(capability_file)
    nonce = secrets.token_urlsafe(24)
    consumed = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != f"/handoff/{nonce}" or consumed.is_set():
                self.send_error(404)
                return
            consumed.set()
            try:
                capability_file.unlink()
            except FileNotFoundError:
                pass
            self.send_response(302)
            self.send_header("Location", capability)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = min(ttl, 0.25)
        _write_ready(ready_file, f"http://127.0.0.1:{server.server_address[1]}/handoff/{nonce}")
        remaining = ttl
        while remaining > 0 and not consumed.is_set():
            server.handle_request()
            remaining -= server.timeout
    try:
        capability_file.unlink()
    except FileNotFoundError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capability-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--ttl", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        return handoff(args.capability_file, args.ready_file, args.ttl)
    except (OSError, ValueError) as exc:
        print(f"browser handoff: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
