#!/usr/bin/env python3
"""sc sprint — planner-side sprint workflow client (spec #20 Event Ingress,
sprint 25 seq 8 task #84; wake ops seq 10 task #86).

    ./sc sprint action begin     --message <id> --operation <op> --target <t>
    ./sc sprint action complete  <receipt_id> [--detail "…"]
    ./sc sprint action unknown   <receipt_id> [--detail "…"]
    ./sc sprint action reconcile <receipt_id> [--detail "…"]
    ./sc sprint unit add   --sprint <doc-id> --seq U1 --title "…" [roles/fields]
    ./sc sprint unit set   --sprint <doc-id> --seq U1 [roles/fields]
    ./sc sprint unit state --sprint <doc-id> --seq U1 <state>
    ./sc sprint unit list  [--sprint <doc-id>]
    ./sc sprint board      --sprint <doc-id>
    ./sc sprint status  [--sprint <doc-id>] [--all]
    ./sc sprint alerts  [--all]
    ./sc sprint retry   --binding <id> [--outcome delivered|not_delivered]

The BOARD is a record, not a markdown table a planner edits by hand (spec
doc 58). The planner is its only writer — devs and reviewers work and
message, and the planner moves the board as their messages arrive; a worker
that could mark its own unit done would make the board agree with reality by
construction and leave the reconciler nothing to compare. Workers read it
freely (`unit list` / `board`); their writes are refused per sprint.

`unit state` is a separate verb from `unit set` on purpose: state is the only
column role expectation derives from, so it moves in its own call and
`state_changed_at` has exactly one writer. The API refuses a state move that
carries other edits — the separation is a property, not a convention.

`board` RENDERS the unit table from the record and prints it. It does not
write it back into the document: the document keeps its prose, the record is
the source, and a stored copy would state the board twice.

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


# ── the board as a record (spec doc 58 U1): unit add/set/state/list, board ──

_STATES = ("pending", "working", "in_review", "blocked", "merged",
           "cancelled")


def _role(value: "str | None"):
    """A --dev/--reviewer argument: a shortname, or the literal 'none' to
    clear the slot. Absent (None) leaves the slot untouched — the API
    distinguishes an omitted field from an explicit null, so clearing a role
    must be said out loud."""
    return None if value is not None and value.lower() == "none" else value


def _field(value: "str | None"):
    return None if value is not None and value.lower() == "none" else value


def _unit_body(args, *, creating: bool) -> dict:
    body = {"sprint_doc_id": args.sprint, "seq": args.seq}
    if creating or args.title is not None:
        body["unit_title"] = args.title
    for name in ("dev", "reviewer"):
        if getattr(args, name) is not None:
            body[name] = _role(getattr(args, name))
    if args.depends_on is not None:
        body["depends_on"] = _field(args.depends_on)
    if args.overlap is not None:
        body["overlap"] = _field(args.overlap)
    if args.branch is not None:
        body["branch"] = _field(args.branch)
    if args.pr is not None:
        body["pr_number"] = None if args.pr < 0 else args.pr
    return body


def _depends_cell(u: dict) -> str:
    """The board's "depends on" cell — the machine-readable dependency and the
    human-readable merge-surface annotation share it, exactly as the markdown
    board writes it today ("— · owns migration 0098; schema.sql"). Two columns,
    one cell: the annotation is how a dev knows whether its head can stand or
    must rebase, so dropping it from the render would lose what the record was
    widened to keep."""
    dep = u.get("depends_on") or "—"
    return f"{dep} · {u['overlap']}" if u.get("overlap") else dep


def _print_unit(u: dict) -> None:
    roles = (f"dev={u.get('dev_shortname') or '—'} "
             f"rev={u.get('reviewer_shortname') or '—'}")
    extra = " ".join(
        p for p in (
            f"depends={_depends_cell(u)}"
            if (u.get("depends_on") or u.get("overlap")) else "",
            f"branch={u['branch']}" if u.get("branch") else "",
            f"pr=#{u['pr_number']}" if u.get("pr_number") else "")
        if p)
    print(f"{u['seq']:<5} [{u['state']}] {u['unit_title']} · {roles}"
          + (f" · {extra}" if extra else ""))


def cmd_unit_add(args) -> int:
    body = _unit_body(args, creating=True)
    if args.state:
        body["state"] = args.state
    r = _api("POST", "/api/sprint-units", body,
             f"unit-add|{args.sprint}|{args.seq}")
    print(f"sc sprint: unit {r['seq']} declared on sprint "
          f"{r['sprint_doc_id']}")
    _print_unit(r)
    return 0


def cmd_unit_set(args) -> int:
    body = _unit_body(args, creating=False)
    if len(body) == 2:
        raise _die("nothing to set — pass at least one of --title, --dev, "
                   "--reviewer, --depends-on, --branch, --pr "
                   "(state moves with `unit state`)")
    r = _api("PATCH", "/api/sprint-units", body,
             f"unit-set|{args.sprint}|{args.seq}|{uuid.uuid4()}")
    _print_unit(r)
    return 0


def cmd_unit_state(args) -> int:
    r = _api("PATCH", "/api/sprint-units",
             {"sprint_doc_id": args.sprint, "seq": args.seq,
              "state": args.state},
             f"unit-state|{args.sprint}|{args.seq}|{args.state}|"
             f"{uuid.uuid4()}")
    _print_unit(r)
    return 0


def _units(sprint: "int | None") -> list:
    path = "/api/sprint-units"
    if sprint is not None:
        path += f"?sprint_doc_id={sprint}"
    return _api("GET", path).get("units", [])


def cmd_unit_list(args) -> int:
    units = _units(args.sprint)
    if not units:
        print("sc sprint: no units on the board"
              + (f" for sprint {args.sprint}" if args.sprint else ""))
        return 0
    for u in units:
        _print_unit(u)
    return 0


def cmd_board(args) -> int:
    """Render the board from the record. A VIEW — printed, never stored back
    into the document body."""
    units = _units(args.sprint)
    if not units:
        print(f"sc sprint: sprint {args.sprint} has no units on record")
        return 0
    print("| seq | unit | dev | reviewer | depends on | branch | pr | state |")
    print("|---|---|---|---|---|---|---|---|")
    for u in units:
        print("| {seq} | {title} | {dev} | {rev} | {dep} | {branch} | {pr} "
              "| {state} |".format(
                  seq=u["seq"], title=u["unit_title"],
                  dev=u.get("dev_shortname") or "—",
                  rev=u.get("reviewer_shortname") or "—",
                  dep=_depends_cell(u),
                  branch=u.get("branch") or "—",
                  pr=f"#{u['pr_number']}" if u.get("pr_number") else "—",
                  state=u["state"]))
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
    un = sub.add_parser("unit", help="the sprint board record — declare, "
                                     "reassign, and move units (planner "
                                     "writes; anyone reads)")
    usub = un.add_subparsers(dest="unit_cmd", required=True)

    def _roles_and_fields(sp, *, creating: bool):
        sp.add_argument("--sprint", type=int, required=True,
                        help="the sprint document id")
        sp.add_argument("--seq", required=True,
                        help="the unit, as the board names it (e.g. U1)")
        sp.add_argument("--title", required=creating, default=None,
                        help="the unit's one-line name")
        for role, who in (("dev", "builds"), ("reviewer", "reviews")):
            sp.add_argument(f"--{role}", default=None,
                            help=f"shortname of the shell that {who} this "
                                 "unit ('none' to clear the slot)")
        sp.add_argument("--depends-on", dest="depends_on", default=None,
                        help="units this one waits on, e.g. U1,U3 "
                             "('none' to clear)")
        sp.add_argument("--overlap", default=None,
                        help="the merge-surface annotation sharing the "
                             "'depends on' cell, e.g. 'shares schema.sql with "
                             "U5 — MUST rebase onto merged U2' "
                             "('none' to clear)")
        sp.add_argument("--branch", default=None, help="('none' to clear)")
        sp.add_argument("--pr", type=int, default=None,
                        help="PR number (-1 to clear)")

    ua = usub.add_parser("add", help="declare a unit on the board (never an "
                                     "upsert — an existing seq is a 409)")
    _roles_and_fields(ua, creating=True)
    ua.add_argument("--state", choices=_STATES, default=None,
                    help="initial state (default pending)")
    us = usub.add_parser("set", help="edit a unit's roles/fields — NOT its "
                                     "state (see `unit state`)")
    _roles_and_fields(us, creating=False)
    ust = usub.add_parser("state", help="move one unit's state; takes no "
                                        "other edits, so state_changed_at "
                                        "has exactly one writer")
    ust.add_argument("--sprint", type=int, required=True)
    ust.add_argument("--seq", required=True)
    ust.add_argument("state", choices=_STATES)
    ul = usub.add_parser("list", help="the board's units, one line each "
                                      "(read-only)")
    ul.add_argument("--sprint", type=int, default=None)

    bd = sub.add_parser("board", help="render the unit table from the record "
                                      "(a VIEW — never written back into the "
                                      "document)")
    bd.add_argument("--sprint", type=int, required=True)

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
    if args.cmd == "unit":
        return {"add": cmd_unit_add, "set": cmd_unit_set,
                "state": cmd_unit_state, "list": cmd_unit_list}[
                    args.unit_cmd](args)
    if args.cmd == "board":
        return cmd_board(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "alerts":
        return cmd_alerts(args)
    return cmd_retry(args)


if __name__ == "__main__":
    raise SystemExit(main())
