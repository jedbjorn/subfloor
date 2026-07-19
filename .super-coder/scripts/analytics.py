#!/usr/bin/env python3
"""Token & session analytics collector — `sc analytics sweep` (spec doc #11).

Pull-based capture: super-coder never calls a model, it launches external
harness CLIs — so the collector parses what each harness leaves on disk
(scripts/token_parsers/*, one plugin per harness) and upserts one row per
(harness session × model) into session_token_usage. Runs at run.py boot
(incremental), on demand, and from the GUI Analytics tab.

Idempotency: (harness, harness_session_ref, model) is the natural key. The
upsert is a manual UPDATE-then-INSERT rather than ON CONFLICT because
`model` is NULL on no_usage rows and SQLite treats NULLs as pairwise
distinct in UNIQUE indexes — ON CONFLICT would re-insert those rows on
every sweep. Updates never touch archive_id/shell_id (attribution owns them).

Attribution: harness session → archive by (cwd → shell, time-window). A
shell's window runs from an archive's started_at to the same shell's next
started_at (open-ended for the latest). Worktree cwds map to the shell whose
shortname names the worktree; the repo root maps to admin-flavor shells.
Residual ambiguity stays archive_id=NULL — visible, flagged unattributed,
never guessed. cwd is transient (attribution runs within the sweep batch);
rows that stay NULL keep their spend visible in the totals regardless.

ended_at backfill: run.py execs the harness, so nothing can write the
archive's end time at exit — the sweep sets it to the last attributed
harness activity (refreshed while a session is still live).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402

import token_parsers  # noqa: E402

WORKTREES = ".sc-worktrees"

UPSERT_COLS = ["provider", "model", "title", "started_at", "ended_at",
               "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "reasoning_tokens", "status",
               "parser_version", "captured_at"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_epoch(ts: "str | None") -> "float | None":
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _load_parsers(only: "str | None", log) -> list:
    mods = []
    for name in token_parsers.HARNESSES:
        if only and name != only:
            continue
        try:
            mods.append(__import__(f"token_parsers.{name}", fromlist=[name]))
        except ImportError as e:
            log(f"analytics: no parser for '{name}' ({e}) — skipped")
    return mods


def _since_fn(con, harness: str, parser_version: str):
    """ref → last captured_at epoch, for one harness's incremental skip.
    Scoped to rows written by the CURRENT parser version: a version bump
    makes every ref look never-captured, forcing the full re-parse that
    lets count-affecting parser fixes reach already-swept sessions."""
    seen: dict[str, float] = {}
    for r in con.execute(
            "SELECT harness_session_ref, MAX(captured_at) c FROM session_token_usage "
            "WHERE harness=? AND parser_version=? GROUP BY harness_session_ref",
            (harness, parser_version)):
        e = _iso_epoch(r["c"])
        if e is not None:
            seen[r["harness_session_ref"]] = e
    return lambda ref: seen.get(ref)


def _upsert(con, r: dict, captured_at: str) -> str:
    """UPDATE-then-INSERT on the (harness, ref, model) natural key, NULL-model
    safe. Returns 'update' or 'insert'."""
    vals = {**r, "captured_at": captured_at}
    cur = con.execute(
        f"UPDATE session_token_usage SET {', '.join(c + '=?' for c in UPSERT_COLS)} "
        "WHERE harness=? AND harness_session_ref=? AND model IS ?",
        [vals.get(c) for c in UPSERT_COLS] + [r["harness"], r["harness_session_ref"], r["model"]])
    if cur.rowcount:
        return "update"
    con.execute(
        f"INSERT INTO session_token_usage (harness, harness_session_ref, {', '.join(UPSERT_COLS)}) "
        f"VALUES (?, ?, {', '.join('?' for _ in UPSERT_COLS)})",
        [r["harness"], r["harness_session_ref"]] + [vals.get(c) for c in UPSERT_COLS])
    return "insert"


def _cwd_shells(con) -> tuple[dict, list]:
    """(worktree-name → shell_id, [admin shell_ids])."""
    by_wt, admins = {}, []
    for s in con.execute("SELECT shell_id, shortname, flavor FROM shells "
                         "WHERE COALESCE(is_deleted,0)=0"):
        if (s["flavor"] or "") == "admin":
            admins.append(s["shell_id"])
        if s["shortname"]:
            by_wt[s["shortname"].lower()] = s["shell_id"]
    return by_wt, admins


def _shell_for_cwd(cwd: str, by_wt: dict, admins: list) -> list[int]:
    root = str(REPO_ROOT).rstrip("/")
    wt_prefix = f"{root}/{WORKTREES}/"
    if cwd.startswith(wt_prefix):
        name = cwd[len(wt_prefix):].split("/", 1)[0]
        sid = by_wt.get(name)
        return [sid] if sid else []
    # repo root (or a subdir outside the worktrees) → admin-flavor shells
    return admins


def _windows(con) -> dict[int, list]:
    """shell_id → [(archive_id, harness, start_epoch, end_epoch|None)],
    end = the same shell's next started_at (open-ended for the latest)."""
    per: dict[int, list] = {}
    for a in con.execute(
            "SELECT archive_id, shell_id, harness, started_at FROM shell_memory_archives "
            "WHERE started_at IS NOT NULL ORDER BY shell_id, started_at"):
        per.setdefault(a["shell_id"], []).append(
            [a["archive_id"], a["harness"], _iso_epoch(a["started_at"]), None])
    for arcs in per.values():
        for i in range(len(arcs) - 1):
            arcs[i][3] = arcs[i + 1][2]
    return per


def _attribute(con, batch: list[dict], log) -> int:
    by_wt, admins = _cwd_shells(con)
    windows = _windows(con)
    attributed = 0
    for r in batch:
        if not r.get("cwd"):
            continue
        t = _iso_epoch(r.get("started_at") or r.get("ended_at"))
        if t is None:
            continue
        hits = []
        for sid in _shell_for_cwd(r["cwd"], by_wt, admins):
            for aid, harness, start, end in windows.get(sid, []):
                if harness == r["harness"] and start is not None \
                        and start <= t and (end is None or t < end):
                    hits.append((aid, sid))
        if len(hits) != 1:
            continue  # 0 = predates lifecycle rows; >1 = ambiguous — stays NULL
        aid, sid = hits[0]
        cur = con.execute(
            "UPDATE session_token_usage SET archive_id=?, shell_id=? "
            "WHERE harness=? AND harness_session_ref=? AND model IS ? AND archive_id IS NULL",
            (aid, sid, r["harness"], r["harness_session_ref"], r["model"]))
        attributed += cur.rowcount
    return attributed


def _backfill_ended(con) -> int:
    cur = con.execute(
        "UPDATE shell_memory_archives SET ended_at = ("
        "  SELECT MAX(u.ended_at) FROM session_token_usage u"
        "  WHERE u.archive_id = shell_memory_archives.archive_id AND u.ended_at IS NOT NULL) "
        "WHERE started_at IS NOT NULL AND archive_id IN "
        "  (SELECT DISTINCT archive_id FROM session_token_usage WHERE archive_id IS NOT NULL)")
    return cur.rowcount


def sweep(only: "str | None" = None, quiet: bool = False) -> dict:
    notes: list[str] = []

    def log(msg: str):
        notes.append(msg)
        if not quiet:
            print(f"  ! {msg}")

    con = db_driver.connect(DB_PATH)
    try:
        captured_at = _now_iso()
        counts = {"insert": 0, "update": 0}
        batch: list[dict] = []
        for mod in _load_parsers(only, log):
            since = _since_fn(con, mod.HARNESS, mod.PARSER_VERSION)
            try:
                rows = mod.sweep(REPO_ROOT, since, log)
            except Exception as e:  # plugin stance: loud, never fatal to the sweep
                log(f"analytics: {mod.HARNESS} parser failed: {e!r}")
                continue
            for r in rows:
                counts[_upsert(con, r, captured_at)] += 1
                batch.append(r)
        attributed = _attribute(con, batch, log)
        ended = _backfill_ended(con)
        con.commit()
        summary = {"inserted": counts["insert"], "updated": counts["update"],
                   "attributed": attributed, "ended_backfilled": ended,
                   "notes": notes}
        if not quiet:
            print(f"analytics: {counts['insert']} new, {counts['update']} refreshed, "
                  f"{attributed} attributed, {ended} archive end(s) backfilled")
        return summary
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: sc analytics sweep [--harness <name>] [--quiet]\n"
              "  parse each harness's on-disk usage data for THIS repo into\n"
              "  session_token_usage (incremental, idempotent)")
        return 0
    if argv[0] != "sweep":
        print(f"sc analytics: unknown command '{argv[0]}' (try: sweep)", file=sys.stderr)
        return 2
    only = None
    quiet = "--quiet" in argv
    if "--harness" in argv:
        i = argv.index("--harness")
        only = argv[i + 1] if i + 1 < len(argv) else None
        if only not in token_parsers.HARNESSES:
            print(f"sc analytics: unknown harness '{only}'", file=sys.stderr)
            return 2
    sweep(only=only, quiet=quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
