#!/usr/bin/env python3
"""Resolve this fork's localhost ports — deterministic per repo, never fixed.

A super-coder fork runs *inside* a host repo that often has its own dev server,
and several forks may run at once. So the port can't be hardcoded. Each fork
derives a stable per-repo offset from its absolute path (sha1 % 100) and lands
in a distinctive band well clear of the neighbors:

    port = 8800 + offset     (away from superCC 8000 / dos-arch 8001, and the
                              common host-app ports 3000 / 5173 / 8080)

v1 runs a single server that serves both the JSON API and the static review UI
on this one port — one port per fork, not two. The resolved port is persisted to
`.super-coder/instance.json` (gitignored — per-instance, local, like the `.db`)
so it stays stable across restarts and can be hand-overridden. At resolve time an
occupied port is bumped to the next free offset and persisted, so a hash clash or
busy port self-heals once.

Usage:
    python3 .super-coder/scripts/ports.py show     # print resolved ports (JSON)
    python3 .super-coder/scripts/ports.py ensure    # resolve + persist instance.json
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
CONFIG = ENGINE / "instance.json"

PORT_BASE = 8800
SPAN = 100  # offsets 0..99 → port 8800-8899


def _offset(seed: str) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest(), 16) % SPAN


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _derive() -> dict:
    """Derive a fresh port from the repo path, bumping the offset past any
    occupied port so it lands free."""
    base = _offset(str(REPO_ROOT))
    for i in range(SPAN):
        port = PORT_BASE + (base + i) % SPAN
        if _free(port):
            break
    else:  # pragma: no cover — all 100 offsets busy is implausible
        port = PORT_BASE + base
    return {"repo": REPO_ROOT.name, "port": port, "harness": "claude"}


def resolve(persist: bool = False) -> dict:
    """Return this fork's config. An existing instance.json wins (respects hand
    edits) and is returned verbatim; otherwise derive a free port from the repo
    path. `persist=True` writes it to instance.json so it stays stable. To force
    a re-derive (e.g. after a port clash), delete the file."""
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
            if "port" in cfg:
                return cfg
        except json.JSONDecodeError:
            pass
    cfg = _derive()
    if persist:
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "show"
    if mode == "ensure":
        print(json.dumps(resolve(persist=True)))
    elif mode == "show":
        print(json.dumps(resolve(persist=False)))
    elif mode == "port":          # bare integer, for shell/sc use
        print(resolve(persist=False)["port"])
    elif mode == "name":          # pm2 process name — unique per fork
        print("sc-" + resolve(persist=False).get("repo", "fork"))
    else:
        sys.exit("usage: ports.py [show|ensure|port|name]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
