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


def _integer_list(values: list[int] | None) -> list[int]:
    return list(dict.fromkeys(values or ()))


def _post(path: str, payload: dict, *, idempotent: bool = False) -> dict:
    return mem._api("POST", path, payload, idempotent=idempotent)


def cmd_declare(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/declare",
        {
            "feature_id": args.feature,
            "planner_shell_id": args.planner_shell,
            "spec_approval_ids": _integer_list(args.spec_approval),
            "participants": _json_array(args.participants_file),
            "merge_grant_enabled": args.merge_grant,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_plan_unit(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/plan-unit",
        {
            "sprint_id": args.sprint,
            "assigned_shell_id": args.developer_shell,
            "reviewer_shell_id": args.reviewer_shell,
            "title": args.title,
            "expected_output": _text(args.expected_output_file, "expected output"),
            "task_ids": _integer_list(args.task),
            "planned_wave": args.wave,
            "dependency_ids": _integer_list(args.depends_on),
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_arm(args: argparse.Namespace) -> int:
    result = _post("/_sc/sprint/arm", {"sprint_id": args.sprint})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_register_pr(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/register-pr",
        {
            "sprint_id": args.sprint,
            "repository": args.repository,
            "pr_number": args.pr,
            "work_unit_ids": [args.work_unit],
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/pause",
        {"sprint_id": args.sprint, "reason": args.reason},
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/resume",
        {"sprint_id": args.sprint, "reason": args.reason},
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/complete",
        {
            "sprint_id": args.sprint,
            "reason": args.reason,
            "terminal_outcome": args.outcome,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/abort",
        {
            "sprint_id": args.sprint,
            "reason": args.reason,
            "terminal_outcome": args.outcome,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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
    result = mem._api("GET", f"/_sc/sprint/{args.sprint}/report?limit={args.limit}")
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

    declare = sub.add_parser(
        "declare", help="Planner creates one editable prepared Sprint envelope"
    )
    declare.add_argument("--feature", type=int, required=True)
    declare.add_argument(
        "--planner-shell",
        type=int,
        help="originating Planner; defaults to the authenticated caller",
    )
    declare.add_argument("--spec-approval", type=int, action="append", required=True)
    declare.add_argument("--participants-file", required=True)
    declare.add_argument("--merge-grant", action="store_true", required=True)
    declare.set_defaults(fn=cmd_declare)

    plan = sub.add_parser(
        "plan-unit", help="Planner groups existing spec tasks into one editing lane"
    )
    plan.add_argument("--sprint", type=int, required=True)
    plan.add_argument("--developer-shell", type=int, required=True)
    plan.add_argument("--reviewer-shell", type=int, required=True)
    plan.add_argument("--title", required=True)
    plan.add_argument("--expected-output-file", required=True)
    plan.add_argument("--task", type=int, action="append", required=True)
    plan.add_argument("--wave", type=int, default=0)
    plan.add_argument("--depends-on", type=int, action="append")
    plan.set_defaults(fn=cmd_plan_unit)

    arm = sub.add_parser("arm", help="Planner atomically arms an eligible plan")
    arm.add_argument("--sprint", type=int, required=True)
    arm.set_defaults(fn=cmd_arm)

    register = sub.add_parser(
        "register-pr", help="Developer registers one PR to its owning work unit"
    )
    register.add_argument("--sprint", type=int, required=True)
    register.add_argument("--repository", required=True)
    register.add_argument("--pr", type=int, required=True)
    register.add_argument("--work-unit", type=int, required=True)
    register.set_defaults(fn=cmd_register_pr)

    pause = sub.add_parser("pause", help="Participant or FnB pauses for integrity")
    pause.add_argument("--sprint", type=int, required=True)
    pause.add_argument("--reason", required=True)
    pause.set_defaults(fn=cmd_pause)

    resume = sub.add_parser("resume", help="Planner or FnB reconciles and re-arms")
    resume.add_argument("--sprint", type=int, required=True)
    resume.add_argument("--reason")
    resume.set_defaults(fn=cmd_resume)

    complete = sub.add_parser("complete", help="Planner or FnB closes successfully")
    complete.add_argument("--sprint", type=int, required=True)
    complete.add_argument("--reason", required=True)
    complete.add_argument("--outcome", required=True)
    complete.set_defaults(fn=cmd_complete)

    abort = sub.add_parser(
        "abort", help="Planner or FnB stops without deleting history"
    )
    abort.add_argument("--sprint", type=int, required=True)
    abort.add_argument("--reason", required=True)
    abort.add_argument("--outcome", default="aborted")
    abort.set_defaults(fn=cmd_abort)

    request = sub.add_parser(
        "request-review", help="Developer hands a green PR to Review"
    )
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
