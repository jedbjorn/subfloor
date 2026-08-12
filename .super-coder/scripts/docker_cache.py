#!/usr/bin/env python3
"""Explicit host-level garbage collection for unused Docker build cache."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence

DEFAULT_UNTIL = "168h"
DURATION = re.compile(r"\A[1-9][0-9]*[smh]\Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sc docker-cache-gc",
        description=(
            "Remove unused host-global Docker build cache. The default keeps "
            "cache used within the last seven days."
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all",
        action="store_true",
        help="remove all unused build cache, regardless of age",
    )
    scope.add_argument(
        "--until",
        metavar="DURATION",
        default=DEFAULT_UNTIL,
        help="remove unused cache older than DURATION (default: 168h)",
    )
    return parser


def _duration(value: str) -> str:
    if not DURATION.fullmatch(value):
        raise ValueError("duration must be a positive integer followed by s, m, or h")
    return value


def main(
    argv: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    args = _parser().parse_args(list(argv))
    command = ["docker", "builder", "prune", "--all", "--force"]
    if args.all:
        print("→ Docker build-cache GC: all unused host-global cache")
    else:
        try:
            until = _duration(args.until)
        except ValueError as exc:
            _parser().error(str(exc))
        command.extend(("--filter", f"until={until}"))
        print(f"→ Docker build-cache GC: unused host-global cache older than {until}")
    try:
        completed = runner(command, check=False, text=True)
    except OSError as exc:
        print(f"docker-cache-gc: cannot run docker: {exc}", file=sys.stderr)
        return 1
    if completed.returncode:
        print(
            f"docker-cache-gc: Docker failed with status {completed.returncode}",
            file=sys.stderr,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
