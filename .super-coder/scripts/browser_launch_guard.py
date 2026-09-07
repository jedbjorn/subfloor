#!/usr/bin/env python3
"""Exec Chromium only when the operator's named profile is already active."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


class GuardRefusal(ValueError):
    pass


def check(config: dict, *, proc: Path = Path("/proc")) -> None:
    """A lock alone can be stale: verify its host, live owner and executable."""
    try:
        directory = Path(config["user_data_dir"])
        lock = directory / "SingletonLock"
        host, raw_pid = os.readlink(lock).rsplit("-", 1)
        if host != socket.gethostname() or not raw_pid.isdecimal():
            raise GuardRefusal("invalid Chromium SingletonLock")
        process = proc / raw_pid
        if process.stat().st_uid != os.geteuid():
            raise GuardRefusal("Chromium lock belongs to another user")
        if (process / "exe").resolve(strict=True) != Path(config["executable"]).resolve(
            strict=True
        ):
            raise GuardRefusal("stale Chromium SingletonLock: executable mismatch")
        state = json.loads((directory / "Local State").read_text())
        active = state["profile"]["last_active_profiles"]
        if not isinstance(active, list) or not all(isinstance(x, str) for x in active):
            raise GuardRefusal("malformed Chromium active-profile list")
        if config["profile_dir_name"] not in active:
            raise GuardRefusal("Subfloor profile is not active")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, GuardRefusal)
            else "missing, malformed, or stale profile state"
        )
        raise GuardRefusal(
            f"extension not connected: {reason}; ask the FnB to open Subfloor"
        ) from exc


def main(argv: list[str]) -> int:
    try:
        config = json.loads(Path(os.environ["SC_BROWSER_GUARD_CONFIG"]).read_text())
        check(config)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    os.execv(config["executable"], [config["executable"], *argv])
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
