#!/usr/bin/env python3
"""CLI attach client for the interface-stream spike.

usage: cli_client.py <session-id> [--role viewer|writer] [--takeover]
                     [--server http://127.0.0.1:18777] [--token TOKEN]

Mints a single-use ticket via the HTTP API (operator bearer), connects the
session-stream WebSocket, puts stdin in raw mode, forwards stdin bytes as
0x01 input frames with a monotonic seq, writes 0x00/0x04 payloads to stdout,
sends 0x03 resize frames on SIGWINCH, heartbeats as a writer, and exits
cleanly on socket close.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty
import urllib.request

from websockets.sync.client import connect


def api(server: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        server + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def winsize() -> tuple[int, int]:
    rows, cols, _, _ = struct.unpack("HHHH", __import__("fcntl").ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
    return rows, cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--role", choices=["viewer", "writer"], default="viewer")
    ap.add_argument("--takeover", action="store_true")
    ap.add_argument("--server", default=os.environ.get("SPIKE_SERVER", "http://127.0.0.1:18777"))
    ap.add_argument("--token", default=os.environ.get("SPIKE_TOKEN", "spike"))
    args = ap.parse_args()

    lease_token = None
    if args.role == "writer":
        try:
            lease = api(args.server, args.token, "POST", "/api/interface/writer-leases",
                        {"session_id": args.session_id, "takeover": args.takeover})
            lease_token = lease["lease_token"]
        except urllib.error.HTTPError as exc:
            print(f"writer lease failed: {exc.code} {exc.read().decode()}", file=sys.stderr)
            return 1
    ticket = api(args.server, args.token, "POST", "/api/interface/stream-tickets",
                 {"session_id": args.session_id, "role": args.role,
                  "lease_token": lease_token})["ticket"]

    ws_url = (args.server.replace("http", "ws", 1)
              + f"/api/interface/session-streams/{args.session_id}?ticket={ticket}")
    ws = connect(ws_url, subprotocols=["sc-term.v1"])

    old_attrs = termios.tcgetattr(0) if os.isatty(0) else None
    if old_attrs:
        tty.setraw(0)
    state = {"seq": 1, "winch": True, "dead": False}
    signal.signal(signal.SIGWINCH, lambda *_: state.__setitem__("winch", True))

    def sender() -> None:
        try:
            while not state["dead"]:
                if state["winch"]:
                    state["winch"] = False
                    rows, cols = winsize()
                    ws.send(b"\x03" + rows.to_bytes(2, "big") + cols.to_bytes(2, "big"))
                r, _, _ = select.select([sys.stdin.buffer], [], [], 0.2)
                if not r:
                    continue
                data = os.read(0, 65536)
                if not data:
                    state["dead"] = True
                    return
                if args.role == "writer":
                    ws.send(b"\x01" + state["seq"].to_bytes(8, "big") + data)
                    state["seq"] += 1
        except Exception:
            state["dead"] = True

    def heartbeater() -> None:
        while not state["dead"]:
            time.sleep(20)
            try:
                ws.send(json.dumps({"type": "heartbeat"}))
            except Exception:
                return

    threads = [threading.Thread(target=sender, daemon=True)]
    if args.role == "writer":
        threads.append(threading.Thread(target=heartbeater, daemon=True))
    for t in threads:
        t.start()

    out = sys.stdout.buffer
    try:
        for message in ws:
            if isinstance(message, bytes):
                if message[:1] in (b"\x00", b"\x04"):
                    out.write(message[1:])
                    out.flush()
            else:
                msg = json.loads(message)
                if msg.get("type") == "error" and msg.get("code") == "terminated":
                    break
                print(f"\x1b[2m[{message}]\x1b[0m", file=sys.stderr)
    except Exception as exc:
        print(f"connection closed: {exc!r}", file=sys.stderr)
    finally:
        state["dead"] = True
        if old_attrs:
            termios.tcsetattr(0, termios.TCSADRAIN, old_attrs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
