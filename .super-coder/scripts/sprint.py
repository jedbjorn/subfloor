#!/usr/bin/env python3
"""sc sprint — planner-side sprint workflow client (spec #20 Event Ingress,
sprint 25 seq 8, task #84).

    ./sc sprint action begin     --message <id> --operation <op> --target <t>
    ./sc sprint action complete  <receipt_id> [--detail "…"]
    ./sc sprint action unknown   <receipt_id> [--detail "…"]
    ./sc sprint action reconcile <receipt_id> [--detail "…"]

Before a planner performs an engine-owned or external side effect for a
message it records action INTENT (begin) under a key derived from
message + operation + target; a completed existing receipt suppresses the
duplicate. After the side effect it records the observed result
(complete), parks it (unknown — the wake item reconciles instead of
requeuing blind), and an operator later resolves the park (reconcile).
Only then is the message marked read. Informational messages need no
receipt. The CLI is a pure API client (shell token); it never touches the
DB directly.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

SC_API_BASE = os.environ.get("SC_API_BASE", "http://127.0.0.1:8800")
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
_TIMEOUT = 10


def _die(msg: str) -> "SystemExit":
    return SystemExit(f"sc sprint: {msg}")


def _api(method: str, path: str, payload: dict, idem_key: str) -> dict:
    if not SC_API_TOKEN:
        raise _die("SC_API_TOKEN unset — this shell has no API credential")
    req = urllib.request.Request(
        SC_API_BASE.rstrip("/") + path,
        data=json.dumps(payload).encode(), method=method,
        headers={"Authorization": f"Bearer {SC_API_TOKEN}",
                 "Content-Type": "application/json",
                 "Idempotency-Key": idem_key})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
            msg = err.get("message", e.reason) if isinstance(err, dict) \
                else str(err)
        except Exception:  # noqa: BLE001
            msg = e.reason
        raise _die(f"{method} {path} → HTTP {e.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _die(f"API unreachable ({getattr(e, 'reason', e)}) — "
                   "the engine server must be up; the write may NOT have "
                   "landed. Check `sc sprint action begin` again with the "
                   "same key before acting — a completed receipt suppresses "
                   "the duplicate.")


def cmd_begin(args) -> int:
    idem_key = f"action|{args.message or '-'}|{args.operation}|{args.target}"
    r = _api("POST", "/api/planner-action-receipts",
             {"message_id": args.message, "operation": args.operation,
              "target": args.target}, idem_key)
    if r.get("duplicate"):
        state = r.get("state")
        note = ("SUPPRESSED — a completed receipt already covers this "
                "action; do NOT perform it again" if r.get("suppressed")
                else f"existing receipt in state {state}")
        print(f"sc sprint: receipt #{r['receipt_id']} ({state}) — {note}")
        return 0 if r.get("suppressed") else 1
    print(f"sc sprint: receipt #{r['receipt_id']} intent recorded "
          f"({r['idem_key']}) — perform the action, then record the result")
    return 0


def _cmd_update(args, state: str) -> int:
    r = _api("PATCH", f"/api/planner-action-receipts/{args.receipt_id}",
             {"state": state, "result_detail": args.detail},
             f"action-update|{args.receipt_id}|{state}")
    print(f"sc sprint: receipt #{r['receipt_id']} → {r['state']}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(prog="sc sprint",
                                description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    act = sub.add_parser("action", help="idempotent planner action receipts")
    asub = act.add_subparsers(dest="action_cmd", required=True)
    b = asub.add_parser("begin", help="record action intent before a side effect")
    b.add_argument("--message", type=int, default=None,
                   help="the sprint message this action answers")
    b.add_argument("--operation", required=True)
    b.add_argument("--target", required=True)
    for name, state in (("complete", "complete"), ("unknown", "unknown"),
                        ("reconcile", "reconciled")):
        sp = asub.add_parser(name, help=f"record the action as {state}")
        sp.add_argument("receipt_id", type=int)
        sp.add_argument("--detail", default=None)
        sp.set_defaults(_state=state)
    args = p.parse_args(argv)
    if args.action_cmd == "begin":
        return cmd_begin(args)
    return _cmd_update(args, args._state)


if __name__ == "__main__":
    raise SystemExit(main())
