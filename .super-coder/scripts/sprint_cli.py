#!/usr/bin/env python3
"""Authenticated shell-facing commands for the Sprints v2 loop."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mem


def _text(path: str, name: str) -> str:
    if path == "-":
        value = sys.stdin.read()
    else:
        try:
            value = Path(path).read_text()
        except OSError as exc:
            raise SystemExit(f"sprint: cannot read {name} file {path}: {exc}") from exc
    value = value.strip()
    if not value:
        raise SystemExit(f"sprint: {name} is empty")
    return value


def _json_array(path: str) -> list[dict]:
    try:
        value = json.loads(_text(path, "findings"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"sprint: findings file is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SystemExit("sprint: findings file must contain a JSON array of objects")
    return value


def _post(path: str, payload: dict, *, idempotent: bool = False) -> dict:
    return mem._api("POST", path, payload, idempotent=idempotent)


def cmd_request_review(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/review-request",
        {
            "sprint_id": args.sprint,
            "registered_pr_id": args.registered_pr,
            "readiness": _text(args.readiness_file, "readiness"),
            "idempotency_key": args.key,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_record_review(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/review-record",
        {
            "sprint_id": args.sprint,
            "registered_pr_id": args.registered_pr,
            "verdict": args.verdict,
            "body": _text(args.body_file, "review body"),
            "idempotency_key": args.key,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_authorize_merge(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/merge-authorize",
        {"sprint_id": args.sprint, "registered_pr_id": args.registered_pr},
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/dispatch",
        {"sprint_id": args.sprint},
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/monitor",
        {"sprint_id": args.sprint},
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_record_conformance(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/conformance",
        {
            "sprint_id": args.sprint,
            "body": _text(args.body_file, "conformance body"),
            "findings": _json_array(args.findings_file),
            "idempotency_key": args.key,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_compile_report(args: argparse.Namespace) -> int:
    result = mem._api(
        "GET", f"/_sc/sprint/{args.sprint}/report?limit={args.limit}"
    )
    print(json.dumps(result["evidence_packet"], indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sc sprint",
        description=(
            "Authenticated Sprints v2 actions; caller identity is resolved from "
            "the launched shell's API wiring."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    request = sub.add_parser("request-review", help="Developer hands a green PR to Review")
    request.add_argument("--sprint", type=int, required=True)
    request.add_argument("--registered-pr", type=int, required=True)
    request.add_argument("--readiness-file", required=True)
    request.add_argument("--key", required=True, help="stable retry identity")
    request.set_defaults(fn=cmd_request_review)

    record = sub.add_parser("record-review", help="Reviewer records an outcome")
    record.add_argument("--sprint", type=int, required=True)
    record.add_argument("--registered-pr", type=int, required=True)
    record.add_argument(
        "--verdict", required=True, choices=("changes_requested", "approved")
    )
    record.add_argument("--body-file", required=True)
    record.add_argument("--key", required=True, help="stable retry identity")
    record.set_defaults(fn=cmd_record_review)

    authorize = sub.add_parser(
        "authorize-merge", help="Developer proves live green + approved head"
    )
    authorize.add_argument("--sprint", type=int, required=True)
    authorize.add_argument("--registered-pr", type=int, required=True)
    authorize.set_defaults(fn=cmd_authorize_merge)

    dispatch = sub.add_parser("dispatch", help="Planner releases every ready lane")
    dispatch.add_argument("--sprint", type=int, required=True)
    dispatch.set_defaults(fn=cmd_dispatch)

    monitor = sub.add_parser("monitor", help="Planner evaluates due liveness evidence")
    monitor.add_argument("--sprint", type=int, required=True)
    monitor.set_defaults(fn=cmd_monitor)

    conformance = sub.add_parser(
        "record-conformance", help="Reviewer records a report and follow-ups"
    )
    conformance.add_argument("--sprint", type=int, required=True)
    conformance.add_argument("--body-file", required=True)
    conformance.add_argument("--findings-file", required=True)
    conformance.add_argument("--key", required=True, help="stable retry identity")
    conformance.set_defaults(fn=cmd_record_conformance)

    report = sub.add_parser(
        "compile-report", help="Planner prints the bounded evidence packet"
    )
    report.add_argument("--sprint", type=int, required=True)
    report.add_argument(
        "--limit",
        type=int,
        default=50,
        choices=range(1, 201),
        metavar="1..200",
    )
    report.set_defaults(fn=cmd_compile_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    mem._PROG = "sprint"
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    mem._require_api()
    return args.fn(args)


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
