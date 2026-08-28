#!/usr/bin/env python3
"""Resolve this fork's localhost ports — deterministic per repo, never fixed.

A super-coder fork runs *inside* a host repo that often has its own dev server,
and several forks may run at once. So the port can't be hardcoded. Each fork
derives a stable per-repo offset from its absolute path (sha1 % 100) and lands
in a distinctive band well clear of the neighbors:

    port = 8800 + offset          (Subfloor API + review UI)

The substrate's own server (JSON API + static review UI) runs on `port`. A
second `dev_port` is derived in the 8800 band for a *project* dev server.
`./sc launch` publishes both on host loopback, and the
shell binds its dev server to 0.0.0.0:dev_port so the host browser can reach it.
Both are persisted to `.super-coder/instance.json` (gitignored — per-instance,
local, like the `.db`) so they stay stable across restarts and can be
hand-overridden. At resolve time an occupied port is bumped to the next free
offset (and dev_port is kept distinct from port), so a hash clash or busy port
self-heals once.

Usage:
    python3 .super-coder/scripts/ports.py show     # print resolved ports (JSON)
    python3 .super-coder/scripts/ports.py ensure    # resolve + persist instance.json
    python3 .super-coder/scripts/ports.py port      # bare serve port
    python3 .super-coder/scripts/ports.py devport   # bare dev-server port
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


def _resolve_offset(
    base: int, avoid: set[int], *, port_base: int = PORT_BASE
) -> int:
    """Walk forward from a base offset to the first free port not in `avoid`."""
    for i in range(SPAN):
        port = port_base + (base + i) % SPAN
        if port not in avoid and _free(port):
            return port
    raise RuntimeError(
        f"no free super-coder port in {port_base}-{port_base + SPAN - 1}"
    )


def _dev_offset(port: int, avoid: set[int] | None = None) -> int:
    """Derive a dev port from a distinct seed, kept free and != the serve port."""
    return _resolve_offset(
        _offset(str(REPO_ROOT) + ":dev"),
        avoid=set(avoid or ()) | {port},
    )


def _sibling_ports() -> set[int]:
    """Read both assigned ports from every sibling fork we can discover.

    Installed forks conventionally share one parent (for example ``~/Repos``).
    Their gitignored instance configs are the durable host-local registry.  A
    malformed or unrelated sibling is ignored; this fork's own config is
    excluded.
    """
    occupied: set[int] = set()
    try:
        children = REPO_ROOT.parent.iterdir()
    except OSError:
        return occupied
    for child in children:
        candidate = child / ".super-coder" / "instance.json"
        if candidate == CONFIG or not candidate.is_file():
            continue
        try:
            sibling = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("port", "dev_port"):
            value = sibling.get(key)
            if isinstance(value, int):
                occupied.add(value)
    return occupied


def _derive(avoid: set[int] | None = None) -> dict:
    """Derive serve + dev ports from the repo path. Each is bumped past any
    occupied port so it lands free, and dev_port is kept distinct from port."""
    occupied = set(avoid or ())
    port = _resolve_offset(_offset(str(REPO_ROOT)), avoid=occupied)
    dev_port = _dev_offset(port, occupied)
    return {
        "repo": REPO_ROOT.name,
        "port": port,
        "dev_port": dev_port,
        "harness": "claude",
    }


def resolve(persist: bool = False) -> dict:
    """Return this fork's config. An existing instance.json wins (respects hand
    edits) and is returned verbatim; otherwise derive a free port from the repo
    path. `persist=True` writes it to instance.json so it stays stable. To force
    a re-derive (e.g. after a port clash), delete the file."""
    cfg = None
    occupied = _sibling_ports()
    if CONFIG.exists():
        try:
            loaded = json.loads(CONFIG.read_text())
            if "port" in loaded:
                cfg = loaded
        except json.JSONDecodeError:
            pass
    if cfg is None:
        cfg = _derive(occupied)
    else:
        # Existing configs remain stable unless they collide with another
        # fork's API *or* dev assignment. A bound copy of our own port is not a
        # reason to move it: resolve() commonly runs while this fork is live.
        port = cfg["port"]
        dev_port = cfg.get("dev_port")
        if port in occupied:
            base = port - PORT_BASE if PORT_BASE <= port < PORT_BASE + SPAN \
                else _offset(str(REPO_ROOT))
            port = _resolve_offset(
                base, occupied | ({dev_port} if isinstance(dev_port, int) else set())
            )
            cfg["port"] = port
        if not isinstance(dev_port, int) or dev_port == port or dev_port in occupied:
            cfg["dev_port"] = _dev_offset(port, occupied)
    if "dev_port" not in cfg:
        # Instance predates dev_port — backfill without disturbing the serve port
        # (respects hand-edits to `port`); persisted below if requested.
        cfg["dev_port"] = _dev_offset(cfg["port"], occupied)
    cfg.pop("deepseek_host_port", None)
    if persist:
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def save(cfg: dict) -> None:
    """Persist the full config dict to instance.json verbatim. Preserves any
    keys this module doesn't own (e.g. the `vm` block written by vm.py)."""
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "show"
    if mode == "ensure":
        print(json.dumps(resolve(persist=True)))
    elif mode == "show":
        print(json.dumps(resolve(persist=False)))
    elif mode == "port":          # bare integer, for shell/sc use
        print(resolve(persist=False)["port"])
    elif mode == "devport":       # bare dev-server port, for shell/sc use
        print(resolve(persist=False)["dev_port"])
    elif mode == "name":          # pm2 process name — unique per fork
        print("sc-" + resolve(persist=False).get("repo", "fork"))
    else:
        sys.exit(
            "usage: ports.py "
            "[show|ensure|port|devport|name]"
        )
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
