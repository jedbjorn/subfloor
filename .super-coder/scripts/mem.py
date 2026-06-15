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
  • snapshots + renders after the write (unless --no-sync) so the change
    survives a `./sc rebuild` — the engine DB is gitignored + rebuilt-from-text,
    so an un-snapshotted write is lost on the next rebuild.

Run from the repo root, like every engine command:

    ./sc mem which                                 # read-only: resolved DB + guard verdict + active shell
    ./sc mem state "<text>"          [--shell <id|shortname>]
    ./sc mem seed  "<body>"          [--date YYYY-MM-DD] [--tag cc] [--shell …]
    ./sc mem lns   "<body>"          [--date …] [--tag …] [--shell …]
    ./sc mem retire <entry_id>
    ./sc mem decision "<decision>"   [--rationale "…"] [--date …] [--parent ID] [--shell …]
    ./sc mem flag open  "<description>" [--name CC-001] [--priority Medium] [--feature ID] [--shell …]
    ./sc mem flag close <flag_id>    [--notes "…"]
    ./sc mem roadmap add "<title>"   [--status brainstorm] [--summary "…"] [--shell …]
    ./sc mem doc add "<title>" --body-file PATH [--feature ID] [--kind spec|doc] [--seq N]
    ./sc mem narrative "<line>"      [--shell …]

Common flags: --no-sync (skip snapshot+render), --db <path> (override target;
still guarded — used by tests).
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SCRIPTS = ENGINE / "scripts"
DEFAULT_DB = ENGINE / "shell_db.db"

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
    con = sqlite3.connect(path)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    missing = ENGINE_SENTINELS - names
    product = PRODUCT_SENTINELS & names
    if missing or product:
        die(f"{path} is NOT the super-coder engine DB — refusing to write.\n"
            f"     missing engine tables: {sorted(missing) or '—'}\n"
            f"     product tables present: {sorted(product) or '—'}\n"
            f"     the engine DB is {DEFAULT_DB}")


def connect(path: Path) -> sqlite3.Connection:
    assert_engine_db(path)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


# ── shell resolution ──────────────────────────────────────────────────────────

def git_branch() -> str | None:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse",
                        "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_shell(con: sqlite3.Connection, spec: str | None) -> int:
    """--shell wins; else infer from a `shell/<name>` worktree branch; else the
    sole non-shared shell; else make the caller pick."""
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
    die(f"could not infer the shell — pass --shell <id|shortname>. candidates: {listing}")


# ── snapshot + render (durability) ────────────────────────────────────────────

def sync() -> None:
    """Serialize the DB to text + re-render flat files, so the write persists a
    rebuild. Same ritual as `./sc snapshot && ./sc render flat`, run for you."""
    for name, sargs in (("snapshot.py", []), ("render.py", ["flat"])):
        r = subprocess.run([sys.executable, str(SCRIPTS / name), *sargs],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"mem: {name} failed (write committed, NOT yet serialized):\n"
                  f"{r.stderr.strip()}", file=sys.stderr)
            return
        tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
        if tail:
            print(f"  sync: {tail[-1]}")


def finish(args, summary: str) -> int:
    print(summary)
    if getattr(args, "no_sync", False):
        print("  (--no-sync: not serialized — run `./sc snapshot && ./sc render flat` to persist)")
    else:
        sync()
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
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        try:
            cur = con.execute(
                "INSERT INTO shell_identity_entries (shell_id, kind, entry_date, source_tag, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, kind, args.date or str(date.today()), args.tag, args.body))
        except sqlite3.IntegrityError as e:
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
    con = connect(Path(args.db))
    try:
        r = con.execute("SELECT kind, retired_at FROM shell_identity_entries "
                        "WHERE entry_id=? AND COALESCE(is_deleted,0)=0",
                        (args.entry_id,)).fetchone()
        if r is None:
            die(f"no identity entry #{args.entry_id}")
        if r["retired_at"]:
            die(f"identity entry #{args.entry_id} is already retired ({r['retired_at']})")
        con.execute("UPDATE shell_identity_entries SET retired_at=datetime('now') "
                    "WHERE entry_id=?", (args.entry_id,))
        con.commit()
    finally:
        con.close()
    return finish(args, f"mem: {r['kind']} entry #{args.entry_id} retired (slot freed)")


def cmd_decision(args) -> int:
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
        con.execute("UPDATE flags SET resolved=1, resolved_date=date('now'), "
                    "resolution_notes=? WHERE flag_id=?", (args.notes, args.flag_id))
        con.commit()
        return finish(args, f"mem: flag #{args.flag_id} closed")
    finally:
        con.close()


def cmd_roadmap(args) -> int:
    con = connect(Path(args.db))
    try:
        sid = resolve_shell(con, args.shell)
        cur = con.execute(
            "INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary) "
            "VALUES (?, ?, 0, ?, ?)", (args.title, args.status, sid, args.summary))
        con.commit()
        fid = cur.lastrowid
    finally:
        con.close()
    return finish(args, f"mem: roadmap feature #{fid} added ('{args.title}', {args.status})")


def cmd_doc(args) -> int:
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


def cmd_narrative(args) -> int:
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
    common.add_argument("--no-sync", action="store_true", help="skip snapshot+render after the write")
    common.add_argument("--shell", help="target shell id or shortname (default: inferred)")

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

    sp = sub.add_parser("roadmap", help="add a roadmap feature")
    rsub = sp.add_subparsers(dest="roadmap_cmd", required=True)
    ra = rsub.add_parser("add", parents=[common]); ra.add_argument("title")
    ra.add_argument("--status", default="brainstorm",
                    choices=["brainstorm", "in_progress", "next", "near_term",
                             "long_term", "shipped", "retired"])
    ra.add_argument("--summary")
    sp.set_defaults(fn=cmd_roadmap)

    sp = sub.add_parser("doc", help="add a spec/doc document")
    dsub = sp.add_subparsers(dest="doc_cmd", required=True)
    da = dsub.add_parser("add", parents=[common]); da.add_argument("title")
    da.add_argument("--body-file", required=True, dest="body_file")
    da.add_argument("--feature", type=int); da.add_argument("--kind", default="spec", choices=["spec", "doc"])
    da.add_argument("--seq", type=int); da.add_argument("--render-path", dest="render_path")
    sp.set_defaults(fn=cmd_doc)

    sp = sub.add_parser("narrative", parents=[common],
                        help="append a [HH:MM] line to the active archive")
    sp.add_argument("line"); sp.set_defaults(fn=cmd_narrative)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
