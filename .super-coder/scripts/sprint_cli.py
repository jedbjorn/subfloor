#!/usr/bin/env python3
"""Authenticated shell-facing commands for the Sprints v2 loop."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mem

PAYLOAD_FILE_HELP = "text file; hard maximum 8,000 characters"
FINDINGS_FILE_HELP = (
    "JSON array; each finding body has a hard maximum of 8,000 characters"
)


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
            "spec_document_ids": _integer_list(args.spec),
            "spec_approval_ids": _integer_list(args.spec_approval),
            "participants": _json_array(args.participants_file),
            "merge_grant_enabled": args.merge_grant,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_record_qaqc(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/qaqc",
        {
            "document_id": args.document,
            "verdict": args.verdict,
            "findings_document_id": args.findings_document,
        },
        idempotent=True,
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
            "output_kind": args.output_kind.replace("-", "_"),
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_replan_unit(args: argparse.Namespace) -> int:
    payload = {
        "sprint_id": args.sprint,
        "work_unit_id": args.work_unit,
    }
    optional = {
        "assigned_shell_id": args.developer_shell,
        "reviewer_shell_id": args.reviewer_shell,
        "title": args.title,
        "expected_output": (
            _text(args.expected_output_file, "expected output")
            if args.expected_output_file
            else None
        ),
        "task_ids": _integer_list(args.task) if args.task is not None else None,
        "planned_wave": args.wave,
        "output_kind": (
            args.output_kind.replace("-", "_") if args.output_kind else None
        ),
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if args.clear_dependencies:
        payload["dependency_ids"] = []
    elif args.depends_on is not None:
        payload["dependency_ids"] = _integer_list(args.depends_on)
    result = _post("/_sc/sprint/replan-unit", payload, idempotent=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_recall_unit(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/recall-unit",
        {
            "sprint_id": args.sprint,
            "work_unit_id": args.work_unit,
            "reason": args.reason,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_reroute_participant(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/reroute-participant",
        {
            "sprint_id": args.sprint,
            "participant_shell_id": args.participant_shell,
            "harness": args.harness,
            "model": args.model,
            "effort": args.effort,
            "route": args.route,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    result = mem._api("GET", f"/_sc/sprint/{args.sprint}/inbox")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_spec_revision(args: argparse.Namespace) -> int:
    result = mem._api(
        "GET", f"/_sc/sprint/spec-revisions/{args.sprint}/{args.document}"
    )
    if args.body_only:
        sys.stdout.write(result["body"])
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/send",
        {
            "sprint_id": args.sprint,
            "to": args.to,
            "body": _text(args.body_file, "message body"),
            "idempotency_key": args.key,
            "intent": args.intent,
            "requires_reply": args.requires_reply,
            "work_unit_id": args.work_unit,
            "sprint_level": args.sprint_level,
            "reply_to_message_id": args.reply_to,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/inbox-read",
        {"sprint_id": args.sprint, "message_id": args.message},
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_decline(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/inbox-decline",
        {
            "sprint_id": args.sprint,
            "message_id": args.message,
            "reason": args.reason,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_complete_unit(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/complete-unit",
        {
            "sprint_id": args.sprint,
            "work_unit_id": args.work_unit,
            "result": _text(args.result_file, "completion result"),
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_cancel_unit(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/cancel-unit",
        {
            "sprint_id": args.sprint,
            "work_unit_id": args.work_unit,
            "reason": args.reason,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_resolve_unit(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/resolve-unit",
        {
            "sprint_id": args.sprint,
            "work_unit_id": args.work_unit,
            "target": args.to,
            "reason": args.reason,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_arm(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/arm",
        {
            "sprint_id": args.sprint,
            "conformance_reviewer_shell_id": args.conformance_reviewer_shell,
        },
    )
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


def cmd_reconcile_pr(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/reconcile-pr",
        {
            "sprint_id": args.sprint,
            "repository": args.repository,
            "pr_number": args.pr,
            "work_unit_id": args.work_unit,
            "reason": args.reason,
        },
        idempotent=True,
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
        {
            "sprint_id": args.sprint,
            "reason": args.reason,
            "conformance_reviewer_shell_id": args.conformance_reviewer_shell,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    if bool(args.report_file) != bool(args.key):
        raise SystemExit("sprint: --report-file and --key must be provided together")
    result = _post(
        "/_sc/sprint/complete",
        {
            "sprint_id": args.sprint,
            "reason": args.reason,
            "terminal_outcome": args.outcome,
            "final_report": (
                _text(args.report_file, "final report") if args.report_file else None
            ),
            "idempotency_key": args.key,
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
    payload = {
        "sprint_id": args.sprint,
        "registered_pr_id": args.registered_pr,
        "idempotency_key": args.key,
    }
    if args.intent is not None:
        payload["intent"] = args.intent
    else:
        payload["readiness"] = _text(args.readiness_file, "readiness")
    result = _post(
        "/_sc/sprint/review-request",
        payload,
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


def cmd_watcher_state(args: argparse.Namespace) -> int:
    result = mem._api("GET", f"/_sc/sprint/watcher-state?sprint_id={args.sprint}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_cleanup_status(args: argparse.Namespace) -> int:
    result = mem._api("GET", f"/_sc/sprint/cleanup-runs/{args.sprint}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/cleanup-runs",
        {
            "sprint_id": args.sprint,
            "idempotency_key": args.key,
            "adopt_legacy": args.adopt_legacy,
        },
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
            "final_report": _text(args.final_report_file, "final report body"),
            "reason": args.reason,
            "terminal_outcome": args.outcome,
            "idempotency_key": args.key,
        },
        idempotent=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_disposition_followup(args: argparse.Namespace) -> int:
    result = _post(
        "/_sc/sprint/followup-disposition",
        {
            "sprint_id": args.sprint,
            "followup_id": args.followup,
            "disposition": args.disposition,
            "resolution": (
                _text(args.resolution_file, "resolution")
                if args.resolution_file
                else None
            ),
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

    qaqc = sub.add_parser(
        "record-qaqc", help="Review shell signs the current exact spec revision"
    )
    qaqc.add_argument("--document", type=int, required=True)
    qaqc.add_argument("--verdict", choices=("pass", "fail"), required=True)
    qaqc.add_argument("--findings-document", type=int)
    qaqc.set_defaults(fn=cmd_record_qaqc)

    declare = sub.add_parser(
        "declare", help="Planner creates one editable prepared Sprint envelope"
    )
    declare.add_argument("--feature", type=int, required=True)
    declare.add_argument(
        "--planner-shell",
        type=int,
        help="originating Planner; defaults to the authenticated caller",
    )
    declare.add_argument(
        "--spec",
        type=int,
        action="append",
        help="current governing spec document; repeat for multiple specs",
    )
    declare.add_argument(
        "--spec-approval",
        type=int,
        action="append",
        help="deprecated: resolve the reviewed document and retain evidence",
    )
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
    plan.add_argument(
        "--output-kind",
        choices=("code", "report-only", "no-code"),
        default="code",
    )
    plan.set_defaults(fn=cmd_plan_unit)

    replan = sub.add_parser(
        "replan-unit", help="Planner revises any fields on one planned lane"
    )
    replan.add_argument("--sprint", type=int, required=True)
    replan.add_argument("--work-unit", type=int, required=True)
    replan.add_argument("--developer-shell", type=int)
    replan.add_argument("--reviewer-shell", type=int)
    replan.add_argument("--title")
    replan.add_argument("--expected-output-file")
    replan.add_argument("--task", type=int, action="append")
    replan.add_argument("--wave", type=int)
    dependencies = replan.add_mutually_exclusive_group()
    dependencies.add_argument("--depends-on", type=int, action="append")
    dependencies.add_argument("--clear-dependencies", action="store_true")
    replan.add_argument(
        "--output-kind", choices=("code", "report-only", "no-code")
    )
    replan.set_defaults(fn=cmd_replan_unit)

    recall = sub.add_parser(
        "recall-unit",
        help="Planner returns one paused unmerged lane to the editable plan",
    )
    recall.add_argument("--sprint", type=int, required=True)
    recall.add_argument("--work-unit", type=int, required=True)
    recall.add_argument("--reason", required=True)
    recall.set_defaults(fn=cmd_recall_unit)

    reroute = sub.add_parser(
        "reroute-participant",
        help="Planner changes a Developer or Reviewer route for future wakes",
    )
    reroute.add_argument("--sprint", type=int, required=True)
    reroute.add_argument("--participant-shell", type=int, required=True)
    reroute.add_argument("--harness", required=True)
    reroute.add_argument("--model")
    reroute.add_argument("--effort")
    reroute.add_argument("--route")
    reroute.set_defaults(fn=cmd_reroute_participant)

    arm = sub.add_parser("arm", help="Planner atomically arms an eligible plan")
    arm.add_argument("--sprint", type=int, required=True)
    arm.add_argument(
        "--conformance-reviewer-shell", type=int, required=True
    )
    arm.set_defaults(fn=cmd_arm)

    inbox = sub.add_parser("inbox", help="Read unread messages addressed to this shell")
    inbox.add_argument("--sprint", type=int, required=True)
    inbox.set_defaults(fn=cmd_inbox)

    spec_revision = sub.add_parser(
        "spec-revision", help="Read one immutable governing Sprint revision"
    )
    spec_revision.add_argument("--sprint", type=int, required=True)
    spec_revision.add_argument("--document", type=int, required=True)
    spec_revision.add_argument(
        "--body-only", action="store_true", help="write the exact body only"
    )
    spec_revision.set_defaults(fn=cmd_spec_revision)

    send = sub.add_parser(
        "send", help="Send one typed relay to another Sprint participant"
    )
    send.add_argument("--sprint", type=int, required=True)
    send.add_argument("--to", required=True, help="recipient shell shortname")
    send.add_argument("--body-file", required=True, help=PAYLOAD_FILE_HELP)
    send.add_argument(
        "--intent",
        choices=("information", "handoff", "question", "blocker", "decision"),
        default="information",
    )
    send.add_argument("--requires-reply", action="store_true")
    scope = send.add_mutually_exclusive_group()
    scope.add_argument("--work-unit", type=int)
    scope.add_argument("--sprint-level", action="store_true")
    send.add_argument("--reply-to", type=int)
    send.add_argument(
        "--key",
        required=True,
        help=(
            "stable retry key; reuse it only for the same recipient, body, intent, "
            "reply linkage, and scope"
        ),
    )
    send.set_defaults(fn=cmd_send)

    accept = sub.add_parser(
        "accept", help="Mark one Sprint message read and accept actionable work"
    )
    accept.add_argument("--sprint", type=int, required=True)
    accept.add_argument("--message", type=int, required=True)
    accept.set_defaults(fn=cmd_accept)

    decline = sub.add_parser(
        "decline", help="Decline one actionable Sprint message with a reason"
    )
    decline.add_argument("--sprint", type=int, required=True)
    decline.add_argument("--message", type=int, required=True)
    decline.add_argument("--reason", required=True)
    decline.set_defaults(fn=cmd_decline)

    complete_unit = sub.add_parser(
        "complete-unit", help="Developer completes an explicit report-only/no-code lane"
    )
    complete_unit.add_argument("--sprint", type=int, required=True)
    complete_unit.add_argument("--work-unit", type=int, required=True)
    complete_unit.add_argument(
        "--result-file", required=True, help=PAYLOAD_FILE_HELP
    )
    complete_unit.set_defaults(fn=cmd_complete_unit)

    cancel_unit = sub.add_parser(
        "cancel-unit", help="Planner cancels one unreleased planned lane"
    )
    cancel_unit.add_argument("--sprint", type=int, required=True)
    cancel_unit.add_argument("--work-unit", type=int, required=True)
    cancel_unit.add_argument("--reason", required=True)
    cancel_unit.set_defaults(fn=cmd_cancel_unit)

    resolve_unit = sub.add_parser(
        "resolve-unit",
        help="Planner force-moves one non-terminal lane to a terminal "
        "disposition while the Sprint is paused",
    )
    resolve_unit.add_argument("--sprint", type=int, required=True)
    resolve_unit.add_argument("--work-unit", type=int, required=True)
    resolve_unit.add_argument(
        "--to", choices=("completed", "cancelled"), required=True
    )
    resolve_unit.add_argument("--reason", required=True)
    resolve_unit.set_defaults(fn=cmd_resolve_unit)

    register = sub.add_parser(
        "register-pr", help="Developer registers one PR to its owning work unit"
    )
    register.add_argument("--sprint", type=int, required=True)
    register.add_argument("--repository", required=True)
    register.add_argument("--pr", type=int, required=True)
    register.add_argument("--work-unit", type=int, required=True)
    register.set_defaults(fn=cmd_register_pr)

    reconcile = sub.add_parser(
        "reconcile-pr",
        help="Planner repairs PR ownership inherited from an aborted Sprint",
    )
    reconcile.add_argument("--sprint", type=int, required=True)
    reconcile.add_argument("--repository", required=True)
    reconcile.add_argument("--pr", type=int, required=True)
    reconcile.add_argument("--work-unit", type=int, required=True)
    reconcile.add_argument("--reason", required=True)
    reconcile.set_defaults(fn=cmd_reconcile_pr)

    pause = sub.add_parser("pause", help="Participant or FnB pauses for integrity")
    pause.add_argument("--sprint", type=int, required=True)
    pause.add_argument("--reason", required=True)
    pause.set_defaults(fn=cmd_pause)

    resume = sub.add_parser("resume", help="Planner or FnB reconciles and re-arms")
    resume.add_argument("--sprint", type=int, required=True)
    resume.add_argument("--reason")
    resume.add_argument("--conformance-reviewer-shell", type=int)
    resume.set_defaults(fn=cmd_resume)

    complete = sub.add_parser("complete", help="Planner or FnB closes successfully")
    complete.add_argument("--sprint", type=int, required=True)
    complete.add_argument("--reason", required=True)
    complete.add_argument("--outcome", required=True)
    complete.add_argument("--report-file", help=PAYLOAD_FILE_HELP)
    complete.add_argument("--key", help="stable final-report retry identity")
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
    review_source = request.add_mutually_exclusive_group(required=True)
    review_source.add_argument(
        "--intent",
        choices=("submit", "resubmit"),
        help="Developer judgment; the engine injects the exact review locator",
    )
    review_source.add_argument(
        "--readiness-file",
        help=f"legacy compatibility only; content is not forwarded; {PAYLOAD_FILE_HELP}",
    )
    request.add_argument("--key", required=True, help="stable retry identity")
    request.set_defaults(fn=cmd_request_review)

    record = sub.add_parser("record-review", help="Reviewer records an outcome")
    record.add_argument("--sprint", type=int, required=True)
    record.add_argument("--registered-pr", type=int, required=True)
    record.add_argument(
        "--verdict", required=True, choices=("changes_requested", "approved")
    )
    record.add_argument("--body-file", required=True, help=PAYLOAD_FILE_HELP)
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

    watcher_state = sub.add_parser(
        "watcher-state", help="Read bounded durable PR-watcher evidence"
    )
    watcher_state.add_argument("--sprint", type=int, required=True)
    watcher_state.set_defaults(fn=cmd_watcher_state)

    cleanup_status = sub.add_parser(
        "cleanup-status", help="Read bounded successful-Sprint cleanup evidence"
    )
    cleanup_status.add_argument("--sprint", type=int, required=True)
    cleanup_status.set_defaults(fn=cmd_cleanup_status)

    cleanup = sub.add_parser(
        "cleanup", help="Retry failed cleanup or explicitly adopt one legacy Sprint"
    )
    cleanup.add_argument("--sprint", type=int, required=True)
    cleanup.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="FnB only: derive targets for one completed pre-scheduling Sprint",
    )
    cleanup.add_argument("--key", required=True, help="stable retry identity")
    cleanup.set_defaults(fn=cmd_cleanup)

    conformance = sub.add_parser(
        "record-conformance", help="Reviewer records a report and follow-ups"
    )
    conformance.add_argument("--sprint", type=int, required=True)
    conformance.add_argument(
        "--body-file", required=True, help=PAYLOAD_FILE_HELP
    )
    conformance.add_argument(
        "--findings-file", required=True, help=FINDINGS_FILE_HELP
    )
    conformance.add_argument(
        "--final-report-file", required=True, help=PAYLOAD_FILE_HELP
    )
    conformance.add_argument("--reason", required=True)
    conformance.add_argument("--outcome", required=True)
    conformance.add_argument("--key", required=True, help="stable retry identity")
    conformance.set_defaults(fn=cmd_record_conformance)

    followup = sub.add_parser(
        "disposition-followup", help="FnB records a terminal follow-up disposition"
    )
    followup.add_argument("--sprint", type=int, required=True)
    followup.add_argument("--followup", type=int, required=True)
    followup.add_argument(
        "--disposition",
        choices=("accepted", "resolved", "dismissed"),
        required=True,
    )
    followup.add_argument("--resolution-file", help=PAYLOAD_FILE_HELP)
    followup.set_defaults(fn=cmd_disposition_followup)

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
