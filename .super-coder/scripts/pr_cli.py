#!/usr/bin/env python3
"""Authenticated shell-facing PR subscription commands."""
from __future__ import annotations

import argparse
import json
import sys

import mem


def cmd_subscribe(args: argparse.Namespace) -> int:
    result = mem._api(
        "POST",
        "/_sc/pr/subscribe",
        {"repository": args.repository, "pr_number": args.pr},
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="./sc pr")
    commands = parser.add_subparsers(dest="command", required=True)
    subscribe = commands.add_parser(
        "subscribe", help="subscribe this Developer shell to one PR"
    )
    subscribe.add_argument("--repository", required=True, help="owner/name")
    subscribe.add_argument("--pr", required=True, type=int, help="PR number")
    subscribe.set_defaults(fn=cmd_subscribe)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
