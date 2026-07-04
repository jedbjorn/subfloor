#!/usr/bin/env python3
"""sc mem — the engine memory surface, over the API. No direct DB. No fallback.

A shell reads and writes its own identity/memory through the engine HTTP API
(`/_sc/mem/*`) and nothing else. There is no `sqlite3` path here: a raw query on
a fork is a foot-gun (the engine DB and a product DB share table names, and
0-byte stub files get silently created into real tables), and a silent direct-DB
fallback let a shell believe a write went through the API when it hadn't. So
every command calls the API and only the API — if the API isn't wired, it fails
loud.

Identity is the token, both ways:
  • run.py injects `SC_API_TOKEN` (the shell's api_key) + `SC_API_BASE` at boot;
  • the server middleware resolves that token → shell_id and scopes every
    operation to it.
So the client passes NO identity — no `--shell`, no DB path. The shell cannot
act as another shell (identity isn't a spoofable argument; it's the secret
token). The one place a *recipient* is named is `message send <to>` — that
addresses someone else's inbox; the sender is always the token.

Run from the repo root, like every engine command:

    ./sc mem which                                 # confirm API reachability + who your token resolves to
    ./sc mem get <surface>           [--json]      # read: state|seed|lns|decisions|flags|roadmap|narrative|messages
    ./sc mem get decisions [<id>|--all]            # default: active index (no rationale); <id> = full row; --all incl. superseded
    ./sc mem state "<text>"
    ./sc mem seed  "<body>"          [--date YYYY-MM-DD] [--tag cc]
    ./sc mem lns   "<body>"          [--date …] [--tag …]
    ./sc mem retire <entry_id>
    ./sc mem decision "<decision>"   [--rationale "…"] [--date …] [--parent ID] [--feature ID] [--doc ID]
    ./sc mem flag open  "<description>" [--name CC-001] [--priority Medium] [--feature ID]
    ./sc mem flag close <flag_id>    [--notes "…"]
    ./sc mem roadmap add "<title>"   [--status brainstorm] [--summary "…"] [--project <shortname|id>]
    ./sc mem roadmap status <feature_id> <status>
    ./sc mem roadmap project <feature_id> <shortname|id|none>   # set/clear the feature's work-stream
    ./sc mem roadmap depends <feature_id> [--on <id> …]         # set dependencies (replaces; omit --on to clear)
    ./sc mem project add <shortname> "<title>" [--purpose …] [--standing …] [--status active] [--role …]
    ./sc mem project standing <shortname|id> "<text>"
    ./sc mem project status <shortname|id> active|inactive|paused
    ./sc mem task add "<title>" --feature <id> --doc <id> --seq <n> [--desc "…"]
    ./sc mem task start <task_id>    ./sc mem task done <task_id>
    ./sc mem oriented                # mark first-run complete (bootstrapped=1)
    ./sc mem doc add "<title>" --body-file PATH [--feature ID] [--kind spec|doc] [--seq N]
    ./sc mem doc edit <document_id>  [--title "…"] [--body-file PATH] [--render-path …]   # unfrozen only
    ./sc mem doc freeze <document_id>
    ./sc mem narrative "<line>"
    ./sc mem message check [N]                         # your unread inbox (read-only)
    ./sc mem message send <to-shortname> "<body>"      # from = you (the token)
    ./sc mem message mark-read <message_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

# API proxy — run.py injects these at boot (token = the shell's api_key).
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE  = os.environ.get("SC_API_BASE", "")


def die(msg: str) -> "NoReturn":  # noqa: F821
    sys.exit(f"mem: {msg}")


def _require_api() -> None:
    """Hard gate: every op goes through the engine API. If it isn't wired, fail
    loud — do NOT silently write the DB behind the API's back (the bug that let a
    shell think a write was API-backed when it wasn't)."""
    if SC_API_TOKEN and SC_API_BASE:
        return
    missing = [n for n, v in (("SC_API_BASE", SC_API_BASE), ("SC_API_TOKEN", SC_API_TOKEN)) if not v]
    die(f"the engine API is required but {' + '.join(missing)} "
        f"{'is' if len(missing) == 1 else 'are'} unset — this shell isn't API-wired. "
        f"Boot via `./sc enter` (run.py injects them) with the server up (`./sc launch`). "
        f"`./sc mem` does not fall back to direct DB.")


def _api(method: str, path: str, payload: "dict | None" = None) -> dict:
    """POST/PATCH/GET to the engine API; die loud on any error."""
    url = SC_API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict = {"Authorization": f"Bearer {SC_API_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:
            msg = e.reason
        die(f"API {method} {path} → HTTP {e.code}: {msg}")
    except Exception as exc:
        die(f"API unreachable ({SC_API_BASE}): {exc}")


def _finish_api(summary: str) -> int:
    print(summary)
    print("  (via engine API — live in the shared engine DB)")
    return 0


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_which(args) -> int:
    """Diagnostic: confirm the API is reachable and report who the token is."""
    me = _api("GET", "/_sc/mem/whoami")
    print(f"engine API : {SC_API_BASE}")
    print(f"shell      : {me.get('display_name')} ({me.get('shortname')}) #{me.get('shell_id')}")
    print("identity   : resolved from your bearer token (SC_API_TOKEN), server-side")
    return 0


GET_SURFACES = ("state", "seed", "lns", "decisions", "flags",
                "roadmap", "narrative", "messages",
                "projects", "documents", "tasks", "shells")

# The write surface is `sc mem doc …` and boot docs say "doc" — accept the
# obvious short forms on the read side too instead of costing a round-trip.
GET_SURFACE_ALIASES = {"doc": "documents", "docs": "documents"}


def _render_get(surface: str, data: dict) -> int:
    if surface == "state":
        print(data.get("current_state") or "(current_state empty)")
        return 0
    if surface in ("seed", "lns"):
        es = data.get("entries", [])
        label = "seed" if surface == "seed" else "L&S"
        if not es:
            print(f"mem: no {label} entries")
            return 0
        for e in es:
            tag = f" [{e['source_tag']}]" if e.get("source_tag") else ""
            print(f"#{e['entry_id']} {e.get('entry_date') or ''}{tag}")
            print("  " + (e.get("body") or "").replace("\n", "\n  "))
        return 0
    if surface == "decisions":
        def _line(d) -> None:
            par = f" (supersedes #{d['parent_decision_id']})" if d.get("parent_decision_id") else ""
            sup = f" (superseded by #{d['superseded_by']})" if d.get("superseded_by") else ""
            print(f"#{d['decision_id']} [{d.get('priority') or 'M'}] "
                  f"{d.get('decision_date') or ''}{par}{sup}")
            print("  " + (d.get("decision") or ""))
        if "decision" in data:                    # single decision, with rationale
            d = data["decision"]
            _line(d)
            if d.get("feature_id"):
                ft = f" — {d['feature_title']}" if d.get("feature_title") else ""
                print(f"  feature: #{d['feature_id']}{ft}")
            if d.get("document_id"):
                dt = f" — {d['document_title']}" if d.get("document_title") else ""
                print(f"  doc: #{d['document_id']}{dt}")
            if d.get("rationale"):
                print("  rationale: " + d["rationale"])
            return 0
        ds = data.get("decisions", [])
        if not ds:
            print("mem: no decisions")
            return 0
        for d in ds:
            _line(d)
        # Index mode: say exactly what the cap hid — never a silent truncation.
        if not data.get("all"):
            hidden = max(0, (data.get("total_active") or len(ds)) - len(ds))
            superseded = data.get("superseded") or 0
            if hidden or superseded:
                bits = []
                if hidden:
                    bits.append(f"{hidden} older active")
                if superseded:
                    bits.append(f"{superseded} superseded")
                print(f"({' + '.join(bits)} not shown — `--all` for the full log; "
                      f"`get decisions <id>` for detail + rationale)")
        return 0
    if surface == "flags":
        fs = data.get("flags", [])
        if not fs:
            print("mem: no open flags")
            return 0
        for f in fs:
            nm = f.get("display_name") or f"#{f['flag_id']}"
            who = f" @{f['owner']}" if f.get("owner") else ""
            print(f"[{nm}]{who} ({f.get('priority') or 'Medium'}) {f.get('description') or ''}")
        return 0
    if surface == "roadmap":
        rm = data.get("roadmap", [])
        if not rm:
            print("mem: roadmap empty")
            return 0
        for x in rm:
            print(f"#{x['feature_id']} [{x.get('roadmap_status')}] {x.get('title')}")
            if x.get("summary"):
                print("  " + x["summary"])
        return 0
    if surface == "narrative":
        print(data.get("narrative") or "(no active narrative)")
        return 0
    if surface == "projects":
        ps = data.get("projects", [])
        if not ps:
            print("mem: no projects")
            return 0
        for p in ps:
            print(f"#{p['project_id']} {p['shortname']} [{p.get('status') or 'active'}] "
                  f"{p.get('title') or ''}")
        return 0
    if surface == "shells":
        sh = data.get("shells", [])
        if not sh:
            print("mem: no shells")
            return 0
        for s in sh:
            fl = f" ({s['flavor']})" if s.get("flavor") else ""
            print(f"#{s['shell_id']} {s['shortname']} — {s.get('display_name') or ''}{fl}")
        return 0
    if surface == "documents":
        # Single doc (with body) when --doc was passed; else the list.
        if "document" in data:
            d = data["document"]
            fz = " [frozen]" if d.get("frozen") else ""
            print(f"#{d['document_id']} {d.get('kind')} seq {d.get('seq')} · "
                  f"feature {d.get('feature_id')}{fz} — {d.get('title') or ''}")
            print()
            print(d.get("body") or "(empty body)")
            return 0
        ds = data.get("documents", [])
        if not ds:
            print("mem: no documents")
            return 0
        for d in ds:
            fz = " [frozen]" if d.get("frozen") else ""
            tc = d.get("task_count")
            tcs = f" · {tc} task(s)" if tc else ""
            print(f"#{d['document_id']} {d.get('kind')} seq {d.get('seq')} · "
                  f"feature {d.get('feature_id')}{fz}{tcs} — {d.get('title') or ''}")
        return 0
    if surface == "tasks":
        ts = data.get("tasks", [])
        if not ts:
            print("mem: no tasks")
            return 0
        for t in ts:
            done = f" ({t['completed_date']})" if t.get("completed_date") else ""
            print(f"#{t['task_id']} seq {t.get('seq')} [{t.get('status')}]{done} "
                  f"{t.get('title') or ''}")
            if t.get("description"):
                print("  " + t["description"])
        return 0
    if surface == "messages":
        msgs = data.get("messages", [])
        if not msgs:
            print("mem: inbox empty")
            return 0
        unread = [m for m in msgs if not m.get("read_at")]
        print(f"mem: {len(msgs)} message(s), {len(unread)} unread:")
        for m in msgs:
            mark = "" if m.get("read_at") else " *unread*"
            print(f"  [#{m['message_id']}] from shell #{m['from_shell_id']} · {m['created_at']}{mark}")
            print("    " + (m.get("body") or "").replace("\n", "\n    "))
        return 0
    die(f"unknown surface '{surface}'")


def cmd_get(args) -> int:
    surface = args.surface
    path = f"/_sc/mem/{surface}"
    if surface == "decisions":
        if args.id is not None:                   # single decision, with rationale
            path = f"/_sc/mem/decisions/{args.id}"
        elif args.all:                            # full log incl. superseded
            path = "/_sc/mem/decisions?all=1"
    elif args.id is not None:
        die(f"get {surface} takes no <id> (only decisions)")
    if surface == "documents":
        if args.doc is not None:                  # single doc, with body
            path = f"/_sc/mem/documents/{args.doc}"
        elif args.feature is not None:            # one feature's docs
            path = f"/_sc/mem/documents?feature={args.feature}"
    elif surface == "tasks":
        if args.doc is not None:
            path = f"/_sc/mem/tasks?doc={args.doc}"
        elif args.feature is not None:
            path = f"/_sc/mem/tasks?feature={args.feature}"
        else:
            die("get tasks needs --doc <id> or --feature <id>")
    data = _api("GET", path)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    return _render_get(surface, data)


def cmd_state(args) -> int:
    _api("POST", "/_sc/mem/state", {"body": args.text})
    return _finish_api(f"mem: current_state updated ({len(args.text)} chars)")


def _insert_identity(args, kind: str) -> int:
    r = _api("POST", f"/_sc/mem/{kind}",
             {"body": args.body,
              "entry_date": args.date or str(date.today()),
              "source_tag": args.tag})
    label = "seed" if kind == "seed" else "L&S"
    return _finish_api(f"mem: {label} entry #{r.get('entry_id', '')} added")


def cmd_seed(args) -> int:
    return _insert_identity(args, "seed")


def cmd_lns(args) -> int:
    return _insert_identity(args, "lns")


def cmd_retire(args) -> int:
    _api("PATCH", f"/_sc/mem/identity-entries/{args.entry_id}/retire")
    return _finish_api(f"mem: identity entry #{args.entry_id} retired (slot freed)")


def cmd_decision(args) -> int:
    r = _api("POST", "/_sc/mem/decisions",
             {"decision": args.decision,
              "rationale": args.rationale,
              "decision_date": args.date or str(date.today()),
              "parent_decision_id": args.parent,
              "feature_id": args.feature,
              "document_id": args.doc})
    fid = r.get("feature_id")
    link = f" → feature #{fid}" if fid else ""
    return _finish_api(f"mem: decision #{r.get('decision_id', '')} recorded{link}")


def cmd_flag(args) -> int:
    if args.flag_cmd == "open":
        r = _api("POST", "/_sc/mem/flags",
                 {"display_name": args.name,
                  "description": args.description,
                  "priority": args.priority,
                  "feature_id": args.feature})
        return _finish_api(f"mem: flag #{r.get('flag_id', '')} opened"
                           f"{f' ({args.name})' if args.name else ''}")
    _api("PATCH", f"/_sc/mem/flags/{args.flag_id}",
         {"resolved": True, "resolution_notes": args.notes})
    return _finish_api(f"mem: flag #{args.flag_id} closed")


def cmd_roadmap(args) -> int:
    if args.roadmap_cmd == "add":
        r = _api("POST", "/_sc/mem/roadmap",
                 {"title": args.title, "summary": args.summary,
                  "roadmap_status": args.status, "project": args.project})
        return _finish_api(f"mem: roadmap feature #{r.get('feature_id', '')} added"
                           f" ('{args.title}', {args.status})")
    if args.roadmap_cmd == "status":
        _api("PATCH", f"/_sc/mem/roadmap/{args.feature_id}", {"roadmap_status": args.status})
        return _finish_api(f"mem: feature #{args.feature_id} → {args.status}")
    if args.roadmap_cmd == "project":
        _api("PATCH", f"/_sc/mem/roadmap/{args.feature_id}", {"project": args.project})
        tgt = ("unassigned (no work-stream)" if str(args.project).lower() in ("none", "-", "")
               else f"work-stream '{args.project}'")
        return _finish_api(f"mem: feature #{args.feature_id} → {tgt}")
    # depends — replace the dependency set (server validates + refuses cycles)
    _api("PATCH", f"/_sc/mem/roadmap/{args.feature_id}", {"blocked_by": args.on or []})
    desc = ", ".join(f"#{d}" for d in args.on) if args.on else "— (cleared)"
    return _finish_api(f"mem: feature #{args.feature_id} depends on {desc}")


def cmd_project(args) -> int:
    if args.project_cmd == "add":
        r = _api("POST", "/_sc/mem/projects",
                 {"shortname": args.shortname, "title": args.title,
                  "purpose": args.purpose, "standing": args.standing,
                  "role": args.role, "status": args.status})
        return _finish_api(f"mem: project #{r.get('project_id', '')} ('{args.shortname}') added")
    if args.project_cmd == "standing":
        _api("PATCH", f"/_sc/mem/projects/{args.project}", {"standing": args.text})
        return _finish_api(f"mem: standing updated for project '{args.project}'")
    _api("PATCH", f"/_sc/mem/projects/{args.project}", {"status": args.status})
    return _finish_api(f"mem: project '{args.project}' → {args.status}")


def cmd_task(args) -> int:
    if args.task_cmd == "add":
        r = _api("POST", "/_sc/mem/tasks",
                 {"title": args.title, "feature_id": args.feature,
                  "document_id": args.doc, "seq": args.seq, "description": args.desc})
        return _finish_api(f"mem: task #{r.get('task_id', '')} added (seq {args.seq}, '{args.title}')")
    status = "in_progress" if args.task_cmd == "start" else "done"
    _api("PATCH", f"/_sc/mem/tasks/{args.task_id}", {"status": status})
    return _finish_api(f"mem: task #{args.task_id} → {status}")


def cmd_oriented(args) -> int:
    _api("POST", "/_sc/mem/oriented")
    return _finish_api("mem: shell marked oriented (bootstrapped=1)")


def cmd_doc(args) -> int:
    if args.doc_cmd == "freeze":
        _api("PATCH", f"/_sc/mem/docs/{args.document_id}/freeze")
        return _finish_api(f"mem: document #{args.document_id} frozen")
    if args.doc_cmd == "edit":
        payload: dict = {}
        if args.title is not None:
            payload["title"] = args.title
        if args.body_file is not None:
            payload["body"] = Path(args.body_file).read_text()
        if args.render_path is not None:
            payload["render_path"] = args.render_path
        if not payload:
            die("nothing to edit — pass at least one of --title / --body-file / --render-path")
        _api("PATCH", f"/_sc/mem/docs/{args.document_id}", payload)
        return _finish_api(f"mem: document #{args.document_id} edited")
    body_text = Path(args.body_file).read_text()
    r = _api("POST", "/_sc/mem/docs",
             {"feature_id": args.feature,
              "kind": args.kind,
              "seq": args.seq,
              "title": args.title,
              "body": body_text,
              "render_path": args.render_path})
    return _finish_api(f"mem: {args.kind} document #{r.get('document_id', '')} added"
                       f" ('{args.title}', {len(body_text)} chars)")


def cmd_narrative(args) -> int:
    line = f"[{datetime.now().strftime('%H:%M')}] {args.line}"
    _api("POST", "/_sc/mem/narrative", {"text": line})
    return _finish_api("mem: narrative appended")


def cmd_message(args) -> int:
    if args.message_cmd == "check":
        r = _api("GET", "/_sc/mem/messages")
        msgs = r.get("messages", [])
        unread = [m for m in msgs if not m.get("read_at")]
        if not unread:
            print("mem: inbox empty")
            return 0
        print(f"mem: {len(unread)} unread:")
        for m in unread:
            print(f"  [#{m['message_id']}] from shell #{m['from_shell_id']} · {m['created_at']}")
            print("    " + (m["body"] or "").replace("\n", "\n    "))
        return 0
    if args.message_cmd == "send":
        if not args.body.strip():
            die("body is empty")
        r = _api("POST", "/_sc/mem/messages", {"to": args.to, "body": args.body})
        return _finish_api(f"mem: message #{r.get('message_id', '')} sent to {args.to}")
    # mark-read
    _api("PATCH", f"/_sc/mem/messages/{args.message_id}/read")
    return _finish_api(f"mem: message #{args.message_id} marked read")


# ── arg parsing ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sc mem", description="engine memory surface (over the API)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("which", help="confirm API reachability + who your token resolves to") \
       .set_defaults(fn=cmd_which)

    sp = sub.add_parser("get", help=f"read a memory surface ({'/'.join(GET_SURFACES)}; doc/docs = documents)")
    sp.add_argument("surface", choices=GET_SURFACES,
                    type=lambda s: GET_SURFACE_ALIASES.get(s, s))
    sp.add_argument("id", nargs="?", type=int,
                    help="decisions: one decision WITH rationale")
    sp.add_argument("--all", action="store_true",
                    help="decisions: full log incl. superseded (default: active index)")
    sp.add_argument("--json", action="store_true", help="raw JSON instead of formatted text")
    sp.add_argument("--feature", type=int,
                    help="scope to a feature (documents/tasks)")
    sp.add_argument("--doc", type=int,
                    help="documents: one doc WITH body; tasks: that doc's plan")
    sp.set_defaults(fn=cmd_get)

    sp = sub.add_parser("state", help="set current_state")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_state)

    for k, fn in (("seed", cmd_seed), ("lns", cmd_lns)):
        sp = sub.add_parser(k, help=f"add a {k} identity entry")
        sp.add_argument("body")
        sp.add_argument("--date")
        sp.add_argument("--tag")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("retire", help="retire an identity entry (frees a cap slot)")
    sp.add_argument("entry_id", type=int)
    sp.set_defaults(fn=cmd_retire)

    sp = sub.add_parser("decision", help="record a Major decision")
    sp.add_argument("decision")
    sp.add_argument("--rationale")
    sp.add_argument("--date")
    sp.add_argument("--parent", type=int, help="parent_decision_id (supersession)")
    sp.add_argument("--feature", type=int,
                    help="feature_id this decision serves (the why-audit link)")
    sp.add_argument("--doc", type=int,
                    help="document_id this decision shaped (implies its feature)")
    sp.set_defaults(fn=cmd_decision)

    sp = sub.add_parser("flag", help="open or close a flag")
    fsub = sp.add_subparsers(dest="flag_cmd", required=True)
    fo = fsub.add_parser("open")
    fo.add_argument("description")
    fo.add_argument("--name")
    fo.add_argument("--priority", default="Medium", choices=["High", "Medium", "Low"])
    fo.add_argument("--feature", type=int)
    fc = fsub.add_parser("close")
    fc.add_argument("flag_id", type=int)
    fc.add_argument("--notes")
    sp.set_defaults(fn=cmd_flag)

    ROADMAP_STATUSES = ["shipped", "in_progress", "next", "near_term",
                        "long_term", "brainstorm", "retired"]
    sp = sub.add_parser("roadmap", help="add a feature, move its status, set its work-stream or dependencies")
    rsub = sp.add_subparsers(dest="roadmap_cmd", required=True)
    ra = rsub.add_parser("add")
    ra.add_argument("title")
    ra.add_argument("--status", default="brainstorm", choices=ROADMAP_STATUSES)
    ra.add_argument("--summary")
    ra.add_argument("--project", help="assign to a work-stream (projects shortname|id)")
    rt = rsub.add_parser("status")
    rt.add_argument("feature_id", type=int)
    rt.add_argument("status", choices=ROADMAP_STATUSES)
    rp = rsub.add_parser("project", help="set/clear a feature's work-stream")
    rp.add_argument("feature_id", type=int)
    rp.add_argument("project", help="work-stream shortname|id, or 'none' to clear")
    rd = rsub.add_parser("depends", help="set a feature's dependencies (replaces the set)")
    rd.add_argument("feature_id", type=int)
    rd.add_argument("--on", type=int, action="append", metavar="FEATURE_ID",
                    help="a prerequisite that must land first (repeatable); omit all to clear")
    sp.set_defaults(fn=cmd_roadmap)

    sp = sub.add_parser("project", help="add a project or update its standing/status")
    psub = sp.add_subparsers(dest="project_cmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("shortname")
    pa.add_argument("title")
    pa.add_argument("--purpose")
    pa.add_argument("--standing")
    pa.add_argument("--role")
    pa.add_argument("--status", default="active", choices=["active", "inactive", "paused"])
    pst = psub.add_parser("standing")
    pst.add_argument("project")
    pst.add_argument("text")
    pss = psub.add_parser("status")
    pss.add_argument("project")
    pss.add_argument("status", choices=["active", "inactive", "paused"])
    sp.set_defaults(fn=cmd_project)

    sp = sub.add_parser("task", help="spec_tasks: add / start / done")
    tsub = sp.add_subparsers(dest="task_cmd", required=True)
    ta = tsub.add_parser("add")
    ta.add_argument("title")
    ta.add_argument("--feature", type=int, required=True)
    ta.add_argument("--doc", type=int, required=True)
    ta.add_argument("--seq", type=int, required=True)
    ta.add_argument("--desc")
    tst = tsub.add_parser("start")
    tst.add_argument("task_id", type=int)
    tdn = tsub.add_parser("done")
    tdn.add_argument("task_id", type=int)
    sp.set_defaults(fn=cmd_task)

    sub.add_parser("oriented", help="mark this shell oriented (bootstrapped=1)") \
       .set_defaults(fn=cmd_oriented)

    sp = sub.add_parser("doc", help="add, edit, or freeze a spec/doc document")
    dsub = sp.add_subparsers(dest="doc_cmd", required=True)
    da = dsub.add_parser("add")
    da.add_argument("title")
    da.add_argument("--body-file", required=True, dest="body_file")
    da.add_argument("--feature", type=int)
    da.add_argument("--kind", default="spec", choices=["spec", "doc"])
    da.add_argument("--seq", type=int)
    da.add_argument("--render-path", dest="render_path")
    de = dsub.add_parser("edit", help="revise an unfrozen doc's title/body/render-path")
    de.add_argument("document_id", type=int)
    de.add_argument("--title")
    de.add_argument("--body-file", dest="body_file")
    de.add_argument("--render-path", dest="render_path")
    df = dsub.add_parser("freeze")
    df.add_argument("document_id", type=int)
    sp.set_defaults(fn=cmd_doc)

    sp = sub.add_parser("narrative", help="append a [HH:MM] line to the active archive")
    sp.add_argument("line")
    sp.set_defaults(fn=cmd_narrative)

    sp = sub.add_parser("message", help="shell-to-shell inbox: check / send / mark-read")
    msub = sp.add_subparsers(dest="message_cmd", required=True)
    mc = msub.add_parser("check")
    mc.add_argument("limit", type=int, nargs="?", default=50,
                    help="(accepted; the API returns your latest 50)")
    ms = msub.add_parser("send")
    ms.add_argument("to")
    ms.add_argument("body")
    mm = msub.add_parser("mark-read")
    mm.add_argument("message_id", type=int)
    sp.set_defaults(fn=cmd_message)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    _require_api()  # every command goes through the API — fail loud if unwired
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
