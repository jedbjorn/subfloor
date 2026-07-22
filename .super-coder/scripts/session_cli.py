#!/usr/bin/env python3
"""Session-control CLI: public operator commands plus adapter-only internals."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn


ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import ports as ports_mod  # noqa: E402

API_BASE = os.environ.get("SC_API_BASE", "") or (
    f"http://127.0.0.1:{ports_mod.resolve().get('port')}"
)
API_TOKEN = os.environ.get("SC_API_TOKEN", "")


def die(message: str) -> NoReturn:
    sys.exit(f"session-control: {message}")


def api(method: str, path: str, payload: dict | None = None,
        *, token_required: bool = True) -> dict:
    if not API_BASE or (token_required and not API_TOKEN):
        die("SC_API_BASE + SC_API_TOKEN are required; boot through ./sc enter")
    data = json.dumps(payload).encode() if payload is not None else None
    headers = ({"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {})
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        API_BASE.rstrip("/") + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", exc.reason)
        except Exception:
            error = exc.reason
        die(f"API {method} {path} -> HTTP {exc.code}: {error}")
    except Exception as exc:
        die(f"API unreachable ({API_BASE}): {exc}")


def binding_arg(binding_id: int | None) -> dict:
    return {"binding_id": binding_id} if binding_id is not None else {}


def print_status(payload: dict) -> None:
    binding = payload.get("binding")
    if not binding:
        print("session-control: no binding for this shell")
        return
    native = binding.get("native_session_id") or "pending"
    managed = "managed" if binding.get("managed") else "manual"
    archive = payload.get("archive") or {}
    summary = payload.get("summary") or {}
    queued = summary.get("queued", (payload.get("jobs") or {}).get("queued", 0))
    errors = summary.get("errors", (payload.get("jobs") or {}).get("failed", 0))
    engine_session = archive.get("session_id") or "pending"
    model = archive.get("model") or "harness default"
    owner = summary.get("owner") or "none"
    print(
        f"binding {binding['binding_id']} · engine={engine_session} · "
        f"{binding['harness']}={native} · model={model} · "
        f"{binding['state']} · {managed} · owner={owner} · "
        f"queued={queued} · errors={errors}"
    )
    if summary.get("last_delivery"):
        print(f"  last delivery: {summary['last_delivery']}")
    if binding.get("last_error"):
        print(f"  error: {binding['last_error']}")


def cmd_status(args) -> int:
    query = ("?binding=" + urllib.parse.quote(str(args.binding_id))) \
        if args.binding_id is not None else ""
    print_status(api("GET", "/_sc/session-control" + query))
    return 0


def cmd_action(args) -> int:
    result = api(
        "POST", f"/_sc/session-control/{args.action}", binding_arg(args.binding_id)
    )
    print_status(result)
    return 0


def cmd_bind(args) -> int:
    payload: dict = {}
    if args.native_id is not None:
        payload["native_session_id"] = args.native_id
    if args.endpoint is not None:
        payload["control_endpoint"] = args.endpoint
    if args.cli_version is not None:
        payload["cli_version"] = args.cli_version
    if args.state is not None:
        payload["state"] = args.state
    if args.capability:
        capabilities: dict[str, bool] = {}
        for item in args.capability:
            name, sep, raw = item.partition("=")
            if not sep or raw.lower() not in ("true", "false"):
                die("--capability must be name=true|false")
            capabilities[name] = raw.lower() == "true"
        payload["control_capabilities"] = capabilities
    if not payload:
        die("bind needs at least one update flag")
    result = api(
        "PATCH", f"/_sc/session-control/bindings/{args.binding_id}", payload
    )
    print_status(result)
    return 0


def cmd_channel(args) -> int:
    payload = {
        "binding_id": args.binding_id,
        "action": args.channel_action,
        "pid": args.pid,
    }
    if args.start_ticks is not None:
        payload["start_ticks"] = args.start_ticks
    result = api("POST", "/_sc/session-control/channel", payload)
    print(json.dumps(result, sort_keys=True))
    return 0


def operator_api(method: str, action: str, shortname: str,
                 payload: dict | None = None) -> dict:
    target = urllib.parse.quote(shortname, safe="")
    return api(
        method, f"/api/session-control/{action}/{target}", payload,
        token_required=False,
    )


def cmd_operator_status(args) -> int:
    if args.shortname:
        print_status(operator_api("GET", "status", args.shortname))
        return 0
    print_status(api("GET", "/_sc/session-control"))
    return 0


def cmd_operator_manage(args) -> int:
    print_status(operator_api(
        "POST", "manage", args.shortname, {"sprint_ref": args.sprint}
    ))
    return 0


def cmd_operator_action(args) -> int:
    if args.action == "release":
        while True:
            status = operator_api("GET", "status", args.shortname)
            if status.get("binding", {}).get("state") == "dispatching":
                if not args.after_turn:
                    die("binding is dispatching; pass --after-turn to wait for release")
                time.sleep(1)
                continue
            try:
                result = operator_api("POST", "release", args.shortname, {})
                break
            except SystemExit as exc:
                if not args.after_turn or "dispatching" not in str(exc):
                    raise
                time.sleep(1)
        print_status(result)
        return 0
    print_status(operator_api("POST", args.action, args.shortname, {}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sc session-control")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("binding_id", nargs="?", type=int)
    status.set_defaults(fn=cmd_status)

    for action in ("manage", "release", "retry"):
        command = sub.add_parser(action)
        command.add_argument("binding_id", nargs="?", type=int)
        command.set_defaults(fn=cmd_action, action=action)

    bind = sub.add_parser("bind")
    bind.add_argument("binding_id", type=int)
    bind.add_argument("--native-id")
    bind.add_argument("--endpoint")
    bind.add_argument("--cli-version")
    bind.add_argument("--state", choices=(
        "starting", "foreground", "idle", "dispatching", "dormant",
        "released", "error",
    ))
    bind.add_argument("--capability", action="append")
    bind.set_defaults(fn=cmd_bind)

    channel = sub.add_parser("channel")
    channel.add_argument("channel_action", choices=("register", "heartbeat", "clear"))
    channel.add_argument("binding_id", type=int)
    channel.add_argument("--pid", type=int, required=True)
    channel.add_argument("--start-ticks", type=int)
    channel.set_defaults(fn=cmd_channel)
    return parser


def build_operator_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sc session")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("shortname", nargs="?")
    status.set_defaults(fn=cmd_operator_status)

    manage = sub.add_parser("manage")
    manage.add_argument("shortname")
    manage.add_argument("--sprint", required=True)
    manage.set_defaults(fn=cmd_operator_manage)

    release = sub.add_parser("release")
    release.add_argument("shortname")
    release.add_argument("--after-turn", action="store_true")
    release.set_defaults(fn=cmd_operator_action, action="release")

    retry = sub.add_parser("retry")
    retry.add_argument("shortname")
    retry.set_defaults(fn=cmd_operator_action, action="retry", after_turn=False)
    return parser


def main(argv: list[str]) -> int:
    operator = bool(argv and argv[0] == "--operator")
    if operator:
        argv = argv[1:]
    args = (build_operator_parser() if operator else build_parser()).parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
