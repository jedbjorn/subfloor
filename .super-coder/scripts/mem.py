#!/usr/bin/env python3
"""sc mem — the engine memory write surface.

ONE DB-resolution-safe door for a shell to write its own identity/memory into
the engine DB (`.super-coder/shell_db.db`). It exists because the alternative —
a raw `sqlite3 <path> "INSERT …"` — is a foot-gun on a fork: the engine DB and a
product DB (e.g. dos-arch's `shell_core/app.db`) share table names (`shells`,
`flags`, `skills`, `documents`, `shell_decisions`, `shell_identity_entries`, …),
so the same statement SUCCEEDS against the wrong file and means a different
thing; and 0-byte stub `shell_db.db` files lying around get silently *created*
into real tables by a mistyped path.

Every write here:
  • resolves the engine DB the engine way (`ENGINE = __file__/../..`),
    cwd/worktree-proof — never a guess;
  • `assert_engine_db()` first — rejects 0-byte stubs and any DB lacking the
    engine sentinels / carrying product sentinels; fails LOUD with the path;
  • writes to the live engine DB and stops there — the DB is the single source of
    truth shared by every shell, so the write is immediately durable + visible to
    all. It does NOT serialize: persisting to git (`.sc-state/` + flat `_sc`) writes
    the shared MAIN worktree and is an admin/GUI publish step, not a shell's job.
    The DB is gitignored + rebuilt-from-text, so admin snapshots (GUI button or
    `SC_ADMIN=1 ./sc snapshot`) to capture accumulated shell writes before a
    `./sc rebuild` — like committing a working tree before a hard reset.

Run from the repo root, like every engine command:

    ./sc mem which                                 # read-only: resolved DB + guard verdict + active shell
    ./sc mem state "<text>"          [--shell <id|shortname>]
    ./sc mem seed  "<body>"          [--date YYYY-MM-DD] [--tag cc] [--shell …]
    ./sc mem lns   "<body>"          [--date …] [--tag …] [--shell …]
    ./sc mem retire <entry_id>
    ./sc mem decision "<decision>"   [--rationale "…"] [--date …] [--parent ID] [--shell …]
    ./sc mem flag open  "<description>" [--name CC-001] [--priority Medium] [--feature ID] [--shell …]
    ./sc mem flag close <flag_id>    [--notes "…"]
    ./sc mem roadmap add "<title>"   [--status brainstorm] [--summary "…"] [--project <shortname|id>] [--shell …]
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
    ./sc mem narrative "<line>"      [--shell …]
    ./sc mem message check [N]       [--shell …]      # your unread inbox (read-only)
    ./sc mem message send <to-shortname> "<body>"     # from = you
    ./sc mem message mark-read <message_id>

Shell resolution: writes target the booted shell — `$SC_SHELL` (exported at boot
by run.py), else the `shell/<name>` worktree branch, else the sole non-shared
shell. Pass `--shell <id|shortname>` (AFTER the leaf command) to override and
write as another shell.

Common flags: --db <path> (override target; still guarded — used by tests).
(`--no-sync` is accepted but now a no-op: mem never serializes — that is an
admin/GUI step.)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_driver  # noqa: E402

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SCRIPTS = ENGINE / "scripts"
DEFAULT_DB = ENGINE / "shell_db.db"

# API proxy — active when run.py injects these at boot.
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE  = os.environ.get("SC_API_BASE", "")


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


# Tables that exist ONLY in the engine DB / ONLY in a product DB. The two sets
# never overlap (verified against dos-arch's app.db), so they cleanly tell the
# engine substrate apart from a substrate *product* that also manages shells.
ENGINE_SENTINELS = {"spec_tasks", "roadmap", "flavor_defaults"}
PRODUCT_SENTINELS = {"contacts", "emails"}


def die(msg: str) -> "NoReturn":  # noqa: F821
    sys.exit(f"mem: {msg}")


# ── DB resolution + guard ─────────────────────────────────────────────────────

def assert_engine_db(path: Path) -> None:
    """Refuse to write anything that is not THE engine DB. Loud, never silent."""
    if not path.exists():
        die(f"no DB at {path} — run `./sc rebuild` first.")
    if path.stat().st_size == 0:
        die(f"{path} is a 0-byte stub, not the engine DB. The engine DB is "
            f"{DEFAULT_DB} — run `./sc rebuild` if it is missing.")
    import sqlite3 as _sq3
    con = _sq3.connect(path)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    missing = ENGINE_SENTINELS - names
    product = PRODUCT_SENTINELS & names
    if missing or product:
        die(f"NOT the super-coder engine DB — refusing to write.\n"
            f"     missing engine tables: {sorted(missing) or '—'}\n"
            f"     product tables present: {sorted(product) or '—'}")


def connect(path: Path):
    assert_engine_db(path)
    return db_driver.connect(path)


# ── shell resolution ──────────────────────────────────────────────────────────

def git_branch() -> str | None:
    # Read the branch of the CALLER's cwd, not the engine root: a shell runs from
    # its `.sc-worktrees/<name>/` worktree (on `shell/<name>`), while the engine
    # root sits on the default branch. Inferring the shell needs the worktree's
    # branch, so don't pin `-C` to REPO_ROOT.
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_shell(con, spec: str | None) -> int:
    """--shell wins; else SC_SHELL (the booted shell's own identity, exported at
    boot by run.py); else infer from a `shell/<name>` worktree branch; else the
    sole non-shared shell; else make the caller pick.

    SC_SHELL is the authoritative answer to "which shell am I" — boot knows it
    for certain, where the git-branch heuristic below fails (admin flavor boots
    on `main`, or the cwd has drifted to the repo root). An explicit --shell
    still overrides it, so a shell can deliberately write as another."""
    if spec is None:
        spec = os.environ.get("SC_SHELL") or None
    if spec is not None:
        if str(spec).isdigit():
            r = con.execute("SELECT shell_id FROM shells WHERE shell_id=? "
                            "AND COALESCE(is_deleted,0)=0", (int(spec),)).fetchone()
            if not r:
                die(f"no shell with id {spec}")
            return r["shell_id"]
        r = con.execute("SELECT shell_id FROM shells WHERE LOWER(shortname)=LOWER(?) "
                        "AND COALESCE(is_deleted,0)=0", (spec,)).fetchone()
        if not r:
            die(f"no shell with shortname '{spec}'")
        return r["shell_id"]

    br = git_branch()
    if br and br.startswith("shell/"):
        name = br.split("/", 1)[1]
        r = con.execute("SELECT shell_id FROM shells WHERE LOWER(shortname)=LOWER(?) "
                        "AND COALESCE(is_deleted,0)=0", (name,)).fetchone()
        if r:
            return r["shell_id"]

    rs = con.execute("SELECT shell_id, shortname FROM shells "
                     "WHERE COALESCE(is_shared,0)=0 AND COALESCE(is_deleted,0)=0 "
                     "ORDER BY shell_id").fetchall()
    if len(rs) == 1:
        return rs[0]["shell_id"]
    listing = ", ".join(f"{r['shortname']}(#{r['shell_id']})" for r in rs) or "none"
    ex = rs[0]["shortname"] if rs else "<id|shortname>"
    die("could not infer the shell — name it explicitly.\n"
        f"     candidates: {listing}\n"
        f"     the --shell flag goes AFTER the leaf command, e.g.:\n"
        f"         ./sc mem message check --shell {ex}\n"
        f"     or export it once for the session:  export SC_SHELL={ex}")


# ── write durability ──────────────────────────────────────────────────────────
# A `./sc mem` write lands in the live engine DB, which is the single source of
# truth shared by every shell. That is durable for the running system — the write
# is immediately visible to all shells. Serializing it to git (`.sc-state/` +
# flat `_sc` mirror) writes into the shared MAIN worktree, so it is an admin/GUI
# publish step, NOT a per-write side-effect of a shell's work. See _serialize_guard.


def finish(args, summary: str) -> int:
    print(summary)
    print("  (live in the shared engine DB — serialize to git via the GUI Snapshot "
          "button, or as admin: SC_ADMIN=1 ./sc snapshot && ./sc render flat)")
    return 0


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_which(args) -> int:
    path = Path(args.db)
    print(f"engine DB : {path}")
    print(f"exists    : {path.exists()}  size={path.stat().st_size if path.exists() else 0}B")
    assert_engine_db(path)  # exits loudly if not the engine DB
    print("guard     : OK — engine sentinels present, no product tables")
    con = connect(path)
    try:
        sid = resolve_shell(con, args.shell)
        r = con.execute("SELECT shortname, display_name FROM shells WHERE shell_id=?",
                        (sid,)).fetchone()
        br = git_branch()
        print(f"active sh : {r['display_name']} ({r['shortname']}) #{sid}"
              f"{f'  [branch {br}]' if br else ''}")
    finally:
        con.close()
    return 0


def cmd_state(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        _api("POST", "/_sc/mem/state", {"body": args.text})
        return _finish_api(f"mem: current_state updated ({len(args.text)} chars)")
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        con.execute("UPDATE shells SET current_state=? WHERE shell_id=?",
                    (args.text, sid))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: current_state updated for shell #{sid} ({len(args.text)} chars)")


def _insert_identity(args, kind: str) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        r = _api("POST", f"/_sc/mem/{kind}",
                 {"body": args.body,
                  "entry_date": args.date or str(date.today()),
                  "source_tag": args.tag})
        label = "seed" if kind == "seed" else "L&S"
        return _finish_api(f"mem: {label} entry #{r.get('entry_id', '')} added")
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        try:
            cur = con.execute(
                "INSERT INTO shell_identity_entries (shell_id, kind, entry_date, source_tag, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, kind, args.date or str(date.today()), args.tag, args.body))
        except db_driver.IntegrityError as e:
            die(str(e))  # cap trigger fired with a clear message
        con.commit()
        eid = cur.lastrowid
    finally:
        con.close()
    label = "seed" if kind == "seed" else "L&S"
    return finish(args, f"mem: {label} entry #{eid} added for shell #{sid}")


def cmd_seed(args) -> int:
    return _insert_identity(args, "seed")


def cmd_lns(args) -> int:
    return _insert_identity(args, "lns")


def cmd_retire(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        _api("PATCH", f"/_sc/mem/identity-entries/{args.entry_id}/retire")
        return _finish_api(f"mem: identity entry #{args.entry_id} retired (slot freed)")
    con = connect(Path(args.db))
    try:
        r = con.execute("SELECT kind, retired_at FROM shell_identity_entries "
                        "WHERE entry_id=? AND COALESCE(is_deleted,0)=0",
                        (args.entry_id,)).fetchone()
        if r is None:
            die(f"no identity entry #{args.entry_id}")
        if r["retired_at"]:
            die(f"identity entry #{args.entry_id} is already retired ({r['retired_at']})")
        con.execute("UPDATE shell_identity_entries SET retired_at=CURRENT_TIMESTAMP "
                    "WHERE entry_id=?", (args.entry_id,))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: {r['kind']} entry #{args.entry_id} retired (slot freed)")


def cmd_decision(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        r = _api("POST", "/_sc/mem/decisions",
                 {"decision": args.decision,
                  "rationale": args.rationale,
                  "decision_date": args.date or str(date.today()),
                  "parent_decision_id": args.parent})
        return _finish_api(f"mem: decision #{r.get('decision_id', '')} recorded")
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        cur = con.execute(
            "INSERT INTO shell_decisions (shell_id, decision_date, priority, decision, "
            "rationale, parent_decision_id) VALUES (?, ?, 'M', ?, ?, ?)",
            (sid, args.date or str(date.today()), args.decision, args.rationale, args.parent))
        con.commit()
        did = cur.lastrowid
    finally:
        con.close()
    return finish(args, f"mem: decision #{did} recorded for shell #{sid}")


def cmd_flag(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
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
    con = connect(Path(args.db))
    try:
        if args.flag_cmd == "open":
            sid = resolve_shell(con, args.shell)
            cur = con.execute(
                "INSERT INTO flags (display_name, description, priority, feature_id, shell_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (args.name, args.description, args.priority, args.feature, sid))
            con.commit()
            return finish(args, f"mem: flag #{cur.lastrowid} opened"
                                f"{f' ({args.name})' if args.name else ''} for shell #{sid}")
        # close
        r = con.execute("SELECT resolved FROM flags WHERE flag_id=? "
                        "AND COALESCE(is_deleted,0)=0", (args.flag_id,)).fetchone()
        if r is None:
            die(f"no flag #{args.flag_id}")
        if r["resolved"]:
            die(f"flag #{args.flag_id} is already resolved")
        con.execute("UPDATE flags SET resolved=1, resolved_date=CURRENT_DATE, "
                    "resolution_notes=? WHERE flag_id=?", (args.notes, args.flag_id))
        con.commit()
        return finish(args, f"mem: flag #{args.flag_id} closed")
    finally:
        con.close()


def cmd_roadmap(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        if args.roadmap_cmd == "add":
            r = _api("POST", "/_sc/mem/roadmap",
                     {"title": args.title,
                      "summary": args.summary,
                      "roadmap_status": args.status})
            return _finish_api(f"mem: roadmap feature #{r.get('feature_id', '')} added"
                               f" ('{args.title}', {args.status})")
        if args.roadmap_cmd == "status":
            _api("PATCH", f"/_sc/mem/roadmap/{args.feature_id}",
                 {"roadmap_status": args.status})
            return _finish_api(f"mem: feature #{args.feature_id} → {args.status}")
        # project / depends need DB-side resolution — fall through to direct-DB
    con = connect(Path(args.db))
    try:
        if args.roadmap_cmd == "status":
            r = con.execute("SELECT title FROM roadmap WHERE feature_id=?",
                            (args.feature_id,)).fetchone()
            if r is None:
                die(f"no roadmap feature #{args.feature_id}")
            con.execute("UPDATE roadmap SET roadmap_status=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE feature_id=?", (args.status, args.feature_id))
            con.commit()
            return finish(args, f"mem: feature #{args.feature_id} ('{r['title']}') → {args.status}")
        if args.roadmap_cmd == "project":
            # assign / clear the feature's work-stream (the Flow view groups on it)
            feat = _resolve_feature(con, args.feature_id)
            if str(args.project).lower() in ("none", "-", ""):
                con.execute("UPDATE roadmap SET project_id=NULL, updated_at=CURRENT_TIMESTAMP "
                            "WHERE feature_id=?", (args.feature_id,))
                con.commit()
                return finish(args, f"mem: feature #{args.feature_id} ('{feat['title']}') "
                                    f"→ unassigned (no work-stream)")
            proj = _resolve_project(con, args.project)
            con.execute("UPDATE roadmap SET project_id=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE feature_id=?", (proj["project_id"], args.feature_id))
            con.commit()
            return finish(args, f"mem: feature #{args.feature_id} ('{feat['title']}') "
                                f"→ work-stream '{proj['shortname']}'")
        if args.roadmap_cmd == "depends":
            # replace the feature's dependency set (feature_blockers): each --on is a
            # prerequisite that must land first. Validate before mutating; refuse cycles.
            feat = _resolve_feature(con, args.feature_id)
            ons: list[int] = []
            for x in (args.on or []):
                if x == args.feature_id:
                    die("a feature can't depend on itself")
                _resolve_feature(con, x)
                if x in ons:
                    continue
                if args.feature_id in _depends_on(con, x):
                    die(f"refusing: #{x} already depends on #{args.feature_id} — "
                        f"this edge would create a cycle")
                ons.append(x)
            con.execute("DELETE FROM feature_blockers WHERE feature_id=?", (args.feature_id,))
            for dep in ons:
                con.execute("INSERT INTO feature_blockers (feature_id, blocked_by) "
                            "VALUES (?, ?)", (args.feature_id, dep))
            con.commit()
            desc = ", ".join(f"#{d}" for d in ons) if ons else "— (cleared)"
            return finish(args, f"mem: feature #{args.feature_id} ('{feat['title']}') "
                                f"depends on {desc}")
        # add
        sid = resolve_shell(con, args.shell)
        pid = _resolve_project(con, args.project)["project_id"] if args.project else None
        cur = con.execute(
            "INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary, project_id) "
            "VALUES (?, ?, 0, ?, ?, ?)", (args.title, args.status, sid, args.summary, pid))
        con.commit()
        fid = cur.lastrowid
    finally:
        con.close()
    return finish(args, f"mem: roadmap feature #{fid} added ('{args.title}', {args.status})")


def _resolve_feature(con, fid: int):
    r = con.execute("SELECT feature_id, title FROM roadmap WHERE feature_id=?",
                    (fid,)).fetchone()
    if r is None:
        die(f"no roadmap feature #{fid}")
    return r


def _depends_on(con, start: int) -> set:
    """Features `start` (transitively) depends on, walking blocked_by edges. Used
    to keep the dependency graph acyclic: an edge (F depends on D) closes a cycle
    iff F is already in D's dependency set."""
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for row in con.execute(
                "SELECT blocked_by FROM feature_blockers WHERE feature_id=?", (n,)):
            b = row["blocked_by"]
            if b not in seen:
                seen.add(b)
                stack.append(b)
    return seen


def _resolve_project(con, spec: str):
    if str(spec).isdigit():
        r = con.execute("SELECT project_id, shortname FROM projects WHERE project_id=? "
                        "AND COALESCE(is_deleted,0)=0", (int(spec),)).fetchone()
    else:
        r = con.execute("SELECT project_id, shortname FROM projects WHERE LOWER(shortname)=LOWER(?) "
                        "AND COALESCE(is_deleted,0)=0", (spec,)).fetchone()
    if r is None:
        die(f"no project '{spec}'")
    return r


def cmd_project(args) -> int:
    con = connect(Path(args.db))
    try:
        if args.project_cmd == "add":
            sid = resolve_shell(con, args.shell)
            try:
                cur = con.execute(
                    "INSERT INTO projects (shortname, title, purpose, standing, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (args.shortname, args.title, args.purpose, args.standing, args.status))
            except db_driver.IntegrityError as e:
                die(str(e))  # UNIQUE(shortname) etc.
            pid = cur.lastrowid
            con.execute("INSERT INTO project_shells (project_id, shell_id, role) VALUES (?, ?, ?)",
                        (pid, sid, args.role))
            con.commit()
            return finish(args, f"mem: project #{pid} ('{args.shortname}') added + linked to shell #{sid}")
        proj = _resolve_project(con, args.project)
        if args.project_cmd == "standing":
            con.execute("UPDATE projects SET standing=? WHERE project_id=?",
                        (args.text, proj["project_id"]))
            con.commit()
            return finish(args, f"mem: standing updated for project '{proj['shortname']}'")
        # status
        con.execute("UPDATE projects SET status=? WHERE project_id=?",
                    (args.status, proj["project_id"]))
        con.commit()
        return finish(args, f"mem: project '{proj['shortname']}' → {args.status}")
    finally:
        con.close()


def cmd_task(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        if args.task_cmd == "add":
            r = _api("POST", "/_sc/mem/tasks",
                     {"title": args.title,
                      "feature_id": args.feature,
                      "document_id": args.doc,
                      "seq": args.seq,
                      "description": args.desc})
            return _finish_api(f"mem: task #{r.get('task_id', '')} added"
                               f" (seq {args.seq}, '{args.title}')")
        status = "in_progress" if args.task_cmd == "start" else "done"
        _api("PATCH", f"/_sc/mem/tasks/{args.task_id}", {"status": status})
        return _finish_api(f"mem: task #{args.task_id} → {status}")
    con = connect(Path(args.db))
    try:
        if args.task_cmd == "add":
            sid = resolve_shell(con, args.shell)
            try:
                cur = con.execute(
                    "INSERT INTO spec_tasks (feature_id, document_id, seq, title, description, shell_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (args.feature, args.doc, args.seq, args.title, args.desc, sid))
            except db_driver.IntegrityError as e:
                die(str(e))  # UNIQUE(document_id, seq) / FK
            con.commit()
            return finish(args, f"mem: task #{cur.lastrowid} added (seq {args.seq}, '{args.title}')")
        r = con.execute("SELECT title, status FROM spec_tasks WHERE task_id=?",
                        (args.task_id,)).fetchone()
        if r is None:
            die(f"no task #{args.task_id}")
        if args.task_cmd == "start":
            con.execute("UPDATE spec_tasks SET status='in_progress' WHERE task_id=?", (args.task_id,))
            con.commit()
            return finish(args, f"mem: task #{args.task_id} ('{r['title']}') → in_progress")
        # done
        con.execute("UPDATE spec_tasks SET status='done', completed_date=CURRENT_DATE "
                    "WHERE task_id=?", (args.task_id,))
        con.commit()
        return finish(args, f"mem: task #{args.task_id} ('{r['title']}') → done")


    finally:
        con.close()


def cmd_oriented(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        _api("POST", "/_sc/mem/oriented")
        return _finish_api("mem: shell marked oriented (bootstrapped=1)")
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        con.execute("UPDATE shells SET bootstrapped=1 WHERE shell_id=?", (sid,))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: shell #{sid} marked oriented (bootstrapped=1)")


def cmd_doc(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
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
        # add
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
    if args.doc_cmd == "freeze":
        return _doc_freeze(args)
    if args.doc_cmd == "edit":
        return _doc_edit(args)
    body = Path(args.body_file).read_text()
    con = connect(Path(args.db))
    try:
        seq = args.seq
        if seq is None:
            r = con.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM documents "
                            "WHERE feature_id IS ? AND kind=?",
                            (args.feature, args.kind)).fetchone()
            seq = r["n"]
        cur = con.execute(
            "INSERT INTO documents (feature_id, kind, seq, title, body, render_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (args.feature, args.kind, seq, args.title, body, args.render_path))
        con.commit()
        docid = cur.lastrowid
    finally:
        con.close()
    return finish(args, f"mem: {args.kind} document #{docid} added "
                        f"('{args.title}', seq {seq}, {len(body)} chars)")


def _doc_freeze(args) -> int:
    con = connect(Path(args.db))
    try:
        r = con.execute("SELECT frozen FROM documents WHERE document_id=?",
                        (args.document_id,)).fetchone()
        if r is None:
            die(f"no document #{args.document_id}")
        if r["frozen"]:
            die(f"document #{args.document_id} is already frozen")
        con.execute("UPDATE documents SET frozen=1, frozen_date=CURRENT_DATE "
                    "WHERE document_id=?", (args.document_id,))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: document #{args.document_id} frozen")


def _doc_edit(args) -> int:
    sets, vals, what = [], [], []
    if args.title is not None:
        sets.append("title=?"); vals.append(args.title); what.append("title")
    if args.body_file is not None:
        body = Path(args.body_file).read_text()
        sets.append("body=?"); vals.append(body); what.append(f"body ({len(body)} chars)")
    if args.render_path is not None:
        sets.append("render_path=?"); vals.append(args.render_path); what.append("render_path")
    if not sets:
        die("nothing to edit — pass at least one of --title / --body-file / --render-path")
    con = connect(Path(args.db))
    try:
        r = con.execute("SELECT frozen FROM documents WHERE document_id=?",
                        (args.document_id,)).fetchone()
        if r is None:
            die(f"no document #{args.document_id}")
        if r["frozen"]:
            die(f"document #{args.document_id} is frozen — open a new spec under the "
                "same feature instead of editing a frozen one")
        sets.append("updated_at=CURRENT_TIMESTAMP")
        con.execute(f"UPDATE documents SET {', '.join(sets)} WHERE document_id=?",
                    (*vals, args.document_id))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: document #{args.document_id} edited ({', '.join(what)})")


def cmd_message(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
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
        if args.message_cmd == "mark-read":
            _api("PATCH", f"/_sc/mem/messages/{args.message_id}/read")
            return _finish_api(f"mem: message #{args.message_id} marked read")
        # send: needs shortname→shell_id resolution — fall through to direct-DB
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        if args.message_cmd == "check":
            n = min(args.limit, 200)
            rows_ = con.execute(
                "SELECT m.message_id, s.shortname AS frm, m.body, m.created_at "
                "FROM shell_messages m JOIN shells s ON s.shell_id = m.from_shell_id "
                "WHERE m.to_shell_id=? AND m.read_at IS NULL "
                "ORDER BY m.created_at LIMIT ?", (sid, n)).fetchall()
            if not rows_:
                print(f"mem: inbox empty for shell #{sid}")
                return 0
            print(f"mem: {len(rows_)} unread for shell #{sid}:")
            for r in rows_:
                print(f"  [#{r['message_id']}] from {r['frm']} · {r['created_at']}")
                print("    " + r["body"].replace("\n", "\n    "))
            return 0  # read-only: no sync
        if args.message_cmd == "send":
            to = con.execute("SELECT shell_id FROM shells WHERE LOWER(shortname)=LOWER(?) "
                             "AND COALESCE(is_deleted,0)=0", (args.to,)).fetchone()
            if not to:
                die(f"recipient shortname '{args.to}' unknown")
            if not args.body.strip():
                die("body is empty")
            cur = con.execute(
                "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) VALUES (?, ?, ?)",
                (sid, to["shell_id"], args.body))
            con.commit()
            return finish(args, f"mem: message #{cur.lastrowid} sent from #{sid} to {args.to}")
        # mark-read
        cur = con.execute(
            "UPDATE shell_messages SET read_at=CURRENT_TIMESTAMP "
            "WHERE message_id=? AND to_shell_id=? AND read_at IS NULL",
            (args.message_id, sid))
        con.commit()
        if cur.rowcount == 0:
            return finish(args, f"mem: message #{args.message_id} already read or not yours "
                                f"(no-op)")
        return finish(args, f"mem: message #{args.message_id} marked read")
    finally:
        con.close()


def cmd_narrative(args) -> int:
    if SC_API_TOKEN and SC_API_BASE:
        line = f"[{datetime.now().strftime('%H:%M')}] {args.line}"
        _api("POST", "/_sc/mem/narrative", {"text": line})
        return _finish_api("mem: narrative appended")
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        r = con.execute("SELECT active_archive_id FROM shells WHERE shell_id=?",
                        (sid,)).fetchone()
        aid = r["active_archive_id"] if r else None
        if not aid:
            die(f"shell #{sid} has no active archive — nothing to append the narrative to")
        line = f"[{datetime.now().strftime('%H:%M')}] {args.line}"
        con.execute(
            "UPDATE shell_memory_archives SET full_narrative = "
            "COALESCE(full_narrative,'') || CASE WHEN COALESCE(full_narrative,'')='' "
            "THEN '' ELSE char(10) END || ? WHERE archive_id=?", (line, aid))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: narrative appended to archive #{aid} (shell #{sid})")


# ── arg parsing ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    # Common flags live on a parent so they parse AFTER the subcommand — the
    # natural position (`./sc mem state "…" --shell cc`), not only before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(DEFAULT_DB), help="engine DB path (default: the fork's)")
    common.add_argument("--no-sync", action="store_true",
                        help="accepted for back-compat; no-op (mem never serializes — admin/GUI step)")
    common.add_argument("--shell", help="target shell id or shortname "
                        "(default: $SC_SHELL, else inferred from the worktree branch)")

    p = argparse.ArgumentParser(prog="sc mem", description="engine memory write surface")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("which", parents=[common],
                   help="show resolved DB + guard verdict + active shell").set_defaults(fn=cmd_which)

    sp = sub.add_parser("state", parents=[common], help="set current_state")
    sp.add_argument("text"); sp.set_defaults(fn=cmd_state)

    for k, fn in (("seed", cmd_seed), ("lns", cmd_lns)):
        sp = sub.add_parser(k, parents=[common], help=f"add a {k} identity entry")
        sp.add_argument("body"); sp.add_argument("--date"); sp.add_argument("--tag")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("retire", parents=[common], help="retire an identity entry (frees a cap slot)")
    sp.add_argument("entry_id", type=int); sp.set_defaults(fn=cmd_retire)

    sp = sub.add_parser("decision", parents=[common], help="record a Major decision")
    sp.add_argument("decision"); sp.add_argument("--rationale"); sp.add_argument("--date")
    sp.add_argument("--parent", type=int, help="parent_decision_id (supersession)")
    sp.set_defaults(fn=cmd_decision)

    sp = sub.add_parser("flag", help="open or close a flag")
    fsub = sp.add_subparsers(dest="flag_cmd", required=True)
    fo = fsub.add_parser("open", parents=[common]); fo.add_argument("description"); fo.add_argument("--name")
    fo.add_argument("--priority", default="Medium", choices=["High", "Medium", "Low"])
    fo.add_argument("--feature", type=int)
    fc = fsub.add_parser("close", parents=[common]); fc.add_argument("flag_id", type=int); fc.add_argument("--notes")
    sp.set_defaults(fn=cmd_flag)

    # Board order (see api/server.py _ORDER). Order here only affects --help
    # display; `add` still defaults to brainstorm (new items enter as ideas).
    ROADMAP_STATUSES = ["shipped", "in_progress", "next", "near_term",
                        "long_term", "brainstorm", "retired"]
    sp = sub.add_parser("roadmap", help="add a feature, move its status, set its work-stream or dependencies")
    rsub = sp.add_subparsers(dest="roadmap_cmd", required=True)
    ra = rsub.add_parser("add", parents=[common]); ra.add_argument("title")
    ra.add_argument("--status", default="brainstorm", choices=ROADMAP_STATUSES)
    ra.add_argument("--summary")
    ra.add_argument("--project", help="assign to a work-stream (projects shortname|id)")
    rt = rsub.add_parser("status", parents=[common])
    rt.add_argument("feature_id", type=int); rt.add_argument("status", choices=ROADMAP_STATUSES)
    rp = rsub.add_parser("project", parents=[common], help="set/clear a feature's work-stream")
    rp.add_argument("feature_id", type=int)
    rp.add_argument("project", help="work-stream shortname|id, or 'none' to clear")
    rd = rsub.add_parser("depends", parents=[common], help="set a feature's dependencies (replaces the set)")
    rd.add_argument("feature_id", type=int)
    rd.add_argument("--on", type=int, action="append", metavar="FEATURE_ID",
                    help="a prerequisite that must land first (repeatable); omit all to clear")
    sp.set_defaults(fn=cmd_roadmap)

    sp = sub.add_parser("project", help="add a project or update its standing/status")
    psub = sp.add_subparsers(dest="project_cmd", required=True)
    pa = psub.add_parser("add", parents=[common]); pa.add_argument("shortname"); pa.add_argument("title")
    pa.add_argument("--purpose"); pa.add_argument("--standing"); pa.add_argument("--role")
    pa.add_argument("--status", default="active", choices=["active", "inactive", "paused"])
    pst = psub.add_parser("standing", parents=[common]); pst.add_argument("project"); pst.add_argument("text")
    pss = psub.add_parser("status", parents=[common]); pss.add_argument("project")
    pss.add_argument("status", choices=["active", "inactive", "paused"])
    sp.set_defaults(fn=cmd_project)

    sp = sub.add_parser("task", help="spec_tasks: add / start / done")
    tsub = sp.add_subparsers(dest="task_cmd", required=True)
    ta = tsub.add_parser("add", parents=[common]); ta.add_argument("title")
    ta.add_argument("--feature", type=int, required=True); ta.add_argument("--doc", type=int, required=True)
    ta.add_argument("--seq", type=int, required=True); ta.add_argument("--desc")
    tst = tsub.add_parser("start", parents=[common]); tst.add_argument("task_id", type=int)
    tdn = tsub.add_parser("done", parents=[common]); tdn.add_argument("task_id", type=int)
    sp.set_defaults(fn=cmd_task)

    sub.add_parser("oriented", parents=[common],
                   help="mark this shell oriented (bootstrapped=1)").set_defaults(fn=cmd_oriented)

    sp = sub.add_parser("doc", help="add, edit, or freeze a spec/doc document")
    dsub = sp.add_subparsers(dest="doc_cmd", required=True)
    da = dsub.add_parser("add", parents=[common]); da.add_argument("title")
    da.add_argument("--body-file", required=True, dest="body_file")
    da.add_argument("--feature", type=int); da.add_argument("--kind", default="spec", choices=["spec", "doc"])
    da.add_argument("--seq", type=int); da.add_argument("--render-path", dest="render_path")
    de = dsub.add_parser("edit", parents=[common],
                         help="revise an unfrozen doc's title/body/render-path"); de.add_argument("document_id", type=int)
    de.add_argument("--title"); de.add_argument("--body-file", dest="body_file")
    de.add_argument("--render-path", dest="render_path")
    df = dsub.add_parser("freeze", parents=[common]); df.add_argument("document_id", type=int)
    sp.set_defaults(fn=cmd_doc)

    sp = sub.add_parser("narrative", parents=[common],
                        help="append a [HH:MM] line to the active archive")
    sp.add_argument("line"); sp.set_defaults(fn=cmd_narrative)

    sp = sub.add_parser("message", help="shell-to-shell inbox: check / send / mark-read")
    msub = sp.add_subparsers(dest="message_cmd", required=True)
    mc = msub.add_parser("check", parents=[common]); mc.add_argument("limit", type=int, nargs="?", default=50)
    ms = msub.add_parser("send", parents=[common]); ms.add_argument("to"); ms.add_argument("body")
    mm = msub.add_parser("mark-read", parents=[common]); mm.add_argument("message_id", type=int)
    sp.set_defaults(fn=cmd_message)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
