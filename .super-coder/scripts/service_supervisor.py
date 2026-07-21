#!/usr/bin/env python3
"""Supervise the review API and session dispatcher as one launch service."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import ports  # noqa: E402


def commands(port: int) -> tuple[list[str], list[str]]:
    python = os.environ.get("SC_PYTHON", sys.executable)
    api = [python, str(ENGINE / "api" / "server.py"), "--port", str(port)]
    dispatcher = [
        python, str(ENGINE / "scripts" / "session_dispatcher.py"),
        "--api-base", f"http://127.0.0.1:{port}",
    ]
    return api, dispatcher


def terminate(child: subprocess.Popen | None, sig: int = signal.SIGTERM) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def wait_or_kill(child: subprocess.Popen | None, timeout: float = 5.0) -> None:
    if child is None:
        return
    try:
        child.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        terminate(child, signal.SIGKILL)
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def supervise(port: int, *, popen=subprocess.Popen) -> int:
    api_command, dispatcher_command = commands(port)
    stopping = False
    forwarded_signal: int | None = None
    api: subprocess.Popen | None = None
    dispatcher: subprocess.Popen | None = None
    previous: dict[signal.Signals, object] = {}

    def forward(signum, _frame) -> None:
        nonlocal stopping, forwarded_signal
        stopping = True
        forwarded_signal = forwarded_signal or signum
        terminate(dispatcher, signum)
        terminate(api, signum)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, forward)
        api = popen(api_command, cwd=str(Path.cwd()), start_new_session=True)
        dispatcher = popen(
            dispatcher_command, cwd=str(Path.cwd()), start_new_session=True
        )
        while not stopping:
            api_code = api.poll()
            if api_code is not None:
                stopping = True
                terminate(dispatcher)
                return api_code
            dispatcher_code = dispatcher.poll()
            if dispatcher_code is not None:
                # The API is the root service.  A dispatcher crash must not take
                # the UI/memory API down; restart the failed child in place.
                print(
                    f"service supervisor: session dispatcher exited "
                    f"{dispatcher_code}; restarting in 1s",
                    file=sys.stderr, flush=True,
                )
                deadline = time.monotonic() + 1
                while not stopping and time.monotonic() < deadline:
                    time.sleep(min(0.1, deadline - time.monotonic()))
                if stopping:
                    break
                dispatcher = popen(
                    dispatcher_command, cwd=str(Path.cwd()), start_new_session=True
                )
            time.sleep(0.1)
        return 128 + forwarded_signal if forwarded_signal else 0
    finally:
        terminate(dispatcher)
        terminate(api)
        wait_or_kill(dispatcher)
        wait_or_kill(api)
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sc serve")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    return supervise(args.port or int(ports.resolve().get("port", 8800)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
