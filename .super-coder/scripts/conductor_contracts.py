#!/usr/bin/env python3
"""Read and emit Conductor Step 4 contract rows through the engine API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SC_API_BASE = os.environ.get("SC_API_BASE", "http://127.0.0.1:8800")
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
_TIMEOUT = 10


def _die(msg: str) -> "SystemExit":
    return SystemExit(f"sc conductor: {msg}")


def _api(method: str, path: str, payload=None):
    headers = {}
    if SC_API_TOKEN:
        headers["Authorization"] = f"Bearer {SC_API_TOKEN}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        SC_API_BASE.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            obj = json.loads(exc.read())
            err = obj.get("error", {})
            message = err.get("message", str(err))
        except Exception:  # noqa: BLE001
            message = exc.reason
        raise _die(f"{method} {path} → HTTP {exc.code}: {message}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _die(f"API unreachable ({getattr(exc, 'reason', exc)})")


def _query(path: str, values: dict) -> str:
    clean = {k: v for k, v in values.items() if v is not None}
    return path + (f"?{urllib.parse.urlencode(clean)}" if clean else "")


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _directive_list(args) -> int:
    result = _api("GET", _query("/api/directives", {
        "status": args.status,
        "kind": args.kind,
        "sprint_doc_id": args.sprint,
        "limit": args.limit,
    }))
    for item in result["directives"]:
        print(
            f"{item['directive_id']:>5} [{item['status']}] "
            f"{item['issuer_flavor']}:{item['kind']} → {item['target']} "
            f"sprint={item['sprint_doc_id'] or '—'} "
            f"unit={item['unit_id'] or '—'}"
        )
    return 0


def _directive_inspect(args) -> int:
    _dump(_api("GET", f"/api/directives/{args.directive_id}"))
    return 0


def _directive_emit(args) -> int:
    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        raise _die(f"--payload is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        raise _die("--payload must decode to a JSON object")
    body = {"kind": args.kind, "target": args.target, "payload": payload}
    if args.sprint is not None:
        body["sprint_doc_id"] = args.sprint
    if args.unit is not None:
        body["unit_id"] = args.unit
    item = _api("POST", "/api/directives", body)
    print(f"sc directives: emitted {item['directive_id']} "
          f"{item['issuer_flavor']}:{item['kind']} → {item['target']}")
    return 0


def _directive_act(args) -> int:
    result = _api("POST", f"/api/directives/{args.directive_id}/act", {})
    _dump(result)
    return 0 if result["status"] in ("executed", "refused") else 2


def _event_list(args) -> int:
    result = _api("GET", _query("/api/sentinel-events", {
        "event_kind": args.kind,
        "sprint_doc_id": args.sprint,
        "unit_id": args.unit,
        "limit": args.limit,
    }))
    for item in result["events"]:
        print(
            f"{item['event_id']:>5} {item['event_kind']} "
            f"shell={item['shell_id'] or '—'} "
            f"sprint={item['sprint_doc_id'] or '—'} "
            f"unit={item['unit_id'] or '—'} "
            f"at={item['observed_at']}"
        )
    return 0


def _event_inspect(args) -> int:
    _dump(_api("GET", f"/api/sentinel-events/{args.event_id}"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="./sc")
    surfaces = parser.add_subparsers(dest="surface", required=True)

    directives = surfaces.add_parser(
        "directives", help="emit and read Conductor directives")
    dsub = directives.add_subparsers(dest="verb", required=True)
    dl = dsub.add_parser("list")
    dl.add_argument("--status", choices=("pending", "executed", "refused"))
    dl.add_argument("--kind")
    dl.add_argument("--sprint", type=int)
    dl.add_argument("--limit", type=int, default=50)
    di = dsub.add_parser("inspect")
    di.add_argument("directive_id", type=int)
    de = dsub.add_parser("emit")
    de.add_argument("kind")
    de.add_argument("--target", required=True)
    de.add_argument("--payload", default="{}")
    de.add_argument("--sprint", type=int)
    de.add_argument("--unit", type=int)
    da = dsub.add_parser(
        "act", help="execute/refuse one pending row as the Conductor")
    da.add_argument("directive_id", type=int)

    events = surfaces.add_parser(
        "events", help="read append-only sentinel observations")
    esub = events.add_subparsers(dest="verb", required=True)
    el = esub.add_parser("list")
    el.add_argument("--kind")
    el.add_argument("--sprint", type=int)
    el.add_argument("--unit", type=int)
    el.add_argument("--limit", type=int, default=50)
    ei = esub.add_parser("inspect")
    ei.add_argument("event_id", type=int)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        ("directives", "list"): _directive_list,
        ("directives", "inspect"): _directive_inspect,
        ("directives", "emit"): _directive_emit,
        ("directives", "act"): _directive_act,
        ("events", "list"): _event_list,
        ("events", "inspect"): _event_inspect,
    }
    return handlers[(args.surface, args.verb)](args)


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
