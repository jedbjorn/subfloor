#!/usr/bin/env python3
"""./sc runtime — which runtime this installation's lifecycle verbs drive.

Two runtimes exist. `sandbox` (the default) runs the review server inside the
docker container that `./sc launch` builds and starts; shells enter it with
`docker exec`. `host` runs the same review server as a supervised host process
(nohup + pidfile under .super-coder/run/) and boots shells directly on the
host — no docker daemon, image, or container anywhere in the lifecycle.

The selection is one `instance.json` key, `runtime`, written by
`./sc install --runtime host` or `./sc runtime host` and read by the
dispatcher (`launch`, `enter`, `down`, `restart`, `logs`, `build`,
`update-harnesses`, `harness-status`), by `./sc doctor`, and by `./sc update`,
which stops and restarts whichever runtime is live around DB maintenance. An
absent key means `sandbox`, so every existing install keeps its behavior.

Usage:
    ./sc runtime                 # print the selected runtime
    ./sc runtime host|sandbox    # select one (takes effect on the next launch)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
INSTANCE = ENGINE / "instance.json"

SANDBOX = "sandbox"
HOST = "host"
MODES = (SANDBOX, HOST)
KEY = "runtime"


class RuntimeError_(ValueError):
    """An unsupported runtime selection."""


def validate(mode: str) -> str:
    value = (mode or "").strip().lower()
    if value not in MODES:
        raise RuntimeError_(
            f"unsupported runtime {mode!r} — choose one of: {', '.join(MODES)}"
        )
    return value


def read_mode(config_path: Path = INSTANCE) -> str:
    """The selected runtime; `sandbox` when unset, missing, or unreadable.

    Unreadable config never escalates here: the dispatcher asks this question
    before it decides which lifecycle to run, and the sandbox path is the one
    every install had before the key existed.
    """
    try:
        payload = json.loads(Path(config_path).read_text())
    except (OSError, json.JSONDecodeError):
        return SANDBOX
    if not isinstance(payload, dict):
        return SANDBOX
    raw = payload.get(KEY)
    if not isinstance(raw, str):
        return SANDBOX
    try:
        return validate(raw)
    except RuntimeError_:
        return SANDBOX


def is_host(config_path: Path = INSTANCE) -> bool:
    return read_mode(config_path) == HOST


def write_mode(mode: str, config_path: Path = INSTANCE) -> str:
    """Persist the selection through the instance-state merge (locked, atomic)."""
    import instance_state  # noqa: PLC0415 — keep `get` importable on minimal floors

    value = validate(mode)
    instance_state.merge_instance_config(Path(config_path), {KEY: value})
    return value


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if cmd == "get":
        # Bare value on stdout for the dispatcher — never decorated.
        print(read_mode())
        return 0
    if cmd == "show":
        mode = read_mode()
        print(f"runtime: {mode}")
        if mode == HOST:
            print("  review server runs as a supervised host process; shells boot on the host")
        else:
            print("  review server runs in the docker sandbox; shells enter the container")
        print("  switch: ./sc runtime host | ./sc runtime sandbox   (then ./sc launch)")
        return 0
    if cmd == "set":
        if len(argv) < 2:
            sys.exit("runtime: set needs a mode (host · sandbox)")
        cmd = argv[1]
    try:
        value = validate(cmd)
    except RuntimeError_ as exc:
        sys.exit(f"runtime: {exc}")
    current = read_mode()
    if not INSTANCE.exists():
        sys.exit(
            f"runtime: {INSTANCE} does not exist — run ./sc install "
            f"(--runtime {value}) first"
        )
    try:
        write_mode(value)
    except Exception as exc:  # instance_state raises its own typed errors
        sys.exit(f"runtime: could not record the selection: {exc}")
    if current == value:
        print(f"runtime: already {value}")
    else:
        print(f"runtime: {current} → {value}")
        print("  takes effect on the next ./sc launch; services started by the")
        print(f"  previous runtime are still running — stop them first (./sc runtime {current}; ./sc down)")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
