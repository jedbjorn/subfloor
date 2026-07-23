#!/usr/bin/env python3
"""sc sprint — planner-side sprint workflow client (spec #20 Event Ingress,
sprint 25 seq 8 task #84; wake ops seq 10 task #86).

    ./sc sprint action begin     --message <id> --operation <op> --target <t>
    ./sc sprint action complete  <receipt_id> [--detail "…"]
    ./sc sprint action unknown   <receipt_id> [--detail "…"]
    ./sc sprint action reconcile <receipt_id> [--detail "…"]
    ./sc sprint status  [--sprint <doc-id>] [--all]
    ./sc sprint alerts  [--all]
    ./sc sprint retry   --binding <id> [--outcome delivered|not_delivered]

Before a planner performs an engine-owned or external side effect for a
message it records action INTENT (begin) under a key derived from
message + operation + target; a completed existing receipt suppresses the
duplicate. After the side effect it records the observed result
(complete), parks it (unknown — the wake item reconciles instead of
requeuing blind), and an operator later resolves the park (reconcile).
Only then is the message marked read. Informational messages need no
receipt.

status / alerts are the read-only wake ops surfaces: binding armed/released,
sprint doc ACTIVE/frozen, batch state, park/quarantine reason, last wake
outcome, and the open wake alerts (session-loss, retry-exhausted,
quarantine, unmanaged-writer). retry is the operator recovery path for a
PARKED/stalled batch: the parked batch is NEVER resubmitted — it resolves
as audit, its items requeue, and the coordinator forms a NEW batch that
re-gates everything before a byte moves. A parked input needs the
operator's explicit --outcome verdict. The CLI is a pure API client (shell
token); it never touches the DB directly.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
import uuid

SC_API_BASE = os.environ.get("SC_API_BASE", "http://127.0.0.1:8800")
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
_TIMEOUT = 10


def _die(msg: str) -> "SystemExit":
    return SystemExit(f"sc sprint: {msg}")


def _api(method: str, path: str, payload: "dict | None" = None,
         idem_key: "str | None" = None) -> dict:
    if not SC_API_TOKEN:
        raise _die("SC_API_TOKEN unset — this shell has no API credential")
    headers = {"Authorization": f"Bearer {SC_API_TOKEN}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    req = urllib.request.Request(
        SC_API_BASE.rstrip("/") + path, data=data, method=method,
        headers=headers)
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


# ── wake ops (seq 10): status / alerts / retry ──────────────────────────────

def _fmt_counts(counts: dict) -> str:
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "—"


def cmd_status(args) -> int:
    q = []
    if args.sprint is not None:
        q.append(f"sprint_doc_id={args.sprint}")
    if args.all:
        q.append("include_released=1")
    path = "/api/interface/sprint-bindings" + ("?" + "&".join(q) if q else "")
    r = _api("GET", path)
    bindings = r.get("bindings", [])
    if not bindings:
        print("sc sprint: no bindings (arm one before the sprint — "
              "POST /api/interface/sprint-bindings)")
        return 0
    for b in bindings:
        doc = b.get("sprint") or {}
        doc_state = ("ACTIVE" if doc.get("active") else "not-ACTIVE") \
            + ("+frozen" if doc.get("frozen") else "")
        state = "released" if b.get("released_at") else "armed"
        print(f"binding #{b['binding_id']} {state} · sprint "
              f"#{b['sprint_doc_id']} ({doc.get('title') or '?'}) {doc_state}"
              f" · planner shell {b['planner_shell_id']} · session "
              f"{b['session_id']} gen {b['generation']}")
        print(f"  wake: {b['wake_state']} · items "
              f"{_fmt_counts(b.get('items') or {})}")
        cur = b.get("current_batch")
        if cur:
            print(f"  batch: #{cur['batch_id']} {cur['state']} "
                  f"(formed {cur['created_at']})")
        last = b.get("last_batch")
        if last:
            print(f"  last outcome: batch #{last['batch_id']} {last['state']}"
                  f" at {last.get('completed_at') or '—'} · "
                  f"{_fmt_counts(last.get('items') or {})}")
        park = b.get("park")
        if park:
            print(f"  PARKED: {park.get('reason') or 'delivery_unknown'}"
                  + (" · input park — retry needs --outcome"
                     if park.get("input_park") else ""))
        for qi in b.get("quarantined") or []:
            print(f"  quarantined: item #{qi['item_id']} msg "
                  f"#{qi['message_id']} after {qi['completed_wakes']} wakes"
                  + (f" — {qi['error']}" if qi.get("error") else ""))
        if b.get("released_at"):
            print(f"  released {b['released_at']} — "
                  f"{b.get('release_reason') or '—'}")
        retry = b.get("retry") or {}
        if retry.get("applicable"):
            print(f"  → recovery: ./sc sprint retry --binding "
                  f"{b['binding_id']}"
                  + (" --outcome delivered|not_delivered"
                     if retry.get("needs_outcome") else ""))
    return 0


def cmd_alerts(args) -> int:
    path = "/api/interface/sprint-alerts"
    if args.all:
        path += "?include_resolved=1"
    r = _api("GET", path)
    alerts = r.get("alerts", [])
    if not alerts:
        print("sc sprint: no open alerts")
        return 0
    for a in alerts:
        state = "resolved " + (a["resolved_at"] or "") if a.get(
            "resolved_at") else "OPEN"
        refs = " ".join(f"{k}#{a[k]}" for k in
                        ("session_id", "binding_id", "message_id", "watch_id")
                        if a.get(k) is not None)
        print(f"[{a['severity']}] {a['reason']} · {state} · "
              f"opened {a['opened_at']}" + (f" · {refs}" if refs else ""))
    return 0


def cmd_retry(args) -> int:
    payload = {}
    if args.outcome:
        payload["outcome"] = args.outcome
    r = _api("POST", f"/api/interface/sprint-bindings/{args.binding}/retry",
             payload, f"retry|{args.binding}|{uuid.uuid4()}")
    print(f"sc sprint: binding #{r['binding_id']} retried — "
          f"wake now {r['wake_state']}")
    for a in r.get("actions", []):
        print(f"  {a}")
    print("  the coordinator re-gates from live state — the parked batch is "
          "never resubmitted; a NEW batch forms through the broker-owned "
          "writer")
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
    st = sub.add_parser("status", help="wake status: binding, batch, park, "
                                       "last outcome (read-only)")
    st.add_argument("--sprint", type=int, default=None,
                    help="filter to one sprint doc id")
    st.add_argument("--all", action="store_true",
                    help="include released bindings")
    al = sub.add_parser("alerts", help="open wake alerts (read-only)")
    al.add_argument("--all", action="store_true",
                    help="include resolved alerts (audit history)")
    rt = sub.add_parser("retry", help="operator recovery for a parked/stalled "
                                      "batch — NEVER resubmits the park; "
                                      "requeues as a NEW gated batch")
    rt.add_argument("--binding", type=int, required=True)
    rt.add_argument("--outcome", choices=("delivered", "not_delivered"),
                    default=None,
                    help="required when the session's input is parked: did "
                         "the parked frame reach the planner?")
    args = p.parse_args(argv)
    if args.cmd == "action":
        if args.action_cmd == "begin":
            return cmd_begin(args)
        return _cmd_update(args, args._state)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "alerts":
        return cmd_alerts(args)
    return cmd_retry(args)


if __name__ == "__main__":
    raise SystemExit(main())
